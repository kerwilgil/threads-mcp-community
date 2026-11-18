"""Application service combining auth, provider calls, and duplicate protection."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from .client import ThreadsClient
from .config import Settings
from .diagnostics import (
    CONTAINER_REJECTION,
    FAILED_REPLAY,
    PUBLISH_REJECTION,
    append_diagnostic,
    diagnostic_from_status,
    safe_stored_failure_message,
    strip_secret,
)
from .errors import (
    AuthRequiredError,
    ClaimLostError,
    ProviderRejectedError,
    SafeTransportError,
    UnknownOutcomeError,
)
from .ledger import PublicationLedger, PublicationRecord
from .models import Profile, PublishResult, ThreadPost, TokenRecord
from .oauth import refresh_long_lived_token
from .token_store import EncryptedTokenStore

_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
# Threads topic_tag: 1-50 characters, no periods and no ampersands (no whitespace/control chars either).
_TOPIC_TAG_RE = re.compile(r"^[^.&\s\x00-\x1f\x7f]{1,50}$")
_IMAGE_URL_MAX = 2048
# Media containers are processed asynchronously; publish only once the container reports FINISHED.
CONTAINER_POLL_ATTEMPTS = 15
CONTAINER_POLL_INTERVAL_SECONDS = 3.0


def validate_topic_tag(topic_tag: str) -> None:
    if topic_tag and not _TOPIC_TAG_RE.fullmatch(topic_tag):
        raise ValueError("topic_tag must be 1-50 characters without '.', '&', or whitespace.")


def validate_image_url(image_url: str) -> None:
    if (
        not image_url or len(image_url) > _IMAGE_URL_MAX or image_url != image_url.strip()
        or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in image_url)
    ):
        raise ValueError("image_url must be a public HTTPS URL without whitespace.")
    parts = urlsplit(image_url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("image_url must be a public HTTPS URL without credentials.")


class ThreadsService:
    def __init__(
        self,
        settings: Settings,
        *,
        token_store: EncryptedTokenStore | None = None,
        ledger: PublicationLedger | None = None,
        client_factory: Callable[[TokenRecord], ThreadsClient] | None = None,
        token_refresher: Callable[[TokenRecord], TokenRecord] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.settings = settings
        self.token_store = token_store or EncryptedTokenStore(
            settings.token_store_path,
            settings.encryption_key,
        )
        self.ledger = ledger or PublicationLedger(settings.ledger_path)
        self._client_factory = client_factory or self._default_client_factory
        self._token_refresher = token_refresher or self._default_token_refresher
        self._sleep = sleeper or time.sleep

    def _default_client_factory(self, token: TokenRecord) -> ThreadsClient:
        return ThreadsClient(
            token.access_token,
            api_base=self.settings.api_base,
            timeout_seconds=self.settings.request_timeout_seconds,
        )

    def _default_token_refresher(self, token: TokenRecord) -> TokenRecord:
        return refresh_long_lived_token(token, timeout_seconds=self.settings.request_timeout_seconds)

    def _load_token(self) -> TokenRecord:
        token = self.token_store.load()
        if token.expires_at and token.expires_at <= datetime.now(UTC):
            raise AuthRequiredError("The Threads token expired. Complete a new OAuth login.")
        if token.expires_at and token.expires_at <= datetime.now(UTC) + timedelta(
            days=self.settings.refresh_window_days
        ):
            refreshed = self._token_refresher(token)
            self.token_store.save(refreshed)
            token = refreshed
        return token

    def _client(self) -> tuple[TokenRecord, ThreadsClient]:
        token = self._load_token()
        return token, self._client_factory(token)

    def get_profile(self) -> Profile:
        token, client = self._client()
        try:
            profile = client.get_profile()
        finally:
            client.close()
        if profile.id and (profile.id != token.account_id or profile.username != token.username):
            self.token_store.save(
                token.model_copy(update={"account_id": profile.id, "username": profile.username})
            )
        return profile

    def get_post(self, post_id: str) -> ThreadPost:
        if not post_id.strip():
            raise ValueError("post_id is required.")
        _, client = self._client()
        try:
            return client.get_post(post_id.strip())
        finally:
            client.close()

    def list_recent_posts(self, limit: int = 10) -> list[ThreadPost]:
        limit = max(1, min(limit, 100))
        _, client = self._client()
        try:
            return client.list_recent_posts(limit)
        finally:
            client.close()

    def publish_text(self, text: str, idempotency_key: str, topic_tag: str = "") -> PublishResult:
        self._validate_publish_input(text, idempotency_key)
        validate_topic_tag(topic_tag)
        return self._publish(text, idempotency_key, image_url="", topic_tag=topic_tag)

    def publish_image(self, text: str, image_url: str, idempotency_key: str, topic_tag: str = "") -> PublishResult:
        self._validate_publish_input(text, idempotency_key)
        validate_image_url(image_url)
        validate_topic_tag(topic_tag)
        return self._publish(text, idempotency_key, image_url=image_url, topic_tag=topic_tag)

    def _publish(self, text: str, idempotency_key: str, *, image_url: str, topic_tag: str) -> PublishResult:
        # The ledger digest binds text, image_url and topic_tag: a key cannot be reused for other content.
        record = self.ledger.reserve(idempotency_key, text, image_url=image_url, topic_tag=topic_tag)

        terminal = self._terminal_or_guarded_result(record)
        if terminal is not None:
            return terminal

        claimed = self.ledger.claim(idempotency_key)
        if claimed is None:
            latest = self.ledger.get(idempotency_key)
            assert latest is not None
            terminal = self._terminal_or_guarded_result(latest)
            if terminal is not None:
                return terminal
            return PublishResult(
                status="unknown",
                idempotency_key=idempotency_key,
                message=(
                    "Another publication attempt is currently using this idempotency key; "
                    "no duplicate write was sent."
                ),
            )

        try:
            return self._publish_claimed(claimed)
        except ClaimLostError:
            latest = self.ledger.get(idempotency_key)
            if latest and latest.status == "published":
                return self._published_result(latest, recovered=False, message="Already published for this key.")
            return PublishResult(
                status="unknown", idempotency_key=idempotency_key,
                message="The publication claim changed; no duplicate write was sent.",
            )

    def _publish_claimed(self, claimed: PublicationRecord) -> PublishResult:
        key = claimed.idempotency_key

        def transition(status: str, **fields: str) -> PublicationRecord:
            return self.ledger.set_status(key, status, owner=claimed.owner, **fields)

        def failure(status: str, message: str) -> PublishResult:
            transition(status, error=message)
            return PublishResult(
                status="unknown" if status == "unknown" else "failed", idempotency_key=key, message=message,
            )

        try:
            token, client = self._client()
        except Exception:
            return failure("retryable", "Threads authentication or client setup failed before publication.")
        try:
            try:
                user_id = token.account_id
                if not user_id:
                    profile = client.get_profile()
                    user_id = profile.id
                    token = token.model_copy(update={"account_id": user_id, "username": profile.username})
                    self.token_store.save(token)
            except Exception:
                return failure("retryable", "Threads account lookup failed before publication.")
            if not user_id:
                return failure("failed", "Threads account id is unavailable.")
            if claimed.account_id and claimed.account_id != user_id:
                return failure("failed", "The connected Threads account changed; this key cannot be reused.")
            transition("creating", account_id=user_id)

            creation_id = claimed.creation_id
            if not creation_id:
                try:
                    if claimed.image_url:
                        creation_id = client.create_image_container(
                            user_id, claimed.text, claimed.image_url, topic_tag=claimed.topic_tag
                        )
                    elif claimed.topic_tag:
                        creation_id = client.create_text_container(user_id, claimed.text, topic_tag=claimed.topic_tag)
                    else:
                        creation_id = client.create_text_container(user_id, claimed.text)
                except ProviderRejectedError as exc:
                    status = "retryable" if self._container_creation_retryable(exc) else "failed"
                    return failure(status, self._provider_message(CONTAINER_REJECTION, exc, secret=token.access_token))
                except Exception:
                    # Creation explicitly disables auto-publish. Orphan containers are safe.
                    return failure("retryable", "Threads container creation returned no usable response.")
            transition("container_created", creation_id=creation_id, error="")

            if claimed.image_url:
                # Read-only status polling; nothing public happens before FINISHED.
                ready = self._wait_for_media_container(client, creation_id)
                if ready == "PUBLISHED":
                    return failure(
                        "unknown", "The Threads media container already reports PUBLISHED; no replay is allowed."
                    )
                if ready in {"ERROR", "EXPIRED"}:
                    # Clear the container so an explicit later retry with the same key creates a fresh one.
                    message = f"Threads media container reported {ready}; nothing was published. Check image_url."
                    transition("retryable", creation_id="", error=message)
                    return PublishResult(status="failed", idempotency_key=key, message=message)
                if ready != "FINISHED":
                    return failure(
                        "retryable",
                        "Threads media container is not ready yet; nothing was published. Retry later with the "
                        "same publication reference (the same container is reused).",
                    )

            # This transaction also fences stale owners. Only its successful claimant
            # may cross the public boundary, which is durable before any network send.
            # Container status is not read; threads_publish follows this durable boundary.
            transition("publish_started", creation_id=creation_id, error="")
            try:
                post_id = client.publish_container(user_id, creation_id)
            except SafeTransportError:
                return failure("retryable", "Threads publish request was proven unsent; retry is safe.")
            except UnknownOutcomeError as exc:
                return failure(
                    "unknown",
                    self._provider_message(
                        "Threads publication may have started; no replay is allowed.",
                        exc,
                        secret=token.access_token,
                    ),
                )
            except ProviderRejectedError as exc:
                # A bare status or proxy body cannot prove a rejected public write.
                if (
                    exc.definitive and exc.status_code is not None
                    and 400 <= exc.status_code < 500 and exc.status_code not in {408, 425, 429}
                ):
                    return failure(
                        "failed",
                        self._provider_message(PUBLISH_REJECTION, exc, secret=token.access_token),
                    )
                return failure(
                    "unknown",
                    self._provider_message(
                        "Threads returned no definitive publish response; no replay is allowed.",
                        exc,
                        secret=token.access_token,
                    ),
                )
            except Exception:
                return failure("unknown", "Threads publication may have started; no replay is allowed.")

            # Persist the definitive post id immediately, before optional permalink lookup.
            if not post_id:
                return failure("unknown", "Threads returned no post id; no replay is allowed.")
            record = transition("published", post_id=post_id, error="")
            try:
                permalink = client.get_post(post_id).permalink
                record = transition("published", permalink=permalink)
            except Exception:
                # Success is already durable; optional lookup cannot reverse it.
                pass
            return self._published_result(record, recovered=False, message="Published successfully to Threads.")
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _wait_for_media_container(self, client: ThreadsClient, creation_id: str) -> str:
        """Poll the container status (GET only). Returns the final status, or '' if it never became final."""
        for attempt in range(CONTAINER_POLL_ATTEMPTS):
            if attempt:
                self._sleep(CONTAINER_POLL_INTERVAL_SECONDS)
            try:
                status = client.get_container_status(creation_id)
            except Exception:
                return ""
            if status in {"FINISHED", "PUBLISHED", "ERROR", "EXPIRED"}:
                return status
        return ""

    def _terminal_or_guarded_result(self, record: PublicationRecord) -> PublishResult | None:
        if record.status == "published":
            return self._published_result(
                record,
                recovered=False,
                message="Already published for this idempotency key.",
            )
        if record.status == "failed":
            return PublishResult(
                status="failed",
                idempotency_key=record.idempotency_key,
                message=safe_stored_failure_message(record.error) or FAILED_REPLAY,
            )
        if record.status in {"publish_started", "unknown"}:
            return PublishResult(
                status="unknown",
                idempotency_key=record.idempotency_key,
                message=(
                    "A previous publish attempt may have reached Threads. No automatic retry was performed "
                    "because that could create a duplicate."
                ),
            )
        if record.status in {"creating", "container_created"}:
            age = datetime.now(UTC) - record.updated_at
            if age < timedelta(seconds=120):
                return PublishResult(
                    status="unknown",
                    idempotency_key=record.idempotency_key,
                    message="A publication attempt is already in progress; no duplicate write was sent.",
                )
        return None

    def _validate_publish_input(self, text: str, idempotency_key: str) -> None:
        if not text or not text.strip():
            raise ValueError("Threads post text is empty.")
        if len(text) > self.settings.max_text_chars:
            raise ValueError(
                f"Threads post text exceeds the configured {self.settings.max_text_chars}-character limit."
            )
        if not _KEY_RE.fullmatch(idempotency_key):
            raise ValueError(
                "idempotency_key must be 8-128 characters using letters, digits, '.', '_', ':', or '-'."
            )

    @staticmethod
    def _provider_message(base: str, exc: ProviderRejectedError | UnknownOutcomeError, *, secret: str) -> str:
        diagnostic = exc.diagnostic
        if diagnostic is None and isinstance(exc, ProviderRejectedError):
            diagnostic = diagnostic_from_status(exc.status_code)
        return strip_secret(append_diagnostic(base, diagnostic), secret)

    @staticmethod
    def _container_creation_retryable(exc: ProviderRejectedError) -> bool:
        # Container creation has no public side effect: retrying creates a new container
        # while any orphan stays unpublished, so transient (429/5xx) or anomalous
        # responses without a status remain retryable. Only a definitive 4xx rejects the key.
        return (
            not exc.definitive or exc.status_code is None or exc.status_code in {408, 425, 429}
            or not 400 <= exc.status_code < 500
        )

    @staticmethod
    def _published_result(
        record: PublicationRecord,
        *,
        recovered: bool,
        message: str,
    ) -> PublishResult:
        return PublishResult(
            status="published",
            idempotency_key=record.idempotency_key,
            post_id=record.post_id,
            permalink=record.permalink,
            recovered=recovered,
            message=message,
        )
