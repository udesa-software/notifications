import json
import logging
from unittest.mock import MagicMock

from src import observability


def test_json_formatter_includes_extra_fields():
    formatter = observability.JsonFormatter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="hello world",
        args=(),
        exc_info=None,
    )
    record.request_id = "req-123"
    record.status = 200

    payload = json.loads(formatter.format(record))

    assert payload["message"] == "hello world"
    assert payload["logger"] == "test.logger"
    assert payload["request_id"] == "req-123"
    assert payload["status"] == 200


def test_json_formatter_includes_exception_info():
    formatter = observability.JsonFormatter()
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = logging.LogRecord(
            name="test.logger",
            level=logging.ERROR,
            pathname=__file__,
            lineno=20,
            msg="failed",
            args=(),
            exc_info=None,
        )
        record.exc_info = __import__("sys").exc_info()

    payload = json.loads(formatter.format(record))

    assert payload["message"] == "failed"
    assert "RuntimeError: boom" in payload["exc_info"]


def test_loki_handler_emit_builds_request_and_starts_thread(monkeypatch):
    monkeypatch.setattr(observability, "LOKI_ENABLED", True)
    monkeypatch.setattr(observability, "LOKI_URL", "https://logs.example.com")
    monkeypatch.setattr(observability, "LOKI_USER", "user")
    monkeypatch.setattr(observability, "LOKI_TOKEN", "token")

    started = {}

    class FakeThread:
        def __init__(self, target, args, daemon):
            started["target"] = target
            started["args"] = args
            started["daemon"] = daemon

        def start(self):
            started["started"] = True

    monkeypatch.setattr(observability.threading, "Thread", FakeThread)

    handler = observability.LokiHandler()
    handler.format = MagicMock(return_value=json.dumps({
        "time": "2026-06-27T12:00:00+00:00",
        "level": "info",
        "message": "hello",
    }))

    record = logging.LogRecord("test", logging.INFO, __file__, 30, "hello", (), None)
    handler.emit(record)

    req = started["args"][0]
    assert started["started"] is True
    assert started["daemon"] is True
    assert req.full_url == "https://logs.example.com/loki/api/v1/push"


def test_loki_handler_emit_returns_when_disabled(monkeypatch):
    monkeypatch.setattr(observability, "LOKI_ENABLED", False)

    handler = observability.LokiHandler()
    handler.format = MagicMock()

    record = logging.LogRecord("test", logging.INFO, __file__, 40, "hello", (), None)
    handler.emit(record)

    handler.format.assert_not_called()


def test_loki_handler_emit_swallows_invalid_payload(monkeypatch):
    monkeypatch.setattr(observability, "LOKI_ENABLED", True)

    handler = observability.LokiHandler()
    handler.format = MagicMock(return_value="not-json")

    record = logging.LogRecord("test", logging.INFO, __file__, 50, "hello", (), None)

    handler.emit(record)


def test_loki_handler_send_swallow_url_errors(monkeypatch):
    monkeypatch.setattr(
        observability.urllib.request,
        "urlopen",
        MagicMock(side_effect=observability.urllib.error.URLError("down")),
    )

    observability.LokiHandler._send(MagicMock())


def test_configure_logging_resets_root_handlers():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    try:
        root.handlers = [logging.NullHandler()]

        logger = observability.configure_logging()

        assert logger.name == "src.observability"
        assert len(root.handlers) == 2
        assert isinstance(root.handlers[0], logging.StreamHandler)
        assert isinstance(root.handlers[1], observability.LokiHandler)
    finally:
        root.handlers = original_handlers
