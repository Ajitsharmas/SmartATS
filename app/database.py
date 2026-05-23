# ---------------------------------------------------------------------------------------------------------------------------
# Purpose: Database Connection, Session Management, and Vector Search Setup
# ---------------------------------------------------------------------------------------------------------------------------

from typing import Generator

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

# 1. The Connection String
# Format: postgresql://<user>:<password>@<host>:<port>/<db_name>
# Read from config system
connection_string = settings.DATABASE_URL

# 2. The Engine
# The Engine is the factory that creates the connections.
# Echo (SQL Logging) is toggled on/off via the DEBUG setting.
engine = create_engine(connection_string, echo=settings.DEBUG)


# 3. The Dependency
# This function yields a session for each request and closes it afterwards.
def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


# 4. Initialization
# Sets up everything the app needs from the database at startup:
#   a. The pgvector extension (idempotent — safe to run every time)
#   b. All tables defined in our SQLModel classes (only creates missing ones)
#   c. HNSW indexes on embedding columns (idempotent via IF NOT EXISTS)
#
# Idempotency means it's safe to call on every startup, whether the DB is
# fresh or already populated.
def create_db_and_tables() -> None:
    # a. Enable pgvector extension
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()

    # b. Create all tables defined by SQLModel metadata
    SQLModel.metadata.create_all(engine)

    # c. Create HNSW indexes on embedding columns.
    # SQLModel cannot express HNSW indexes declaratively, so we drop down to raw SQL.
    # vector_cosine_ops uses the <=> operator (cosine distance) at query time.
    hnsw_statements = [
        """
        CREATE INDEX IF NOT EXISTS resume_embedding_hnsw_idx
        ON resumeembedding
        USING hnsw (embedding vector_cosine_ops)
        """,
        """
        CREATE INDEX IF NOT EXISTS job_embedding_hnsw_idx
        ON jobembedding
        USING hnsw (embedding vector_cosine_ops)
        """,
    ]
    with engine.connect() as conn:
        for stmt in hnsw_statements:
            conn.execute(text(stmt))
        conn.commit()
