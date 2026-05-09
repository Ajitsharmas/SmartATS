# ---------------------------------------------------------------------------------------------------------------------------
# Purpose: Database Connection and Session Management
# ---------------------------------------------------------------------------------------------------------------------------

from sqlmodel import SQLModel, create_engine, Session
from typing import Generator
from app.config import settings

# 1. The Connection String
# Format: postgresql;//<user>:<password>@<host>?:<port>/<db_name>
# Read from config system
connection_string = settings.DATABASE_URL

# 2. The Engine
# The Engine is the factory that creates the connections
# Read echo from config system to toggle the echo (SQL Logging) to ON/OFF
engine = create_engine(connection_string, echo=settings.DEBUG)

# 3. The Dependency
# This function yields a session for each request and closes it afterwards
def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session

# 4. Initialization
# This creates all the tables defined in our models.
def create_db_and_tables():
    SQLModel.metadata.create_all(engine)