"""Authentication utilities for inventory system."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from config import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES
from models import SessionLocal, User, UserRole, has_permission

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


# ── Password utils ───────────────────────────────────────────────

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── JWT utils ────────────────────────────────────────────────────

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


# ── Dependency: get DB ───────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Dependency: get current user ─────────────────────────────────

def get_current_user(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Get user from JWT token (header) or session cookie (for web)."""
    if not token:
        token = request.cookies.get("inv_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = decode_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    user = db.query(User).filter(User.id == int(user_id)).first()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    return user


# ── Permission-based access control ──────────────────────────────

def require_permission(permission: str):
    """Dependency factory: require a specific permission."""
    async def checker(user: User = Depends(get_current_user)):
        if not has_permission(user, permission):
            raise HTTPException(status_code=403, detail=f"Permission '{permission}' required")
        return user
    return checker


def require_any_permission(*permissions: str):
    """Dependency factory: require at least one of the given permissions."""
    async def checker(user: User = Depends(get_current_user)):
        for p in permissions:
            if has_permission(user, p):
                return user
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return checker


# ── Backward-compatible role check ───────────────────────────────

def require_role(*roles: UserRole):
    """Dependency factory: require one of the given roles. (Legacy compatibility.)"""
    async def checker(user: User = Depends(get_current_user)):
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return checker


# ── Seed default admin ───────────────────────────────────────────

def seed_admin():
    """Create default owner account if none exists."""
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            admin = User(
                username="admin",
                password_hash=hash_password("admin123"),
                display_name="Owner",
                role=UserRole.OWNER,
            )
            db.add(admin)
            db.commit()
            print("✅ Default admin created: admin / admin123")
    finally:
        db.close()
