# ---------------------------------------------------------------------------
# Purpose: Authentication Routes (Login & Token Issuance)
# ---------------------------------------------------------------------------

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from sqlmodel import Session, select

from app.config import settings
from app.database import get_session

# CRITICAL: Import the new schemas (UserCreate, UserPublic)
from app.models import User, UserCreate, UserPublic
from app.security import create_access_token, get_password_hash, verify_password

# Create a dedicated router for Auth endpoints
auth_router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]

# 1. Define the Scheme
# This tells FastAPI: "The client must send a Bearer Token."
# "tokenUrl" tells Swagger UI where to send the user's password to GET that token.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


# --- LOGIN ENDPOINT (Get Token) ---
@auth_router.post("/token")
async def login_for_access_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()], session: SessionDep
):
    """
    OAuth2 compatible token login.
    Exchanges Email/Password for a JWT Access Token.
    """
    # 1. Find the User
    # Note: OAuth2PasswordRequestForm strictly requires 'username' and 'password' fields.
    # We map 'username' to our 'email' column.
    statement = select(User).where(User.email == form_data.username)
    user = session.exec(statement).first()

    # 2. Verify Identity
    # We check if the user exists AND if the password matches the hash.
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 3. Create Key Card (JWT)
    # If identity is verified, we issue a token signed with our SECRET_KEY.
    access_token = create_access_token(data={"sub": user.email})
    # 4. Return the Token
    # The frontend will save this token and send it in the Header for future requests.
    return {"access_token": access_token, "token_type": "bearer"}


# --- REGISTRATION ENDPOINT (New Route) ---
# Notice response_model=UserPublic. This ensures we NEVER return the password (hashed or plain).
@auth_router.post("/register", response_model=UserPublic)
async def register_user(
    user_in: UserCreate, session: SessionDep
):  # <--- Input is UserCreate (Has password)
    """
    Register a new recruiter.
    Accepts JSON payload, hashes password, saves to DB.
    """
    # 1. Check for duplicates
    # We must ensure emails are unique before inserting.
    statement = select(User).where(User.email == user_in.email)
    existing_user = session.exec(statement).first()

    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")

    # 2. Hash Password (CRITICAL)
    # We transform the plain password ("secret123") into Argon2 hash("$argon2id$...")
    hashed_pw = get_password_hash(user_in.password)

    # 3. Save to DB (Map UserCreate -> User)
    # We use model_dump to unpack fields (email, full_name) but exclude the plain password.
    # Then we explicitly add the hashed_password.
    new_user = User(
        **user_in.model_dump(exclude={"password"}), hashed_password=hashed_pw
    )

    session.add(new_user)
    session.commit()
    session.refresh(new_user)

    return new_user


# --- THE GATEKEEPER (Dependency) ---
async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)], session: SessionDep
) -> User:
    """
    The Gatekeeper.
    This function protects routes. If a user wants to access /jobs (POST),
    they must pass this check.

    1. Grabs the token from the Authorization Header.
    2. Decodes it using the SECRET_KEY.
    3. Checks if the user actually exists in the DB.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        # A. Decode the Token (The Wax Seal Check)
        # If the token was tampered with, this will fail immediately.
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )

        # B. Extract the Email (The Subject)
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception

    except JWTError:
        # If the signature is broken or the token is expired, this error fires.
        raise credentials_exception

    # C. Verify User Existence
    # Just because the token is valid doesn't mean the user wasn't banned 5 minutes ago

    # We always double-check the database.
    statement = select(User).where(User.email == email)
    user = session.exec(statement).first()

    if user is None:
        raise credentials_exception

    return user