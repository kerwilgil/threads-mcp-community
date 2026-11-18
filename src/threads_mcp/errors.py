"""Domain exceptions for Threads MCP."""

from .diagnostics import ProviderDiagnostic


class ThreadsMCPError(Exception):
    """Base class for safe-to-surface domain failures."""


class ConfigurationError(ThreadsMCPError):
    """Required local configuration is absent or invalid."""


class AuthRequiredError(ThreadsMCPError):
    """The stored Threads credential is missing, expired, or unusable."""


class IdempotencyConflictError(ThreadsMCPError):
    """An idempotency key was reused for different content."""


class ClaimLostError(ThreadsMCPError):
    """A newer worker owns this publication; the old worker must stop."""


class ProviderRejectedError(ThreadsMCPError):
    """The provider returned a definitive rejection response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        definitive: bool = False,
        diagnostic: ProviderDiagnostic | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.definitive = definitive
        self.diagnostic = diagnostic


class SafeTransportError(ThreadsMCPError):
    """The request is proven not to have reached the provider."""


class UnknownOutcomeError(ThreadsMCPError):
    """A write may have reached the provider; blindly retrying is unsafe."""

    def __init__(self, message: str, *, diagnostic: ProviderDiagnostic | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic
