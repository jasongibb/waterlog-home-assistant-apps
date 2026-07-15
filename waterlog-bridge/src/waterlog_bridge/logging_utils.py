"""Logging setup that defensively removes bearer credentials."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable


_BEARER = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+")
_OPTION_SECRET = re.compile(
    r'(?i)(["\']?waterlog_credential["\']?\s*[:=]\s*["\']?)[^"\'\s,}]+'
)


def _redact(message: str, secrets: tuple[str, ...]) -> str:
    message = _BEARER.sub(r"\1[REDACTED]", message)
    message = _OPTION_SECRET.sub(r"\1[REDACTED]", message)
    for secret in secrets:
        message = message.replace(secret, "[REDACTED]")
    return message


class SecretRedactionFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.getMessage(), self._secrets)
        record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """Also redact formatted exception text, not only the log message."""

    def __init__(self, fmt: str, secrets: Iterable[str] = ()) -> None:
        super().__init__(fmt)
        self._secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        return _redact(super().format(record), self._secrets)


def configure_logging(level: str, secrets: Iterable[str] = ()) -> None:
    secrets = tuple(secret for secret in secrets if secret)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        RedactingFormatter(
            "%(asctime)s %(levelname)s waterlog_bridge %(message)s", secrets
        )
    )
    handler.addFilter(SecretRedactionFilter(secrets))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
