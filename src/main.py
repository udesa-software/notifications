import logging
import os
import httpx
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, Header
from sqlalchemy.orm import Session
import firebase_admin
from firebase_admin import credentials, messaging, exceptions

from .auth_utils import decode_jwt_payload_manually, get_user_id_from_auth, verify_internal_secret
from .config import settings
from .database import get_db, init_db, UserToken, Notification
from .schemas import TokenRegistration, NotificationRequest, PaginatedNotifications, NotificationResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Udesamigos Notifications Service")

@app.on_event("startup")
def on_startup():
    init_db()

# Safe initialization of Firebase Admin SDK
firebase_app = None
if settings.FIREBASE_SERVICE_ACCOUNT_PATH and os.path.exists(settings.FIREBASE_SERVICE_ACCOUNT_PATH):
    try:
        cred = credentials.Certificate(settings.FIREBASE_SERVICE_ACCOUNT_PATH)
        firebase_app = firebase_admin.initialize_app(cred)
        logger.info("Firebase initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing Firebase: {e}")
else:
    logger.warning("Firebase service account path not found. Direct FCM fallback disabled (will mock direct FCM).")

# --- Auxiliary Sending Methods ---

async def send_via_expo(token: str, title: str, body: str, data: dict, db_token: UserToken, db: Session):
    """Sends a notification using the Expo Push Notification service (used for Expo Go)."""
    url = "https://exp.host/--/api/v2/push/send"
    payload = {
        "to": token,
        "sound": "default",
        "title": title,
        "body": body,
        "data": data or {}
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, timeout=10.0)
            res_data = response.json()
            
            # Check for HTTP status code errors
            if response.status_code != 200:
                logger.error(f"Expo API error status {response.status_code}: {response.text}")
                return {"status": "error", "message": f"Expo API status {response.status_code}"}

            # CA.4: Check for unregistered or invalid device token errors in response payload
            errors = res_data.get("data", [])
            for err in errors:
                if err.get("status") == "error":
                    details = err.get("details", {})
                    if details.get("error") in ["DeviceNotRegistered", "InvalidCredentials"]:
                        logger.warning(f"Expo token for user {db_token.user_id} is invalid ({details.get('error')}). Deleting from DB.")
                        db.delete(db_token)
                        db.commit()
                        return {"status": "error", "message": "Token invalid and deleted"}
            
            logger.info(f"Notification sent successfully via Expo to user {db_token.user_id}")
            return {"status": "sent", "provider": "expo"}
            
        except Exception as e:
            logger.error(f"HTTP error calling Expo Push API: {e}")
            return {"status": "error", "message": str(e)}

def send_via_fcm(token: str, title: str, body: str, data: dict, db_token: UserToken, db: Session):
    """Sends a notification using direct Firebase Cloud Messaging (FCM)."""
    if not firebase_app:
        logger.info(f"[MOCK PUSH - FCM] To: {db_token.user_id}, Title: {title}, Body: {body}, Data: {data}")
        return {"status": "mock_sent", "message": "Firebase not configured"}

    try:
        # Convert all dictionary values in data to strings as required by FCM
        string_data = {k: str(v) for k, v in (data or {}).items()}
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=string_data,
            token=token,
        )
        response = messaging.send(message)
        logger.info(f"Notification sent successfully via FCM: {response}")
        return {"status": "sent", "provider": "fcm", "message_id": response}

    except (messaging.UnregisteredError, exceptions.InvalidArgumentError, ValueError) as e:
        # CA.4: Delete the invalid or unregistered token from the database
        logger.warning(f"FCM token for user {db_token.user_id} is invalid. Deleting from DB. Reason: {e}")
        db.delete(db_token)
        db.commit()
        return {"status": "error", "message": f"Token invalid and deleted: {str(e)}"}
    except Exception as e:
        logger.error(f"Error sending direct FCM: {e}")
        return {"status": "error", "message": str(e)}

# --- Endpoints ---

@app.post("/tokens")
async def register_token(
    reg: TokenRegistration, 
    authorization: Optional[str] = Header(None), 
    db: Session = Depends(get_db)
):
    """Registers or updates a push notification token for a user."""
    user_id = reg.user_id
    
    # Infer user_id from Authorization JWT if not supplied in body
    if not user_id and authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]
        payload = decode_jwt_payload_manually(token)
        user_id = payload.get("sub")
        
    if not user_id:
        raise HTTPException(
            status_code=400, 
            detail="User ID not provided in request body and could not be inferred from Authorization header"
        )
        
    db_token = db.query(UserToken).filter(UserToken.user_id == user_id).first()
    if db_token:
        db_token.fcm_token = reg.fcm_token
    else:
        db_token = UserToken(user_id=user_id, fcm_token=reg.fcm_token)
        db.add(db_token)
    
    db.commit()
    logger.info(f"Token registered/updated for user {user_id}")
    return {"status": "ok", "message": "Token registered successfully"}

@app.delete("/tokens/{user_id}", dependencies=[Depends(verify_internal_secret)])
async def delete_token(user_id: str, db: Session = Depends(get_db)):
    """Deletes a user's push token (triggered during logout, suspension, or deletion)."""
    db_token = db.query(UserToken).filter(UserToken.user_id == user_id).first()
    if db_token:
        db.delete(db_token)
        db.commit()
        logger.info(f"Token deleted for user {user_id}")
        return {"status": "ok", "message": "Token deleted"}
    return {"status": "ok", "message": "No token found"}

@app.post("/notify", dependencies=[Depends(verify_internal_secret)])
async def send_notification(req: NotificationRequest, db: Session = Depends(get_db)):
    """Persists and sends a push notification to a user using the appropriate delivery channel."""
    # 1. Always persist the notification in the DB
    db_notification = Notification(
        user_id=req.user_id,
        title=req.title,
        body=req.body,
        data=req.data
    )
    db.add(db_notification)
    db.commit()
    db.refresh(db_notification)
    logger.info(f"Notification persisted in DB for user {req.user_id} with ID {db_notification.id}")

    # 2. Retrieve user push token
    db_token = db.query(UserToken).filter(UserToken.user_id == req.user_id).first()
    if not db_token:
        logger.warning(f"No push token registered for user {req.user_id}. Push delivery skipped.")
        return {
            "status": "persisted",
            "message": "Notification saved to DB, but push skipped (no token)",
            "notification_id": db_notification.id
        }

    token = db_token.fcm_token
    
    # 3. Route according to token format
    if token.startswith("ExponentPushToken"):
        push_res = await send_via_expo(token, req.title, req.body, req.data, db_token, db)
    else:
        push_res = send_via_fcm(token, req.title, req.body, req.data, db_token, db)

    # Add notification_id to push result for reference
    if isinstance(push_res, dict):
        push_res["notification_id"] = db_notification.id
    return push_res

@app.get("/", response_model=PaginatedNotifications)
async def get_notifications(
    page: int = 1,
    per_page: int = 20,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
):
    """Retrieves paginated notifications for the authenticated user, ordered from newest to oldest (CA.3)."""
    user_id = get_user_id_from_auth(authorization)
    
    if page < 1:
        page = 1
    if per_page < 1 or per_page > 100:
        per_page = 20
        
    query = db.query(Notification).filter(
        Notification.user_id == user_id,
        Notification.is_deleted == False
    )
    
    total = query.count()
    pages = (total + per_page - 1) // per_page if total > 0 else 0
    
    notifications = query.order_by(Notification.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
    
    return {
        "notifications": notifications,
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page
    }

@app.put("/{notification_id}/read")
async def mark_as_read(
    notification_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
):
    """Marks a single notification as read for the authenticated user."""
    user_id = get_user_id_from_auth(authorization)
    
    notification = db.query(Notification).filter(
        Notification.id == notification_id,
        Notification.user_id == user_id,
        Notification.is_deleted == False
    ).first()
    
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
        
    notification.is_read = True
    db.commit()
    logger.info(f"Notification {notification_id} marked as read for user {user_id}")
    return {"status": "ok", "message": "Notification marked as read"}

@app.put("/read-all")
async def mark_all_as_read(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
):
    """Marks all non-deleted notifications as read for the authenticated user (CA.2)."""
    user_id = get_user_id_from_auth(authorization)
    
    db.query(Notification).filter(
        Notification.user_id == user_id,
        Notification.is_read == False,
        Notification.is_deleted == False
    ).update({Notification.is_read: True}, synchronize_session=False)
    
    db.commit()
    logger.info(f"All notifications marked as read for user {user_id}")
    return {"status": "ok", "message": "All notifications marked as read"}

@app.delete("/{notification_id}")
async def delete_notification(
    notification_id: int,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
):
    """Logically deletes a single notification for the authenticated user (CA.4, CA.5)."""
    user_id = get_user_id_from_auth(authorization)
    
    notification = db.query(Notification).filter(
        Notification.id == notification_id,
        Notification.user_id == user_id,
        Notification.is_deleted == False
    ).first()
    
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
        
    notification.is_deleted = True
    db.commit()
    logger.info(f"Notification {notification_id} logically deleted for user {user_id}")
    return {"status": "ok", "message": "Notification deleted successfully"}

@app.get("/health")
async def health_check():
    return {"status": "healthy"}
