from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from firebase_admin import exceptions as firebase_exceptions

from src.main import (
    delete_notification,
    delete_token,
    get_notifications,
    health_check,
    mark_all_as_read,
    mark_as_read,
    register_token,
    send_notification,
    send_via_expo,
    send_via_fcm,
)
from src.schemas import NotificationRequest, TokenRegistration


def make_query(first=None, all_items=None, count=0):
    query = MagicMock()
    query.filter.return_value = query
    query.first.return_value = first
    query.all.return_value = all_items or []
    query.count.return_value = count
    query.order_by.return_value = query
    query.offset.return_value = query
    query.limit.return_value = query
    query.update.return_value = None
    return query


class MockAsyncClient:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.post = AsyncMock(side_effect=self._call)

    async def _call(self, *args, **kwargs):
        if self._error:
            raise self._error
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.anyio
async def test_send_via_expo_success(monkeypatch):
    response = MagicMock(status_code=200)
    response.json.return_value = {"data": [{"status": "ok"}]}
    db = MagicMock()
    db_token = SimpleNamespace(user_id="user-1")
    client = MockAsyncClient(response=response)
    monkeypatch.setattr("src.main.httpx.AsyncClient", lambda: client)

    result = await send_via_expo("ExponentPushToken[x]", "Hi", "Body", {"a": 1}, db_token, db)

    assert result == {"status": "sent", "provider": "expo"}
    db.delete.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.anyio
async def test_send_via_expo_returns_error_for_non_200(monkeypatch):
    response = MagicMock(status_code=500, text="boom")
    response.json.return_value = {}
    db = MagicMock()
    client = MockAsyncClient(response=response)
    monkeypatch.setattr("src.main.httpx.AsyncClient", lambda: client)

    result = await send_via_expo("ExponentPushToken[x]", "Hi", "Body", {}, SimpleNamespace(user_id="u"), db)

    assert result == {"status": "error", "message": "Expo API status 500"}


@pytest.mark.anyio
async def test_send_via_expo_deletes_invalid_token(monkeypatch):
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "data": [{"status": "error", "details": {"error": "DeviceNotRegistered"}}]
    }
    db = MagicMock()
    db_token = SimpleNamespace(user_id="user-1")
    client = MockAsyncClient(response=response)
    monkeypatch.setattr("src.main.httpx.AsyncClient", lambda: client)

    result = await send_via_expo("ExponentPushToken[x]", "Hi", "Body", {}, db_token, db)

    assert result == {"status": "error", "message": "Token invalid and deleted"}
    db.delete.assert_called_once_with(db_token)
    db.commit.assert_called_once()


@pytest.mark.anyio
async def test_send_via_expo_handles_http_exception(monkeypatch):
    db = MagicMock()
    client = MockAsyncClient(error=RuntimeError("network down"))
    monkeypatch.setattr("src.main.httpx.AsyncClient", lambda: client)

    result = await send_via_expo("ExponentPushToken[x]", "Hi", "Body", {}, SimpleNamespace(user_id="u"), db)

    assert result == {"status": "error", "message": "network down"}


def test_send_via_fcm_returns_mock_when_firebase_not_configured(monkeypatch):
    monkeypatch.setattr("src.main.firebase_app", None)

    result = send_via_fcm("fcm-token", "Hi", "Body", {"flag": True}, SimpleNamespace(user_id="u"), MagicMock())

    assert result == {"status": "mock_sent", "message": "Firebase not configured"}


def test_send_via_fcm_success(monkeypatch):
    monkeypatch.setattr("src.main.firebase_app", object())
    monkeypatch.setattr("src.main.messaging.Message", lambda **kwargs: kwargs)
    monkeypatch.setattr("src.main.messaging.Notification", lambda title, body: {"title": title, "body": body})
    monkeypatch.setattr("src.main.messaging.send", lambda message: "msg-123")

    result = send_via_fcm("fcm-token", "Hi", "Body", {"n": 1}, SimpleNamespace(user_id="u"), MagicMock())

    assert result == {"status": "sent", "provider": "fcm", "message_id": "msg-123"}


def test_send_via_fcm_deletes_invalid_token(monkeypatch):
    monkeypatch.setattr("src.main.firebase_app", object())
    monkeypatch.setattr("src.main.messaging.Message", lambda **kwargs: kwargs)
    monkeypatch.setattr("src.main.messaging.Notification", lambda title, body: {"title": title, "body": body})
    monkeypatch.setattr(
        "src.main.messaging.send",
        lambda message: (_ for _ in ()).throw(ValueError("bad token")),
    )
    db = MagicMock()
    db_token = SimpleNamespace(user_id="u")

    result = send_via_fcm("fcm-token", "Hi", "Body", {}, db_token, db)

    assert result["status"] == "error"
    assert "Token invalid and deleted" in result["message"]
    db.delete.assert_called_once_with(db_token)
    db.commit.assert_called_once()


def test_send_via_fcm_handles_unexpected_error(monkeypatch):
    monkeypatch.setattr("src.main.firebase_app", object())
    monkeypatch.setattr("src.main.messaging.Message", lambda **kwargs: kwargs)
    monkeypatch.setattr("src.main.messaging.Notification", lambda title, body: {"title": title, "body": body})
    monkeypatch.setattr(
        "src.main.messaging.send",
        lambda message: (_ for _ in ()).throw(RuntimeError("push failed")),
    )

    result = send_via_fcm("fcm-token", "Hi", "Body", {}, SimpleNamespace(user_id="u"), MagicMock())

    assert result == {"status": "error", "message": "push failed"}


@pytest.mark.anyio
async def test_register_token_creates_new_token(monkeypatch):
    db = MagicMock()
    query = make_query(first=None)
    db.query.return_value = query

    result = await register_token(TokenRegistration(user_id="user-1", fcm_token="token-1"), None, db)

    assert result["status"] == "ok"
    db.add.assert_called_once()
    db.commit.assert_called_once()


@pytest.mark.anyio
async def test_register_token_updates_existing_token():
    existing = SimpleNamespace(fcm_token="old")
    db = MagicMock()
    db.query.return_value = make_query(first=existing)

    await register_token(TokenRegistration(user_id="user-1", fcm_token="new"), None, db)

    assert existing.fcm_token == "new"
    db.add.assert_not_called()
    db.commit.assert_called_once()


@pytest.mark.anyio
async def test_register_token_uses_authorization_sub(monkeypatch):
    db = MagicMock()
    db.query.return_value = make_query(first=None)
    monkeypatch.setattr("src.main.decode_jwt_payload_manually", lambda token: {"sub": "jwt-user"})

    await register_token(TokenRegistration(fcm_token="new"), "Bearer abc", db)

    db.add.assert_called_once()
    added = db.add.call_args.args[0]
    assert added.user_id == "jwt-user"


@pytest.mark.anyio
async def test_register_token_rejects_missing_user_id(monkeypatch):
    monkeypatch.setattr("src.main.decode_jwt_payload_manually", lambda token: {})

    with pytest.raises(HTTPException) as exc:
        await register_token(TokenRegistration(fcm_token="new"), "Bearer abc", MagicMock())

    assert exc.value.status_code == 400


@pytest.mark.anyio
async def test_delete_token_returns_ok_when_not_found():
    db = MagicMock()
    db.query.return_value = make_query(first=None)

    result = await delete_token("user-1", db)

    assert result == {"status": "ok", "message": "No token found"}


@pytest.mark.anyio
async def test_delete_token_deletes_when_found():
    token = SimpleNamespace(user_id="user-1")
    db = MagicMock()
    db.query.return_value = make_query(first=token)

    result = await delete_token("user-1", db)

    assert result == {"status": "ok", "message": "Token deleted"}
    db.delete.assert_called_once_with(token)
    db.commit.assert_called_once()


@pytest.mark.anyio
async def test_send_notification_returns_persisted_when_no_token():
    db = MagicMock()
    notification = SimpleNamespace(id=99)
    db.query.return_value = make_query(first=None)
    db.refresh.side_effect = lambda obj: setattr(obj, "id", 99)

    result = await send_notification(NotificationRequest(user_id="user-1", title="Hi", body="Body"), db)

    assert result["status"] == "persisted"
    assert result["notification_id"] == 99


@pytest.mark.anyio
async def test_send_notification_routes_to_expo(monkeypatch):
    db = MagicMock()
    token_row = SimpleNamespace(user_id="user-1", fcm_token="ExponentPushToken[abc]")
    db.query.return_value = make_query(first=token_row)
    db.refresh.side_effect = lambda obj: setattr(obj, "id", 7)
    monkeypatch.setattr("src.main.send_via_expo", AsyncMock(return_value={"status": "sent", "provider": "expo"}))

    result = await send_notification(NotificationRequest(user_id="user-1", title="Hi", body="Body"), db)

    assert result["status"] == "sent"
    assert result["notification_id"] == 7


@pytest.mark.anyio
async def test_send_notification_routes_to_fcm(monkeypatch):
    db = MagicMock()
    token_row = SimpleNamespace(user_id="user-1", fcm_token="raw-fcm-token")
    db.query.return_value = make_query(first=token_row)
    db.refresh.side_effect = lambda obj: setattr(obj, "id", 8)
    monkeypatch.setattr("src.main.send_via_fcm", lambda *args, **kwargs: {"status": "sent", "provider": "fcm"})

    result = await send_notification(NotificationRequest(user_id="user-1", title="Hi", body="Body"), db)

    assert result["provider"] == "fcm"
    assert result["notification_id"] == 8


@pytest.mark.anyio
async def test_get_notifications_normalizes_invalid_pagination(monkeypatch):
    notifications = [SimpleNamespace(id=1, title="one")]
    db = MagicMock()
    db.query.return_value = make_query(all_items=notifications, count=1)
    monkeypatch.setattr("src.main.get_user_id_from_auth", lambda auth: "user-1")

    result = await get_notifications(page=0, per_page=999, authorization="Bearer x", db=db)

    assert result["page"] == 1
    assert result["per_page"] == 20
    assert result["pages"] == 1
    assert result["notifications"] == notifications


@pytest.mark.anyio
async def test_mark_as_read_raises_when_not_found(monkeypatch):
    db = MagicMock()
    db.query.return_value = make_query(first=None)
    monkeypatch.setattr("src.main.get_user_id_from_auth", lambda auth: "user-1")

    with pytest.raises(HTTPException) as exc:
        await mark_as_read(1, "Bearer x", db)

    assert exc.value.status_code == 404


@pytest.mark.anyio
async def test_mark_as_read_updates_notification(monkeypatch):
    notification = SimpleNamespace(is_read=False)
    db = MagicMock()
    db.query.return_value = make_query(first=notification)
    monkeypatch.setattr("src.main.get_user_id_from_auth", lambda auth: "user-1")

    result = await mark_as_read(1, "Bearer x", db)

    assert result["status"] == "ok"
    assert notification.is_read is True
    db.commit.assert_called_once()


@pytest.mark.anyio
async def test_mark_all_as_read_updates_query(monkeypatch):
    db = MagicMock()
    query = make_query()
    db.query.return_value = query
    monkeypatch.setattr("src.main.get_user_id_from_auth", lambda auth: "user-1")

    result = await mark_all_as_read("Bearer x", db)

    assert result["status"] == "ok"
    query.update.assert_called_once()
    db.commit.assert_called_once()


@pytest.mark.anyio
async def test_delete_notification_raises_when_not_found(monkeypatch):
    db = MagicMock()
    db.query.return_value = make_query(first=None)
    monkeypatch.setattr("src.main.get_user_id_from_auth", lambda auth: "user-1")

    with pytest.raises(HTTPException) as exc:
        await delete_notification(1, "Bearer x", db)

    assert exc.value.status_code == 404


@pytest.mark.anyio
async def test_delete_notification_marks_deleted(monkeypatch):
    notification = SimpleNamespace(is_deleted=False)
    db = MagicMock()
    db.query.return_value = make_query(first=notification)
    monkeypatch.setattr("src.main.get_user_id_from_auth", lambda auth: "user-1")

    result = await delete_notification(1, "Bearer x", db)

    assert result["status"] == "ok"
    assert notification.is_deleted is True
    db.commit.assert_called_once()


@pytest.mark.anyio
async def test_health_check_returns_healthy():
    assert await health_check() == {"status": "healthy"}
