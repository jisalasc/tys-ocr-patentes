import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt


load_dotenv()

ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 8
security = HTTPBearer(auto_error=False)


def _get_secret() -> str:
    secret = os.getenv("JWT_SECRET", "").strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT_SECRET no esta configurado en el entorno.",
        )
    return secret


def authenticate_user(username: str, password: str) -> bool:
    admin_user = os.getenv("ADMIN_USER", "").strip()
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    return username == admin_user and password == admin_password


def create_access_token(subject: str) -> str:
    now = datetime.now(timezone.utc)
    expire = now + timedelta(hours=TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    return jwt.encode(payload, _get_secret(), algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, _get_secret(), algorithms=[ALGORITHM])
        return payload
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalido o expirado.",
        ) from exc


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Se requiere Authorization: Bearer <token>.",
        )

    payload = decode_access_token(credentials.credentials)
    subject = payload.get("sub")
    if not subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token sin sujeto valido.",
        )
    return subject
