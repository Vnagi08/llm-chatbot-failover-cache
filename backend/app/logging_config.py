"""Logging setup with a filter that redacts configured API keys from every record."""

from __future__ import annotations

import logging
from collections.abc import Iterable

_secrets: list[str] = []


def redact(text: str) -> str:
    for secret in _secrets:
        if secret:
            text = text.replace(secret, "***REDACTED***")
    return text


class RedactSecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if _secrets:
            try:
                message = record.getMessage()
            except Exception:  # pragma: no cover - malformed log call
                return True
            # Only rewrite records that actually contain a secret: some formatters
            # (e.g. uvicorn's access formatter) rely on the original record.args.
            if any(secret in message for secret in _secrets):
                record.msg = redact(message)
                record.args = None
        return True


def configure_logging(level: str, secrets: Iterable[str]) -> None:
    _secrets[:] = [s for s in secrets if s]
    root = logging.getLogger()
    if not any(isinstance(f, RedactSecretsFilter) for h in root.handlers for f in h.filters):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        handler.addFilter(RedactSecretsFilter())
        root.addHandler(handler)
    root.setLevel(level.upper())
    # Uvicorn installs its own handlers; attach the filter there as well.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for h in logging.getLogger(name).handlers:
            if not any(isinstance(f, RedactSecretsFilter) for f in h.filters):
                h.addFilter(RedactSecretsFilter())
    # LiteLLM is very chatty at INFO and may echo request details.
    for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "litellm", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)
