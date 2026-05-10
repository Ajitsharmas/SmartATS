# -----------------------------------------------------------------------------------------------------------------------------------------------
# Purpose: Authentication Routes (Login & Token Issuance)
# -----------------------------------------------------------------------------------------------------------------------------------------------
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm, OAuth2PasswordBearer
from sqlmodel import Session, select
from typing import Annotated
from jose import JWTError, jwt
from app.database import get_session
from app.models import User
from app.security import verify_password, create_access_token, get_password_hash
from app.config import settings

# Create a dedicated Router
auth_router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]

@auth_router.post("/token")
async def login_for_access_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()], session: SessionDep
):
    """
    OAuth2 compatible token login, get an access token for future requestes.
    """

    # 1. Find the User
    # Note: OAuth2 form uses 'username', even if we treat it as email
    statement = select(User).where(User.email==form_data.username)
    user = session.exec(statement).first()

    # 2. Verify Identity
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
            )

    # 3. Create Key Card (JWT)
    access_token = create_access_token(data={"sub": user.email})

    # 4. Return the Token
    return {"access_token": access_token, "token_type": "bearer"}

@auth_router.post("/register")
async def register_user(email: str, password: str, full_name: str, session: SessionDep):
    """
    Helper endpoint to create a user so we can actually login
    """
    # Check if user exists
    statement = select(User).where(User.email == email)
    existing_user = session.exec(statement).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered.")
    
    # Create new user with HASHED Password
    new_user = User(
        email=email,
        hashed_password=get_password_hash(password),
        full_name=full_name
    )
    session.add(new_user)
    session.commit()
    session.refresh(new_user)

    return {"message": "User created successfully", "email": new_user.email}


# The security card reader

# 1. Define the scheme
# This tells FastAPI: "The client must send a Bearer Token."
# "tokenUrl" tells Swagger UI where to send the user's password to GET that token.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# 2. The Dependency Function
async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    session:  SessionDep
) -> User:
    """
    The Gatekeeper.
    1. Grabs the token from Authorization Header.
    2. Decodes it using the SECRET_KEY.
    3. Checks if the user actually exists in the DB.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        # A. Decode the Token  (The Wax Seal Check)
        payload = jwt.decode(token, settings.SECRET_KEY,
                algorithms=[settings.ALGORITHM])
        
        # B. Extract the Email (The Subject)
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        # If the signature is broken or token is expired, this error fires.
        raise credentials_exception
    
    # C. Verify User Existence
    # Just because the token is valid doesn't mean the user wasn't banned 5 minutes ago.
    # We always double check with the database.
    statement = select(User).where(User.email==email)
    user = session.exec(statement)

    if user is None:
        raise credentials_exception
    
    return user
