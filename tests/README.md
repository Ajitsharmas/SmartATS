# Tests

Fast, isolated unit and route tests. No database, no Redis, no MinIO, no Gemini required.

For end-to-end integration coverage (real Postgres + pgvector + Redis + Celery + Gemini), see [`scripts/smoke_test_phase*.py`](../scripts/). The two layers are complementary — pytest tells you a pure function or route guard is correct in <2 seconds; the smoke tests tell you the whole stack still works end-to-end against real infrastructure.

---

## Running the tests

```bash
# One-time setup
.venv/bin/pip install -r requirements-dev.txt

# Run everything
.venv/bin/python -m pytest

# Single file
.venv/bin/python -m pytest tests/test_outreach.py

# Single test class or function
.venv/bin/python -m pytest tests/test_outreach.py::TestFilterEmailBody
.venv/bin/python -m pytest tests/test_outreach.py::TestFilterEmailBody::test_foreign_url_stripped

# Quiet mode (just summary)
.venv/bin/python -m pytest -q
```

Pytest configuration lives in [`../pytest.ini`](../pytest.ini). Shared fixtures and env-var overrides are in [`conftest.py`](conftest.py).

---

## Test file map

| File | What it tests | Lines of test | Infra needed |
|---|---|---|---|
| [`test_outreach.py`](test_outreach.py) | URL output filter (security), JSON parsing with markdown fences, prompt construction (tag wrapping, cross-match URL injection) | ~140 | None |
| [`test_agent.py`](test_agent.py) | `_strip_tool_echoes`, `_extract_chunk_text` (multi-modal coercion), tool-palette regression check (13 tools, no `send_email`), guardrail constants | ~110 | None |
| [`test_rerank.py`](test_rerank.py) | `_parse_llm_response` (score clamping, JSON shape), cache-key derivation, `RerankResult` invariants | ~75 | None |
| [`test_ai_errors.py`](test_ai_errors.py) | Gemini error classifier against synthetic exceptions (old `google-api-core` SDK + new `google-genai` SDK shapes) | ~80 | None |
| [`test_models.py`](test_models.py) | Pydantic validation: `JobListing` title/description/salary, `JobListingUpdate`, `SearchQuery` bounds, `ChatRequest`, `CrossMatchInviteRequest`, `UserCreate` email | ~125 | None |
| [`test_security.py`](test_security.py) | `_check_critical_secrets` default-key fail-fast + warnings, file size cap (413) on `/upload`, magic-byte check (400), content-type check (400) | ~95 | FastAPI TestClient |
| [`test_auth_routes.py`](test_auth_routes.py) | Protected JSON routes reject anon callers with 401/403, public pages serve, `/token` rejects empty creds | ~120 | FastAPI TestClient |

**Roughly 113 test functions across 7 files. The full suite runs in under 3 seconds.**

---

## What's deliberately NOT tested here

- **Real database queries**: covered by the smoke tests against a live Postgres + pgvector. Mocking SQLModel sessions is fragile and low-value.
- **LangGraph internals**: framework code; tested upstream. Our agent's tool palette + helpers are exercised here; the loop itself is exercised by `smoke_test_phase6.py`.
- **Gemini API mocking**: outputs vary, and mocking the SDK is brittle. The smoke tests make real calls (which cost a handful of Gemini requests per run, well within the free tier).
- **End-to-end `/process` flow**: the full upload → score → embed → match pipeline is exercised by `smoke_test_phase1.py` / `phase3.py` / `phase5.py`.
- **Frontend (HTML/JS)**: no automated browser tests. Manual QA pass after frontend changes; smoke tests assert backend behaviour the frontend depends on.

---

## How `conftest.py` keeps the tests hermetic

The fixture sets four env vars **before any app module imports**:

```python
os.environ.setdefault("SECRET_KEY", "test-suite-only-not-the-default-value")
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")
os.environ.setdefault("APP_BASE_URL", "http://localhost:8000")
os.environ.setdefault("RATE_LIMITER_STORAGE_URL", "memory://")
```

The fourth is the non-obvious one: SlowAPI's `Limiter` reads its storage URI at module import time. By redirecting to `memory://` before `app.limiter` is imported, the rate limiter uses in-memory counters instead of trying to talk to Redis. This lets `TestClient` exercise rate-limited routes without a Redis dependency.

---

## When to add a new test

- **A pure function gets a bug** → add a regression test in the matching `tests/test_<module>.py`.
- **A new security guardrail** ships → add a `tests/test_security.py` entry that exercises both the happy path and the failure mode.
- **A new tool joins the agent palette** → update `TestToolPalette::test_*_tools_present` in `test_agent.py`.
- **A new route gets added** → add a one-line auth check in `test_auth_routes.py` confirming anon callers get 401/403.

When **not** to add a test here:

- The thing you're testing needs Postgres/Redis/MinIO/Gemini → it belongs in a smoke test, not here.
- The thing is a "test that LangGraph still works" / "test that pgvector still works" — it's framework code, leave it alone.

---

## Coverage gaps that would be worth adding eventually

- **`_build_top_resume_chunks`** (in `worker.py`) — pure SQL builder, but currently only covered by smoke tests.
- **`outreach.draft_email_for_application`** — full path with a mocked LLM client would be valuable. Out of scope for the first batch because mocking LangChain's `ChatGoogleGenerativeAI` is fragile.
- **Phase 4 RAG `_format_excerpts`** — pure function, easy add. Not in v1 because the tag-wrapping is covered indirectly by `test_outreach.py`.
- **Auth helpers** (`hash_password`, `verify_password`, JWT encode/decode) — security-critical, worth a focused file at some point.
