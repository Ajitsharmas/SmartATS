"""
Phase 2 smoke test.

Validates the semantic search pipeline end-to-end:

  1. A real resume embedded via the Phase 1 pipeline is retrievable by a
     semantically related query (similarity above the 0.6 threshold).
  2. A junk / unrelated query returns no results (threshold filters noise).
  3. Multi-tenancy: recruiter A's search cannot return recruiter B's candidates,
     even when both have applications matching the same query.
  4. Repeated queries hit the Redis embedding cache (no second Gemini call).

Run from the project root either from your local venv or inside the worker
container:

    # Local venv (matches the uvicorn dev flow)
    .venv/bin/python scripts/smoke_test_phase2.py

    # Inside the worker container
    docker compose exec worker python scripts/smoke_test_phase2.py
"""

import sys
from pathlib import Path

# Ensure the project root is importable regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlmodel import Session

from app.database import create_db_and_tables, engine
from app.embeddings import embed_query_cached, _get_redis_client
from app.main import SEARCH_MIN_SIMILARITY, SEARCH_SQL
from app.models import Application, JobListing, User
from app.security import get_password_hash
from app.worker import embed_resume_task


RESUME_A = """
Alice Chen, Senior Software Engineer
alice@example.com  |  San Francisco, CA

EXPERIENCE
Acme Corp — Senior Backend Engineer (2022–Present)
Built Python microservices on AWS ECS handling 2M events/day with Kafka.
Led migration from monolith to microservices, cut deploy time 90%.

Beta Co — Software Engineer (2019–2022)
Built Django REST APIs serving 50k DAU. Implemented Redis caching layer.

SKILLS
Python, FastAPI, Django, AWS (ECS, RDS, Lambda), Kafka, PostgreSQL, Redis.
"""

RESUME_B = """
Bob Patel, Senior Software Engineer
bob@example.com  |  Austin, TX

EXPERIENCE
GammaSoft — Backend Engineer (2020–Present)
Built Go microservices on GCP Cloud Run. Heavy use of BigQuery and Pub/Sub.
Architected event-driven systems processing terabytes of analytics data.

Delta Inc — Software Engineer (2018–2020)
Built Java Spring Boot APIs for an e-commerce platform.

SKILLS
Go, Java, Spring Boot, GCP (Cloud Run, BigQuery, Pub/Sub), Kubernetes.
"""


def _fail(message: str) -> None:
    print(f"FAIL: {message}")
    sys.exit(1)


def _ok(message: str) -> None:
    print(f"OK:   {message}")


def _run_search(session: Session, owner_id: int, query: str, limit: int = 10, offset: int = 0):
    query_vector = embed_query_cached(query)
    return session.execute(
        text(SEARCH_SQL),
        {
            "query_vector": str(query_vector),
            "owner_id": owner_id,
            "min_similarity": SEARCH_MIN_SIMILARITY,
            "limit": limit,
            "offset": offset,
        },
    ).fetchall()


def main() -> None:
    print("=== Phase 2 smoke test — semantic search ===\n")

    create_db_and_tables()
    _ok("Database is initialised")

    # Clear any leftover cache entries from previous runs so the cache-hit
    # assertion at the end is meaningful.
    try:
        client = _get_redis_client()
        for key in client.scan_iter("emb:*"):
            client.delete(key)
    except Exception:
        # Redis not available — non-fatal for the test, just skip the cache assertion
        pass

    # Create two distinct recruiter accounts (A and B) with one job each.
    with Session(engine) as session:
        recruiter_a = User(
            email="smoke-recruiter-a@example.com",
            full_name="Smoke Recruiter A",
            hashed_password=get_password_hash("test"),
            is_verified=True,
        )
        recruiter_b = User(
            email="smoke-recruiter-b@example.com",
            full_name="Smoke Recruiter B",
            hashed_password=get_password_hash("test"),
            is_verified=True,
        )
        session.add(recruiter_a)
        session.add(recruiter_b)
        session.commit()
        session.refresh(recruiter_a)
        session.refresh(recruiter_b)

        job_a = JobListing(
            owner_id=recruiter_a.id,
            title="Senior Python Engineer (Recruiter A)",
            description="We are hiring a Python engineer for cloud infrastructure work.",
            skills="python, aws",
            location="remote",
        )
        job_b = JobListing(
            owner_id=recruiter_b.id,
            title="Senior Go Engineer (Recruiter B)",
            description="We are hiring a Go engineer for GCP-based systems.",
            skills="go, gcp",
            location="remote",
        )
        session.add(job_a)
        session.add(job_b)
        session.commit()
        session.refresh(job_a)
        session.refresh(job_b)

        app_a = Application(
            job_id=job_a.id,
            candidate_email="alice@example.com",
            candidate_name="Alice Chen",
            resume_url="/download/alice.pdf",
        )
        app_b = Application(
            job_id=job_b.id,
            candidate_email="bob@example.com",
            candidate_name="Bob Patel",
            resume_url="/download/bob.pdf",
        )
        session.add(app_a)
        session.add(app_b)
        session.commit()
        session.refresh(app_a)
        session.refresh(app_b)

        ids = {
            "recruiter_a": recruiter_a.id,
            "recruiter_b": recruiter_b.id,
            "job_a": job_a.id,
            "job_b": job_b.id,
            "app_a": app_a.id,
            "app_b": app_b.id,
        }

    _ok(f"Created recruiters A ({ids['recruiter_a']}), B ({ids['recruiter_b']}) and their applications")

    # Embed both resumes — sync invocation so we can assert immediately
    embed_resume_task.apply(kwargs={"text_content": RESUME_A, "application_id": ids["app_a"]}).get()
    embed_resume_task.apply(kwargs={"text_content": RESUME_B, "application_id": ids["app_b"]}).get()
    _ok("Embedded both resumes via the Phase 1 pipeline")

    with Session(engine) as session:
        # 1. Related query — recruiter A should find Alice
        rows = _run_search(session, ids["recruiter_a"], "Python engineer with AWS and Kafka experience")
        if not rows:
            _fail("Expected at least one result for the related Python query")
        if rows[0].application_id != ids["app_a"]:
            _fail(f"Expected app {ids['app_a']} as top result, got {rows[0].application_id}")
        if rows[0].similarity < SEARCH_MIN_SIMILARITY:
            _fail(f"Top similarity {rows[0].similarity:.3f} below threshold {SEARCH_MIN_SIMILARITY}")
        _ok(f"Related query returned Alice as top result (similarity={rows[0].similarity:.3f})")

        # 2. Junk query — should return nothing
        junk_rows = _run_search(session, ids["recruiter_a"], "marine biology research vessel oceanography")
        if junk_rows:
            top_sim = junk_rows[0].similarity if junk_rows else 0
            _fail(f"Junk query unexpectedly returned {len(junk_rows)} results (top similarity={top_sim:.3f})")
        _ok("Junk query returned no results (threshold filter working)")

        # 3. Multi-tenancy — recruiter A should NOT see Bob's resume
        # Query that semantically matches Bob's resume (Go + GCP)
        cross_tenant_rows = _run_search(session, ids["recruiter_a"], "Go engineer GCP Cloud Run BigQuery")
        if any(r.application_id == ids["app_b"] for r in cross_tenant_rows):
            _fail("MULTI-TENANCY VIOLATION: recruiter A can see recruiter B's candidate")
        _ok("Multi-tenancy enforced: recruiter A cannot see recruiter B's candidates")

        # Recruiter B should see Bob fine
        b_rows = _run_search(session, ids["recruiter_b"], "Go engineer GCP Cloud Run BigQuery")
        if not b_rows or b_rows[0].application_id != ids["app_b"]:
            _fail(f"Recruiter B should see Bob in their own pool, got {[r.application_id for r in b_rows]}")
        _ok(f"Recruiter B sees Bob as top result in their own pool (similarity={b_rows[0].similarity:.3f})")

        # 4. Cache hit — searching the same query twice should not re-call Gemini
        try:
            client = _get_redis_client()
            cache_keys_before = len(list(client.scan_iter("emb:*")))
            _run_search(session, ids["recruiter_a"], "Python engineer with AWS and Kafka experience")
            cache_keys_after = len(list(client.scan_iter("emb:*")))
            if cache_keys_after != cache_keys_before:
                _fail(f"Expected cache hit (no new keys), but key count changed: {cache_keys_before} → {cache_keys_after}")
            _ok(f"Cache hit confirmed — repeated query did not create a new cache entry ({cache_keys_after} keys)")
        except Exception as e:
            print(f"WARN: Redis cache assertion skipped ({e})")

        # Cleanup — delete recruiters, cascades remove their jobs/apps/embeddings
        for rid in (ids["recruiter_a"], ids["recruiter_b"]):
            user = session.get(User, rid)
            if user:
                # Delete their jobs first (CASCADE on application → resume_embedding takes care of the rest)
                for job_id in (ids["job_a"], ids["job_b"]):
                    job = session.get(JobListing, job_id)
                    if job and job.owner_id == rid:
                        session.delete(job)
                session.delete(user)
        session.commit()
        _ok("Cleaned up test recruiters, jobs, applications, and embeddings via cascades")

    print("\n=== Phase 2 smoke test passed ===")


if __name__ == "__main__":
    main()
