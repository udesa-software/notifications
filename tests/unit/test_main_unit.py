import pytest
from fastapi import HTTPException

from src.auth_utils import decode_jwt_payload_manually, get_user_id_from_auth, verify_internal_secret


def test_decode_jwt_payload_manually_returns_payload():
    token = "header.eyJzdWIiOiAidXNlci0xMjMifQ.signature"

    payload = decode_jwt_payload_manually(token)

    assert payload == {"sub": "user-123"}


def test_decode_jwt_payload_manually_returns_empty_dict_for_invalid_token():
    payload = decode_jwt_payload_manually("invalid-token")

    assert payload == {}


def test_verify_internal_secret_accepts_valid_secret(monkeypatch):
    monkeypatch.setattr("src.auth_utils.settings.INTERNAL_SECRET", "secret-ok")

    assert verify_internal_secret("secret-ok") is None


def test_verify_internal_secret_rejects_invalid_secret(monkeypatch):
    monkeypatch.setattr("src.auth_utils.settings.INTERNAL_SECRET", "secret-ok")

    with pytest.raises(HTTPException) as exc:
        verify_internal_secret("wrong-secret")

    assert exc.value.status_code == 403
    assert exc.value.detail == "Invalid internal secret"


def test_get_user_id_from_auth_returns_sub():
    authorization = "Bearer header.eyJzdWIiOiAidXNlci1hYmMifQ.signature"

    user_id = get_user_id_from_auth(authorization)

    assert user_id == "user-abc"


def test_get_user_id_from_auth_rejects_missing_header():
    with pytest.raises(HTTPException) as exc:
        get_user_id_from_auth(None)

    assert exc.value.status_code == 401
    assert exc.value.detail == "Missing or invalid Authorization header"


def test_get_user_id_from_auth_rejects_missing_sub():
    authorization = "Bearer header.eyJ1c2VybmFtZSI6ICJ0ZXN0In0.signature"

    with pytest.raises(HTTPException) as exc:
        get_user_id_from_auth(authorization)

    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid token payload: sub not found"
