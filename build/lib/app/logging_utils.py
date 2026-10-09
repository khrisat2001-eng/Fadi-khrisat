"""Log redaction so credentials never reach log files or error reports."""
from __future__ import annotations

import logging
import re

_SENSITIVE = re.compile(
    r"""(?ix)
    (api[_-]?secret|secret[_-]?key|passphrase|api[_-]?key|
     ok-access-(?:key|sign|passphrase)|kc-api-(?:key|sign|passphrase))
    (["']?\s*[:=]\s*["']?)
    ([^"'\s,}&]+)
    """
)


def redact(text: str) -> str:
    return _SENSITIVE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover
            return True
        record.msg = redact(message)
        record.args = ()
        return True


def install() -> None:
    f = RedactingFilter()
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "app"):
        logger = logging.getLogger(name)
        logger.addFilter(f)
        for handler in logger.handlers:
            handler.addFilter(f)
