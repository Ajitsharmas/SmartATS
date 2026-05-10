# -----------------------------------------------------------------------------------------------------------------------------------------------
# Purpose: Security Utilities (Hashing & JWT Handling)
# -----------------------------------------------------------------------------------------------------------------------------------------------
from datetime import datetime, timedelta, timezone
from passlib.context import CryptContext
from jose import jwt
from app.config import settings


# 1. password Hashing Configuration
# we use argon2, the industry standard for password hashing.
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verifies a plain password against the stored hash
    """
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """
    Generates a secure hash from a plain password.
    """
    return pwd_context.hash(password)


# 2. JWT Configuration
def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """
    Creates a JWT token with a set expireation time.
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=15)
    
    # Add the expiration claim ('exp') to payload
    to_encode.update({"exp": expire})

    # Sign the token using our SECRET_KEY
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    
    return encoded_jwt