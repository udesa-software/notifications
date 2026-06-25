import base64
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config import settings
from src.database import Base, Notification, UserToken, get_db
from src.main import app

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


def create_mock_jwt(user_id: str):
    payload_dict = {"sub": user_id, "username": "testuser"}
    payload_json = json.dumps(payload_dict)
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip("=")
    return f"header.{payload_b64}.signature"


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_register_token_with_body_user_id():
    payload = {
        "user_id": "user-body-123",
        "fcm_token": "ExponentPushToken[mock]",
    }
    response = client.post("/tokens", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

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
    client.post("/tokens", json={"user_id": "user-update", "fcm_token": "OldToken"})
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

    db = TestingSessionLocal()
    notif = db.query(Notification).filter(Notification.user_id == "ghost_user").first()
    assert notif is not None
    assert notif.title == "Hi"
    assert notif.body == "World"
    assert notif.is_read is False
    assert notif.is_deleted is False
    db.close()


def test_get_notifications_history():
    db = TestingSessionLocal()

    for i in range(25):
        notif = Notification(
            user_id="user-1",
            title=f"Notification {i}",
            body=f"Body {i}",
            created_at=datetime.utcnow() + timedelta(minutes=i),
        )
        db.add(notif)
    db.commit()
    db.close()

    mock_jwt = create_mock_jwt("user-1")
    headers = {"Authorization": f"Bearer {mock_jwt}"}

    response = client.get("/?page=1&per_page=20", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 25
    assert data["page"] == 1
    assert data["pages"] == 2
    assert len(data["notifications"]) == 20
    assert data["notifications"][0]["title"] == "Notification 24"

    response = client.get("/?page=2&per_page=20", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data["notifications"]) == 5
    assert data["notifications"][-1]["title"] == "Notification 0"

    response = client.get("/?page=1")
    assert response.status_code == 401


def test_mark_all_notifications_as_read():
    db = TestingSessionLocal()
    db.add(Notification(user_id="user-2", title="N1", body="B1", is_read=False))
    db.add(Notification(user_id="user-2", title="N2", body="B2", is_read=False))
    db.add(Notification(user_id="user-3", title="N3", body="B3", is_read=False))
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
    notif = Notification(user_id="user-4", title="To Delete", body="Delete me")
    db.add(notif)
    db.commit()
    notif_id = notif.id
    db.close()

    mock_jwt = create_mock_jwt("user-4")
    headers = {"Authorization": f"Bearer {mock_jwt}"}

    response = client.delete(f"/{notif_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    db = TestingSessionLocal()
    db_notif = db.query(Notification).filter(Notification.id == notif_id).first()
    assert db_notif is not None
    assert db_notif.is_deleted is True
    db.close()

    response = client.get("/", headers=headers)
    assert response.status_code == 200
    assert response.json()["total"] == 0

    mock_jwt_other = create_mock_jwt("user-other")
    headers_other = {"Authorization": f"Bearer {mock_jwt_other}"}
    response = client.delete(f"/{notif_id}", headers=headers_other)
    assert response.status_code == 404


def test_mark_single_notification_as_read():
    db = TestingSessionLocal()
    notif = Notification(user_id="user-5", title="Notif Title", body="Notif Body", is_read=False)
    db.add(notif)
    db.commit()
    notif_id = notif.id
    db.close()

    mock_jwt = create_mock_jwt("user-5")
    headers = {"Authorization": f"Bearer {mock_jwt}"}

    response = client.put(f"/{notif_id}/read", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    db = TestingSessionLocal()
    db_notif = db.query(Notification).filter(Notification.id == notif_id).first()
    assert db_notif.is_read is True
    db.close()


def test_mark_single_notification_as_read_not_found():
    mock_jwt = create_mock_jwt("user-5")
    headers = {"Authorization": f"Bearer {mock_jwt}"}

    response = client.put("/99999/read", headers=headers)
    assert response.status_code == 404

