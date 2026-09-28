"""JSON logging. Passenger names/PNRs go to logs only masked (INFO) or at DEBUG level;
known secret values are redacted from every record as a safety net."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

_STD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Anything passed via extra={...} (job_id, image_id, duration_ms, ...)
        for key, value in vars(record).items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                data[key] = value
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str, ensure_ascii=False)


class RedactSecretsFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            msg = record.getMessage()
            if any(s in msg for s in self.secrets):
                for s in self.secrets:
                    msg = msg.replace(s, "***")
                record.msg, record.args = msg, None
        return True


def setup_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactSecretsFilter(secrets or []))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # HTTP client libraries can log request details; keep them quiet.
    for noisy in ("httpx", "httpcore", "google_genai", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def mask(value: str | None, keep: int = 2) -> str:
    """Mask passenger data for log lines: 'SHUKLA' -> 'SH****'."""
    if not value:
        return "<empty>"
    return value[:keep] + "*" * max(len(value) - keep, 0)
