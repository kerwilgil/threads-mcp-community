"""HTTP transport with explicit ambiguity semantics for Threads writes."""

from __future__ import annotations

from typing import Any

import httpx

from .config import validate_api_base
from .diagnostics import ProviderDiagnostic, append_diagnostic, parse_provider_diagnostic, strip_secret
from .errors import ProviderRejectedError, SafeTransportError, UnknownOutcomeError
from .redact import private_http_logs, sanitize_payload


class GraphTransport:
    def __init__(
        self,
        bearer_token: str,
        *,
        api_base: str,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        validate_api_base(api_base)
        self._secret = bearer_token
        self._base = api_base.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            headers={"Authorization": f"Bearer {bearer_token}"},
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        may_publish: bool = False,
    ) -> dict[str, Any]:
        try:
            with private_http_logs():
                response = self._client.request(
                    method,
                    f"{self._base}/{path.lstrip('/')}",
                    params=params,
                    data=data,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise SafeTransportError("Threads could not be reached before the request was sent.") from None
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError):
            if may_publish:
                raise UnknownOutcomeError("Threads returned no definitive publish response.") from None
            raise SafeTransportError("Threads returned no usable response.") from None
        except httpx.RequestError:
            if may_publish:
                raise UnknownOutcomeError("Threads transport failed after publication may have started.") from None
            raise SafeTransportError("Threads request failed before a definitive response.") from None

        if may_publish and (response.status_code >= 500 or response.status_code in {408, 425, 429}):
            diagnostic = self._safe_diagnostic(response)
            raise UnknownOutcomeError(
                self._with_diagnostic(
                    f"Threads returned HTTP {response.status_code} after the publish request; outcome is unknown.",
                    diagnostic,
                ),
                diagnostic=diagnostic,
            )
        if not 200 <= response.status_code < 300:
            diagnostic = self._safe_diagnostic(response)
            definitive = self._definitive_rejection(response)
            if may_publish and not definitive:
                raise UnknownOutcomeError(
                    self._with_diagnostic(
                        "Threads returned an unverified publish rejection; outcome is unknown.",
                        diagnostic,
                    ),
                    diagnostic=diagnostic,
                )
            raise ProviderRejectedError(
                self._with_diagnostic(
                    f"Threads rejected the request with HTTP {response.status_code}; provider details [redacted].",
                    diagnostic,
                ),
                status_code=response.status_code,
                definitive=definitive,
                diagnostic=diagnostic,
            )
        try:
            payload = response.json()
        except ValueError:
            if may_publish:
                raise UnknownOutcomeError("Threads returned an unreadable publish response.") from None
            raise ProviderRejectedError("Threads returned an unreadable response.") from None
        if not isinstance(payload, dict) or "error" in payload:
            if may_publish:
                raise UnknownOutcomeError("Threads returned an unexpected publish response; outcome is unknown.")
            raise ProviderRejectedError("Threads returned an unexpected response shape.")
        return sanitize_payload(payload, self._secret)

    def _safe_diagnostic(self, response: httpx.Response) -> ProviderDiagnostic:
        payload: object | None = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return parse_provider_diagnostic(payload, http_status=response.status_code, secret=self._secret)

    def _with_diagnostic(self, message: str, diagnostic: ProviderDiagnostic) -> str:
        # Labeled redaction would erase an allowlisted Graph code (`code=`). Drop only the bearer value.
        return strip_secret(append_diagnostic(message, diagnostic), self._secret)

    @staticmethod
    def _definitive_rejection(response: httpx.Response) -> bool:
        if not 400 <= response.status_code < 500 or response.status_code in {408, 425, 429}:
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        error = payload.get("error") if isinstance(payload, dict) else None
        return (
            isinstance(error, dict) and "id" not in payload
            and type(error.get("code")) is int and 0 <= error["code"] <= 999999
            and error.get("is_transient", False) is False
        )
