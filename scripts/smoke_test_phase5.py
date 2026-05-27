"""
Phase 5 smoke test — LLM re-ranking for search and cross-job matching.

Validates the two-stage retrieve-then-rerank pipeline introduced in Phase 5.
The core claim is that the LLM rerank correctly punishes language/framework
mismatches that pure vector similarity inflates (e.g. a Python resume vs a
Java job, which used to score 75% under bidirectional cosine alone).

Assertions:
  1. LLM rerank scores a Python resume against a Java job below 50.
  2. LLM rerank scores the same Python resume against a Python job above 60.
  3. match_jobs_task either drops the Java job entirely or only surfaces it
     under MATCH_LLM_MIN_SCORE — the prior bug was a 75% match.
  4. The rerank Redis cache fills after the first call and is reused on the
     second identical call (cache hit returns the same RerankResult instance
     value without invoking the LLM again).
  5. When the LLM client is mocked to fail, rerank_parallel returns all-None
     and the caller can branch to the degraded fallback path.

Run from the project root either from your local venv or inside the worker
container:

    .venv/bin/python scripts/smoke_test_phase5.py
    docker compose exec worker python scripts/smoke_test_phase5.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select

from app import rerank as rerank_module
from app.config import settings
from app.database import create_db_and_tables, engine
from app.models import Application, CrossJobMatch, JobListing, User
from app.rerank import (
    RerankError,
    _cache_key,
    _get_redis_client,
    clear_application_rerank_cache,
    rerank_parallel,
    rerank_sequential,
)
from app.security import get_password_hash
from app.worker import (
    MATCH_LLM_MIN_SCORE,
    _build_top_resume_chunks,
    embed_job_task,
    embed_resume_task,
    match_jobs_task,
)


JOB_PYTHON_DESC = """
We are hiring a Senior Backend Engineer to build Python services with FastAPI.
You will design REST APIs, manage Postgres schemas with SQLAlchemy, run
Celery workers for background tasks, and deploy to AWS. Strong Python
fundamentals, asyncio, pytest, and FastAPI experience are required.
"""

JOB_JAVA_DESC = """
We are hiring a Senior Backend Engineer to build Java services with Spring Boot.
You will design REST APIs, manage Postgres schemas with Hibernate JPA, run
Kafka consumers for background tasks, and deploy to AWS. Strong Java
fundamentals, JVM tuning, JUnit, and Spring Boot experience are required.
Familiarity with Maven and Gradle is essential.
"""

RESUME_PYTHON = """
Jordan Park, Senior Backend Engineer
jordan@example.com  |  Austin, TX

EXPERIENCE
Acme Corp — Senior Python Engineer (2021–Present)
Designed and built FastAPI services serving 200K daily requests. Wrote
SQLAlchemy models against Postgres, ran Celery workers on Redis for async
work, and deployed to AWS ECS via Docker. Drove the migration from Flask
to FastAPI across 14 microservices. Wrote pytest fixtures and ran integration
suites against ephemeral Postgres containers.

Beta Industries — Python Engineer (2018–2021)
Built Django REST APIs. Heavy asyncio usage. Owned the data-pipeline
infrastructure end to end.

SKILLS
Python, FastAPI, asyncio, SQLAlchemy, Celery, Redis, Postgres, pytest,
Docker, AWS ECS. No JVM experience.
"""


def _fail(message: str) -> None:
    print(f"FAIL: {message}")
    sys.exit(1)


def _ok(message: str) -> None:
    print(f"OK:   {message}")


def main() -> None:
    print("=== Phase 5 smoke test — LLM rerank ===\n")

    create_db_and_tables()
    _ok("Database is initialised")

    # --- Setup: recruiter + two jobs + one Python-leaning application ---
    with Session(engine) as session:
        recruiter = User(
            email="smoke-p5@example.com",
            full_name="Smoke P5 Recruiter",
            hashed_password=get_password_hash("test"),
            is_verified=True,
        )
        session.add(recruiter)
        session.commit()
        session.refresh(recruiter)

        job_python = JobListing(
            owner_id=recruiter.id,
            title="Senior Backend Engineer (Python / FastAPI)",
            description=JOB_PYTHON_DESC,
            skills="python, fastapi, sqlalchemy, celery, postgres",
            location="remote",
        )
        job_java = JobListing(
            owner_id=recruiter.id,
            title="Senior Backend Engineer (Java / Spring Boot)",
            description=JOB_JAVA_DESC,
            skills="java, spring boot, hibernate, kafka, maven",
            location="remote",
        )
        session.add(job_python)
        session.add(job_java)
        session.commit()
        session.refresh(job_python)
        session.refresh(job_java)

        application = Application(
            job_id=job_python.id,
            candidate_email="jordan@example.com",
            candidate_name="Jordan Park",
            resume_url="/download/jordan.pdf",
        )
        session.add(application)
        session.commit()
        session.refresh(application)

        ids = {
            "recruiter": recruiter.id,
            "job_python": job_python.id,
            "job_java": job_java.id,
            "app": application.id,
        }
    _ok(f"Created recruiter, two jobs (Python id={ids['job_python']}, Java id={ids['job_java']}), application (id={ids['app']})")

    # Pre-clear any leftover rerank cache from prior runs of this smoke test
    clear_application_rerank_cache(ids["app"])

    embed_job_task.apply(kwargs={"job_id": ids["job_python"]}).get()
    embed_job_task.apply(kwargs={"job_id": ids["job_java"]}).get()
    embed_resume_task.apply(
        kwargs={"text_content": RESUME_PYTHON, "application_id": ids["app"]}
    ).get()
    _ok("Embedded both jobs and the Python resume")

    # ---- Test 1: direct LLM rerank — Python resume vs Java job ----
    java_job_text = (
        f"Title: Senior Backend Engineer (Java / Spring Boot)\n"
        f"Description: {JOB_JAVA_DESC}\n"
        f"Required skills: java, spring boot, hibernate, kafka, maven\n"
        f"Location: remote"
    )
    python_job_text = (
        f"Title: Senior Backend Engineer (Python / FastAPI)\n"
        f"Description: {JOB_PYTHON_DESC}\n"
        f"Required skills: python, fastapi, sqlalchemy, celery, postgres\n"
        f"Location: remote"
    )

    results = rerank_sequential([
        (ids["app"], java_job_text, RESUME_PYTHON),
        (ids["app"], python_job_text, RESUME_PYTHON),
    ])
    java_result, python_result = results
    if java_result is None or python_result is None:
        _fail(f"LLM rerank returned None — Gemini may be unreachable: {results}")

    if java_result.score >= 50:
        _fail(
            f"Java job scored {java_result.score}/100 against a Python resume — "
            f"this is the bug Phase 5 is meant to fix. Critique: {java_result.critique!r}"
        )
    _ok(f"Python resume vs Java job scored {java_result.score}/100 (< 50, as required). Critique: {java_result.critique[:120]}…")

    if python_result.score < 60:
        _fail(
            f"Python job scored only {python_result.score}/100 against a strongly Python resume "
            f"— the LLM is being too strict. Critique: {python_result.critique!r}"
        )
    _ok(f"Python resume vs Python job scored {python_result.score}/100 (>= 60, as required)")

    # ---- Test 2: redis cache fills and is reused ----
    redis_client = _get_redis_client()
    cache_key = _cache_key(ids["app"], java_job_text, RESUME_PYTHON)
    if not redis_client.exists(cache_key):
        _fail(f"Expected Redis cache entry at {cache_key} after rerank, found none")
    _ok(f"Redis rerank cache key present after first call: {cache_key}")

    # Force a second call and verify it does not change the cached value
    second = rerank_sequential([(ids["app"], java_job_text, RESUME_PYTHON)])
    if second[0] is None:
        _fail("Second rerank call returned None unexpectedly")
    if second[0].score != java_result.score or second[0].critique != java_result.critique:
        _fail(
            "Second rerank call returned a different result — cache was not hit. "
            f"First: ({java_result.score}, {java_result.critique!r}); "
            f"Second: ({second[0].score}, {second[0].critique!r})"
        )
    _ok("Second identical rerank call returns the cached result")

    # ---- Test 2b (Phase 5.2): _build_top_resume_chunks returns trimmed text ----
    # The test resume is short (< 10 chunks total) so K=8 plus chunk 0 will
    # often include nearly all chunks anyway. The assertion is just that the
    # helper runs, returns non-empty text, and stays at or below
    # K + 1 chunks worth — guards against a regression that accidentally
    # concatenates every chunk back together.
    with Session(engine) as session:
        trimmed = _build_top_resume_chunks(
            session,
            ids["app"],
            ids["job_java"],
            settings.RERANK_RESUME_CHUNK_TOP_K,
        )
        if not trimmed:
            _fail("_build_top_resume_chunks returned empty text")
        # Approximate ceiling: K chunks + chunk 0 + newlines, each chunk ~600 chars
        ceiling = (settings.RERANK_RESUME_CHUNK_TOP_K + 1) * (settings.RESUME_CHUNK_SIZE + settings.RESUME_CHUNK_OVERLAP + 10)
        if len(trimmed) > ceiling:
            _fail(
                f"_build_top_resume_chunks returned {len(trimmed)} chars, above the "
                f"K+1 chunk ceiling of {ceiling} — chunk-trim regression?"
            )
        _ok(f"_build_top_resume_chunks returned {len(trimmed)} chars (≤ {ceiling} ceiling for K={settings.RERANK_RESUME_CHUNK_TOP_K})")

    # ---- Test 3: match_jobs_task no longer surfaces the Java job ----
    result = match_jobs_task.apply(kwargs={"application_id": ids["app"]}).get()
    if result.get("status") != "success":
        _fail(f"match_jobs_task returned non-success: {result!r}")

    with Session(engine) as session:
        matches = session.exec(
            select(CrossJobMatch).where(CrossJobMatch.application_id == ids["app"])
        ).all()
        matched_job_ids = {m.matched_job_id for m in matches}
        if ids["job_python"] in matched_job_ids:
            _fail("Self-match violation: the candidate's own applied job appeared in matches")

        java_match = next((m for m in matches if m.matched_job_id == ids["job_java"]), None)
        if java_match is not None:
            # The Java job survived. Only acceptable if its similarity is below
            # the LLM threshold (which shouldn't normally happen because the
            # task filters before persisting — but accept low scores as a guard).
            score_pct = int(java_match.similarity * 100)
            if score_pct >= MATCH_LLM_MIN_SCORE:
                _fail(
                    f"Phase 5 regression: Java job persisted as a match with score "
                    f"{score_pct}/100 against the Python resume (threshold is "
                    f"{MATCH_LLM_MIN_SCORE}). Critique: {java_match.critique!r}"
                )
            _ok(f"Java job present but only at score {score_pct}/100 (< {MATCH_LLM_MIN_SCORE})")
        else:
            _ok("Java job correctly excluded from cross-job matches for a Python candidate")

    # ---- Test 4: degraded mode — mock the LLM to fail ----
    class _FailingLLM:
        def invoke(self, _prompt: str):
            raise RuntimeError("simulated Gemini outage")

    original_get_llm = rerank_module._get_llm
    # _get_llm is @lru_cache — clearing first so the next call returns our stub.
    rerank_module._get_llm.cache_clear()
    rerank_module._get_llm = lambda: _FailingLLM()  # type: ignore[assignment]

    try:
        # Use a fresh query so we don't hit the cache populated by Test 1
        fresh_query = "Senior Rust systems engineer with Tokio experience"
        # Bypass the cache by clearing first
        clear_application_rerank_cache(ids["app"])

        async def _run() -> list:
            return await rerank_parallel([
                (ids["app"], fresh_query, RESUME_PYTHON),
                (ids["app"], fresh_query + " variant", RESUME_PYTHON),
            ])

        results = asyncio.run(_run())
        if not all(r is None for r in results):
            _fail(f"Expected all rerank results to be None under simulated LLM outage, got {results}")
        _ok("rerank_parallel returns all-None under simulated Gemini outage (degraded path)")

        # Verify RerankError propagates internally too
        try:
            rerank_module._score_one(ids["app"], fresh_query + " sync", RESUME_PYTHON)
            _fail("_score_one did not raise RerankError under simulated LLM outage")
        except RerankError:
            pass
        _ok("_score_one raises RerankError under simulated LLM outage (callers can branch on it)")
    finally:
        rerank_module._get_llm = original_get_llm  # type: ignore[assignment]
        rerank_module._get_llm.cache_clear()

    # ---- Cleanup ----
    clear_application_rerank_cache(ids["app"])
    with Session(engine) as session:
        for job_id in (ids["job_python"], ids["job_java"]):
            j = session.get(JobListing, job_id)
            if j:
                session.delete(j)
        u = session.get(User, ids["recruiter"])
        if u:
            session.delete(u)
        session.commit()
    _ok("Cleaned up recruiter, jobs, applications, embeddings, matches, and rerank cache")

    print("\n=== Phase 5 smoke test passed ===")


if __name__ == "__main__":
    main()
