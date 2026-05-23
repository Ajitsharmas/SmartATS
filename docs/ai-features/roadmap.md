# AI Features Roadmap

High-level scope of the AI/RAG features planned for SmartATS. Detailed requirements and design will be added per feature before implementation.

---

## Goals

1. **Add genuine user value** — features that make the recruiter's workflow meaningfully faster, not tech demos.
2. **Cover modern AI/LLM skills** — RAG, embeddings, vector databases, LLM orchestration — all of which appear consistently in modern backend job descriptions.
3. **Stay on the GCP / Gemini free tier** — no new paid infrastructure.

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

**Why it's worth doing later:** this single feature covers **tool calling**, **multi-step reasoning**, **LangChain or LangGraph agents** — directly relevant to AI-focused JDs like *AI Engineer — Foundational Agents* or *Duo Chat*. Adds the buzzword "agent" to your resume in a defensible, substantive way.

**Free tier compatibility:**
- Gemini 2.5 Flash supports function calling / tool use natively — no extra API tier needed
- Each agent invocation uses 3–5 LLM calls on average (one per reasoning step)
- Free tier allows 1500 LLM requests/day → ~300–500 agent invocations/day, more than enough for demo and personal use
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
| 0 | Swap Postgres → pgvector image, add embedding tables, install Gemini embeddings client | 1–2 hours | Pending |
| 1 | Resume chunking + embedding Celery task; embed on upload | 1 day | Pending |
| 2 | **Feature 2 — Semantic Search** endpoint and dashboard search box | 1 day | Pending |
| 3 | **Feature 3 — Cross-Job Match** — embed jobs, run match on application submit | Half day | Pending |
| 4 | **Feature 1 — RAG Q&A** endpoint and chat UI on candidate detail page | 1–2 days | Pending |
| 5 | **Feature 4 — Recruiter Assistant Agent** — LangChain/LangGraph agent with tool calling | 2–3 days | Deferred — start after Phase 4 |

---

## Constraints

- **Free-tier only.** `gemini-2.5-flash` (15 RPM, 1500 requests/day) and `gemini-embedding-001` (1500 RPM) are sufficient for demo and modest real-world usage.
- **No new paid infrastructure.** pgvector runs inside the existing Postgres container.
- **Graceful degradation.** If Gemini is unavailable, the app must still function — search and matching should return the existing keyword-based results as a fallback, and Q&A should display a clear error.

---

## Status

Roadmap approved. Detailed requirements and design for each phase to be discussed before implementation.
