"""Structured logging with size-capped rotation.

Log files are rotated at a small size so an unattended Pi cannot fill its
microSD card.  Secrets are never passed to the logging layer in the first
place; the redaction filter is a second line of defence.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from typing import Any

from . import paths

_CONFIGURED = False

#: Patterns that must never reach a log file.
_REDACTIONS = (
    (re.compile(r"(wpa_passphrase\s*=\s*)\S+", re.IGNORECASE), r"\1[redacted]"),
    (re.compile(r"(wpa_psk\s*=\s*)[0-9a-fA-F]{32,}", re.IGNORECASE), r"\1[redacted]"),
    (re.compile(r'("?(?:password|passphrase|token|csrf|secret)"?\s*[:=]\s*)("?)[^",}\s]+', re.IGNORECASE), r"\1\2[redacted]"),
)


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        for pattern, replacement in _REDACTIONS:
            message = pattern.sub(replacement, message)
        record.msg = message
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line: easy to read with jq, cheap to write."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("event", "category", "detail", "duration_ms", "command", "returncode"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def setup(level: str = "INFO", max_bytes: int = 512 * 1024, backups: int = 2, filename=None) -> None:
    """Configure the package logger once."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("networkos")
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.propagate = False

    formatter = JsonFormatter()
    redaction = RedactionFilter()

    log_path = filename or paths.APP_LOG
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=max(64 * 1024, int(max_bytes)), backupCount=max(1, int(backups))
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redaction)
        root.addHandler(file_handler)
    except OSError:
        pass  # read-only filesystem or missing permissions: journald still works

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    stream.addFilter(redaction)
    root.addHandler(stream)

    for noisy in ("werkzeug", "urllib3", "waitress.queue"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"networkos.{name}")
