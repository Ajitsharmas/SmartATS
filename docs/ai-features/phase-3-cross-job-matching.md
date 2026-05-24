# Phase 3 — Cross-Job Matching

When a candidate applies to one job, the system automatically evaluates them against every other open role owned by the same recruiter and surfaces alternative matches on the dashboard. This phase reuses the embedding pipeline from Phase 1 and the vector-search machinery from Phase 2 — applied in the opposite direction (candidate → jobs rather than query → candidates).

For the overall roadmap, see [roadmap.md](roadmap.md). Depends on [Phase 1](phase-1-embedding-pipeline.md) (resume embeddings) and [Phase 2](phase-2-semantic-search.md) (vector-search patterns).

---

## Goal

A candidate applies for *"Senior Frontend Engineer"* and gets a score of 65. The system notices they're actually a better fit for *"Tech Lead"* (which the same recruiter has posted) at 0.89 cosine similarity, and surfaces a recommendation in the dashboard: *"This candidate is also a strong match for your Tech Lead role — consider routing them there too."*

---

## Acceptance criteria

- Jobs get chunked and embedded into `job_embedding` on creation, and re-embedded when scoring-relevant fields change
- After scoring + resume embedding complete for a new application, the system computes the candidate's top alternative job matches within the same recruiter's pool
- Matches are persisted in a new `cross_job_match` table
- Two manual triggers are available to the recruiter: per-candidate recheck and bulk "re-match every applicant"
- Dashboard surfaces matches in two places: a compact badge on the applicants row and a detailed section inside the candidate analysis modal
- Multi-tenancy is enforced at the SQL layer — matches never cross recruiter boundaries
- All match-related Celery tasks integrate with the Phase 1 failure-tracking + retry mechanism

---

## Decisions

### 1. Job re-embedding triggers — Option C (scoring-relevant fields only)

Jobs get embedded on creation, and re-embedded on edit **only when one of the scoring-relevant fields changes** — the same set the existing rescore mechanism watches: `title`, `description`, `skills`, `location`. Salary changes do not trigger re-embedding (they have no effect on semantic match).

This mirrors the existing rescore-on-edit logic exactly, keeping the system consistent and predictable.

### 2. Match timing — Option A (compute at application time) + manual recheck

The default flow is automatic at application time:

```
Candidate applies → analyze_resume_task + embed_resume_task (parallel)
                                                │
                                                ▼ (chained)
                                       match_jobs_task → cross_job_match rows
```

Plus **two manual override paths** for freshness:

- **Per-candidate recheck** — a button on the candidate analysis modal dispatches `match_jobs_task` for that single application
- **Bulk recheck (new)** — a button on the dashboard dispatches `match_jobs_task` for **every application** belonging to jobs the recruiter owns. Useful after posting a batch of new jobs

This combination keeps the default cost predictable while giving the recruiter explicit control when they want freshness. Background auto-refresh (e.g. via Celery Beat) is deliberately not added — the manual controls cover the use case without the operational complexity of a periodic job.

### 3. Match score aggregation — Option C (top-3-chunk average)

For each `(application, candidate_job)` pair:

1. For every resume chunk, find its best-matching chunk within the candidate job
2. Take those similarity scores
3. Keep the top 3, average them

This requires the candidate to have **at least three strong points of overlap** with the job, not just one fluke chunk. It also avoids the dilution caused by averaging across all chunks (which would penalise long, varied resumes).

### 4. Match threshold — 0.7 minimum

Only matches with aggregate similarity ≥ 0.7 are stored and surfaced. Higher than search's 0.6 threshold because:

- A cross-job suggestion is a stronger claim than a search hit ("this candidate fits another role you posted" vs "this candidate's resume mentions something relevant to your query")
- Showing weak matches dilutes the signal and trains the recruiter to ignore the feature

If no jobs clear 0.7, the candidate gets no match badge and no "Also a good fit for" section. Better silent than noisy.

### 5. Top-N — 3 matches per candidate

Three is the right number to avoid clutter while giving the recruiter genuine alternatives to consider.

### 6. Self-match exclusion

The candidate's own application's job is excluded from match candidates (`cand.id != orig.id`). We do not also exclude jobs where the candidate previously applied — that's a deeper feature requiring `(candidate_email, job_id)` lookups across applications, which can be added later if recruiters report seeing redundant suggestions.

### 7. Storage shape — dedicated `cross_job_match` table

A separate table rather than columns on `Application` or `JobListing`:

- Decouples the match data from the application's processing lifecycle (re-matching does not touch the application row)
- Many-to-many relationship needs its own table
- Enables future features like match history, trending, or comparison

Schema:

| Column | Type | Purpose |
|---|---|---|
| `id` | int (PK) | — |
| `application_id` | int (FK, CASCADE) | The candidate's application |
| `matched_job_id` | int (FK, CASCADE) | An alternative job in the recruiter's pool |
| `similarity` | float | Aggregate score (top-3-chunk average) |
| `created_at` | datetime | When the match was computed |

`UNIQUE (application_id, matched_job_id)` — one row per pair.

Cascade behaviour:
- Deleting a job → removes any `cross_job_match` rows where it was a candidate match (CASCADE on `matched_job_id`)
- Deleting an application → removes all of its match rows (CASCADE on `application_id`)

### 8. Failure tracking

New column on `Application`: `matching_error: str | None`. Populated by the `MatchingTask.on_failure` hook (analogous to `scoring_error` and `embedding_error`). The retry endpoint extends to handle this new error column.

### 9. `match_jobs_task` retry policy — `max_retries=1`

`match_jobs_task` is pure SQL — no external API calls. Failures are unlikely to be transient (most would be DB-related: deadlock, connection pool exhaustion, schema drift). A single retry catches transient DB issues without burning resources retrying real bugs.

```python
max_retries=1, retry_backoff=True
```

After one failed retry, `on_failure` populates `matching_error` and the recruiter sees the failed state on the dashboard with a retry button.

### 10. `embed_job_task` retry policy — same as `embed_resume_task`

`autoretry_for=(EmbeddingError,)`, exponential backoff, `max_retries=4`. Same Gemini transient-failure handling.

---

## Rate limiting decisions

All new endpoints are protected and rate-limited via SlowAPI. Limits chosen based on the cost profile of each endpoint.

| Endpoint | Limit | Key | Rationale |
|---|---|---|---|
| `GET /applications/{id}/matches` | 60 / minute | per user | Read-only DB query; cheap; can be polled |
| `POST /applications/{id}/match-refresh` | 10 / minute | per user | Dispatches one Celery task; matches the search endpoint's cadence |
| `POST /matches/refresh-all` | 2 / hour | per user | Heavy operation — re-dispatches matching for every applicant in the recruiter's pool. A genuine bulk action that should not be run often |

The bulk endpoint's `2/hour` limit is strict by design. With hundreds of applications, one bulk recheck queues hundreds of Celery tasks; multiple invocations within minutes would overwhelm the worker pool with redundant work.

---

## Architecture

### Task topology

```
On application submit (POST /process):

    analyze_resume_task ──┐
                          │   (parallel, independent)
    embed_resume_task ────┤
                          │
                          ▼ (chained: match runs AFTER embedding completes)
                  match_jobs_task → cross_job_match rows


On job create/edit (POST /jobs, PATCH /jobs/{id}):

    embed_job_task → jobembedding rows


On manual recheck (POST /applications/{id}/match-refresh):

    match_jobs_task → cross_job_match rows (idempotent overwrite)


On bulk recheck (POST /matches/refresh-all):

    For each application in recruiter's pool:
        match_jobs_task → cross_job_match rows
```

### Why `match_jobs_task` is **chained** after `embed_resume_task` and not parallel

`match_jobs_task` reads from `resume_embedding`. If it ran in parallel with `embed_resume_task`, it would find an empty table and produce no matches. We use Celery's `chain` primitive to enforce ordering:

```python
from celery import chain

chain(
    embed_resume_task.s(text_content=..., application_id=...),
    match_jobs_task.si(application_id=...),
).apply_async()
```

`.si()` (immutable signature) tells Celery not to pass the previous task's return value as input to the next. The chain only runs `match_jobs_task` after `embed_resume_task` succeeds; if embedding fails, matching is never attempted.

---

## SQL for computing matches

```sql
WITH chunk_pairs AS (
    -- Every resume chunk × every job chunk for OTHER jobs in the same recruiter's pool
    SELECT
        je.job_id,
        re.id AS resume_chunk_id,
        1 - (re.embedding <=> je.embedding) AS pair_similarity
    FROM resumeembedding re
    JOIN application a   ON a.id = re.application_id
    JOIN joblisting orig ON orig.id = a.job_id            -- the job they applied to
    JOIN joblisting cand ON cand.owner_id = orig.owner_id -- same recruiter
                        AND cand.id != orig.id            -- exclude their own job
    JOIN jobembedding je ON je.job_id = cand.id
    WHERE a.id = :application_id
),
best_per_resume_chunk AS (
    -- For each (resume chunk, candidate job), keep the best matching job chunk
    SELECT job_id, resume_chunk_id, MAX(pair_similarity) AS best_similarity
    FROM chunk_pairs
    GROUP BY job_id, resume_chunk_id
),
ranked_chunks AS (
    -- Within each job, rank resume chunks by how well they match
    SELECT
        job_id,
        best_similarity,
        ROW_NUMBER() OVER (
            PARTITION BY job_id
            ORDER BY best_similarity DESC
        ) AS chunk_rank
    FROM best_per_resume_chunk
)
SELECT
    job_id,
    AVG(best_similarity) AS aggregate_similarity
FROM ranked_chunks
WHERE chunk_rank <= 3                    -- top-3 average (Option C)
GROUP BY job_id
HAVING AVG(best_similarity) >= 0.7       -- threshold
ORDER BY aggregate_similarity DESC
LIMIT 3;                                 -- top-3 matches
```

### Walking through the query, piece by piece

The query has four logical stages, three of them inside CTEs and one as the final outer `SELECT`. Each stage transforms the previous one. The structure mirrors the conceptual question: *for each candidate job in the recruiter's pool, how strongly does this candidate's resume align with that job, when we look only at the candidate's most relevant points of overlap?*

#### Stage 1 — `chunk_pairs` CTE: the raw resume-chunk × job-chunk similarity grid

```sql
WITH chunk_pairs AS (
    SELECT
        je.job_id,
        re.id AS resume_chunk_id,
        1 - (re.embedding <=> je.embedding) AS pair_similarity
    FROM resumeembedding re
    JOIN application a   ON a.id = re.application_id
    JOIN joblisting orig ON orig.id = a.job_id
    JOIN joblisting cand ON cand.owner_id = orig.owner_id
                        AND cand.id != orig.id
    JOIN jobembedding je ON je.job_id = cand.id
    WHERE a.id = :application_id
)
```

This stage produces the cartesian product of `(resume chunks of this application) × (chunks of every candidate alternative job in the recruiter's pool)`, with a similarity score on each pair. The joins:

- **`resumeembedding re`** — the candidate's resume chunks. Multiple rows per application.
- **`JOIN application a ON a.id = re.application_id`** — link each chunk back to its application so we can find the parent job.
- **`JOIN joblisting orig ON orig.id = a.job_id`** — `orig` is the job the candidate *actually applied to*. We need this only to (a) determine which recruiter owns it and (b) exclude it from the candidate-job set.
- **`JOIN joblisting cand ON cand.owner_id = orig.owner_id AND cand.id != orig.id`** — `cand` is a candidate alternative job. Two filter conditions: same owner (multi-tenancy enforcement), different job (no self-match). This single join clause is what guarantees both security and correctness.
- **`JOIN jobembedding je ON je.job_id = cand.id`** — the actual embedding rows of the candidate jobs we'll compare against.
- **`WHERE a.id = :application_id`** — restrict the whole computation to one candidate's application.

**The similarity expression** — `1 - (re.embedding <=> je.embedding)` — converts pgvector's cosine *distance* (output of `<=>`, range `[0, 2]`) to cosine *similarity* (range `[-1, 1]`, usually `[0, 1]` in practice for normalised embeddings). The convention everywhere else in the app uses similarity, so we convert here at the source.

**Concrete shape after this stage** — for one application with 10 resume chunks and 2 candidate jobs (5 chunks each), this produces `10 × 2 × 5 = 100` rows: one row per resume chunk per job chunk per candidate job.

**Where the HNSW indexes activate** — the `re.embedding <=> je.embedding` operation references two indexed vector columns. Postgres uses both HNSW indexes to skip full-table scans, which is essential for performance at scale.

#### Stage 2 — `best_per_resume_chunk` CTE: collapse to "best alignment per (resume chunk, candidate job)"

```sql
best_per_resume_chunk AS (
    SELECT job_id, resume_chunk_id, MAX(pair_similarity) AS best_similarity
    FROM chunk_pairs
    GROUP BY job_id, resume_chunk_id
)
```

The previous stage produced multiple rows per `(candidate job, resume chunk)` pair — one for each chunk of that candidate job. This stage collapses them, keeping only the highest similarity.

**Why this matters semantically** — a resume chunk that mentions *"led a team of 8 engineers"* might align strongly with a job chunk about *"team leadership"* and only weakly with a job chunk about *"AWS infrastructure"*. We only care about the strongest signal — that resume chunk *does* express leadership, regardless of whether other job chunks happened to be irrelevant. Taking `MAX` over the job chunks captures this correctly.

**Concrete shape** — the 100 rows from Stage 1 become `10 × 2 = 20` rows: one row per resume chunk per candidate job, each carrying the "best part of that job this resume chunk matches".

#### Stage 3 — `ranked_chunks` CTE: rank resume chunks within each candidate job

```sql
ranked_chunks AS (
    SELECT
        job_id,
        best_similarity,
        ROW_NUMBER() OVER (
            PARTITION BY job_id
            ORDER BY best_similarity DESC
        ) AS chunk_rank
    FROM best_per_resume_chunk
)
```

`ROW_NUMBER()` is a **window function** — it computes a value per row without collapsing the result set. Here it ranks the resume chunks against each candidate job:

- **`PARTITION BY job_id`** — start a fresh ranking for each candidate job (so resume chunk rankings for job A don't interfere with job B).
- **`ORDER BY best_similarity DESC`** — rank 1 goes to the resume chunk that aligned most strongly with that job, rank 2 to the next strongest, and so on.

**Concrete shape** — the 20 rows stay 20 rows; we've just added a `chunk_rank` column. For each candidate job, ranks run `1, 2, 3, ..., 10` (one rank per resume chunk).

**Why we need this ranking** — the final aggregation only averages the top-K resume chunks per candidate job. Without ranking we'd average all chunks, which dilutes the score with chunks that are weakly relevant to that particular job.

#### Stage 4 — outer `SELECT`: top-K average per candidate job, threshold, and final ordering

```sql
SELECT
    job_id,
    AVG(best_similarity) AS aggregate_similarity
FROM ranked_chunks
WHERE chunk_rank <= :top_k
GROUP BY job_id
HAVING AVG(best_similarity) >= :min_similarity
ORDER BY aggregate_similarity DESC
LIMIT :top_n
```

This is where the final score is computed and the result is shaped.

- **`WHERE chunk_rank <= :top_k`** — keep only the top K resume chunks (K=3) per candidate job. The other 7 resume chunks per job are dropped — they didn't make the cut for what this candidate offers to *this* role.
- **`GROUP BY job_id`** — one row per candidate job.
- **`AVG(best_similarity)`** — average the top-K similarities. This is the aggregate match score for that candidate job. Because the top-K are by definition the highest, the average is a fair representation of "how strong are the candidate's best signals for this job".
- **`HAVING AVG(...) >= :min_similarity`** — threshold filter (0.7). Jobs whose top-3-chunk average doesn't clear this are dropped.
- **`ORDER BY aggregate_similarity DESC LIMIT :top_n`** — sort by score and keep the top N (3) jobs.

**Concrete shape** — at most 3 rows, each `(job_id, aggregate_similarity)`. The application layer joins these against `joblisting` to get the human-readable job title before returning to the frontend.

### Why this aggregation strategy (top-K-of-best-per-chunk average)?

There are several ways to roll up many chunk-pair similarities into a single match score. The chosen approach has specific properties:

| Strategy | Pro | Con |
|---|---|---|
| Average over **all** chunk pairs | Smooth, easy to explain | Long, varied resumes with one strong section get diluted by irrelevant sections |
| Single `MAX` over all pairs | Picks up one strong signal | One fluke alignment dominates — false positives |
| **Top-K of best-per-resume-chunk, then average (chosen)** | Requires K distinct strong points of overlap; balanced against noise | Slightly more complex SQL |

The top-K approach essentially asks: *"Does this resume have at least three different points of overlap with this job, all of them strong?"* That's a much more defensible signal of fit than either a single strong match or a smeared average.

### Why the joins happen inside the CTE, not at the end

The owner-scope filter (`cand.owner_id = orig.owner_id`) and the self-exclusion (`cand.id != orig.id`) live in Stage 1, before any aggregation or window function runs. Three reasons:

1. **Multi-tenancy enforced at the database layer.** Even if a future bug let a request reach this query with the wrong application ID, the joins still exclude jobs that don't belong to the same recruiter. The data simply cannot be returned.
2. **Smaller working set for the window function.** Stage 3's `ROW_NUMBER` is the most computationally interesting step. By filtering down to the relevant recruiter and excluding the self-job before that point, we keep the partitions small.
3. **Composability with HNSW.** The HNSW indexes are most effective when Postgres can push the cosine-distance computation against a pre-filtered candidate set rather than computing similarities and then filtering.

### Indexes that make this query fast

| Index | What it accelerates |
|---|---|
| HNSW on `resumeembedding.embedding` | The `<=>` operator in Stage 1 — looking up nearby vectors instead of scanning every row |
| HNSW on `jobembedding.embedding` | Same, for the job side of the join |
| B-tree on `application.id`, `joblisting.id`, `joblisting.owner_id` | Fast joins on the parent rows. These already exist from the original schema. |
| B-tree on `resumeembedding.application_id` | Restricts Stage 1 to the chunks of a single application — created automatically because the column is FK-indexed in `models.py` |

### Performance characteristics

For one application against a recruiter pool of 50 jobs (~250 job chunks total) and a 10-chunk resume:

| Step | Approx. cost |
|---|---|
| Stage 1 cartesian product | ~2,500 chunk-pair similarity computations, accelerated by HNSW; sub-100ms in practice |
| Stage 2 group + MAX | ~500 row scans, in-memory; negligible |
| Stage 3 window function | ~500 row scans; negligible |
| Stage 4 GROUP BY + ORDER BY | ~50 group rows; negligible |

End-to-end runtime is dominated by Stage 1 (the vector math). The whole query typically completes in tens of milliseconds — well under the threshold where it would be worth caching results.

### Multi-tenancy guarantee — restated

The owner-scope filter (`cand.owner_id = orig.owner_id`) is enforced **inside the first CTE**, before any aggregation or matching happens. A recruiter cannot see another recruiter's jobs as match candidates even if the application/job IDs were tampered with — the database query will not return them. The Phase 3 smoke test asserts this explicitly.

---

## Schemas

```python
class CrossJobMatch(SQLModel, table=True):
    """A computed match between an application and an alternative job posting."""
    __table_args__ = (
        UniqueConstraint("application_id", "matched_job_id", name="uq_cross_job_match"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    application_id: int = Field(
        sa_column=Column(Integer, ForeignKey("application.id", ondelete="CASCADE"), nullable=False, index=True)
    )

    matched_job_id: int = Field(
        sa_column=Column(Integer, ForeignKey("joblisting.id", ondelete="CASCADE"), nullable=False, index=True)
    )

    similarity: float
    created_at: datetime = Field(default_factory=datetime.now)


class CrossJobMatchResult(SQLModel):
    """Response shape for cross-job-match listings on the dashboard."""
    matched_job_id: int
    job_title: str
    similarity: float
```

Plus the `matching_error: str | None` column on `Application`.

---

## Files to create / modify

| File | Change |
|---|---|
| `app/models.py` | Add `CrossJobMatch` table, `CrossJobMatchResult` schema, `matching_error` column on `Application` |
| `app/worker.py` | Add `JobEmbeddingTask` and `MatchingTask` failure-tracking base classes; `embed_job_task` (chunk + embed); `match_jobs_task` (run cross-match SQL) |
| `app/main.py` | Wire `embed_job_task` into `POST /jobs` and `PATCH /jobs/{id}` (Option C field check). Chain `match_jobs_task` after `embed_resume_task` in `POST /process`. Extend `POST /applications/{id}/retry` for `matching_error`. New endpoints: `GET /applications/{id}/matches`, `POST /applications/{id}/match-refresh`, `POST /matches/refresh-all`. |
| `app/static/dashboard.html` | Match-count badge on the applicants row; "Also a good fit for" section inside the analysis modal; per-candidate recheck button in the modal; bulk recheck button in the left panel |
| `app/static/js/api.js` | `Api.getMatches(applicationId)`, `Api.refreshMatches(applicationId)`, `Api.refreshAllMatches()` |
| `scripts/smoke_test_phase3.py` | End-to-end test including multi-tenancy and bulk-recheck path |

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Cross-recruiter data leakage | Owner-scope filter (`cand.owner_id = orig.owner_id`) enforced inside the SQL CTE |
| Self-match (suggesting the job they applied to) | `cand.id != orig.id` filter inside the CTE |
| Matching runs before resume embedding is complete | Celery `chain` enforces ordering; matching only fires after embedding succeeds |
| Job description too short to embed | Skip embedding if `len(text.strip()) < 50`, same pattern as resume embedding |
| Recruiter has only one job | Cross-match query returns zero rows; no badge surfaces — no clutter |
| Bulk recheck overwhelms worker pool | `2/hour` rate limit; idempotent task (delete-then-insert) tolerates concurrent runs gracefully |
| Stale matches after new job added | Recruiter clicks per-candidate recheck or the bulk recheck button |
| `match_jobs_task` hits a real bug | Single retry, then `matching_error` set, dashboard shows "Failed" with retry button via existing recovery flow |

---

## Scalability notes

Cost characteristics of the matching pipeline:

- **No Gemini calls in matching itself** — `match_jobs_task` is pure pgvector + SQL. Fast and free per invocation.
- **Match compute** is O(R × J × C) where R = resume chunks (~10), J = jobs in the recruiter's pool (typically tens), C = job chunks (~5). Hundreds of jobs still complete well under one second per application.
- **`embed_job_task`** is one small batched Gemini call per job. Free tier handles this comfortably.
- **Bulk recheck cost** scales linearly with applicant count — N applications produces N queued tasks. Worker pool drains in batches; total wall time depends on Celery concurrency. The `2/hour` rate limit prevents runaway dispatch.

No new infrastructure. Uses the existing Celery worker pool and pgvector indexes from Phase 0.

---

## Smoke test — `scripts/smoke_test_phase3.py`

After Phase 3 lands:

1. Create recruiter A with two jobs: a Frontend Engineer role and a Tech Lead role with strong leadership/architecture emphasis
2. Create one candidate applying to Frontend Engineer with a resume that emphasises leadership and architecture experience
3. Embed both jobs via `embed_job_task`, embed the resume via `embed_resume_task`
4. Run `match_jobs_task` for the application
5. Assert: at least one row in `cross_job_match` pointing to Tech Lead (not Frontend, which they applied to)
6. Assert: similarity score ≥ 0.7
7. Create recruiter B with a similar role; embed it
8. Re-run `match_jobs_task` — confirm recruiter B's job is NOT in the matches (multi-tenancy)
9. Trigger bulk recheck logic — verify it dispatches a `match_jobs_task` per application owned by recruiter A
10. Cleanup via cascade delete

---

## Out of scope for Phase 3

- Excluding jobs the candidate previously applied to (requires `(candidate_email, job_id)` deduplication across applications) — deferred
- Background auto-refresh of matches when new jobs land (Celery Beat) — deferred; manual controls cover this
- Match score history / trending visualisation — deferred
- Cross-recruiter shared talent pool — never (requires org model + consent)
- Phase 4 (RAG Q&A) — separate phase

---

## Running the Phase 3 smoke test

After Phase 3 changes are in place, validate with `scripts/smoke_test_phase3.py`. The script creates two recruiters with three jobs (two for recruiter A, one for recruiter B), applies a leadership-heavy resume to recruiter A's frontend job, runs the embedding and matching pipeline, and asserts that:

- The matched alternative is the Tech Lead role (not the Frontend role the candidate applied to)
- Recruiter B's job never appears in recruiter A's match results (multi-tenancy)
- Re-running `match_jobs_task` is idempotent

### Steps (local dev)

```bash
# Restart the stack to pick up the new schema and tasks
docker compose down -v
docker compose up -d --build

# Run the smoke test from the local venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/smoke_test_phase3.py
```

Or inside the worker container:

```bash
docker compose exec worker python scripts/smoke_test_phase3.py
```

### Expected output

```
=== Phase 3 smoke test — cross-job matching ===

OK:   Database is initialised
OK:   Created recruiters, jobs, and application (Alice → Frontend, id=1)
OK:   Embedded all three jobs via embed_job_task
OK:   All three jobs have non-empty embedding rows
OK:   Embedded Alice's resume via embed_resume_task
OK:   match_jobs_task returned success with 1 matches
OK:   Self-match excluded: Frontend job (the one Alice applied to) is not in matches
OK:   Multi-tenancy enforced: recruiter B's job is not in recruiter A's match results
OK:   Tech Lead match present with similarity 0.812
OK:   Idempotency: re-running match_jobs_task produces the same number of matches
OK:   Cleaned up test recruiters, jobs, applications, embeddings, and matches via cascades

=== Phase 3 smoke test passed ===
```

If the multi-tenancy assertion ever fails, that is a security regression — investigate the owner-scope filter (`cand.owner_id = orig.owner_id`) in `CROSS_JOB_MATCH_SQL` immediately.

---

## Status

Design approved. Implementation complete. Smoke test ready to run.
