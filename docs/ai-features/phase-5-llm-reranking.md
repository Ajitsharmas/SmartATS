# Phase 5 — LLM Re-ranking for Semantic Search and Cross-Job Matching

A quality enhancement to [Phase 2](phase-2-semantic-search.md) and [Phase 3](phase-3-cross-job-matching.md). Adds a Gemini-based **re-ranking stage** on top of the existing pgvector retrieval so the final results reflect actual semantic-and-lexical fit, not just embedding-space proximity.

For the overall roadmap, see [roadmap.md](roadmap.md). This phase precedes the Phase 6 agent and addresses a real limitation surfaced while testing Phase 3.

---

## Why this is needed

Dense embedding models (including `gemini-embedding-001`) measure semantic similarity in *topic space*, not keyword presence. To the model:

- *"Python developer with 5 years experience"* and *"Java developer with 5 years experience"* sit very close in embedding space — they share the meaning *"professional backend programmer with years of experience"*
- Cosine similarity between these strings is typically **0.70–0.78** — high enough to surface as a "match" under both Phase 2 search and Phase 3 cross-job matching

Empirical confirmation from real testing:

- **Phase 3 failure case** — a recruiter completely rewrote a Python job to require only Java. A Python-only resume still scored **75% match** under bidirectional matching with harmonic mean, when it should have scored around **30–40%**
- **Phase 2 noise** — searches like *"Python engineer with cloud experience"* surface candidates with vaguely software-engineering-shaped resumes regardless of whether they actually mention Python or cloud

This is a well-known limitation of pure dense retrieval. The standard production answer is **two-stage retrieve-then-rerank**:

1. **Stage 1 — cheap retrieval** uses vector similarity to narrow the candidate set quickly
2. **Stage 2 — accurate re-ranking** sends the surviving candidates' actual text to an LLM for considered judgement

Embeddings are kept for what they're good at (fast pre-filter over large pools); the LLM is added for what *it's* good at (reading actual words and understanding specific technical content).

---

## Goal

Both Phase 2 search and Phase 3 cross-job matching produce results that **track recruiter intuition**. A Python-only resume against a Java-only job should land in the 30–40% range, not 75%. A search for *"Kubernetes operator developer"* should surface candidates whose resumes actually mention Kubernetes operators, not generic "DevOps engineers".

---

## Acceptance criteria

- **Phase 2 search** endpoint (`POST /search/candidates`) returns LLM-scored results, ordered by LLM score (with vector similarity as the pre-filter cull)
- **Phase 3 cross-job matching** (`match_jobs_task`) produces LLM-scored matches, stored alongside an LLM-generated critique on each `cross_job_match` row
- Both flows fall back gracefully to vector-only scoring if Gemini is unreachable or rate-limited
- LLM scoring results are cached in Redis to absorb repeated work
- Free-tier quota remains usable for normal demo workflows (some throttling on bulk operations expected)
- Smoke tests confirm the Python/Java case now scores around 30–40% instead of 75%

---

## Architecture — two-stage retrieve-then-rerank

The pattern is identical for both Phase 2 and Phase 3, only the inputs differ:

```
                ┌──────────────────────────────────────────────┐
                │  Stage 1 — pgvector pre-filter (free, fast)  │
                │                                              │
                │  - Phase 2: cosine vs all resume chunks      │
                │             owned by the recruiter           │
                │  - Phase 3: bidirectional cosine + harmonic  │
                │             mean across the recruiter's      │
                │             other jobs                       │
                │                                              │
                │  Result: top-K candidates (K = 10)           │
                └────────────────────┬─────────────────────────┘
                                     │
                                     ▼
                ┌──────────────────────────────────────────────┐
                │  Stage 2 — LLM re-rank (paid, accurate)      │
                │                                              │
                │  For each of the top-K candidates:           │
                │    1. Check Redis cache for prior score      │
                │    2. If miss: build prompt with the actual  │
                │       text (resume + query/job), call Gemini │
                │    3. Parse structured response (score +     │
                │       critique)                              │
                │    4. Cache the result for 1 hour            │
                │                                              │
                │  Result: top-K candidates with LLM scores    │
                │          and critiques                       │
                └────────────────────┬─────────────────────────┘
                                     │
                                     ▼
                ┌──────────────────────────────────────────────┐
                │  Stage 3 — final ordering + threshold filter │
                │                                              │
                │  - Sort by LLM score descending              │
                │  - Drop below configured threshold           │
                │  - Return top N to caller / persist          │
                └──────────────────────────────────────────────┘
```

### Why two stages and not LLM-everything

| | Pure vector | Pure LLM | Two-stage (chosen) |
|---|---|---|---|
| Accuracy on Python/Java case | Poor (~0.75) | Excellent | Excellent |
| Latency per request | ~10 ms | ~50 LLM calls × 1–2 s = unusable | ~10 ms + 10 LLM calls × ~1–2 s ≈ 10–20 s |
| Free tier daily quota usage | 0 LLM calls | Hundreds per recheck | ~10 per query / per match recheck |
| Behaves on cold cache | Fine | Fine | Fine |
| Behaves on warm cache | Fine | Fine | Fast (cache hits skip LLM) |
| Falls back if Gemini down | N/A | Broken | Falls back to vector-only |

LLM-everything is impractical at any scale because the LLM cost is per-pair and the candidate set grows linearly with applicant pool size. Two-stage keeps the LLM cost bounded to a fixed K regardless of how large the pool grows.

---

## Decisions

### 1. Pre-filter `K = 10`

Both phases pre-filter to top 10 candidates via pgvector before calling the LLM. Rationale:

- **Phase 2** — the recruiter only sees top 10 in the dashboard anyway (pagination kicks in beyond that). LLM-ranking 10 is enough to produce a high-quality first page.
- **Phase 3** — we only show 3 cross-job suggestions per candidate. Pre-filtering to 10 gives the LLM enough variety to find the best 3 without wasting calls.
- **Cost** — 10 LLM calls per operation is reasonable on the free tier (15 RPM, 1500/day allows ~150 search-or-match operations/day).
- **Tunability** — `LLM_RERANK_TOP_K` constant in `app/main.py` (or worker), easy to adjust.

If the recruiter requests page 2 of Phase 2 search via `offset`, the endpoint re-runs Stage 1 with a wider K (e.g., 20) and re-ranks the next 10. Pagination handles this without changing the pre-filter constant per request.

### 2. Parallel LLM calls within a single request (Phase 2 only)

Phase 2 search is **synchronous** — the recruiter is waiting for the response. With 10 sequential LLM calls at 1–2 s each, the response would take 10–20 seconds. Unacceptable.

Solution: fire all 10 LLM calls in parallel via `asyncio.gather()`. With Gemini's free-tier RPM cap (15/min), 10 concurrent calls land safely. Total wall-clock time: 1–2 s (the duration of the slowest call), not 10–20 s.

Phase 3 cross-job matching runs in a Celery worker — already async from the recruiter's perspective. We keep the LLM calls **sequential** to:
- Stay well under the 15 RPM cap during bulk recheck operations
- Make per-task progress logging clearer
- Avoid hammering free tier when 100 applications recheck simultaneously

### 3. LLM prompt — same structure as primary scoring

We reuse the same prompt pattern that `analyze_resume_task` already uses, adjusted slightly for context. The prompt has three parts:

```
You are an expert recruiter. Score how well this resume fits the role described
below. Be honest and strict. Consider specific technical skills, years of
experience, role level, and domain knowledge — not vague semantic similarity.

JOB / QUERY:
{job_text or search_query}

RESUME:
{resume_text}

Return a strict JSON object:
{
  "score": <integer 0-100>,
  "critique": <one-paragraph reasoning explaining what matched and what didn't>
}
```

Note the "Be honest and strict" — we observed earlier that the LLM's defaults are generous (similar problem to the embeddings). Explicit strictness in the prompt produces more discriminating scores. We'll calibrate by running the Python/Java case through it during implementation.

For **Phase 2 search**, `{job_text or search_query}` is the recruiter's free-text query (e.g., *"Python engineer with cloud experience"*). For **Phase 3 cross-job match**, it's the full job description, skills, and location of the candidate alternative job.

### 4. Caching — Redis with 1-hour TTL

LLM scoring is expensive. We cache results aggressively:

- **Cache key:** `rerank:{sha256(query_text + resume_text)}`
- **Cache value:** JSON-encoded `{"score": int, "critique": str}`
- **TTL:** 1 hour (matches the Phase 2 query embedding cache)
- **Invalidation:** automatic via TTL. The reanalyze endpoint already clears chat history; we'll extend it to clear the rerank cache for that application.

Cache hit rate will be high for Phase 3 because cross-job matches recompute against unchanged data (resume + job text) until either side is edited. The bulk recheck button benefits especially.

Cache hit rate for Phase 2 will be lower because recruiter queries vary. But common phrases ("Senior backend engineer", "AWS architect") repeat across sessions and will hit cache.

### 5. Threshold calibration — to be tuned during implementation

Vector-similarity thresholds (Phase 2 = 0.7, Phase 3 = 0.55) don't apply to LLM scores. LLM scores are integers 0–100 with different semantics. Initial values to validate during implementation:

| Phase | Threshold | Rationale |
|---|---|---|
| Phase 2 search | 60 | Surface only matches the LLM considers a real fit (60% or better) |
| Phase 3 cross-job match | 65 | Cross-job match is a stronger claim — higher bar |

Both are constants — `LLM_SEARCH_MIN_SCORE` and `LLM_MATCH_MIN_SCORE` — easy to tune.

### 6. Fallback when Gemini is unreachable

LLM scoring depends on Gemini. If Gemini is down (`GeminiUnavailableError`) or rate-limited:

- **Phase 2 search** — return the Stage 1 vector results ordered by vector similarity, with a flag in the response (`degraded: true`) that the dashboard can use to show a small "AI rerank unavailable, showing vector matches" notice
- **Phase 3 cross-job match** — fall back to the existing bidirectional cosine + harmonic mean algorithm (current Phase 3 behavior), with the threshold raised slightly to compensate for the lower precision

This means the system **never goes completely dark**. Vector-only is worse than LLM-rerank but still useful, and degrades visibly so the recruiter knows the difference.

### 7. Rate limit adjustments

Adding LLM calls to existing endpoints multiplies their effective Gemini usage. Updated limits:

| Endpoint | Old limit | New limit | Reason |
|---|---|---|---|
| `POST /search/candidates` (Phase 2) | 10 / minute | 5 / minute | Now 10× more LLM calls per request |
| `POST /applications/{id}/match-refresh` (Phase 3 per-candidate) | 10 / minute | 5 / minute | Same reason |
| `POST /matches/refresh-all` (Phase 3 bulk) | 2 / hour | 1 / hour | A 100-application bulk recheck is now ~1000 LLM calls — already at risk of saturating daily quota |
| `POST /applications/{id}/reanalyze` | 10 / minute | (unchanged) | Already triggers Phase 3; rate effectively cascaded |

### 8. Database schema change — add `critique` to `cross_job_match`

The LLM produces a reasoning paragraph alongside each score. Worth storing — the recruiter sees it as a tooltip / expandable panel on each match suggestion in the "Also a good fit for" section. Helps the recruiter understand *why* the system surfaced a particular alternative job.

```sql
ALTER TABLE crossjobmatch ADD COLUMN critique TEXT;
```

Phase 2 search results are ephemeral (returned in response, not persisted), so no schema change there. The critique is included in `SearchResult` and `SearchResponse` Pydantic models.

---

## Files to create / modify

| File | Change |
|---|---|
| `app/rerank.py` | **NEW** — `rerank_with_llm(query_text, candidate_texts)` returns list of `(index, score, critique)`. Handles caching, parallel/sequential, fallback. Reused by both Phase 2 and Phase 3. |
| `app/models.py` | Add `critique: str \| None` to `CrossJobMatch`, `SearchResult`, `CrossJobMatchResult` |
| `app/main.py` | `POST /search/candidates` runs Stage 1 then `rerank_with_llm` (parallel). Update rate limit to 5/min. Return `degraded` flag if rerank failed. Update `reanalyze` to also clear rerank cache. |
| `app/worker.py` | `match_jobs_task` runs the existing vector pre-filter to top-K=10, then `rerank_with_llm` (sequential) to score. Persist score + critique on `cross_job_match`. Fallback to current behavior on LLM failure. |
| `app/static/dashboard.html` | Show `critique` as a tooltip / expandable note on each cross-job match badge. Search results show critique inline (one-line truncated) with click-to-expand. |
| `app/static/js/api.js` | No interface change — response shapes extended, not changed |
| `scripts/smoke_test_phase5.py` | **NEW** — verifies the Python/Java case scores in the 30–40% range. Confirms cache hits. Confirms fallback when Gemini is unavailable (mocked). |

---

## Free-tier cost analysis

Gemini 2.5 Flash free tier: 15 RPM, 1500 requests/day.

| Operation | LLM calls | Notes |
|---|---|---|
| One Phase 2 search | 10 | Parallel; lands in ~1 burst of 10 |
| Search with cache hits | 0–10 | Common queries skip the LLM entirely |
| Phase 3 match for one application | 10 | Sequential in Celery |
| Bulk Phase 3 recheck (100 apps) | up to 1000 | ⚠️ Saturates daily quota — caching reduces this dramatically if resumes/jobs haven't changed |
| Phase 4 RAG Q&A | 1 | Unchanged |
| Primary `analyze_resume_task` | 1 | Unchanged |

**Conclusion** — the free tier is sufficient for normal workflows. Bulk recheck of large pools is the only operation at risk; the 1/hour rate limit on `refresh-all` keeps it bounded, and the cache absorbs repeat work.

If a recruiter pool ever exceeded 150 candidates and the recruiter genuinely wanted to re-score the entire pool daily, the right answer is to move to Gemini's paid tier — not to change the architecture.

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Gemini quota exhaustion during demo | 5/min per-user + 1/hour bulk + Redis cache absorbs repeats |
| LLM returns malformed JSON | Strict parsing with fallback to "score 0, critique unavailable"; same pattern as `analyze_resume_task` |
| LLM is too generous on scoring | Iterate on prompt during implementation; "Be honest and strict" wording + concrete examples in prompt |
| Search latency jumps to 1–2 s | Parallel LLM calls; users used to LLM-shaped wait times from RAG Q&A. We may add a loading spinner during search |
| Bulk recheck takes minutes | Acceptable — already async via Celery, recruiter sees progress in UI as matches update |
| Cache size grows unbounded | TTL=1h + Redis volatile-lru eviction already configured. Worst case Redis evicts oldest entries; system continues to work, just with more LLM calls |
| `rerank_with_llm` becomes the bottleneck | It's pure I/O-bound — adding more workers / async parallelism is the answer, not optimisation. Standard scaling path |
| Multi-tenancy: rerank cache leaks across recruiters | Cache key is hash of (query + resume text). Two different recruiters with two different queries against two different resumes have different cache keys. No leakage possible |

---

## Implementation order (for next session)

1. **Foundation** — create `app/rerank.py` with the shared `rerank_with_llm` function. Cache + parallel/sequential variants + Gemini call + JSON parsing + fallback. No endpoint wiring yet.
2. **Phase 2 wiring** — update `POST /search/candidates` to use rerank. Add `degraded` flag. Adjust rate limit. Update `SearchResult` schema.
3. **Phase 3 wiring** — update `match_jobs_task` to add a Stage 2 LLM rerank step after the existing bidirectional matching. Persist critique. Update `CrossJobMatch` schema with migration.
4. **Frontend** — render critiques in the dashboard (tooltip on match badges, expandable note in search results).
5. **Cache invalidation in `reanalyze`** — extend the existing `clear_application_chats` pattern to also clear rerank cache entries for an application.
6. **Smoke test** — write `scripts/smoke_test_phase5.py` covering the Python/Java case, cache hits, and fallback.

Each step is independently testable and the system remains functional between steps (vector-only fallback is preserved).

---

## Smoke test plan — `scripts/smoke_test_phase5.py`

After Phase 5 lands:

1. Create recruiter A, two jobs:
   - Job α: Python developer with FastAPI experience
   - Job β: Java developer with Spring Boot experience
2. Create one candidate with a strongly Python-leaning resume applying to Job α
3. Embed everything and run match
4. Assert: Job β appears in the candidate's matches but with **score < 50** (current bug: scores 75)
5. Run Phase 2 search as recruiter A with query *"Java backend developer with Spring Boot"*
6. Assert: the Python candidate does NOT appear in the top 5 results (under current code they would, at ~70% similarity)
7. Run a second identical query immediately
8. Assert: Redis cache key for the (query × resume) pair exists, second query is faster (timing assertion)
9. Mock `embed_query_cached` or LLM client to raise `GeminiUnavailableError`
10. Run search again, assert response has `degraded: true` and falls back to vector-ordered results
11. Cleanup

---

## Out of scope for Phase 5

- **Hybrid search with Postgres FTS / BM25** — Option A from the prior discussion. Would also fix the keyword-presence problem and is free of LLM cost, but adds significant SQL complexity and isn't strictly needed once LLM rerank is in place. May be added later as a third pre-filter signal alongside vector similarity, but not in this phase.
- **Caching LLM scores in Postgres** — Redis with TTL is sufficient. Persistent storage of LLM scores would help analytics but isn't load-bearing for the user feature.
- **Streaming LLM rerank to the dashboard via SSE** — Would let Phase 2 search results appear progressively. Real product polish but adds complexity. Not in this phase.
- **A/B testing LLM-rerank vs vector-only** — Out of scope. We have user-reported evidence the current behavior is wrong; the change is well-motivated without formal evaluation.

---

## Status

Complete. Shipped in `app/rerank.py` (shared LLM rerank module), with integrations in:

- `app/main.py` — `/search/candidates` runs the two-stage retrieve-then-rerank flow with parallel LLM calls and surfaces `degraded=true` on total LLM failure.
- `app/worker.py` — `match_jobs_task` runs the same flow sequentially, persists `critique` on each `CrossJobMatch`, and falls back to vector scoring if the whole batch fails.
- `app/models.py` — `CrossJobMatch.critique`, `SearchResult.critique`, `CrossJobMatchResult.critique`, `SearchResponse.degraded`.
- `app/static/dashboard.html` — renders critiques inline on search results and match badges; shows a degraded-mode banner when the LLM is unavailable.
- `scripts/smoke_test_phase5.py` — verifies the Python-vs-Java mismatch is correctly punished, that the rerank cache fills and is reused, and that the degraded path returns all-None results when the LLM client is mocked to fail.

Rate-limit adjustments applied alongside this phase: `/search/candidates` 10/min → 5/min, `/applications/{id}/match-refresh` 10/min → 5/min, `/matches/refresh-all` 2/hour → 1/hour. `/applications/{id}/reanalyze` now also clears the rerank cache for the application.

---

## Follow-up 5.1 — Search latency

### Problem

`POST /search/candidates` is noticeably slower than it was pre-Phase-5. Empirically the round-trip from clicking "Search" to seeing results is in the 3–5 second range — fine but not great. Two factors:

- `SEARCH_PREFILTER_TOP_K = 20` means up to 20 parallel LLM calls.
- Even fully parallelised via `asyncio.gather`, total latency is bounded by the **slowest** Gemini response, which is occasionally 2–4 seconds.
- The dashboard shows only a generic spinner, so the user has no signal that work is in progress and no sense of how long it will take.

This is inherent to the design — the LLM is the source of accuracy *and* of latency. We can't make the LLM faster, but we can reduce the number of calls and make the wait more bearable.

### Decisions

**1. Reduce `SEARCH_PREFILTER_TOP_K` from 20 to 10.**

Recall analysis: in normal recruiter pools (10–200 applicants per recruiter), the top-10 pgvector pre-filter survivors already contain every candidate the LLM would have surfaced from a top-20 pre-filter. The vector signal is reliable enough as a *recall* filter that the bottom half of the top-20 was almost always either rejected by the LLM threshold or duplicated higher-scoring candidates. Halving the LLM fan-out gives roughly half the p95 latency at the cost of one rare edge case (a borderline candidate at vector rank 11–20 who would have scored above the LLM threshold).

**2. Drop pagination from the search response.**

`SearchResponse.has_more` and `payload.offset` were already approximate — only the first 20 pre-filter candidates were ever re-rankable without a fresh request. With top-K=10 there is no useful second page; we surface up to 10 and stop. If we ever need to surface more, the right answer is a "show next page" button that re-issues the pre-filter with a different vector offset, not silently held-back results.

**3. Two-stage loading UI.**

The dashboard's search box switches its loading text in two stages:

- Stage 1 (vector pre-filter, ~100 ms): *"Searching applicant pool…"*
- Stage 2 (LLM rerank, 2–4 s): *"AI-ranking N candidates…"*

The frontend gets the stage-2 count by waiting on a one-shot HEAD-like pre-fetch — except simpler: we don't actually fetch a count separately. The search endpoint returns the pre-filter count first as the first SSE event, then the ranked results as a second SSE event. **Deferred** — for v1, the dashboard simply shows the stage-1 spinner for ~200 ms and then transitions to a "AI-ranking your candidates…" message after a 200 ms timeout. No backend change needed.

**4. Streaming results via SSE — deferred.**

The most user-friendly fix is to stream LLM scores back as they complete, so the page fills progressively. Real value, real complexity (SSE plumbing in FastAPI + a partial-render JS client). Not in scope for this follow-up; keep on the roadmap as a Phase 5.2 if recruiters still find latency painful after 5.1 ships.

### Implementation plan

1. **app/main.py** — change `SEARCH_PREFILTER_TOP_K` from 20 to 10. Remove the `payload.offset + payload.limit` extension on `prefilter_k`. Set `has_more` to a constant `False` (no pagination). Cap `payload.limit` at the new top-K via an explicit `min(payload.limit, SEARCH_PREFILTER_TOP_K)`.
2. **app/main.py** — leave `SearchQuery.offset` in the schema for back-compat but ignore it. Note in the docstring that offset is currently inert.
3. **app/static/dashboard.html** (`performSearch`) — replace the single spinner with a two-stage loading message. Use a 200 ms `setTimeout` to switch from "Searching applicant pool…" to "AI-ranking your candidates…" while the fetch is in flight. Clear the timeout when the fetch resolves.
4. **scripts/smoke_test_phase5.py** — no functional change required; the rerank thresholds and assertions still hold. (Optionally tighten the test by asserting that `prefilter_k` is observed as 10 — but this couples the test to the constant unnecessarily; skip.)
5. **docs/ai-features/phase-2-semantic-search.md** — add a one-line cross-reference to this section noting the expected latency profile.

### Estimated effort

~30 minutes including manual testing in the browser.

### Status

Complete. Shipped in:

- `app/main.py` — `SEARCH_PREFILTER_TOP_K` lowered from 20 to 10; `prefilter_k = max(...)` extension removed; `effective_limit = min(payload.limit, SEARCH_PREFILTER_TOP_K)`; `has_more` hard-coded to `False`. `SearchQuery.offset` remains in the schema but is now inert (documented in the endpoint docstring).
- `app/static/dashboard.html` — `#searchResultsLoadingLabel` element added; `performSearch` swaps the label from *"Searching applicant pool…"* to *"AI-ranking your candidates…"* after 250 ms via `setTimeout`, and clears the timer on fetch resolve/error. Pagination accumulation simplified — `searchState.accumulated = data.results` (single page).
- `docs/ai-features/phase-2-semantic-search.md` — cross-reference added pointing here for the expected latency profile.

The dead "Show more" button (`#searchShowMoreWrap`) is left in the DOM but stays hidden because `has_more` is always `False`. It will be removed cleanly as part of the [Frontend Multi-Page](../frontend-multipage.md) refactor.

---

## Follow-up 5.2 — Top-K resume chunks to rerank (not full resume)

### Problem

Phase 5 sends the **full concatenated resume** to Gemini for every rerank call, in both Phase 2 search and Phase 3 cross-job matching. That decision had a defensible-sounding reason at the time ("if we cherry-pick by vector similarity we're filtering with the same signal we're trying to overrule") but it does not survive scrutiny under load.

Three things are wrong with sending full resumes:

1. **It defeats the purpose of chunking.** We chunked the resume specifically so we could embed each chunk separately and retrieve the relevant pieces against a query. If we then concatenate every chunk back and send it as one blob, the only thing chunking earned us is the embedding pipeline — the retrieval pipeline is wasted. We may as well send the raw text from the PDF (which is what Phase 1 already does).
2. **It does not scale.** A typical resume is ~10 chunks × ~600 chars = ~6 KB. With `SEARCH_PREFILTER_TOP_K = 10`, every search request shovels ~60 KB of resume text through Gemini. Multiply by `match_jobs_task` runs across a recruiter's pool (one resume × N pre-filter jobs per application) and a bulk recheck for a recruiter with 100 applications becomes 1000 LLM calls each carrying ~6 KB of resume input. Even at Gemini Flash's cheap input-token rate, this is a self-inflicted cost ceiling.
3. **It is slower.** Input tokens still affect time-to-first-token. Sending ~6 KB of resume when the LLM only needs the relevant 2–3 KB to make a fit decision adds non-trivial latency per call.

The earlier worry — *"top-K retrieval reintroduces vector similarity at exactly the dimension Phase 5 was trying to overrule"* — does not hold. The Phase 5 bug was vector similarity inflating **scores** (Python ≈ Java at 0.7 cosine), not vector retrieval failing to find the relevant chunks. For a Python resume vs a Java job, *all* of the resume's chunks are Python-flavoured; top-K retrieval still returns Python chunks; the LLM still reads "this is Python, the role is Java" and scores low. We are not hiding the mismatch — we are trimming filler.

The user-supplied framing for this decision (recorded here as the durable rationale, not just an implementation note):

> *"If we chunk first and send all chunks doesn't this defeat the purpose of chunking? If we are sending all the chunks to Gemini, we would rather send the whole resume initially itself like Phase 1."*

Exactly right. The right division of labour is: **Phase 1 sends full text because it is the canonical first read**; **Phases 2, 3, 4 retrieve top-K chunks against a query** (search text, job description, chat question respectively) because that is what the chunking + embedding infrastructure was built for.

### Decisions

**1. Phase 1 (initial scoring) stays full-text.**

Runs once per upload. No query exists at this point — the score *is* the first read. Volume is bounded by hiring throughput, not by recruiter actions. Full raw extracted text is correct here.

**2. Phase 2 (search rerank) sends top-K resume chunks by similarity to the search query.**

For each pre-filter survivor, retrieve the top-K resume chunks ranked by cosine distance to the **search query** vector. Concatenate in original chunk-index order so the LLM sees the resume sections in the order they actually appear (not by relevance — that would confuse the model into thinking the most-relevant section is the resume header).

**3. Phase 3 (cross-job match rerank) sends top-K resume chunks by similarity to the job description.**

For each candidate job in the Phase 3 pre-filter, retrieve the top-K resume chunks ranked by their best cosine distance to *any* of that job's chunks (we already have job chunks embedded in `jobembedding`). Concatenate in original chunk-index order. Per-pair retrieval — runs once per (application, candidate-job) pair, ~10 queries per `match_jobs_task` invocation.

**4. Always include chunk 0.**

Chunk 0 of a resume is almost always the header — name, contact, title, sometimes a one-line summary. It carries seniority and role-level signal ("Senior Engineer," "Tech Lead," "5 years experience") that may not vector-match the query/job and would otherwise be dropped from a strict top-K selection. Cost is one extra chunk per call. Cheap insurance against losing the "who is this person at a glance" read.

**5. K is configurable. Default 8.**

Surface as `settings.RERANK_RESUME_CHUNK_TOP_K` in `app/config.py` alongside the other tunables (`RESUME_CHUNK_SIZE`, `EMBEDDING_DIMENSIONS`, etc), defaulting to 8.

Why 8 and not Phase 4 RAG's 5: RAG-style Q&A answers *one question* from cited passages; 5 chunks of context is plenty. Rerank judges *whole-person fit against a whole role*; it benefits from broader surface area. 8 chunks × ~600 chars ≈ 5 KB of resume — a meaningful trim from the ~6 KB full resume but enough material for the LLM to form a complete view. Configurable so this can be raised or lowered without a code change if the score quality regresses or if we want to push further on speed/cost.

**6. Model and output-token cap unchanged.**

Earlier proposals suggested switching the rerank model to `gemini-2.5-flash-lite` and capping `max_output_tokens=200`. We are deliberately *not* doing those here — the top-K change is sufficient and avoids any quality-vs-speed tradeoff on the model side. We keep `gemini-2.5-flash` and the existing prompt as-is.

### Why this scales

For a recruiter pool of N applications:

| Operation | Pre-5.2 input tokens | Post-5.2 input tokens | Reduction |
|---|---|---|---|
| Search request (1 query × 10 candidates) | 10 × ~6 KB = ~60 KB resume + prompt | 10 × ~5 KB = ~50 KB resume + prompt | ~15% |
| Per-application cross-job match | ~10 × ~6 KB = ~60 KB | ~10 × ~5 KB = ~50 KB | ~15% |
| Bulk recheck for 100 applications | ~100 × 60 KB = ~6 MB | ~100 × 50 KB = ~5 MB | ~15% |

The token-count savings look modest on paper because resumes happen to be short. The structural win is what matters: **we stop sending data we already know is irrelevant to the LLM's decision**. As resumes grow (the system has no upper bound on resume length), the savings compound — a 30-chunk resume drops from ~18 KB to ~5 KB per call, a ~70% reduction.

The bigger latency win comes from less input → faster time-to-first-token. Per-call rerank latency typically falls from 2–4 s to 1–2 s; total search round-trip should land around 2 s instead of 3–5 s.

### Implementation plan

1. **`app/config.py`** — add `RERANK_RESUME_CHUNK_TOP_K: int = 8` in the chunking-strategy section. Settings reload from env, so deployment can override without code change.
2. **`app/main.py`** — replace `_fetch_full_resume_text(session, application_ids)` with `_fetch_top_resume_chunks(session, application_ids, query_vector, top_k)`. Single SQL with `ROW_NUMBER() OVER (PARTITION BY application_id ORDER BY embedding <=> :query_vector)` plus a chunk-0 union; return `{app_id: text}` where each text is chunks joined in original chunk-index order.
3. **`app/worker.py`** — replace `_build_resume_text(session, application_id)` with `_build_top_resume_chunks(session, application_id, job_id, top_k)`. Per-pair query that ranks resume chunks by best cosine distance to *any* chunk of `job_id`, includes chunk 0, returns concatenated text in chunk-index order. `match_jobs_task` calls this once per (application, candidate-job) pair after the pre-filter survives.
4. **Smoke tests** — `scripts/smoke_test_phase5.py` re-runs unchanged and must still assert the Python-vs-Java case scores < 50 and the Python-vs-Python case scores ≥ 60. If either assertion regresses, we've trimmed too aggressively and need to raise K. (Smoke test is the calibration gate.)
5. **Docs** — flip this 5.2 section to Complete with a record of what shipped where. No change needed to phase-2 or phase-3 docs — the public behaviour is unchanged; only the LLM-input composition changed.

### Estimated effort

~1 hour including SQL design, two helpers, and re-running smoke tests.

### Status

Complete. Shipped in:

- `app/config.py` — new `RERANK_RESUME_CHUNK_TOP_K: int = 8` setting (env-overridable).
- `app/main.py` — `_fetch_full_resume_text` replaced by `_fetch_top_resume_chunks(session, application_ids, query_vector, top_k)`. Single SQL with `ROW_NUMBER() OVER (PARTITION BY application_id ORDER BY embedding <=> :query_vector)` plus a `rank <= :top_k OR chunk_index = 0` filter; returns `{app_id: text}` joined in original chunk-index order. Call site in `search_candidates` passes `settings.RERANK_RESUME_CHUNK_TOP_K`.
- `app/worker.py` — `_build_resume_text` replaced by `_build_top_resume_chunks(session, application_id, job_id, top_k)`. Cross-join against `jobembedding` ranks resume chunks by `MIN(distance)` to any chunk of the candidate job; chunk 0 is always included. `match_jobs_task` now calls this per (application, candidate-job) pair so each pair gets a job-specific top-K slice.
- `scripts/smoke_test_phase5.py` — new Test 2b asserts `_build_top_resume_chunks` returns non-empty text within a K+1 chunk ceiling, guarding against an accidental "concatenate everything" regression. The existing Python-vs-Java assertion remains the calibration gate for "K is not too low."

No public API or schema change. Phase 1 (initial scoring) stays full-text by design.

---

## Appendix — Batching: why one call per (query, candidate), not one megaprompt

A natural question once the cost picture is clear: *"Phase 2 makes 10 Gemini calls per search. Phase 3 makes 10 per match task. Couldn't we collapse those into a single 'megaprompt' that scores all 10 candidates in one shot?"*

We deliberately do not. There are two batching levels worth distinguishing.

### Level 1 — chunks within one candidate's prompt

We **already** concatenate the top-K chunks into a single prompt per (query, candidate) pair. We do not make per-chunk Gemini calls. The chunking infrastructure feeds *retrieval* (which chunks are relevant) but the LLM input is always a single text blob per scoring decision. See [`_fetch_top_resume_chunks`](../../app/main.py), [`_build_top_resume_chunks`](../../app/worker.py), [`_score_one`](../../app/rerank.py).

### Level 2 — candidates within one Gemini call (the megaprompt question)

Today:

- **Phase 2 search**: 10 parallel Gemini calls via `asyncio.gather`, one per pre-filter survivor. Total latency ≈ slowest single call (~2–3 s p95).
- **Phase 3 match**: 10 sequential Gemini calls via `rerank_sequential`. Total ~15–25 s, but it's a Celery task — not user-facing.
- **Phase 4 chat**: one call per question — already a single LLM round-trip with top-5 chunks. The right shape for grounded Q&A.

A megaprompt approach would send `(query + 10 candidate resumes, each tagged)` to one Gemini call and ask for 10 (score, critique) pairs in a single JSON response. We chose not to. The reasoning:

#### Performance — counterintuitive, but megaprompt is *slower*

LLM **input** processing (prefill) is parallelisable on Gemini's side. LLM **output** generation is fundamentally sequential — autoregressive, one token at a time, ~50–80 tokens/s for `gemini-2.5-flash`.

Per scoring decision the model emits ~150 output tokens (score + ~300-char critique). Numbers:

- **Parallel today** — 10 calls each emitting ~150 tokens, parallelised → total wall-clock ≈ max of 10 ≈ **2–3 s**.
- **Megaprompt** — 1 call emitting ~1500 tokens sequentially → **~20–30 s** of pure generation, plus a larger prefill on ~50 KB of resume text.

The batched approach trades fan-out latency (which Gemini absorbs for free) for serialised output latency (which we pay for token-by-token). The math goes the wrong way.

#### Quality — the bigger problem

Multiple independent failure modes argue for sandboxed scoring:

1. **Attention dilution / "lost in the middle"** — ~5 KB × 10 candidates ≈ 50 KB of resume text in one prompt. The 1M-token nominal context is real, but *effective* attention drops well before the limit. Candidates rendered later in the prompt are routinely under-weighted.
2. **Cross-contamination** — with ten resumes co-located, the model can subtly conflate them (*"Alice mentioned Kafka, so Bob's Python stack is also event-driven"*). Independent scoring sandboxes each judgment to one candidate.
3. **Scoring drift / calibration shift** — when the model emits N decisions in sequence, its yardstick adjusts as it generates. Candidate 1 may be scored 70 and Candidate 10 scored 50 *because the model recalibrated mid-response*, not because of the underlying fit. Well-documented in LLM-as-judge research.
4. **All-or-nothing failure** — one malformed JSON token from the model, one mid-response truncation, one hallucinated structure, and **all 10 scores are unusable**. Today a single failed call only loses that candidate; the rest still surface.
5. **No streaming / partial results** — with per-candidate calls we can in principle stream results as each completes (future polish). A megaprompt has no usable output until the whole response lands.

This is the same reason every production rerank library — Cohere Rerank, BGE, Voyage, Anthropic's claude-3-rerank — scores *one (query, candidate) pair at a time* even though they could batch on their backends. The architecture is not an accident.

#### Cost — marginally favours batching, irrelevant in practice

Megaprompt would:

- **Use 1 request** against the 15 RPM / 1500-per-day free-tier cap instead of 10. Real RPM-budget benefit at scale.
- **Save HTTP/TLS overhead** for 9 round-trips. Minor in absolute terms.
- **Lose prompt-prefix caching** (when enabled): per-call prompts share the same instruction prefix, so Gemini can cache it; a megaprompt is unique per request, no cache hit.

Total tokens-per-judgment is roughly equal between the two modes. The free-tier RPM ceiling is real — but the right answer there is upgrading the tier or trimming the pool, not sacrificing quality.

#### Summary table

| Dimension                       | Per-candidate parallel (today) | Batched megaprompt |
|---------------------------------|--------------------------------|--------------------|
| Wall-clock latency (Phase 2)    | ~2–3 s (max of 10)             | ~20–30 s (sequential output) |
| Per-judgment quality            | High (sandboxed)               | Lower (drift, contamination, lost-in-middle) |
| Failure mode                    | Per-candidate degradation      | All-or-nothing                 |
| Free-tier RPM usage             | 10 / minute                    | 1 / minute ✓                   |
| Prompt-cache fit                | Reusable prefix ✓              | Unique per request             |
| Future streaming UX             | Possible                       | Impossible                     |

#### When megaprompts *are* the right call

Phase 4 RAG chat: one question, top-K chunks, one user-visible streamed answer. We already do this. Don't undo it. The relevant batching unit there is *chunks for one decision*, not *candidates for many decisions*.

#### Recommended response if RPM becomes painful

In order of leverage, before reaching for megaprompts:

1. **Smaller K on the pre-filter.** Already at 10 after [Phase 5.1](#follow-up-51--search-latency).
2. **Aggressive Redis caching.** Already in place — keys are `rerank:{app_id}:{sha256(query+candidate)}`, 1 h TTL.
3. **Paid Gemini tier.** Removes the 15 RPM ceiling entirely.
4. **Partial batching as a deliberate slider.** Only after the above — e.g., 2 candidates per call to halve calls while keeping batches small enough to preserve quality. Never as the default.

Megaprompts look clever on a whiteboard and get worse on the actual numbers for *scoring* workloads. They are the right tool for *generation* workloads (Phase 4). The architecture intentionally uses each in its right place.
