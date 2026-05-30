"""
Phase 6 smoke test — recruiter assistant agent + outreach pipeline.

Exercises the parts of Phase 6 that don't require a running HTTP server:
the tools themselves, the contextual cross-match-invite shortcut, and the
outreach draft lifecycle. The SSE endpoint and the LangGraph loop are
validated indirectly via `agent.run_turn_stream` being callable and the
tools producing the right shapes.

Run from project root either via the local venv or inside the worker:

    .venv/bin/python scripts/smoke_test_phase6.py
    docker compose exec worker python scripts/smoke_test_phase6.py
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select

from app import agent as agent_module
from app.agent import (
    TOOLS,
    clear_history,
    set_agent_context,
)
from app.database import create_db_and_tables, engine
from app.models import (
    Application,
    CrossJobMatch,
    JobListing,
    OutreachEmail,
    User,
)
from app.outreach import draft_email_for_application
from app.security import get_password_hash
from app.worker import (
    embed_job_task,
    embed_resume_task,
    match_jobs_task,
)


JOB_PYTHON_DESC = """
We are hiring a Senior Backend Engineer to build Python services with FastAPI.
You will design REST APIs, manage Postgres schemas with SQLAlchemy, and run
Celery workers for background tasks. Strong Python fundamentals, asyncio,
pytest, and FastAPI experience are required.
"""

JOB_TECH_LEAD_DESC = """
We are hiring a Tech Lead to drive architectural decisions across our backend
platform. You will mentor a team of 4-6 engineers, lead system design reviews,
and own delivery of major cross-team initiatives. Experience with distributed
systems, Kafka, microservices on AWS, and leading large migrations is essential.
"""

RESUME_LEADERSHIP = """
Alice Chen, Senior Software Engineer
alice@example.com | San Francisco, CA

EXPERIENCE
Acme Corp - Senior Backend Engineer (2022-Present)
Led migration of 12 microservices from monolith to AWS ECS, cutting deploy time 90%.
Mentored a team of 5 junior engineers, conducting weekly code reviews and pair sessions.
Owned the architectural redesign of the event pipeline - Kafka, distributed systems.

LEADERSHIP
Promoted twice. Conducted hiring interviews. Mentored 8 engineers over 3 years.

SKILLS
Python, Go, AWS (ECS, Kafka, RDS), distributed systems, system design, mentorship.
"""


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def _ok(msg: str) -> None:
    print(f"OK:   {msg}")


def main() -> None:
    print("=== Phase 6 smoke test - agent + outreach ===\n")

    create_db_and_tables()
    _ok("Database initialised")

    # ----- fixtures -----
    with Session(engine) as session:
        recruiter = User(
            email="smoke-p6@example.com",
            full_name="Smoke P6 Recruiter",
            hashed_password=get_password_hash("test"),
            is_verified=True,
        )
        session.add(recruiter)
        session.commit()
        session.refresh(recruiter)

        job_python = JobListing(
            owner_id=recruiter.id,
            title="Senior Backend Engineer (Python)",
            description=JOB_PYTHON_DESC,
            skills="python, fastapi, sqlalchemy, celery",
            location="remote",
        )
        job_tech_lead = JobListing(
            owner_id=recruiter.id,
            title="Tech Lead - Backend Platform",
            description=JOB_TECH_LEAD_DESC,
            skills="leadership, distributed systems, kafka",
            location="remote",
        )
        session.add(job_python)
        session.add(job_tech_lead)
        session.commit()
        session.refresh(job_python)
        session.refresh(job_tech_lead)

        application = Application(
            job_id=job_python.id,
            candidate_email="alice@example.com",
            candidate_name="Alice Chen",
            resume_url="/download/alice.pdf",
            ai_score=85,
            ai_critique="Strong distributed-systems background; clear leadership signal.",
        )
        session.add(application)
        session.commit()
        session.refresh(application)

        ids = {
            "recruiter": recruiter.id,
            "job_python": job_python.id,
            "job_tech_lead": job_tech_lead.id,
            "app_alice": application.id,
        }
    _ok(f"Created recruiter, two jobs, one application (Alice id={ids['app_alice']})")

    # Embed everything so search and cross-match tools have data to work with
    embed_job_task.apply(kwargs={"job_id": ids["job_python"]}).get()
    embed_job_task.apply(kwargs={"job_id": ids["job_tech_lead"]}).get()
    embed_resume_task.apply(
        kwargs={"text_content": RESUME_LEADERSHIP, "application_id": ids["app_alice"]}
    ).get()
    _ok("Embedded both jobs and Alice's resume")

    match_jobs_task.apply(kwargs={"application_id": ids["app_alice"]}).get()
    _ok("Computed cross-job matches for Alice")

    # ----- Test 1: tool palette is non-empty and well-formed -----
    tool_names = {t.name for t in TOOLS}
    expected = {
        "list_jobs", "get_job_details", "get_applicants", "get_candidate",
        "get_cross_matches", "search_candidates", "ask_about_resume",
        "draft_job_description", "improve_job_description",
        "generate_interview_questions", "generate_screening_rubric",
        "draft_email", "list_drafts",
    }
    missing = expected - tool_names
    if missing:
        _fail(f"Tool palette missing expected tools: {missing}")
    _ok(f"Tool palette registers all {len(expected)} tools")

    # ----- Test 2: read-only tools run with the recruiter scope -----
    with Session(engine) as session:
        recruiter_obj = session.get(User, ids["recruiter"])
        set_agent_context(recruiter_obj, session)

        # list_jobs should return both jobs
        result = json.loads(agent_module.list_jobs.invoke({}))
        if len(result) != 2:
            _fail(f"list_jobs returned {len(result)} jobs (expected 2)")
        _ok(f"list_jobs returns {len(result)} jobs for the recruiter")

        # get_candidate should return Alice
        result = json.loads(agent_module.get_candidate.invoke({"application_id": ids["app_alice"]}))
        if "error" in result:
            _fail(f"get_candidate errored: {result['error']}")
        if result.get("candidate_name") != "Alice Chen":
            _fail(f"get_candidate returned wrong candidate: {result}")
        _ok("get_candidate returns Alice with full critique")

        # get_cross_matches should include Tech Lead
        result = json.loads(agent_module.get_cross_matches.invoke({"application_id": ids["app_alice"]}))
        if not isinstance(result, list) or not result:
            _fail(f"get_cross_matches returned no matches: {result}")
        match_ids = {r["matched_job_id"] for r in result}
        if ids["job_tech_lead"] not in match_ids:
            _fail(f"get_cross_matches missing Tech Lead: {match_ids}")
        _ok(f"get_cross_matches surfaces Tech Lead (similarity {result[0]['similarity']:.3f})")

    # ----- Test 3: read-only tool refuses cross-tenant access -----
    # Create a second recruiter and verify the tools can't see the first's data
    with Session(engine) as session:
        recruiter_b = User(
            email="smoke-p6-b@example.com",
            full_name="Smoke P6 Recruiter B",
            hashed_password=get_password_hash("test"),
            is_verified=True,
        )
        session.add(recruiter_b)
        session.commit()
        session.refresh(recruiter_b)

        set_agent_context(recruiter_b, session)
        result = json.loads(agent_module.list_jobs.invoke({}))
        if len(result) != 0:
            _fail(f"Multi-tenancy regression: recruiter B sees recruiter A's jobs ({len(result)} returned)")
        _ok("Multi-tenancy: recruiter B sees no jobs from recruiter A's pool")

        # And cannot get_candidate for an application they don't own
        result = json.loads(agent_module.get_candidate.invoke({"application_id": ids["app_alice"]}))
        if "error" not in result:
            _fail(f"Multi-tenancy regression: recruiter B can read recruiter A's application: {result}")
        _ok("Multi-tenancy: recruiter B cannot read recruiter A's application via get_candidate")

        ids["recruiter_b"] = recruiter_b.id

    # ----- Test 4: cross-match-invite outreach flow end-to-end -----
    with Session(engine) as session:
        recruiter_obj = session.get(User, ids["recruiter"])
        draft = draft_email_for_application(
            session,
            application_id=ids["app_alice"],
            target_job_id=ids["job_tech_lead"],
            intent="cross_match_invite",
            recruiter=recruiter_obj,
            tone="warm and inviting",
        )
        if not draft.id:
            _fail("draft_email_for_application returned a row without an id")
        if not draft.subject or not draft.body:
            _fail(f"Draft has empty subject or body: subject={draft.subject!r}, body[:50]={draft.body[:50]!r}")
        # cross_match_invite must embed the public URL on its own line
        from app.config import settings as _s
        expected_url = f"{_s.APP_BASE_URL}/job/{ids['job_tech_lead']}"
        if expected_url not in draft.body:
            _fail(f"cross_match_invite draft missing public URL {expected_url} in body")
        _ok(f"draft_email_for_application produced a cross_match_invite draft (id={draft.id}) with the public URL embedded")
        ids["draft"] = draft.id

    # ----- Test 5: list_drafts surfaces the new draft -----
    with Session(engine) as session:
        recruiter_obj = session.get(User, ids["recruiter"])
        set_agent_context(recruiter_obj, session)
        result = json.loads(agent_module.list_drafts.invoke({"application_id": ids["app_alice"]}))
        if not result:
            _fail("list_drafts returned empty for Alice")
        if not any(d["id"] == ids["draft"] for d in result):
            _fail(f"list_drafts missing draft id={ids['draft']}: {result}")
        _ok(f"list_drafts returns the new draft (status='{result[0]['status']}')")

    # ----- Test 6: draft status transitions (without firing Resend) -----
    with Session(engine) as session:
        draft_row = session.get(OutreachEmail, ids["draft"])
        if draft_row.status != "draft":
            _fail(f"Draft starts in wrong status: {draft_row.status}")
        # Soft-delete via the model directly (the endpoint logic is tested by integration)
        draft_row.status = "discarded"
        session.add(draft_row)
        session.commit()
        session.refresh(draft_row)
        if draft_row.status != "discarded":
            _fail("Status transition draft->discarded did not persist")
    _ok("Draft status transitions persist (draft -> discarded)")

    # ----- Test 7: agent.run_turn_stream is awaitable and emits events -----
    # We can't easily exercise the full LangGraph loop without burning real
    # Gemini quota, so we monkey-patch the planner LLM to short-circuit with
    # an immediate final answer.
    from langchain_core.messages import AIMessage as _AIMessage

    class _FakePlannerLLM:
        def invoke(self, messages):
            return _AIMessage(content="I see two jobs in your pool. Anything specific?")
        def bind_tools(self, tools):
            return self

    original_planner = agent_module._planner_llm
    agent_module._planner_llm.cache_clear()
    agent_module._planner_llm = lambda: _FakePlannerLLM()
    agent_module._compiled_graph.cache_clear()

    try:
        clear_history(ids["recruiter"])
        with Session(engine) as session:
            recruiter_obj = session.get(User, ids["recruiter"])
            set_agent_context(recruiter_obj, session)

            async def _collect():
                events = []
                async for chunk in agent_module.run_turn_stream(ids["recruiter"], "How many jobs do I have?"):
                    events.append(chunk)
                return events

            collected = asyncio.run(_collect())
            if not collected:
                _fail("run_turn_stream produced no events")
            if not any("event: done" in c for c in collected):
                _fail(f"run_turn_stream did not emit a done event: {collected[-2:]}")
            _ok(f"run_turn_stream emitted {len(collected)} SSE events including a final 'done'")
    finally:
        agent_module._planner_llm = original_planner
        agent_module._planner_llm.cache_clear()
        agent_module._compiled_graph.cache_clear()

    # ----- cleanup -----
    with Session(engine) as session:
        # The outreach_email row(s) cascade with the application; deleting
        # the job cascades to applications.
        for job_id in (ids["job_python"], ids["job_tech_lead"]):
            j = session.get(JobListing, job_id)
            if j:
                session.delete(j)
        for rec_id in (ids["recruiter"], ids["recruiter_b"]):
            u = session.get(User, rec_id)
            if u:
                session.delete(u)
        session.commit()
        clear_history(ids["recruiter"])
    _ok("Cleaned up test users, jobs, applications, drafts, and chat history")

    print("\n=== Phase 6 smoke test passed ===")


if __name__ == "__main__":
    main()
