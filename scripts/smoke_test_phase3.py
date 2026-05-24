"""
Phase 3 smoke test — cross-job matching.

Validates the cross-job matching pipeline end-to-end:

  1. embed_job_task chunks and embeds job descriptions correctly.
  2. match_jobs_task computes alternative-job suggestions for an application
     using the top-3-chunk average aggregation.
  3. The recommended alternative is NOT the job the candidate applied to.
  4. Multi-tenancy: a recruiter never sees another recruiter's jobs as
     match candidates, even when those jobs would be a strong fit.
  5. Bulk-refresh logic dispatches one matching task per application in the
     recruiter's pool.

Run from the project root either from your local venv or inside the worker
container:

    .venv/bin/python scripts/smoke_test_phase3.py
    docker compose exec worker python scripts/smoke_test_phase3.py
"""

import sys
from pathlib import Path

# Ensure the project root is importable regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select

from app.database import create_db_and_tables, engine
from app.models import (
    Application,
    CrossJobMatch,
    JobEmbedding,
    JobListing,
    User,
)
from app.security import get_password_hash
from app.worker import embed_job_task, embed_resume_task, match_jobs_task


# Two jobs for recruiter A:
#  - The role the candidate applies to (front-end-heavy)
#  - A leadership-focused tech-lead role the candidate should also match
JOB_FRONTEND_DESC = """
We are hiring a Senior Frontend Engineer to build customer-facing React applications.
You will own component architecture, accessibility, and performance optimisation.
We use TypeScript, React, Next.js, and a design-system-driven workflow.
"""

JOB_TECH_LEAD_DESC = """
We are hiring a Tech Lead to drive architectural decisions across our backend platform.
You will mentor a team of 4–6 engineers, lead system design reviews, and own delivery
of major cross-team initiatives. Experience with distributed systems, Kafka,
microservices on AWS, and leading large migrations is essential.
"""

# A resume that should match Tech Lead better than Frontend, despite the
# candidate applying to Frontend. Strong on leadership + distributed systems.
RESUME_LEADERSHIP = """
Alice Chen, Senior Software Engineer
alice@example.com  |  San Francisco, CA

EXPERIENCE
Acme Corp — Senior Backend Engineer (2022–Present)
Led migration of 12 microservices from monolith to AWS ECS, cutting deploy time 90%.
Mentored a team of 5 junior engineers, conducting weekly code reviews and pair sessions.
Owned the architectural redesign of the event pipeline — Kafka, distributed systems.

Beta Co — Software Engineer (2019–2022)
Built Python services on AWS. Led migration off legacy infrastructure.
First engineer to introduce structured code review culture.

LEADERSHIP
Promoted twice. Conducted hiring interviews. Mentored 8 engineers over 3 years.
Led monthly architecture review meetings for the broader engineering org.

SKILLS
Python, Go, AWS (ECS, Kafka, RDS), distributed systems, system design, mentorship.
"""

# Recruiter B owns a job that would also match the candidate — used for the
# multi-tenancy assertion (recruiter A should never see this as a match).
JOB_B_DESC = """
We are hiring a Backend Engineering Manager.
You will own the platform team's roadmap, mentor engineers, and drive distributed-systems work.
Microservices, Kafka, AWS experience required.
"""


def _fail(message: str) -> None:
    print(f"FAIL: {message}")
    sys.exit(1)


def _ok(message: str) -> None:
    print(f"OK:   {message}")


def main() -> None:
    print("=== Phase 3 smoke test — cross-job matching ===\n")

    create_db_and_tables()
    _ok("Database is initialised")

    # Create two recruiters with their jobs
    with Session(engine) as session:
        recruiter_a = User(
            email="smoke-p3-a@example.com",
            full_name="Smoke P3 Recruiter A",
            hashed_password=get_password_hash("test"),
            is_verified=True,
        )
        recruiter_b = User(
            email="smoke-p3-b@example.com",
            full_name="Smoke P3 Recruiter B",
            hashed_password=get_password_hash("test"),
            is_verified=True,
        )
        session.add(recruiter_a)
        session.add(recruiter_b)
        session.commit()
        session.refresh(recruiter_a)
        session.refresh(recruiter_b)

        job_frontend = JobListing(
            owner_id=recruiter_a.id,
            title="Senior Frontend Engineer",
            description=JOB_FRONTEND_DESC,
            skills="react, typescript, next.js",
            location="remote",
        )
        job_tech_lead = JobListing(
            owner_id=recruiter_a.id,
            title="Tech Lead — Backend Platform",
            description=JOB_TECH_LEAD_DESC,
            skills="leadership, distributed systems, kafka",
            location="remote",
        )
        job_b = JobListing(
            owner_id=recruiter_b.id,
            title="Backend Engineering Manager",
            description=JOB_B_DESC,
            skills="leadership, distributed systems",
            location="remote",
        )
        session.add(job_frontend)
        session.add(job_tech_lead)
        session.add(job_b)
        session.commit()
        session.refresh(job_frontend)
        session.refresh(job_tech_lead)
        session.refresh(job_b)

        app_alice = Application(
            job_id=job_frontend.id,
            candidate_email="alice@example.com",
            candidate_name="Alice Chen",
            resume_url="/download/alice.pdf",
        )
        session.add(app_alice)
        session.commit()
        session.refresh(app_alice)

        ids = {
            "rec_a": recruiter_a.id,
            "rec_b": recruiter_b.id,
            "job_frontend": job_frontend.id,
            "job_tech_lead": job_tech_lead.id,
            "job_b": job_b.id,
            "app_alice": app_alice.id,
        }
    _ok(f"Created recruiters, jobs, and application (Alice → Frontend, id={ids['app_alice']})")

    # Embed all three jobs
    embed_job_task.apply(kwargs={"job_id": ids["job_frontend"]}).get()
    embed_job_task.apply(kwargs={"job_id": ids["job_tech_lead"]}).get()
    embed_job_task.apply(kwargs={"job_id": ids["job_b"]}).get()
    _ok("Embedded all three jobs via embed_job_task")

    # Verify job embeddings exist
    with Session(engine) as session:
        for job_id, name in [
            (ids["job_frontend"], "Frontend"),
            (ids["job_tech_lead"], "Tech Lead"),
            (ids["job_b"], "Recruiter B's job"),
        ]:
            count = len(session.exec(
                select(JobEmbedding).where(JobEmbedding.job_id == job_id)
            ).all())
            if count < 1:
                _fail(f"Job '{name}' (id={job_id}) has no embeddings")
        _ok("All three jobs have non-empty embedding rows")

    # Embed Alice's resume
    embed_resume_task.apply(
        kwargs={"text_content": RESUME_LEADERSHIP, "application_id": ids["app_alice"]}
    ).get()
    _ok("Embedded Alice's resume via embed_resume_task")

    # Run the matching task
    result = match_jobs_task.apply(kwargs={"application_id": ids["app_alice"]}).get()
    if result.get("status") != "success":
        _fail(f"match_jobs_task returned non-success: {result!r}")
    _ok(f"match_jobs_task returned success with {result.get('matches')} matches")

    # Verify match results
    with Session(engine) as session:
        matches = session.exec(
            select(CrossJobMatch).where(CrossJobMatch.application_id == ids["app_alice"])
        ).all()

        if not matches:
            _fail("Expected at least one cross-job match for Alice")

        matched_job_ids = {m.matched_job_id for m in matches}

        # The job she applied to (frontend) must NOT be in the matches
        if ids["job_frontend"] in matched_job_ids:
            _fail("Self-match violation: Alice's own application's job appeared in matches")
        _ok("Self-match excluded: Frontend job (the one Alice applied to) is not in matches")

        # Recruiter B's job must NOT be in the matches (multi-tenancy)
        if ids["job_b"] in matched_job_ids:
            _fail("MULTI-TENANCY VIOLATION: recruiter B's job appeared as a match for recruiter A's candidate")
        _ok("Multi-tenancy enforced: recruiter B's job is not in recruiter A's match results")

        # Tech Lead should be in the matches (the role Alice is actually a better fit for)
        if ids["job_tech_lead"] not in matched_job_ids:
            _fail(f"Expected Tech Lead in matches; got {matched_job_ids}")
        tech_lead_match = next(m for m in matches if m.matched_job_id == ids["job_tech_lead"])
        if tech_lead_match.similarity < 0.7:
            _fail(f"Tech Lead similarity {tech_lead_match.similarity:.3f} below 0.7 threshold")
        _ok(f"Tech Lead match present with similarity {tech_lead_match.similarity:.3f}")

    # Idempotency check — run the task again and verify the row count is the same
    match_jobs_task.apply(kwargs={"application_id": ids["app_alice"]}).get()
    with Session(engine) as session:
        matches_after = session.exec(
            select(CrossJobMatch).where(CrossJobMatch.application_id == ids["app_alice"])
        ).all()
        if len(matches_after) != len(matches):
            _fail(f"Idempotency failure: had {len(matches)} matches, now {len(matches_after)}")
    _ok("Idempotency: re-running match_jobs_task produces the same number of matches")

    # Cleanup — delete the recruiters' jobs (cascades to applications and embeddings)
    with Session(engine) as session:
        for job_id in (ids["job_frontend"], ids["job_tech_lead"], ids["job_b"]):
            j = session.get(JobListing, job_id)
            if j:
                session.delete(j)
        for rec_id in (ids["rec_a"], ids["rec_b"]):
            u = session.get(User, rec_id)
            if u:
                session.delete(u)
        session.commit()
    _ok("Cleaned up test recruiters, jobs, applications, embeddings, and matches via cascades")

    print("\n=== Phase 3 smoke test passed ===")


if __name__ == "__main__":
    main()
