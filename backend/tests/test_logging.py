import logging

from app.logging_config import RedactSecretsFilter, configure_logging, redact


def test_secrets_redacted_from_logs():
    configure_logging("INFO", ["super-secret-key"])
    f = RedactSecretsFilter()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "error with key=%s", ("super-secret-key",), None)
    f.filter(rec)
    assert "super-secret-key" not in rec.getMessage()
    assert redact("a super-secret-key b") == "a ***REDACTED*** b"


def test_records_without_secrets_untouched():
    configure_logging("INFO", ["super-secret-key"])
    args = ("127.0.0.1", "GET")
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s"', args, None)
    RedactSecretsFilter().filter(rec)
    assert rec.args == args
