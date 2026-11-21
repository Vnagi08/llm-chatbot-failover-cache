"""Classification of provider errors into "try the next key" vs "give up"."""

from __future__ import annotations

import litellm

ALL_PROVIDERS_UNAVAILABLE = "all providers unavailable"

_QUOTA_MARKERS = ("quota", "resource_exhausted", "resource exhausted", "rate limit", "insufficient_quota")


class AllProvidersUnavailableError(Exception):
    """Every configured key failed with a failover-eligible error."""

    def __init__(self, attempts: int) -> None:
        super().__init__(ALL_PROVIDERS_UNAVAILABLE)
        self.attempts = attempts


class ProviderRequestRejectedError(Exception):
    """The provider rejected the request itself (e.g. malformed / too long).

    Retrying with another key would fail identically, so failover stops.
    """


def status_code_of(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def is_failover_error(exc: BaseException) -> bool:
    """Return True when the next key should be tried.

    Fail over on:
      * HTTP 429 / rate limits / quota exhaustion
      * any 5xx, timeouts and connection failures
      * key-specific auth failures (401/403 - a revoked or invalid key should not
        take the whole service down while another key still works)
    Do not fail over on request-level errors (400 bad request, context window, 404 model).
    """
    if isinstance(
        exc,
        (
            litellm.RateLimitError,
            litellm.InternalServerError,
            litellm.ServiceUnavailableError,
            litellm.BadGatewayError,
            litellm.APIConnectionError,
            litellm.Timeout,
            litellm.AuthenticationError,
            litellm.PermissionDeniedError,
        ),
    ):
        return True
    if isinstance(exc, litellm.ContextWindowExceededError):
        return False

    code = status_code_of(exc)
    if code is not None and (code == 429 or code >= 500 or code in (401, 403)):
        return True

    message = str(exc).lower()
    if any(marker in message for marker in _QUOTA_MARKERS):
        return True

    # Unknown, non-HTTP failures (e.g. network stack errors) are treated as transient.
    return code is None and not isinstance(exc, (litellm.BadRequestError, litellm.NotFoundError))
