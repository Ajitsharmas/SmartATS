"""
Phase 4 smoke test — RAG-powered candidate Q&A.

Validates the RAG pipeline end-to-end by invoking the underlying components
directly (retrieval + LangChain stream + accumulation). Skips the HTTP/SSE
layer because programmatically consuming SSE in a test is awkward; the
endpoint itself is a thin wrapper around these components plus auth.

The test asserts:
  1. A question answerable from the resume gets a grounded answer that
     references the relevant content.
  2. A question NOT answerable from the resume gets an honest refusal
     ("does not mention" / "no mention of") rather than a hallucinated answer.
  3. An application with NO embeddings returns no citations from retrieval.

Run from the project root either from your local venv or inside the worker
container:

    .venv/bin/python scripts/smoke_test_phase4.py
    docker compose exec worker python scripts/smoke_test_phase4.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlmodel import Session, select

from app.chat_history import (
    append_turn,
    load_history,
    new_session_id,
    reset_session,
)
from app.database import create_db_and_tables, engine
from app.embeddings import embed_query_cached
from app.main import CHAT_RETRIEVAL_SQL, CHAT_TOP_K
from app.models import Application, Citation, JobListing, ResumeEmbedding, User
from app.rag import stream_rag_answer
from app.security import get_password_hash
from app.worker import embed_resume_task


# Resume with very specific, verifiable content. The test asserts both that
# the assistant uses the Acme Corp content (positive) AND that it honestly
# refuses to claim Microsoft experience (negative).
SAMPLE_RESUME = """
Alice Chen, Senior Software Engineer
alice@example.com  |  San Francisco, CA

EXPERIENCE

Acme Corp, San Francisco, CA — Senior Backend Engineer
2022 — Present
Led the migration of 12 microservices from monolithic architecture to AWS ECS,
reducing deployment time from 45 minutes to under 5 minutes. Designed and built
a real-time event streaming pipeline using Kafka and Python that processes 2
million events per day. Managed a Kubernetes cluster of 40 nodes including
writing custom operators with Kubebuilder.

Beta Industries, Remote — Software Engineer
2019 — 2022
Built and maintained a Django-based REST API serving 50,000 daily active users.

EDUCATION

University of California, Berkeley — BS Computer Science
2015 — 2019
GPA: 3.8 / 4.0

SKILLS

Languages: Python, Go, TypeScript, SQL
Infrastructure: AWS (ECS, S3, RDS, Lambda), Docker, Kubernetes, Terraform
"""


def _fail(message: str) -> None:
    print(f"FAIL: {message}")
    sys.exit(1)


def _ok(message: str) -> None:
    print(f"OK:   {message}")


def _retrieve_citations(session: Session, application_id: int, question: str) -> list[Citation]:
    query_vector = embed_query_cached(question)
    rows = session.execute(
        text(CHAT_RETRIEVAL_SQL),
        {
            "query_vector": str(query_vector),
            "application_id": application_id,
            "top_k": CHAT_TOP_K,
        },
    ).fetchall()
    return [
        Citation(
            chunk_index=row.chunk_index,
            chunk_text=row.chunk_text,
            similarity=float(row.similarity),
        )
        for row in rows
    ]


def _accumulate_answer(question: str, citations: list[Citation]) -> str:
    """Run the streaming chain and collect all tokens into a single string."""
    return "".join(stream_rag_answer(question, citations, history=[]))


def main() -> None:
    print("=== Phase 4 smoke test — RAG Q&A ===\n")

    create_db_and_tables()
    _ok("Database is initialised")

    # Create recruiter + job + application, then embed the resume
    with Session(engine) as session:
        recruiter = User(
            email="smoke-p4@example.com",
            full_name="Smoke P4 Recruiter",
            hashed_password=get_password_hash("test"),
            is_verified=True,
        )
        session.add(recruiter)
        session.commit()
        session.refresh(recruiter)

        job = JobListing(
            owner_id=recruiter.id,
            title="Phase 4 Smoke Test Role",
            description="Temporary job created by the RAG Q&A smoke test.",
            skills="python, aws, kafka",
            location="remote",
        )
        session.add(job)
        session.commit()
        session.refresh(job)

        application = Application(
            job_id=job.id,
            candidate_email="alice@example.com",
            candidate_name="Alice Chen",
            resume_url="/download/alice.pdf",
        )
        session.add(application)
        session.commit()
        session.refresh(application)

        ids = {
            "recruiter": recruiter.id,
            "job": job.id,
            "app": application.id,
        }

    _ok(f"Created recruiter, job, application (id={ids['app']})")

    embed_resume_task.apply(
        kwargs={"text_content": SAMPLE_RESUME, "application_id": ids["app"]}
    ).get()
    _ok("Embedded resume via Phase 1 pipeline")

    with Session(engine) as session:
        # ---- Test 1: answerable question ----
        positive_question = "What was their main project at Acme Corp?"
        citations = _retrieve_citations(session, ids["app"], positive_question)
        if len(citations) == 0:
            _fail("Retrieval returned zero chunks for an answerable question")
        _ok(f"Retrieved {len(citations)} chunks for the answerable question")

        answer = _accumulate_answer(positive_question, citations)
        if not answer or len(answer) < 20:
            _fail(f"LLM returned an unusably short answer: {answer!r}")

        # The answer should reference Acme Corp content. We're lenient about
        # exact wording but require at least one related keyword to appear.
        answer_lower = answer.lower()
        expected_keywords = ["acme", "microservice", "ecs", "migration", "kafka", "kubernetes"]
        matched_keywords = [kw for kw in expected_keywords if kw in answer_lower]
        if not matched_keywords:
            _fail(f"Answer did not mention any expected Acme Corp content. Got: {answer[:200]}")
        _ok(f"Answer references resume content (matched keywords: {matched_keywords})")

        # Citations should be embedded in the answer text as [chunk N]
        if "[chunk" not in answer_lower:
            _fail(f"Answer did not include any [chunk N] citations. Got: {answer[:200]}")
        _ok("Answer includes [chunk N] inline citations")

        # ---- Test 2: question NOT in the resume — honest refusal ----
        negative_question = "Has this candidate worked at Microsoft?"
        neg_citations = _retrieve_citations(session, ids["app"], negative_question)
        neg_answer = _accumulate_answer(negative_question, neg_citations)
        neg_lower = neg_answer.lower()
        refusal_phrases = [
            "does not mention",
            "no mention",
            "not mention",
            "doesn't mention",
            "doesn't say",
            "does not say",
            "not in the resume",
            "no information",
            "no reference",
        ]
        refusal_matched = any(phrase in neg_lower for phrase in refusal_phrases)
        if not refusal_matched:
            _fail(
                "LLM did not honestly refuse a question not answerable from the resume. "
                f"This indicates hallucination risk. Got: {neg_answer[:300]}"
            )
        _ok("Honest refusal: LLM said it does not have information about Microsoft")

        # ---- Test 3: empty embeddings → retrieval returns no chunks ----
        # Create a fresh application with no embeddings
        empty_app = Application(
            job_id=ids["job"],
            candidate_email="empty@example.com",
            candidate_name="Empty Embeddings Candidate",
            resume_url="/download/empty.pdf",
        )
        session.add(empty_app)
        session.commit()
        session.refresh(empty_app)

        empty_citations = _retrieve_citations(session, empty_app.id, "What experience do they have?")
        if len(empty_citations) != 0:
            _fail(f"Expected zero retrievals for an application with no embeddings, got {len(empty_citations)}")
        _ok("Retrieval correctly returns zero chunks for an application with no embeddings")

        # ---- Test 4: Redis-backed chat history round-trip ----
        sid = new_session_id()
        # Append a couple of turns
        append_turn(ids["recruiter"], ids["app"], sid, "user", "Have they worked with Kafka?")
        append_turn(ids["recruiter"], ids["app"], sid, "assistant", "Yes, at Acme Corp [chunk 2].")
        loaded = load_history(ids["recruiter"], ids["app"], sid)
        if len(loaded) != 2:
            _fail(f"Expected 2 history turns, got {len(loaded)}")
        if loaded[0].role != "user" or "Kafka" not in loaded[0].content:
            _fail(f"First history turn does not match what was stored: {loaded[0]!r}")
        if loaded[1].role != "assistant" or "Acme" not in loaded[1].content:
            _fail(f"Second history turn does not match what was stored: {loaded[1]!r}")
        _ok("Redis chat history append + load round-trip works")

        # ---- Test 5: history isolation across session_ids ----
        other_sid = new_session_id()
        other_history = load_history(ids["recruiter"], ids["app"], other_sid)
        if len(other_history) != 0:
            _fail("Different session_id leaked history from the first session")
        _ok("History is isolated by session_id — fresh session sees no prior turns")

        # ---- Test 6: sliding-window trim (max 12 entries) ----
        trim_sid = new_session_id()
        for i in range(20):
            append_turn(ids["recruiter"], ids["app"], trim_sid, "user", f"Q{i}")
            append_turn(ids["recruiter"], ids["app"], trim_sid, "assistant", f"A{i}")
        trimmed = load_history(ids["recruiter"], ids["app"], trim_sid)
        if len(trimmed) != 12:
            _fail(f"Expected sliding window trim to 12 entries, got {len(trimmed)}")
        # The oldest entries should have been trimmed — the first stored should
        # be the (40-12)=28th raw append, which is the back-half of the loop
        if "Q14" not in trimmed[0].content:
            _fail(f"Sliding window kept the wrong tail — first content is {trimmed[0].content}")
        _ok("Sliding-window trim keeps only the latest 12 entries")

        # ---- Test 7: reset_session clears history ----
        reset_session(ids["recruiter"], ids["app"], trim_sid)
        after_reset = load_history(ids["recruiter"], ids["app"], trim_sid)
        if len(after_reset) != 0:
            _fail(f"reset_session did not clear history; {len(after_reset)} turns remain")
        _ok("reset_session clears the conversation")

        # Cleanup leftover test sessions
        reset_session(ids["recruiter"], ids["app"], sid)

        # Cleanup — delete recruiter, cascades remove job, applications, embeddings
        u = session.get(User, ids["recruiter"])
        if u:
            # Need to delete the job first since user FK doesn't cascade jobs
            j = session.get(JobListing, ids["job"])
            if j:
                session.delete(j)
            session.delete(u)
        session.commit()
    _ok("Cleaned up test data via cascades")

    print("\n=== Phase 4 smoke test passed ===")


if __name__ == "__main__":
    main()
