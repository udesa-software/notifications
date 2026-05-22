import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import base64
import json

from src.database import Base, get_db, UserToken
from src.main import app
from src.config import settings

# --- Setup In-Memory SQLite Database for Testing ---
SQLALCHEMY_DATABASE_URL = "sqlite://"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

@pytest.fixture(autouse=True)
def setup_database():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)

client = TestClient(app)


# --- Helper for Mocking JWTs ---
def create_mock_jwt(user_id: str):
    payload_dict = {"sub": user_id, "username": "testuser"}
    payload_json = json.dumps(payload_dict)
    # URL safe base64 without padding
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip("=")
    return f"header.{payload_b64}.signature"


# --- Tests ---

def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}

def test_register_token_with_body_user_id():
    payload = {
        "user_id": "user-body-123",
        "fcm_token": "ExponentPushToken[mock]"
    }
    response = client.post("/tokens", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    
    # Check DB
    db = TestingSessionLocal()
    token = db.query(UserToken).filter(UserToken.user_id == "user-body-123").first()
    assert token is not None
    assert token.fcm_token == "ExponentPushToken[mock]"
    db.close()

def test_register_token_with_jwt_authorization():
    mock_jwt = create_mock_jwt("user-jwt-123")
    headers = {"Authorization": f"Bearer {mock_jwt}"}
    payload = {"fcm_token": "ExponentPushToken[mock-jwt-token]"}
    
    response = client.post("/tokens", json=payload, headers=headers)
    assert response.status_code == 200
    
    db = TestingSessionLocal()
    token = db.query(UserToken).filter(UserToken.user_id == "user-jwt-123").first()
    assert token is not None
    assert token.fcm_token == "ExponentPushToken[mock-jwt-token]"
    db.close()

def test_register_token_fails_missing_user_id():
    payload = {"fcm_token": "ExponentPushToken[mock-token]"}
    response = client.post("/tokens", json=payload)
    assert response.status_code == 400
    assert "User ID not provided" in response.json()["detail"]

def test_update_existing_token():
    # Insert first
    client.post("/tokens", json={"user_id": "user-update", "fcm_token": "OldToken"})
    # Update
    client.post("/tokens", json={"user_id": "user-update", "fcm_token": "NewToken"})
    
    db = TestingSessionLocal()
    token = db.query(UserToken).filter(UserToken.user_id == "user-update").first()
    assert token.fcm_token == "NewToken"
    db.close()

def test_delete_token_success():
    client.post("/tokens", json={"user_id": "user-delete", "fcm_token": "SomeToken"})
    
    headers = {"X-Internal-Secret": settings.INTERNAL_SECRET}
    response = client.delete("/tokens/user-delete", headers=headers)
    
    assert response.status_code == 200
    db = TestingSessionLocal()
    token = db.query(UserToken).filter(UserToken.user_id == "user-delete").first()
    assert token is None
    db.close()

def test_delete_token_unauthorized():
    headers = {"X-Internal-Secret": "wrong_secret"}
    response = client.delete("/tokens/user-delete", headers=headers)
    assert response.status_code == 403

def test_notify_unauthorized():
    payload = {"user_id": "some_user", "title": "Hi", "body": "World"}
    headers = {"X-Internal-Secret": "wrong_secret"}
    response = client.post("/notify", json=payload, headers=headers)
    assert response.status_code == 403

def test_notify_persisted_if_token_not_found():
    headers = {"X-Internal-Secret": settings.INTERNAL_SECRET}
    payload = {"user_id": "ghost_user", "title": "Hi", "body": "World"}
    response = client.post("/notify", json=payload, headers=headers)
    
    assert response.status_code == 200
    assert response.json()["status"] == "persisted"
    assert "notification_id" in response.json()

    # Verify it was persisted in DB
    db = TestingSessionLocal()
    from src.database import Notification
    notif = db.query(Notification).filter(Notification.user_id == "ghost_user").first()
    assert notif is not None
    assert notif.title == "Hi"
    assert notif.body == "World"
    assert notif.is_read is False
    assert notif.is_deleted is False
    db.close()

def test_get_notifications_history():
    # Insert 25 notifications for user-1
    db = TestingSessionLocal()
    from src.database import Notification
    from datetime import datetime, timedelta
    
    for i in range(25):
        notif = Notification(
            user_id="user-1",
            title=f"Notification {i}",
            body=f"Body {i}",
            created_at=datetime.utcnow() + timedelta(minutes=i)
        )
        db.add(notif)
    db.commit()
    db.close()

    mock_jwt = create_mock_jwt("user-1")
    headers = {"Authorization": f"Bearer {mock_jwt}"}

    # Page 1
    response = client.get("/?page=1&per_page=20", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 25
    assert data["page"] == 1
    assert data["pages"] == 2
    assert len(data["notifications"]) == 20
    # Order check: most recent (i=24) first
    assert data["notifications"][0]["title"] == "Notification 24"

    # Page 2
    response = client.get("/?page=2&per_page=20", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data["notifications"]) == 5
    # Oldest of page 2 should be i=0
    assert data["notifications"][-1]["title"] == "Notification 0"

    # Test unauthorized
    response = client.get("/?page=1")
    assert response.status_code == 401

def test_mark_all_notifications_as_read():
    db = TestingSessionLocal()
    from src.database import Notification
    db.add(Notification(user_id="user-2", title="N1", body="B1", is_read=False))
    db.add(Notification(user_id="user-2", title="N2", body="B2", is_read=False))
    db.add(Notification(user_id="user-3", title="N3", body="B3", is_read=False))  # another user
    db.commit()
    db.close()

    mock_jwt = create_mock_jwt("user-2")
    headers = {"Authorization": f"Bearer {mock_jwt}"}

    response = client.put("/read-all", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    db = TestingSessionLocal()
    user2_notifs = db.query(Notification).filter(Notification.user_id == "user-2").all()
    assert all(n.is_read for n in user2_notifs)

    user3_notif = db.query(Notification).filter(Notification.user_id == "user-3").first()
    assert user3_notif.is_read is False
    db.close()

def test_logical_delete_notification():
    db = TestingSessionLocal()
    from src.database import Notification
    notif = Notification(user_id="user-4", title="To Delete", body="Delete me")
    db.add(notif)
    db.commit()
    notif_id = notif.id
    db.close()

    mock_jwt = create_mock_jwt("user-4")
    headers = {"Authorization": f"Bearer {mock_jwt}"}

    # Delete
    response = client.delete(f"/{notif_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    # Verify soft deleted in DB
    db = TestingSessionLocal()
    db_notif = db.query(Notification).filter(Notification.id == notif_id).first()
    assert db_notif is not None
    assert db_notif.is_deleted is True
    db.close()

    # Verify excluded from history GET
    response = client.get("/", headers=headers)
    assert response.status_code == 200
    assert response.json()["total"] == 0

    # Test deleting someone else's notification
    mock_jwt_other = create_mock_jwt("user-other")
    headers_other = {"Authorization": f"Bearer {mock_jwt_other}"}
    response = client.delete(f"/{notif_id}", headers=headers_other)
    assert response.status_code == 404

def test_mark_single_notification_as_read():
    db = TestingSessionLocal()
    from src.database import Notification
    notif = Notification(user_id="user-5", title="Notif Title", body="Notif Body", is_read=False)
    db.add(notif)
    db.commit()
    notif_id = notif.id
    db.close()

    mock_jwt = create_mock_jwt("user-5")
    headers = {"Authorization": f"Bearer {mock_jwt}"}

    # Mark as read
    response = client.put(f"/{notif_id}/read", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    # Verify in DB
    db = TestingSessionLocal()
    db_notif = db.query(Notification).filter(Notification.id == notif_id).first()
    assert db_notif.is_read is True
    db.close()

    # Test marking someone else's notification as read
    mock_jwt_other = create_mock_jwt("user-other")
    headers_other = {"Authorization": f"Bearer {mock_jwt_other}"}
    response = client.put(f"/{notif_id}/read", headers=headers_other)
    assert response.status_code == 404

# --- Additional Test Cases for Coverage ---

def test_init_db():
    from unittest.mock import patch
    with patch("src.database.Base.metadata.create_all") as mock_create:
        from src.database import init_db
        init_db()
        mock_create.assert_called_once()

def test_get_db():
    from unittest.mock import MagicMock, patch
    mock_session = MagicMock()
    with patch("src.database.SessionLocal", return_value=mock_session):
        from src.database import get_db
        generator = get_db()
        db = next(generator)
        assert db == mock_session
        try:
            next(generator)
        except StopIteration:
            pass
        mock_session.close.assert_called_once()

def test_on_startup():
    from unittest.mock import patch
    with patch("src.main.init_db") as mock_init:
        from src.main import on_startup
        on_startup()
        mock_init.assert_called_once()

def test_delete_token_not_found():
    headers = {"X-Internal-Secret": settings.INTERNAL_SECRET}
    response = client.delete("/tokens/nonexistent-user", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "message": "No token found"}

def test_get_user_id_from_auth_no_sub():
    import base64, json
    payload_dict = {"username": "testuser"}
    payload_json = json.dumps(payload_dict)
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip("=")
    mock_jwt = f"header.{payload_b64}.signature"
    
    headers = {"Authorization": f"Bearer {mock_jwt}"}
    response = client.get("/?page=1", headers=headers)
    assert response.status_code == 401
    assert "sub not found" in response.json()["detail"]

def test_invalid_jwt_format_and_exception():
    headers = {"Authorization": "Bearer bad-token"}
    response = client.get("/?page=1", headers=headers)
    assert response.status_code == 401
    
    headers = {"Authorization": "Bearer part1.invalid-b64-value!!!!.part3"}
    response = client.get("/?page=1", headers=headers)
    assert response.status_code == 401

def test_get_notifications_pagination_out_of_bounds():
    mock_jwt = create_mock_jwt("user-1")
    headers = {"Authorization": f"Bearer {mock_jwt}"}
    response = client.get("/?page=0&per_page=150", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["page"] == 1
    assert data["per_page"] == 20

@pytest.mark.anyio
async def test_send_notification_expo_success():
    client.post("/tokens", json={"user_id": "user-expo-ok", "fcm_token": "ExponentPushToken[ok]"})
    
    from unittest.mock import patch, AsyncMock, MagicMock
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"data": [{"status": "ok"}]}
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        headers = {"X-Internal-Secret": settings.INTERNAL_SECRET}
        payload = {"user_id": "user-expo-ok", "title": "Expo", "body": "Success"}
        response = client.post("/notify", json=payload, headers=headers)
        
        assert response.status_code == 200
        assert response.json()["status"] == "sent"
        assert response.json()["provider"] == "expo"
        mock_post.assert_called_once()

@pytest.mark.anyio
async def test_send_notification_expo_invalid_token():
    client.post("/tokens", json={"user_id": "user-expo-fail", "fcm_token": "ExponentPushToken[bad]"})
    
    from unittest.mock import patch, AsyncMock, MagicMock
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [{
            "status": "error",
            "details": {"error": "DeviceNotRegistered"}
        }]
    }
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        headers = {"X-Internal-Secret": settings.INTERNAL_SECRET}
        payload = {"user_id": "user-expo-fail", "title": "Expo", "body": "Bad Token"}
        response = client.post("/notify", json=payload, headers=headers)
        
        assert response.status_code == 200
        assert response.json()["status"] == "error"
        assert "deleted" in response.json()["message"]
        
        db = TestingSessionLocal()
        token = db.query(UserToken).filter(UserToken.user_id == "user-expo-fail").first()
        assert token is None
        db.close()

@pytest.mark.anyio
async def test_send_notification_expo_http_error():
    client.post("/tokens", json={"user_id": "user-expo-http-fail", "fcm_token": "ExponentPushToken[http]"})
    
    from unittest.mock import patch, AsyncMock, MagicMock
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        headers = {"X-Internal-Secret": settings.INTERNAL_SECRET}
        payload = {"user_id": "user-expo-http-fail", "title": "Expo", "body": "Http Error"}
        response = client.post("/notify", json=payload, headers=headers)
        
        assert response.status_code == 200
        assert response.json()["status"] == "error"
        assert "Expo API status 500" in response.json()["message"]

@pytest.mark.anyio
async def test_send_notification_expo_exception():
    client.post("/tokens", json={"user_id": "user-expo-exc", "fcm_token": "ExponentPushToken[exc]"})
    
    from unittest.mock import patch, AsyncMock
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = Exception("Network failure")
        headers = {"X-Internal-Secret": settings.INTERNAL_SECRET}
        payload = {"user_id": "user-expo-exc", "title": "Expo", "body": "Exception"}
        response = client.post("/notify", json=payload, headers=headers)
        
        assert response.status_code == 200
        assert response.json()["status"] == "error"
        assert "Network failure" in response.json()["message"]

def test_send_notification_fcm_success():
    client.post("/tokens", json={"user_id": "user-fcm-ok", "fcm_token": "FCMToken-123"})
    
    from unittest.mock import patch, MagicMock
    mock_send = MagicMock(return_value="mock_msg_id")
    
    with patch("src.main.firebase_app", new=True), \
         patch("firebase_admin.messaging.send", mock_send):
             
        headers = {"X-Internal-Secret": settings.INTERNAL_SECRET}
        payload = {"user_id": "user-fcm-ok", "title": "FCM", "body": "Success"}
        response = client.post("/notify", json=payload, headers=headers)
        
        assert response.status_code == 200
        assert response.json()["status"] == "sent"
        assert response.json()["provider"] == "fcm"
        assert response.json()["message_id"] == "mock_msg_id"

def test_send_notification_fcm_invalid_token():
    client.post("/tokens", json={"user_id": "user-fcm-fail", "fcm_token": "FCMToken-bad"})
    
    from firebase_admin import messaging
    from unittest.mock import patch
    
    with patch("src.main.firebase_app", new=True), \
         patch("firebase_admin.messaging.send", side_effect=messaging.UnregisteredError("Unregistered token")):
             
        headers = {"X-Internal-Secret": settings.INTERNAL_SECRET}
        payload = {"user_id": "user-fcm-fail", "title": "FCM", "body": "Fail"}
        response = client.post("/notify", json=payload, headers=headers)
        
        assert response.status_code == 200
        assert response.json()["status"] == "error"
        assert "deleted" in response.json()["message"]
        
        db = TestingSessionLocal()
        token = db.query(UserToken).filter(UserToken.user_id == "user-fcm-fail").first()
        assert token is None
        db.close()

def test_send_notification_fcm_exception():
    client.post("/tokens", json={"user_id": "user-fcm-exc", "fcm_token": "FCMToken-exc"})
    
    from unittest.mock import patch
    with patch("src.main.firebase_app", new=True), \
         patch("firebase_admin.messaging.send", side_effect=Exception("Firebase generic error")):
             
        headers = {"X-Internal-Secret": settings.INTERNAL_SECRET}
        payload = {"user_id": "user-fcm-exc", "title": "FCM", "body": "Exception"}
        response = client.post("/notify", json=payload, headers=headers)
        
        assert response.status_code == 200
        assert response.json()["status"] == "error"
        assert "Firebase generic error" in response.json()["message"]

def test_firebase_init_exception():
    import importlib
    from unittest.mock import patch
    with patch("firebase_admin.credentials.Certificate", side_effect=Exception("Certificate error")), \
         patch("os.path.exists", return_value=True):
             
             import src.main
             importlib.reload(src.main)

def test_send_notification_fcm_mock_fallback():
    client.post("/tokens", json={"user_id": "user-fcm-mock", "fcm_token": "FCMToken-mock"})
    
    from unittest.mock import patch
    with patch("src.main.firebase_app", new=None):
        headers = {"X-Internal-Secret": settings.INTERNAL_SECRET}
        payload = {"user_id": "user-fcm-mock", "title": "FCM", "body": "Mock"}
        response = client.post("/notify", json=payload, headers=headers)
        
        assert response.status_code == 200
        assert response.json()["status"] == "mock_sent"
        assert "not configured" in response.json()["message"]





