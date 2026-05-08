# ---------------------------------------------------------------------------------------------------------------------------
# Purpose: Database Connection and Session Management
# ---------------------------------------------------------------------------------------------------------------------------

from sqlmodel import SQLModel, create_engine, Session
from typing import Generator

# 1. The Connection String
# In a real ap, we should use a .env file. For now, we hardcode Docker defaults.
# Format: postgresql;//<user>:<password>@<host>?:<port>/<db_name>
DATABASE_URL = "postgresql://resume_user:resume_pass@localhost:5432/resume_db"

# 2. The Engine
# The Engine is the factory that creates the connections
engine = create_engine(DATABASE_URL, echo=True)

# 3. The Dependency
# This function yields a session for each request and closes it afterwards
def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session

# 4. Initialization
# This creates all the tables defined in our models.
def create_db_and_tables():
    SQLModel.metadata.create_all(engine)