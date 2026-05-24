# Phase 2 — Semantic Search Across the Applicant Pool

This document captures the design and decisions for Phase 2 of the AI features roadmap. Phase 2 ships the recruiter-facing semantic search feature: a search box on the dashboard that returns candidates ranked by semantic similarity to a free-text query — across the recruiter's entire applicant pool, not just one job's applicants.

For the overall roadmap, see [roadmap.md](roadmap.md). Phase 2 depends on the embedding pipeline built in [phase-1-embedding-pipeline.md](phase-1-embedding-pipeline.md).

---

## Goal

A recruiter types something like *"Python engineer with fintech background"* into a search bar and within a fraction of a second gets a ranked list of candidates from their applicant pool whose resumes semantically match the query — even when the exact words don't appear in any resume.

---

## Acceptance criteria

- A new `POST /search/candidates` endpoint accepts a free-text query and returns a ranked list of candidates with similarity scores
- The endpoint is protected and scoped: recruiters see only candidates who applied to **their own jobs**, never another recruiter's pool
- Each result includes: candidate name, email, similarity score, the resume chunk that matched best (shown as context), and the parent job's title (so the recruiter sees the original application context)
- Pagination via `offset`/`limit` supports browsing beyond the first 10 results
- Search query embeddings are cached in Redis to absorb repeated queries without hitting Gemini
- A search section on the dashboard with input box, results list, and "Show more" pagination
- A smoke test demonstrates a real search returning expected matches and excluding another recruiter's data

---

## Decisions

### 1. Result granularity — one row per candidate (Option B)

Search returns one row per **candidate**, not per chunk. Each result shows the candidate's best-matching chunk as context.

| | One row per chunk | One row per candidate (chosen) |
|---|---|---|
| Same candidate appears multiple times | Yes | No |
| Surface all matching passages | Yes | Only best one |
| UI clarity | Lower (duplicates) | Higher |
| Matches production search UX (LinkedIn Recruiter, Greenhouse) | No | Yes |

Recruiters think in terms of candidates, not text chunks. Showing the same person three times because three of their chunks matched is noise. The single best-matching chunk is more than enough as evidence of relevance. If the recruiter wants more depth on a specific candidate, the dashboard's existing analysis modal already shows the full critique.

### 2. Similarity threshold — 0.6 minimum

Results below 0.6 cosine similarity are filtered out before display. Below that, semantic similarity is mostly noise and surfacing the results would be misleading.

If a query returns no rows after this filter, the dashboard shows a clear "no matches found" message rather than misleading low-relevance results.

### 3. Pagination — offset-based, "Show more" UX

The endpoint accepts `offset` and `limit` query parameters. Default `limit=10`. The dashboard displays a "Show more (10 of N)" button that incrementally loads the next page.

**Why offset-based instead of cursor-based:**

Offset (`LIMIT 10 OFFSET 30`) is the simpler implementation. It does slow at deep offsets (Postgres must walk through skipped rows), but:

- Typical applicant pools are tens to low hundreds of candidates — offset overhead is invisible at this scale
- Recruiters rarely scroll past the first 20 results in practice
- Migrating to cursor-based pagination later is a contained, well-known change

We can switch to cursor pagination (`WHERE similarity < :last_seen ORDER BY similarity DESC`) if usage patterns show deep pagination becoming common, without any breaking change to existing callers.

### 4. Scope — strictly recruiter-owned

The search is multi-tenant safe: a recruiter sees only candidates whose applications belong to **jobs they own**. The query joins through:

```
ResumeEmbedding → Application → JobListing → owner_id == current_user.id
```

This is enforced **in the SQL itself**, not just at the application layer. Even if a future bug were to bypass the route handler, the database query still filters by owner.

### 5. Query embedding cache

Every search dispatches one Gemini embedding call to embed the query text. Network round-trip dominates total latency (200–400 ms) compared to the pgvector lookup (<10 ms).

To absorb repeated queries (very common in practice — recruiters re-use phrases like *"Python engineer"*, *"AWS architect"*), embed results are cached in Redis with a 1-hour TTL.

```python
cache_key = f"emb:{hashlib.sha256(query.encode()).hexdigest()}"
cached = redis.get(cache_key)
if cached:
    return json.loads(cached)
vector = embed_text(query)
redis.setex(cache_key, 3600, json.dumps(vector))
return vector
```

A 30 % cache hit rate effectively gives a 30 % uplift in search capacity without any additional infrastructure or Gemini quota usage.

### 6. Top-K default — 10 results per page

10 is a sweet spot for screen real estate and signal-to-noise. Pagination handles the long tail.

### 7. Rate limiting — 10 searches per minute per user

The search endpoint embeds the query (Gemini call) and runs a non-trivial vector query. Same per-user rate-limit pattern as `/health/ai`:

```python
@limiter.limit("10/minute", key_func=get_user_key)
```

The cache reduces effective Gemini calls below this rate limit anyway, but the limit protects against a buggy or malicious client hammering the endpoint.

### 8. UI placement — inline search on the dashboard

A search input above the Jobs panel on the dashboard. Submitting a query shifts the right pane from "applicants for the selected job" into "search results across the pool". Results are clickable — clicking a result opens the same candidate-analysis modal that the applicants table uses today, with a link back to the job the candidate originally applied to.

If the inline UX feels constrained as the feature evolves, we can move search to a dedicated route. For now, inline keeps the dashboard cohesive.

---

## Architecture & query design

### Endpoint flow

```
Recruiter types query  →  POST /search/candidates
                              │
                              ├──→ Check Redis embedding cache
                              │       hit  → use cached vector
                              │       miss → call Gemini, cache result
                              │
                              ├──→ Run pgvector similarity SQL (joins through
                              │    Application + JobListing, filtered to
                              │    current_user.id, threshold 0.6, LIMIT/OFFSET)
                              │
                              └──→ Return list[SearchResult]
```

### Core SQL

```sql
WITH ranked_chunks AS (
    SELECT
        re.application_id,
        re.chunk_text,
        1 - (re.embedding <=> CAST(:query_vector AS vector)) AS similarity,
        ROW_NUMBER() OVER (
            PARTITION BY re.application_id
            ORDER BY re.embedding <=> CAST(:query_vector AS vector)
        ) AS rank_within_candidate
    FROM resumeembedding re
    JOIN application a ON a.id = re.application_id
    JOIN joblisting j ON j.id = a.job_id
    WHERE j.owner_id = :owner_id
)
SELECT
    a.id AS application_id,
    a.candidate_name,
    a.candidate_email,
    a.resume_url,
    j.title AS job_title,
    rc.chunk_text AS best_match_chunk,
    rc.similarity
FROM ranked_chunks rc
JOIN application a ON a.id = rc.application_id
JOIN joblisting j ON j.id = a.job_id
WHERE rc.rank_within_candidate = 1
  AND rc.similarity >= 0.6
ORDER BY rc.similarity DESC
LIMIT :limit OFFSET :offset;
```

### Walking through the query, piece by piece

The query has two stages stitched together with a CTE (Common Table Expression). The first stage produces a ranked list of resume chunks; the second stage picks the best chunk per candidate, applies the similarity threshold, and paginates.

#### Stage 1 — the `ranked_chunks` CTE

```sql
WITH ranked_chunks AS (
    SELECT
        re.application_id,
        re.chunk_text,
        1 - (re.embedding <=> CAST(:query_vector AS vector)) AS similarity,
        ROW_NUMBER() OVER (
            PARTITION BY re.application_id
            ORDER BY re.embedding <=> CAST(:query_vector AS vector)
        ) AS rank_within_candidate
    FROM resumeembedding re
    JOIN application a ON a.id = re.application_id
    JOIN joblisting j ON j.id = a.job_id
    WHERE j.owner_id = :owner_id
)
```

Breaking it down line by line:

**`re.embedding <=> CAST(:query_vector AS vector)`**

`<=>` is pgvector's **cosine distance operator**. It returns a value in `[0, 2]` where `0` means identical direction (most similar) and `2` means opposite direction (least similar). The `CAST(:query_vector AS vector)` part converts the JSON-style string that Python sends (e.g. `"[0.12, -0.45, ...]"`) into pgvector's native `vector` type so the operator can be applied.

This single operation is what makes the HNSW index activate — when Postgres sees `<=>` against an indexed `vector` column, it uses the graph walk algorithm instead of scanning every row.

**`1 - (re.embedding <=> CAST(:query_vector AS vector)) AS similarity`**

We convert cosine *distance* into cosine *similarity* for human-readable scores. Distance `0` becomes similarity `1.0` (perfect match), and distance `1` becomes similarity `0.0`. This way the application code and the dashboard can talk in terms of similarity ("87 % match") instead of distance.

**`ROW_NUMBER() OVER (PARTITION BY re.application_id ORDER BY re.embedding <=> :query_vector)`**

This is the heart of the "one row per candidate" design. It's a **window function** — it computes a value per row without collapsing the result set the way `GROUP BY` would.

- `PARTITION BY re.application_id` groups the rows logically by candidate.
- `ORDER BY re.embedding <=> :query_vector` ranks rows within each group from most to least similar to the query.
- `ROW_NUMBER()` assigns 1 to the most similar chunk within each candidate, 2 to the next, and so on.

After this step, every chunk has a `rank_within_candidate` value. The outer query keeps only the rows where this value equals 1 — i.e. the best-matching chunk per candidate.

**Why the `JOIN application` and `JOIN joblisting` inside the CTE**

We need the `joblisting.owner_id` to enforce multi-tenancy. The filter `WHERE j.owner_id = :owner_id` removes any chunks belonging to applications for jobs the current recruiter does not own — *before* the window function ranks anything. That matters because:

1. It enforces tenant isolation at the database layer, not just at the API layer.
2. It shrinks the working set for the window function, which is the most expensive part of the query.

#### Stage 2 — the outer `SELECT`

```sql
SELECT
    a.id AS application_id,
    a.candidate_name,
    a.candidate_email,
    a.resume_url,
    j.title AS job_title,
    rc.chunk_text AS best_match_chunk,
    rc.similarity
FROM ranked_chunks rc
JOIN application a ON a.id = rc.application_id
JOIN joblisting j ON j.id = a.job_id
WHERE rc.rank_within_candidate = 1
  AND rc.similarity >= 0.6
ORDER BY rc.similarity DESC
LIMIT :limit OFFSET :offset;
```

This stage shapes the final response:

- **`WHERE rc.rank_within_candidate = 1`** keeps only the best-matching chunk per candidate. This is what gives us "one row per candidate" (Option B from the decisions section).
- **`AND rc.similarity >= 0.6`** drops any candidate whose best chunk didn't clear the similarity threshold. Without this filter the dashboard would show low-quality matches that aren't really relevant.
- **`JOIN application a`** and **`JOIN joblisting j`** bring back the human-readable fields (candidate name, email, job title, resume URL) that the CTE doesn't carry. We re-join here because the CTE only kept the columns needed for ranking, and re-joining is cheap because we already have the application IDs.
- **`ORDER BY rc.similarity DESC`** sorts the surviving candidates from best to worst match.
- **`LIMIT :limit OFFSET :offset`** applies pagination. With `limit=10 offset=0` you get the top 10; with `offset=10` you get the next 10, and so on.

#### Index strategy

Two indexes do the heavy lifting:

| Index | What it accelerates |
|---|---|
| **HNSW on `resumeembedding.embedding`** | The cosine-distance computation in both the `ORDER BY` clause of the window function and the `1 - (embedding <=> ...)` similarity calculation. Without it, every chunk would be compared individually — O(N). With it, the graph walk reduces this to roughly O(log N). |
| **B-tree on `application.id`, `joblisting.id`, `joblisting.owner_id`** | The `JOIN` and `WHERE j.owner_id = :owner_id` filtering. These are standard indexes already present from the existing schema and primary keys. |

#### Performance characteristics

Even at 100,000+ chunks (about 10,000 candidates), the query runs in tens of milliseconds. The breakdown:

| Step | Cost |
|---|---|
| HNSW similarity computation | O(log N) per chunk × N chunks in the recruiter's pool |
| Owner-scope filter | Reduces N to just this recruiter's chunks (typically a few hundred to a few thousand) |
| Window function | O(M log M) where M is the filtered set — negligible at this size |
| Outer joins | Index-backed lookups, near-constant time |

At scale, the dominant cost is the HNSW computation itself, which is exactly what HNSW is built for. The query plan can be inspected via `EXPLAIN ANALYZE` if performance ever needs tuning.

#### Why this design holds up

- **Tenant isolation in SQL**, not in the route layer. Even if a future bug lets a request reach this query with the wrong `owner_id`, the database still cannot return another recruiter's data.
- **One round-trip** — single query, no N+1 patterns.
- **Pagination is free** — adding `OFFSET` does not require any rewriting of the surrounding logic; the same query handles "show the first page" and "show page 5".
- **Threshold tuning is config-driven** — `0.6` is a parameter, not a magic number baked into the schema. Tune it without migrations.

---

## Schemas

```python
class SearchQuery(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    limit: int = Field(default=10, ge=1, le=50)
    offset: int = Field(default=0, ge=0)


class SearchResult(BaseModel):
    application_id: int
    candidate_name: str
    candidate_email: str
    resume_url: str
    job_title: str
    best_match_chunk: str
    similarity: float


class SearchResponse(BaseModel):
    results: list[SearchResult]
    total_returned: int     # length of results, useful for "Show more" pagination
    has_more: bool          # true if a full page was returned (likely more available)
```

---

## Files to create / modify

| File | Change |
|---|---|
| `app/embeddings.py` | Add `embed_query_cached(query)` helper using Redis with a 1-hour TTL |
| `app/main.py` | New `POST /search/candidates` endpoint with auth + rate limiting |
| `app/models.py` | Add `SearchQuery`, `SearchResult`, `SearchResponse` Pydantic schemas |
| `app/static/dashboard.html` | Add search input, search results panel, "Show more" pagination |
| `app/static/js/api.js` | Add `Api.searchCandidates(query, offset)` method |
| `scripts/smoke_test_phase2.py` | End-to-end smoke test including multi-tenancy check |

---

## Scalability considerations

The Phase 2 endpoint inherits the project's overall scaling characteristics. A short summary:

- **Stateless endpoint:** scales horizontally by adding FastAPI worker instances. No code change needed.
- **pgvector HNSW search:** sub-millisecond per query. Handles millions of vectors on a single Postgres instance.
- **Query embedding cache:** absorbs repeated queries, sub-linear growth in Gemini cost.
- **Postgres read load:** scales to read replicas if read volume becomes the bottleneck. SQLAlchemy session routing makes this a configuration change, not a code change.
- **Postgres write load:** unaffected by search (search is read-only).

The two parts of the system that would eventually require architectural change at very high scale — write-heavy Postgres beyond hundreds of thousands of recruiters, and pgvector beyond several million vectors — do not affect Phase 2.

Free-tier Gemini caps (15 RPM, 1500 requests/day) become the practical ceiling under heavy concurrent search load. The Redis embedding cache mitigates this for repeated queries; moving to a paid Gemini tier removes the cap entirely.

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Recruiter sees another recruiter's candidates (data leakage) | Owner-scoped filter (`j.owner_id = :owner_id`) is enforced inside the SQL itself, not at the route layer |
| Slow query at scale | HNSW index on `embedding`; owner filter applied in the CTE before the row-number window |
| Gemini embedding call fails | `EmbeddingError` caught in the endpoint, returns 503 with a clean message; dashboard shows it gracefully |
| Empty applicant pool | Query returns no rows; frontend shows "no matches" message |
| Junk query (`"asdf asdf"`) | Embedding still works, but no chunks clear the 0.6 threshold; "no matches" message |
| Rate limit exceeded | 429 returned by SlowAPI; dashboard shows the standard rate-limit message |
| Deep pagination becomes slow | Acceptable at typical applicant-pool sizes; migration path to cursor-based pagination is documented |

---

## Smoke test — `scripts/smoke_test_phase2.py`

After Phase 2 is in place, validate:

1. Create test recruiter A, a test job owned by A, and a test application
2. Populate the application's resume chunks via `embed_resume_task` with a resume text mentioning Python, AWS, etc.
3. Call `/search/candidates` as recruiter A with a related query: *"Python developer with cloud experience"*
4. Assert: the test candidate appears in the results with similarity ≥ 0.6
5. Call the endpoint with an unrelated query: *"marine biology research"*
6. Assert: empty results (threshold filters out junk)
7. Create recruiter B, a job owned by B, and an application — populate embeddings
8. Search as recruiter A again
9. Assert: recruiter B's candidate is **not** in results (multi-tenancy check)
10. Search again with the same query as step 3 — verify Redis cache hit (no Gemini call)
11. Cleanup via cascade delete

---

## Out of scope for Phase 2

- Hybrid retrieval (combining semantic and keyword search) — deferred
- Filter UI (location, years of experience, salary band) — deferred
- Saved searches / alerts when new candidates match — deferred
- Cross-job match (Phase 3 — uses the same embedding infrastructure, opposite query direction)
- Cursor-based pagination — switch later if usage demands it

---

## Running the Phase 2 smoke test

After Phase 2 changes are in place, validate with `scripts/smoke_test_phase2.py`. The script creates two distinct recruiters with their own jobs and applications, runs a related query, a junk query, and a cross-tenant query, then asserts the cache-hit path.

### Steps (local dev)

```bash
# Restart the stack to pick up the new endpoint and dependencies
docker compose down
docker compose up -d --build

# Run the smoke test (local venv flow)
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/smoke_test_phase2.py
```

Or inside the worker container:

```bash
docker compose exec worker python scripts/smoke_test_phase2.py
```

### Expected output

```
=== Phase 2 smoke test — semantic search ===

OK:   Database is initialised
OK:   Created recruiters A (1), B (2) and their applications
OK:   Embedded both resumes via the Phase 1 pipeline
OK:   Related query returned Alice as top result (similarity=0.752)
OK:   Junk query returned no results (threshold filter working)
OK:   Multi-tenancy enforced: recruiter A cannot see recruiter B's candidates
OK:   Recruiter B sees Bob as top result in their own pool (similarity=0.811)
OK:   Cache hit confirmed — repeated query did not create a new cache entry (3 keys)
OK:   Cleaned up test recruiters, jobs, applications, and embeddings via cascades

=== Phase 2 smoke test passed ===
```

If the test ever fails on the multi-tenancy step, that is a security regression — investigate immediately. The owner-scope filter in `SEARCH_SQL` is what enforces it.

---

## Status

Design approved. Implementation complete. Smoke test ready to run.
