# ---------------------------------------------------------------------------
# Purpose: Authentication Routes (Login, Registration, Verification, Reset)
# ---------------------------------------------------------------------------

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import ExpiredSignatureError, JWTError
from pydantic import BaseModel, EmailStr
from sqlmodel import Session, select

from resend.exceptions import ResendError

from app.config import settings
from app.limiter import limiter
from app.database import get_session
from app.email import send_password_reset_email, send_verification_email
from app.models import User, UserCreate, UserPublic
from app.security import (
    create_access_token,
    decode_token,
    get_password_hash,
    verify_password,
)

auth_router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


# --- Request schemas (auth-only, no DB table needed) ---

class VerifyTokenRequest(BaseModel):
    token: str

class EmailOnlyRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str
    confirm_password: str


# --- LOGIN ---

@auth_router.post("/token", tags=["Auth"])
@limiter.limit("10/minute")
async def login_for_access_token(
    request: Request,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: SessionDep,
):
    """
    Exchange email and password for a JWT access token (OAuth2 password flow).

    - **username**: the recruiter's email address (OAuth2 form field name is `username`)
    - **password**: the recruiter's plain-text password

    Returns a Bearer token to be sent in the `Authorization` header on all
    protected requests.

    **Errors:**
    - `401` – email or password is incorrect
    - `403 EMAIL_NOT_VERIFIED` – account exists but the email has not been
      verified yet; the frontend should offer a Resend link
    """
    statement = select(User).where(User.email == form_data.username)
    user = session.exec(statement).first()

    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="EMAIL_NOT_VERIFIED",
        )

    access_token = create_access_token(data={"sub": user.email})
    return {"access_token": access_token, "token_type": "bearer"}


# --- REGISTRATION ---

@auth_router.post("/register", tags=["Auth"])
@limiter.limit("5/minute")
async def register_user(request: Request, user_in: UserCreate, session: SessionDep):
    """
    Register a new recruiter account.

    Creates the user with `is_verified=False`, generates a 10-minute
    email-verification JWT, stores it on the user record, and sends a
    verification email via Resend.  The account cannot be used to log in
    until the verification link is clicked.

    **Errors:**
    - `400` – email address is already registered
    """
    statement = select(User).where(User.email == user_in.email)
    if session.exec(statement).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    hashed_pw = get_password_hash(user_in.password)
    new_user = User(
        **user_in.model_dump(exclude={"password"}),
        hashed_password=hashed_pw,
        is_verified=False,
    )

    token = create_access_token(
        data={"sub": new_user.email, "purpose": "email_verification"},
        expires_delta=timedelta(minutes=10),
    )
    new_user.verification_token = token

    session.add(new_user)
    session.commit()

    try:
        send_verification_email(new_user.email, new_user.full_name or "", token)
    except ResendError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Account created but verification email could not be sent: {e}. "
                   "Use the Resend button on the login page to try again.",
        )

    return {"message": "Account created. Please check your email to verify your address."}


# --- EMAIL VERIFICATION ---

@auth_router.post("/verify-email", tags=["Auth"])
@limiter.limit("20/minute")
async def verify_email(request: Request, payload: VerifyTokenRequest, session: SessionDep):
    """
    Verify a recruiter's email address using the token from the verification email.

    Validates that the JWT is unexpired, carries the `email_verification` purpose,
    and matches the token stored on the user record (preventing reuse after a
    resend supersedes an older link).  On success, sets `is_verified=True` and
    clears the stored token.

    Called automatically by the JavaScript on `/verify-email` when the user
    clicks the link in their inbox.

    **Errors:**
    - `400` – token is expired, structurally invalid, wrong purpose, or has
      already been used / superseded by a newer resend
    - `404` – no user found for the email encoded in the token
    """
    try:
        data = decode_token(payload.token)
    except ExpiredSignatureError:
        raise HTTPException(status_code=400, detail="Verification link has expired. Please request a new one.")
    except JWTError:
        raise HTTPException(status_code=400, detail="Invalid verification link.")

    if data.get("purpose") != "email_verification":
        raise HTTPException(status_code=400, detail="Invalid verification link.")

    email = data.get("sub")
    user = session.exec(select(User).where(User.email == email)).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    if user.is_verified:
        return {"message": "Email already verified. You can log in."}

    if user.verification_token != payload.token:
        raise HTTPException(status_code=400, detail="Verification link has already been used or superseded.")

    user.is_verified = True
    user.verification_token = None
    session.add(user)
    session.commit()

    return {"message": "Email verified successfully. You can now log in."}


# --- RESEND VERIFICATION ---

@auth_router.post("/resend-verification", tags=["Auth"])
@limiter.limit("5/minute")
async def resend_verification(request: Request, payload: EmailOnlyRequest, session: SessionDep):
    """
    Re-send the email verification link for an unverified account.

    Generates a fresh 10-minute JWT, overwrites the previously stored token
    (which immediately invalidates any older link still in the user's inbox),
    and sends a new verification email.

    Intended for two cases:
    1. The original link expired before the user clicked it.
    2. The user never received the first email.

    **Errors:**
    - `400` – the account is already verified
    - `404` – no account found for the given email
    """
    user = session.exec(select(User).where(User.email == payload.email)).first()

    if not user:
        raise HTTPException(status_code=404, detail="No account found with that email.")

    if user.is_verified:
        raise HTTPException(status_code=400, detail="This email is already verified.")

    token = create_access_token(
        data={"sub": user.email, "purpose": "email_verification"},
        expires_delta=timedelta(minutes=10),
    )
    user.verification_token = token
    session.add(user)
    session.commit()

    try:
        send_verification_email(user.email, user.full_name or "", token)
    except ResendError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not send verification email: {e}",
        )

    return {"message": "Verification email sent. Please check your inbox."}


# --- FORGOT PASSWORD ---

@auth_router.post("/forgot-password", tags=["Auth"])
@limiter.limit("5/minute")
async def forgot_password(request: Request, payload: EmailOnlyRequest, session: SessionDep):
    """
    Initiate the password-reset flow for a verified recruiter account.

    If the email belongs to a verified account, generates a 15-minute
    password-reset JWT, stores it on the user record, and sends a reset email.
    The endpoint intentionally returns the **same success message regardless
    of whether the email exists**, to prevent account enumeration.

    Unverified accounts are silently ignored — the recruiter must verify their
    email before they can reset a password.
    """
    user = session.exec(select(User).where(User.email == payload.email)).first()

    if user and user.is_verified:
        token = create_access_token(
            data={"sub": user.email, "purpose": "password_reset"},
            expires_delta=timedelta(minutes=15),
        )
        user.reset_token = token
        session.add(user)
        session.commit()
        try:
            send_password_reset_email(user.email, user.full_name or "", token)
        except ResendError:
            pass  # Never reveal whether the address exists or why it failed

    return {"message": "If that email is registered, a password reset link has been sent."}


# --- RESET PASSWORD ---

@auth_router.post("/reset-password", tags=["Auth"])
@limiter.limit("10/minute")
async def reset_password(request: Request, payload: ResetPasswordRequest, session: SessionDep):
    """
    Complete the password-reset flow by setting a new password.

    Validates that:
    1. `new_password` and `confirm_password` match.
    2. The JWT is unexpired and carries the `password_reset` purpose.
    3. The token matches what is stored on the user record (one-time use —
       calling this endpoint a second time with the same token is rejected).

    On success, hashes and saves the new password and clears the stored token.

    **Errors:**
    - `400` – passwords do not match, token expired, token invalid/wrong
      purpose, or token already used
    - `404` – no user found for the email encoded in the token
    """
    if payload.new_password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")

    try:
        data = decode_token(payload.token)
    except ExpiredSignatureError:
        raise HTTPException(status_code=400, detail="Reset link has expired. Please request a new one.")
    except JWTError:
        raise HTTPException(status_code=400, detail="Invalid reset link.")

    if data.get("purpose") != "password_reset":
        raise HTTPException(status_code=400, detail="Invalid reset link.")

    email = data.get("sub")
    user = session.exec(select(User).where(User.email == email)).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    if user.reset_token != payload.token:
        raise HTTPException(status_code=400, detail="Reset link has already been used or superseded.")

    user.hashed_password = get_password_hash(payload.new_password)
    user.reset_token = None
    session.add(user)
    session.commit()

    return {"message": "Password updated successfully. You can now log in."}


# --- GATEKEEPER ---

async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)], session: SessionDep
) -> User:
    """
    FastAPI dependency that protects routes requiring an authenticated recruiter.

    Decodes the Bearer token from the `Authorization` header, extracts the
    subject (email), and confirms the user still exists in the database.
    Raises `401 Unauthorized` on any failure so the frontend can redirect to
    login.

    Usage::

        @app.get("/protected")
        def protected_route(current_user: Annotated[User, Depends(get_current_user)]):
            ...
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = decode_token(token)
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = session.exec(select(User).where(User.email == email)).first()
    if user is None:
        raise credentials_exception

    return user
