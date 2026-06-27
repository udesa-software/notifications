import base64
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone


SERVICE = os.getenv("SERVICE_NAME", "notifications")
ENV = os.getenv("NODE_ENV", os.getenv("ENV", "development"))
LOKI_URL = os.getenv("LOKI_URL")
LOKI_USER = os.getenv("LOKI_USER")
LOKI_TOKEN = os.getenv("LOKI_TOKEN")
LOKI_ENABLED = bool(LOKI_URL and LOKI_USER and LOKI_TOKEN)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "service": SERVICE,
            "env": ENV,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key.startswith("_") or key in {
                "args",
                "created",
                "exc_info",
                "exc_text",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "message",
                "msg",
                "name",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "thread",
                "threadName",
            }:
                continue
            payload[key] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=True)


class LokiHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if not LOKI_ENABLED:
            return

        try:
            line = self.format(record)
            parsed = json.loads(line)
            ts = parsed.get("time")
            timestamp_ns = str(
                int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1_000_000_000)
                if ts
                else int(record.created * 1_000_000_000)
            )
            payload = {
                "streams": [{
                    "stream": {
                        "service": SERVICE,
                        "env": ENV,
                        "level": parsed.get("level", record.levelname.lower()),
                    },
                    "values": [[timestamp_ns, line]],
                }]
            }
            auth = base64.b64encode(f"{LOKI_USER}:{LOKI_TOKEN}".encode()).decode()
            req = urllib.request.Request(
                f"{LOKI_URL.rstrip('/')}/loki/api/v1/push",
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Basic {auth}",
                },
                method="POST",
            )
            threading.Thread(target=self._send, args=(req,), daemon=True).start()
        except Exception:
            return

    @staticmethod
    def _send(req: urllib.request.Request) -> None:
        try:
            with urllib.request.urlopen(req, timeout=2):
                return
        except (urllib.error.URLError, TimeoutError, ValueError):
            return


def configure_logging() -> logging.Logger:
    formatter = JsonFormatter()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    loki_handler = LokiHandler()
    loki_handler.setFormatter(formatter)
    root.addHandler(loki_handler)

    return logging.getLogger(__name__)
