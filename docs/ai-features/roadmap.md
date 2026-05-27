# AI Features Roadmap

High-level scope of the AI/RAG features planned for SmartATS. Detailed requirements and design will be added per feature before implementation.

---

## Goals

1. **Genuine user value** — features that make the recruiter's workflow meaningfully faster.
2. **Modern AI/LLM patterns applied to real problems** — RAG, embeddings, vector databases, and LLM orchestration used where they solve a real recruiter need, not as bolt-ons.
3. **Stay on the GCP / Gemini free tier** — no new paid infrastructure for the default deployment.

---

## Features in scope

### Feature 1 — RAG-Powered Candidate Q&A

A chat interface on each candidate's detail page where the recruiter can ask natural-language questions about that candidate's resume. The system retrieves the most relevant sections of the resume and uses Gemini to synthesise an answer that cites the supporting excerpts.

- Example: *"What was their main project at Acme Corp?"* → grounded answer with citation
- Use case: pre-interview prep, targeted screening, gap identification

### Feature 2 — Semantic Search Across All Resumes

A search box on the recruiter dashboard that accepts natural-language queries and returns candidates ranked by semantic similarity to the query — across the entire applicant pool, not just one job's applicants.

- Example: *"Python engineer with fintech background"* → ranked list of matching candidates
- Use case: talent pipeline, sourcing for new roles from past applicants

### Feature 3 — Cross-Job Matching

When a candidate applies to one job, the system also evaluates them against every other open role and surfaces alternative matches on the dashboard.

- Example: Candidate applies for *"Senior Frontend Dev"* (score 65) but matches *"Tech Lead"* role at score 89 → recommendation shown
- Use case: maximise candidate utilisation, reduce sourcing time for adjacent roles

### Feature 4 — Recruiter Assistant Agent (deferred until Features 1–3 are complete)

A conversational agent on the dashboard that helps recruiters complete multi-step workflows by autonomously deciding which tools to call and chaining them together. The agent has access to the capabilities built in Features 1–3 (semantic search, candidate Q&A, cross-job matching), plus a few extra tools.

**Examples of what the agent does in one turn:**

- *"Find top 3 Python engineers in our pool and draft a personalised outreach email to each one"*
  → Agent calls semantic search → queries each candidate's resume via RAG → drafts a tailored email per candidate
- *"Shortlist applicants for the Senior Backend role and suggest interview questions"*
  → Agent retrieves applicants → reads each resume → identifies strengths and gaps → generates question lists
- *"Which of these three candidates has the strongest leadership experience?"*
  → Agent queries each resume for leadership signal → compares → reports with citations

**Use case:** Automates the connective tissue between actions a recruiter currently has to do manually — searching, reading, summarising, drafting. Saves significant time on repetitive workflows.

**Tools the agent will have access to:**

| Tool | What it does | Built in |
|---|---|---|
| `semantic_search(query)` | Find candidates by meaning | Feature 2 |
| `query_candidate(candidate_id, question)` | Ask a question about a specific candidate's resume | Feature 1 |
| `find_matching_jobs(candidate_id)` | Get other roles this candidate fits | Feature 3 |
| `list_applicants(job_id)` | Get applicants for a specific job | Existing API |
| `draft_email(candidate_id, purpose)` | Compose an outreach email (LLM, no external send) | New, agent-only |
| `compare_candidates(ids)` | Compare candidates side-by-side | New, agent-only |

**Why this is deferred until 1–3 are complete:** the agent's tools *are* features 1–3. Building it before they exist would be wasted scaffolding.

**Why it's the right capstone for the AI features:** the agent is the natural top of the stack — its tools *are* Features 1–3, so once those exist, exposing them as an orchestrated agent unlocks multi-step workflows recruiters cannot otherwise perform in a single action. It also demonstrates the modern agent pattern (tool calling, multi-step reasoning) using the LangChain / LangGraph ecosystem.

**Free tier compatibility:**
- Gemini 2.5 Flash supports function calling / tool use natively — no extra API tier needed
- Each agent invocation uses 3–5 LLM calls on average (one per reasoning step)
- Free tier allows 1500 LLM requests/day → ~300–500 agent invocations/day, sufficient for typical recruiter usage
- No new infrastructure — runs in the existing web/worker containers

---

## Shared infrastructure

All three features sit on the same foundation:

| Component | Purpose |
|---|---|
| `pgvector` Postgres extension | Vector storage and similarity search |
| Embedding pipeline | Convert resumes and job descriptions to vectors |
| Gemini embedding model (`gemini-embedding-001`) | Free-tier embedding API, configurable output dimension (768/1536/3072) |
| Gemini LLM (existing) | Generation for RAG Q&A only |
| Celery worker (existing) | Asynchronous embedding on resume upload and job creation |

Building this foundation once enables all three features.

---

## Implementation phases

| Phase | Deliverable | Effort | Status |
|---|---|---|---|
| 0 | Swap Postgres → pgvector image, add embedding tables, install Gemini embeddings client | 1–2 hours | Complete |
| 1 | Resume chunking + embedding Celery task; embed on upload | 1 day | Complete |
| 2 | **Feature 2 — Semantic Search** endpoint and dashboard search box | 1 day | Complete |
| 3 | **Feature 3 — Cross-Job Match** — embed jobs, run match on application submit | Half day | Complete |
| 4 | **Feature 1 — RAG Q&A** endpoint and chat UI on candidate detail page | 1–2 days | Complete |
| 5 | **LLM Re-ranking** — two-stage retrieve-then-rerank quality improvement for Phase 2 + 3 | 1–2 days | Complete |
| 6 | **Feature 4 — Recruiter Assistant Agent** — read-only chat assistant + job-authoring tools + candidate outreach (drafts only, recruiter-approved sends via Resend). Scope: Tiers 2 + 3 + 5 from the agentic-tiers analysis. | 1 week | Designed — see [phase-6-agent.md](phase-6-agent.md) |

---

## Constraints

- **Free-tier only.** `gemini-2.5-flash` (15 RPM, 1500 requests/day) and `gemini-embedding-001` (1500 RPM) are sufficient for small-scale deployments. Paid tiers remove these caps when needed at higher load.
- **No new paid infrastructure.** pgvector runs inside the existing Postgres container.
- **Graceful degradation.** If Gemini is unavailable, the app must still function — search and matching should return the existing keyword-based results as a fallback, and Q&A should display a clear error.

---

## Status

Roadmap approved. Detailed requirements and design for each phase to be discussed before implementation.
