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
    assert response.status_code == 401

def test_notify_unauthorized():
    payload = {"user_id": "some_user", "title": "Hi", "body": "World"}
    headers = {"X-Internal-Secret": "wrong_secret"}
    response = client.post("/notify", json=payload, headers=headers)
    assert response.status_code == 401

def test_notify_skipped_if_token_not_found():
    headers = {"X-Internal-Secret": settings.INTERNAL_SECRET}
    payload = {"user_id": "ghost_user", "title": "Hi", "body": "World"}
    response = client.post("/notify", json=payload, headers=headers)
    
    assert response.status_code == 200
    assert response.json()["status"] == "skipped"

