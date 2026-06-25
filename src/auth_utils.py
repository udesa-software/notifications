import base64
import json
import logging
from typing import Optional

from fastapi import Header
from fastapi import HTTPException

from .config import settings

logger = logging.getLogger(__name__)


def verify_internal_secret(x_internal_secret: Optional[str] = Header(None)):
    if not x_internal_secret or x_internal_secret != settings.INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Invalid internal secret")


def decode_jwt_payload_manually(token: str) -> dict:
    """Decodes a JWT payload manually without signature verification."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid JWT format")

        payload_b64 = parts[1]
        payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)

        payload_bytes = base64.b64decode(payload_b64.replace("-", "+").replace("_", "/"))
        return json.loads(payload_bytes.decode("utf-8"))
    except Exception as e:
        logger.error(f"Error decoding JWT manually: {e}")
        return {}


def get_user_id_from_auth(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization.split(" ")[1]
    payload = decode_jwt_payload_manually(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload: sub not found")
    return user_id
