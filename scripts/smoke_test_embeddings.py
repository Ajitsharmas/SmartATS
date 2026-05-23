"""
Phase 0 smoke test.

Validates the full embedding round-trip:
  1. Gemini's gemini-embedding-001 returns a 768-dim vector for a piece of
     text (we explicitly request output_dimensionality=768 in app/embeddings.py
     — the model defaults to 3072).
  2. pgvector stores it correctly via the SQLModel ResumeEmbedding class.
  3. HNSW similarity search returns the inserted row when queried with a
     related phrase, with similarity above a sensible threshold.

Run from the project root either from your local venv or inside the worker
container:

    # Local venv (matches the uvicorn dev flow)
    .venv/bin/python scripts/smoke_test_embeddings.py

    # Inside the worker container
    docker compose exec worker python scripts/smoke_test_embeddings.py

Cleans up its own test rows on success.
"""

import sys
from pathlib import Path

# Ensure the project root is importable so `app.*` modules can be found
# regardless of where the script is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlmodel import Session, select

from app.database import create_db_and_tables, engine
from app.embeddings import embed_text
from app.models import Application, JobListing, ResumeEmbedding


SIMILARITY_THRESHOLD = 0.7


def _fail(message: str) -> None:
    print(f"FAIL: {message}")
    sys.exit(1)


def _ok(message: str) -> None:
    print(f"OK:   {message}")


def main() -> None:
    print("=== Phase 0 smoke test — embeddings round-trip ===\n")

    # Ensure infrastructure is set up (extension, tables, HNSW indexes).
    create_db_and_tables()
    _ok("Database is initialised (vector extension + tables + HNSW indexes)")

    # 1. Generate an embedding for a known phrase.
    source_text = "Senior Python Developer with 5 years of AWS experience"
    vector = embed_text(source_text)

    if not isinstance(vector, list) or len(vector) != 768:
        _fail(f"Expected list[float] of length 768, got {type(vector).__name__} of length {len(vector) if hasattr(vector, '__len__') else '?'}")
    _ok(f"Embedding generated — 768-dim vector for: {source_text!r}")

    # 2. Insert a temporary test row.
    # ResumeEmbedding requires an application_id with a FK to Application,
    # which itself requires a job_id with a FK to JobListing.
    # We create throwaway parent rows for the test, then clean up at the end.
    with Session(engine) as session:
        test_job = JobListing(
            title="Smoke Test Role",
            description="Temporary row created by the embeddings smoke test.",
            skills="test",
            location="test",
        )
        session.add(test_job)
        session.commit()
        session.refresh(test_job)

        test_app = Application(
            job_id=test_job.id,
            candidate_email="smoketest@example.com",
            candidate_name="Smoke Test",
            resume_url="/download/smoketest.pdf",
        )
        session.add(test_app)
        session.commit()
        session.refresh(test_app)

        test_embedding = ResumeEmbedding(
            application_id=test_app.id,
            chunk_index=0,
            chunk_text=source_text,
            embedding=vector,
        )
        session.add(test_embedding)
        session.commit()
        session.refresh(test_embedding)
        _ok(f"Test row inserted (resume_embedding id={test_embedding.id})")

        # 3. Query for the nearest neighbour to a semantically related phrase.
        query_text = "Lead Python Engineer with cloud experience"
        query_vector = embed_text(query_text)

        # Raw SQL because SQLModel cannot express the <=> operator.
        # 1 - cosine_distance = cosine similarity.
        result = session.execute(
            text("""
                SELECT id, 1 - (embedding <=> CAST(:qv AS vector)) AS similarity
                FROM resumeembedding
                WHERE application_id = :app_id
                ORDER BY embedding <=> CAST(:qv AS vector)
                LIMIT 1
            """),
            {"qv": str(query_vector), "app_id": test_app.id},
        ).fetchone()

        if result is None:
            _fail("Similarity query returned no rows")

        returned_id, similarity = result
        if returned_id != test_embedding.id:
            _fail(f"Expected id={test_embedding.id}, got id={returned_id}")
        if similarity < SIMILARITY_THRESHOLD:
            _fail(f"Similarity {similarity:.3f} below threshold {SIMILARITY_THRESHOLD}")
        _ok(f"Similarity search returned the inserted row with similarity {similarity:.3f}")

        # 4. Cleanup. Deleting the test job cascades to the application and
        # the resume embedding via the FK CASCADE constraints, so one delete
        # is enough.
        session.delete(test_job)
        session.commit()
        _ok("Test rows cleaned up")

    print("\n=== Smoke test passed ===")


if __name__ == "__main__":
    main()
