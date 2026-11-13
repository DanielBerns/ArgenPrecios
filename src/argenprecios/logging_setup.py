"""Structured JSON logging configuration with credential redaction for ArgenPrecios."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pythonjsonlogger.json import JsonFormatter

logger = logging.getLogger(__name__)

_DSN_PASSWORD_RE = re.compile(r"(postgresql(?:\+\w+)?://[^:/@\s]+:)([^@\s]+)(@)")


class CredentialRedactingFilter(logging.Filter):
    """Redacts PostgreSQL passwords and bearer tokens before log serialization."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _DSN_PASSWORD_RE.sub(r"\1***REDACTED***\3", record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    _DSN_PASSWORD_RE.sub(r"\1***REDACTED***\3", a) if isinstance(a, str) else a
                    for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: (_DSN_PASSWORD_RE.sub(r"\1***REDACTED***\3", v) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
        return True


def configure_logging(
    level: int | str = logging.INFO,
    log_dir: Path | None = None,
) -> None:
    """Configure root/package structured JSON stream logging and optional rotating file handler."""
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clear pre-existing handlers to keep CLI/server startups idempotent
    for existing in list(root_logger.handlers):
        root_logger.removeHandler(existing)

    formatter = JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
    )
    redactor = CredentialRedactingFilter()

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redactor)
    root_logger.addHandler(stream_handler)

    if log_dir is not None:
        resolved_dir = log_dir.expanduser().resolve()
        resolved_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            resolved_dir / "argenprecios.jsonl",
            maxBytes=20 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        root_logger.addHandler(file_handler)


__all__ = [
    "CredentialRedactingFilter",
    "configure_logging",
]
