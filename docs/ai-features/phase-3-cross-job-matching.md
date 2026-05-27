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

### 3. Match score aggregation — bidirectional top-3-chunk average with harmonic mean

The algorithm went through one iteration. We started with a one-directional **resume → job** top-3-chunk average ("for each resume chunk, find its best matching job chunk, average the top three"). Testing surfaced a real flaw: adding more requirements to a job barely lowered the match score.

**Concrete failure case.** A Python/FastAPI resume scored 79% against a job requiring just Python and FastAPI. The recruiter edited the job to also require Java, Kafka, Docker, and Kubernetes — none of which the resume mentioned. After recheck, the score dropped to 78%. The added requirements had almost no effect on the score.

**Why this happens with one-directional aggregation.** The "best match per resume chunk" lookup is asymmetric. It measures *"are the candidate's strongest points represented somewhere in the job?"* — not *"are the job's requirements covered by the resume?"*. The Python and FastAPI resume chunks still align best to the Python and FastAPI job chunks (top-3 unchanged), and the new Java / Kafka / Docker / Kubernetes job chunks have no effect because nothing in the resume aligns to them. They simply don't get picked.

**The bidirectional fix.** Compute coverage in both directions and combine them:

| Direction | What it asks | How it's computed |
|---|---|---|
| **Resume → Job** (existing) | "What are the candidate's strongest points relative to this job?" | For each resume chunk, find best job chunk; average top 3 |
| **Job → Resume** (added) | "Which of the job's requirements are best covered by the candidate?" | For each job chunk, find best resume chunk; average top 3 |

Combine the two with the **harmonic mean**:

```
final_score = 2 × resume_avg × job_avg / (resume_avg + job_avg)
```

Harmonic mean is the standard way to combine precision-and-recall-style metrics. Its key property: the result is dragged down by the lower of the two inputs. A resume that strongly matches some parts of the job but misses most of the job's requirements scores in the middle of the two values, not near the top.

**Re-running the failure case under the new algorithm:**
- Resume → Job: ~0.78 (unchanged — Python/FastAPI alignments still strong)
- Job → Resume: ~0.45 (4 of 6 requirement areas — Java, Kafka, Docker, Kubernetes — have no good resume match)
- Harmonic mean: **~0.57** — the right answer for "Python developer applying to a polyglot role"

**Alternatives considered and rejected:**

| Alternative | Why not |
|---|---|
| `MIN(resume_avg, job_avg)` instead of harmonic mean | More punishing but less smooth — small changes in either side cause abrupt jumps |
| Pure job-side aggregation | Flips the bias the other way — over-penalises candidates with strong-but-narrow expertise |
| Full pairwise average (every chunk × every chunk) | Floor of irrelevant pairs drags everything down; signal lost |
| Smaller chunks (e.g. 200 chars) | Marginal; same asymmetric problem at finer granularity |
| Larger chunks | More dilution — a chunk talking about "Python AND Java" would always match either kind of job |
| Switch embedding models | Significant cost; doesn't fix the aggregation flaw |
| Fine-tune embedding model on resume data | Impractical at this scale — requires labelled match data, evaluation framework, retraining infrastructure. Wrong tool for an aggregation problem |

### 4. Match threshold — 0.55 minimum (calibrated for bidirectional scoring)

The threshold was previously 0.7, calibrated against the old one-directional algorithm. Bidirectional scoring produces **lower absolute values** because the harmonic mean drags scores down when either side is weak. Most legitimate matches that scored 0.78–0.85 under the old algorithm score 0.55–0.70 under the new one.

We dropped the threshold to **0.55** to keep the volume of suggested matches comparable while gaining the algorithm's accuracy. Below 0.55 the match feels weak in both directions and is unlikely to be useful to the recruiter.

If no jobs clear 0.55, the candidate gets no match badge and no "Also a good fit for" section. Better silent than noisy.

Both the threshold and the top-K window size are configurable via the `MATCH_MIN_SIMILARITY` and `MATCH_TOP_K_CHUNKS` constants in `app/worker.py`. Tune without migrations.

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

`match_jobs_task` is mostly SQL — the only external call is the per-candidate Gemini rerank added in [Phase 5](phase-5-llm-reranking.md). Rerank failures are handled inside `rerank_sequential` itself (per-call try/except with a `None` result), and on total-LLM-failure the task falls back to vector-only scoring rather than raising. So failures that reach Celery's retry layer are almost always DB-related: deadlock, connection pool exhaustion, schema drift. A single retry catches transient DB issues without burning resources retrying real bugs.

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
    -- All (resume_chunk × job_chunk) similarity scores, scoped to the
    -- recruiter's own jobs and excluding the candidate's own application's job.
    SELECT
        je.job_id,
        je.id  AS job_chunk_id,
        re.id  AS resume_chunk_id,
        1 - (re.embedding <=> je.embedding) AS pair_similarity
    FROM resumeembedding re
    JOIN application a   ON a.id = re.application_id
    JOIN joblisting orig ON orig.id = a.job_id             -- the job they applied to
    JOIN joblisting cand ON cand.owner_id = orig.owner_id  -- same recruiter
                        AND cand.id != orig.id             -- exclude their own job
    JOIN jobembedding je ON je.job_id = cand.id
    WHERE a.id = :application_id
),
-- Direction 1 — Resume → Job:
-- "Are the candidate's strongest points represented somewhere in the job?"
resume_side_best AS (
    SELECT job_id, resume_chunk_id, MAX(pair_similarity) AS best_similarity
    FROM chunk_pairs
    GROUP BY job_id, resume_chunk_id
),
resume_side_ranked AS (
    SELECT
        job_id,
        best_similarity,
        ROW_NUMBER() OVER (
            PARTITION BY job_id ORDER BY best_similarity DESC
        ) AS chunk_rank
    FROM resume_side_best
),
resume_coverage AS (
    SELECT job_id, AVG(best_similarity) AS resume_avg
    FROM resume_side_ranked
    WHERE chunk_rank <= 3                       -- top-3
    GROUP BY job_id
),
-- Direction 2 — Job → Resume:
-- "Are the job's requirements covered by the candidate's resume?"
job_side_best AS (
    SELECT job_id, job_chunk_id, MAX(pair_similarity) AS best_similarity
    FROM chunk_pairs
    GROUP BY job_id, job_chunk_id
),
job_side_ranked AS (
    SELECT
        job_id,
        best_similarity,
        ROW_NUMBER() OVER (
            PARTITION BY job_id ORDER BY best_similarity DESC
        ) AS chunk_rank
    FROM job_side_best
),
job_coverage AS (
    SELECT job_id, AVG(best_similarity) AS job_avg
    FROM job_side_ranked
    WHERE chunk_rank <= 3                       -- top-3
    GROUP BY job_id
),
-- Harmonic mean of the two coverages. Dragged down by the lower value, so a
-- job with many uncovered requirements (low job_avg) scores far lower than
-- a one-directional simple average would suggest.
combined AS (
    SELECT
        r.job_id,
        r.resume_avg,
        j.job_avg,
        CASE
            WHEN (r.resume_avg + j.job_avg) = 0 THEN 0
            ELSE (2 * r.resume_avg * j.job_avg) / (r.resume_avg + j.job_avg)
        END AS aggregate_similarity
    FROM resume_coverage r
    JOIN job_coverage j ON r.job_id = j.job_id
)
SELECT job_id, aggregate_similarity
FROM combined
WHERE aggregate_similarity >= 0.55             -- threshold
ORDER BY aggregate_similarity DESC
LIMIT 3;                                       -- top-3 matches
```

### Walking through the query, piece by piece

The query has eight logical stages, seven of them inside CTEs and one as the final outer `SELECT`. The structure mirrors the conceptual question: *for each candidate job in the recruiter's pool, how strongly does this candidate's resume cover the job AND how strongly does the job's requirements appear in the resume?* The harmonic mean of the two answers is the final match score.

#### Stage 1 — `chunk_pairs` CTE: the raw resume-chunk × job-chunk similarity grid

```sql
WITH chunk_pairs AS (
    SELECT
        je.job_id,
        je.id  AS job_chunk_id,
        re.id  AS resume_chunk_id,
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

#### Stage 2 — `resume_side_best` CTE: best job-chunk per (job, resume chunk)

```sql
resume_side_best AS (
    SELECT job_id, resume_chunk_id, MAX(pair_similarity) AS best_similarity
    FROM chunk_pairs
    GROUP BY job_id, resume_chunk_id
)
```

For each `(candidate job, resume chunk)` pair, keep the single best matching job chunk. The other job chunks for that pair are dropped — we only care about the strongest alignment.

**Why this matters semantically** — a resume chunk that mentions *"led a team of 8 engineers"* might align strongly with a job chunk about *"team leadership"* and only weakly with a job chunk about *"AWS infrastructure"*. We care about the strongest signal: the resume chunk *does* express leadership, regardless of whether other job chunks happened to be irrelevant. `MAX` over the job chunks captures this.

**Concrete shape** — for a 10-chunk resume and 2 candidate jobs with 5 chunks each, the 100 rows from Stage 1 collapse to `10 × 2 = 20` rows.

#### Stage 3 — `resume_side_ranked` CTE: rank resume chunks within each candidate job

```sql
resume_side_ranked AS (
    SELECT
        job_id,
        best_similarity,
        ROW_NUMBER() OVER (
            PARTITION BY job_id ORDER BY best_similarity DESC
        ) AS chunk_rank
    FROM resume_side_best
)
```

`ROW_NUMBER()` is a **window function** that ranks rows without collapsing the result set:
- **`PARTITION BY job_id`** — fresh ranking per candidate job.
- **`ORDER BY best_similarity DESC`** — rank 1 = resume chunk that aligned most strongly with that job.

The 20 rows stay 20 rows; a `chunk_rank` column is added. We need this ranking so the next stage can keep only the top K.

#### Stage 4 — `resume_coverage` CTE: top-K average for the Resume → Job direction

```sql
resume_coverage AS (
    SELECT job_id, AVG(best_similarity) AS resume_avg
    FROM resume_side_ranked
    WHERE chunk_rank <= 3
    GROUP BY job_id
)
```

For each candidate job, average the top 3 resume chunks' best alignments. This produces the **Resume → Job** coverage score: *"How well do the candidate's strongest points show up in this job?"*

If the candidate has 3 strong areas of overlap with the job, `resume_avg` is high. If most of the resume isn't relevant, the top 3 are still the best 3 so this metric can stay high — which is exactly why we need the second direction.

#### Stages 5–7 — Job → Resume direction (mirror of Stages 2–4)

```sql
job_side_best AS (
    SELECT job_id, job_chunk_id, MAX(pair_similarity) AS best_similarity
    FROM chunk_pairs
    GROUP BY job_id, job_chunk_id
),
job_side_ranked AS (
    SELECT
        job_id,
        best_similarity,
        ROW_NUMBER() OVER (
            PARTITION BY job_id ORDER BY best_similarity DESC
        ) AS chunk_rank
    FROM job_side_best
),
job_coverage AS (
    SELECT job_id, AVG(best_similarity) AS job_avg
    FROM job_side_ranked
    WHERE chunk_rank <= 3
    GROUP BY job_id
)
```

Same shape as Stages 2–4, but partitioned on **`job_chunk_id`** instead of `resume_chunk_id`. For each `(candidate job, job chunk)`, find the best matching resume chunk; rank job chunks within each candidate job; average the top 3.

This produces the **Job → Resume** coverage score: *"How well are this job's requirements covered by the resume?"*

If the job lists 10 distinct requirements and only 2 are in the resume, the top 3 are: the 2 covered ones (high similarity) + the next best (probably low). The average lands in the middle.

#### Stage 8 — `combined` CTE: harmonic mean of the two coverages

```sql
combined AS (
    SELECT
        r.job_id,
        r.resume_avg,
        j.job_avg,
        CASE
            WHEN (r.resume_avg + j.job_avg) = 0 THEN 0
            ELSE (2 * r.resume_avg * j.job_avg) / (r.resume_avg + j.job_avg)
        END AS aggregate_similarity
    FROM resume_coverage r
    JOIN job_coverage j ON r.job_id = j.job_id
)
```

For each candidate job, combine the two directional scores via harmonic mean:

```
final_score = 2 × resume_avg × job_avg / (resume_avg + job_avg)
```

**Why harmonic mean** — the harmonic mean is dragged down toward the lower input. If `resume_avg = 0.85` but `job_avg = 0.40` (the candidate has some strong points but the job has many uncovered requirements), the harmonic mean is `~0.54` — much closer to the lower value than the arithmetic mean (`0.625`) would suggest. This is the desired behaviour: a strong-on-one-side / weak-on-the-other match should not look like a balanced one.

The `CASE WHEN ... = 0` guard avoids division by zero in the degenerate case where both averages are zero.

#### Final `SELECT` — threshold, ordering, top-N

```sql
SELECT job_id, aggregate_similarity
FROM combined
WHERE aggregate_similarity >= 0.55
ORDER BY aggregate_similarity DESC
LIMIT 3
```

- **`WHERE aggregate_similarity >= 0.55`** — threshold filter. Calibrated for the harmonic mean's lower absolute range (see decision 4 above).
- **`ORDER BY ... DESC`** — best matches first.
- **`LIMIT 3`** — at most 3 alternative-job suggestions per candidate.

**Concrete shape** — at most 3 rows, each `(job_id, aggregate_similarity)`. The application layer joins these against `joblisting` for the human-readable title.

### Why the joins happen inside the CTE, not at the end

The owner-scope filter (`cand.owner_id = orig.owner_id`) and the self-exclusion (`cand.id != orig.id`) live in Stage 1, before any aggregation or window function runs. Three reasons:

1. **Multi-tenancy enforced at the database layer.** Even if a future bug let a request reach this query with the wrong application ID, the joins still exclude jobs that don't belong to the same recruiter. The data simply cannot be returned.
2. **Smaller working set for the window functions.** Stages 3 and 6 (the `ROW_NUMBER` calls — one per direction) are the most computationally interesting steps. Filtering down to the relevant recruiter and excluding the self-job before that point keeps the partitions small.
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
| Stage 1 — cartesian product | ~2,500 chunk-pair similarity computations, accelerated by HNSW; sub-100ms in practice |
| Stages 2–4 — Resume → Job direction | ~500 row scans + window function + GROUP BY; negligible |
| Stages 5–7 — Job → Resume direction (mirror) | ~250 row scans + window function + GROUP BY; negligible |
| Stage 8 — `combined` join + harmonic mean | ~50 group rows; constant-time arithmetic; negligible |

End-to-end runtime is dominated by Stage 1 (the vector math). The bidirectional pass adds one more GROUP BY + ROW_NUMBER over the same `chunk_pairs` CTE, which is cheap because the CTE is materialised once and re-used. The whole query typically completes in tens of milliseconds — well under the threshold where it would be worth caching results.

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

Cost characteristics of the matching pipeline. This section reflects the post-[Phase 5](phase-5-llm-reranking.md) and post-[Phase 5.2](phase-5-llm-reranking.md#follow-up-52--top-k-resume-chunks-to-rerank-not-full-resume) state — the original Phase 3 design had no LLM in the hot path; that changed with Phase 5's rerank stage.

- **Stage 1 — vector pre-filter** is pure pgvector + SQL. O(R × J × C) where R = resume chunks (~10), J = jobs in the recruiter's pool (typically tens), C = job chunks (~5). Hundreds of jobs still complete well under one second per application. Free.
- **Stage 2 — LLM rerank** (Phase 5) sends one Gemini call per pre-filter survivor, capped at `MATCH_PREFILTER_TOP_K = 10` per task. Sequential, ~1–2 s each → ~10–20 s tail per `match_jobs_task` invocation. Cached in Redis with 1 h TTL keyed by `(application_id, query_text, candidate_text)`, so unchanged-input re-runs hit cache.
- **Resume input per LLM call** (Phase 5.2) is the top-`RERANK_RESUME_CHUNK_TOP_K` resume chunks ranked by best cosine distance to *any* chunk of the candidate job, plus chunk 0 (header). ~5 KB per call instead of the full ~6 KB resume. Each call's resume slice is computed in <10 ms via a cross-join SQL query before the LLM call fires.
- **`embed_job_task`** is one small batched Gemini call per job. Free tier handles this comfortably.
- **Bulk recheck cost** scales linearly with applicant count: N applications produces N queued tasks, each running ~10 LLM rerank calls → up to 10 × N LLM calls total. Rate limit is now `1/hour` (Phase 5; tightened from the original `2/hour`) and the rerank cache absorbs the steady-state cost on re-runs. Worker pool drains in batches; total wall time depends on Celery concurrency.

No new infrastructure. Uses the existing Celery worker pool and pgvector indexes from Phase 0, plus the Redis instance already used for rate-limit counters and chat history.

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

---

## Update 3.1 — Inverse view: "Good matches from other job applications"

### Problem

The Phase 3 dashboard shows cross-job matches from the **candidate's perspective**: open Alice's analysis modal, see *"Also a strong match for: Tech Lead (89%), Backend Engineer (74%)"*. This is useful, but it hides matches from the recruiter when they are looking at a single job's applicants:

- Recruiter has Job A (Frontend) and Job B (Tech Lead) open.
- Alice applied to Job A. Phase 3 correctly identifies her as a better fit for Job B.
- Recruiter opens Job B's applicants list looking for Tech Leads. Alice is **not** there — she only appears under Job A unless the recruiter happens to click her modal.

The fix is to surface the inverse direction of the same data. Same `cross_job_match` rows, queried by `matched_job_id` instead of `application_id`.

### Decisions

**1. New section on the applicants view of every job.**

Below the regular applicants list for Job B, add a new section: *"Good matches from your other job applications"*. Lists candidates who applied elsewhere but match Job B above the threshold, with:

- Candidate name + email
- The job they actually applied to (clickable — opens that job's applicants list)
- LLM match score against Job B
- LLM critique
- A "View resume" link

Recruiter can route a strong cross-match to the right pipeline without opening every candidate modal.

**2. Keep the existing per-candidate view too.**

The candidate detail modal still shows *"Also a strong match for…"* in the candidate's direction. Both directions are useful — they answer different questions ("where else could this candidate go?" vs "who else might fit this role?") — and they read from the exact same table. No data duplication, no extra LLM cost.

**3. Show the candidate's original job in the inverse section.**

Without it, the recruiter sees a name with no context. With it, they can see *"Alice — currently in your Frontend pipeline"* and decide whether to invite her to interview for Tech Lead as well, or move her over entirely. The original job title also disambiguates candidates who exist in multiple pipelines.

**4. New endpoint, not an extension of an existing one.**

The applicants list endpoint already returns applications *for this job*. Inverse matches are applications *for other jobs*. Mixing them into one response would make the schema confusing (every row would need a *"this is actually for another job"* flag). A separate endpoint keeps both schemas clean.

**5. Multi-tenancy check on the matched job, not the originating job.**

Cross-job matches are only computed within a recruiter's own pool (enforced by the `cand.owner_id = orig.owner_id` clause in `CROSS_JOB_MATCH_SQL`). So if the matched job belongs to the current recruiter, the originating job necessarily does too. The endpoint only needs to verify ownership of `job_id` in the path; no extra join needed.

**6. Reuse the existing `MATCH_LLM_MIN_SCORE` threshold.**

If a cross-job match made it into the `cross_job_match` table, it already cleared the threshold during Phase 3 scoring. The inverse endpoint returns whatever's in the table — no additional filtering needed.

### Data flow

```
                  ┌────────────────────────────────────────────────────┐
                  │  cross_job_match table (already populated by       │
                  │  match_jobs_task in Phase 3 / Phase 5)             │
                  │                                                    │
                  │  application_id │ matched_job_id │ similarity │ critique │
                  └─────┬───────────────────────┬──────────────────────┘
                        │                       │
            ┌───────────▼────────┐   ┌──────────▼───────────────────────┐
            │ Existing direction │   │ NEW inverse direction            │
            │ GET /applications/ │   │ GET /jobs/{job_id}/              │
            │   {id}/matches     │   │   cross-applicants               │
            │                    │   │                                  │
            │ "where else could  │   │ "who else might fit this role?"  │
            │  this candidate    │   │                                  │
            │  go?"              │   │ rendered as bottom section of    │
            │                    │   │ the Job's applicants view        │
            └────────────────────┘   └──────────────────────────────────┘
```

### API design

`GET /jobs/{job_id}/cross-applicants`

**Auth**: requires `current_user` to own the job. Returns 403 otherwise.

**Response** (`list[CrossApplicantResult]`):

```python
class CrossApplicantResult(SQLModel):
    application_id: int
    candidate_name: str
    candidate_email: str
    resume_url: str
    original_job_id: int            # the job they actually applied to
    original_job_title: str
    similarity: float               # 0.0–1.0; same scale as cross_job_match.similarity
    critique: str | None
```

**Query** (single statement, no Python-side joins):

```sql
SELECT
    a.id            AS application_id,
    a.candidate_name,
    a.candidate_email,
    a.resume_url,
    orig.id         AS original_job_id,
    orig.title      AS original_job_title,
    m.similarity,
    m.critique
FROM crossjobmatch m
JOIN application a    ON a.id = m.application_id
JOIN joblisting orig  ON orig.id = a.job_id
WHERE m.matched_job_id = :job_id
ORDER BY m.similarity DESC
LIMIT 20
```

Ordering by descending similarity puts the strongest cross-matches first. `LIMIT 20` is a safety cap — in practice each job will have a handful of cross-matches at most.

### Dashboard rendering

Below the existing applicants table on a job's view, a new collapsible section:

```
┌─────────────────────────────────────────────────────────────────────┐
│  Good matches from your other job applications                      │
│  ─────────────────────────────────────────────────────────────────  │
│  Alice Chen — applied to Senior Frontend Engineer                   │
│    Match: 87%  ▸ Strong overlap on distributed systems, mentorship  │
│    [View resume]  [Open original application]                       │
│                                                                     │
│  Jordan Park — applied to Junior Engineer                           │
│    Match: 72%  ▸ Has the required Kafka and Postgres experience…    │
│    [View resume]  [Open original application]                       │
└─────────────────────────────────────────────────────────────────────┘
```

Empty state: section hidden entirely if the endpoint returns `[]`. No "no cross-matches" message — it would just be noise on the most common case.

### Implementation plan

1. **app/models.py** — add `CrossApplicantResult` schema (no DB table; pure Pydantic).
2. **app/main.py** — add `GET /jobs/{job_id}/cross-applicants` endpoint. Verify ownership of `job_id` against `current_user.id`. Execute the SQL above. Map rows to `CrossApplicantResult`. Apply the same `/jobs` rate-limit family as the existing job endpoints (no new limiter category).
3. **app/static/js/api.js** — add `fetchCrossApplicants(jobId)` helper.
4. **app/static/dashboard.html** (current single-page UI) — when an applicants table is rendered for a job, also fire `fetchCrossApplicants` in parallel and render the new section below. Hide the section if the response is empty. (Once Frontend Multi-Page lands — see [`docs/frontend-multipage.md`](../frontend-multipage.md) — this moves to the per-job page instead.)
5. **scripts/smoke_test_phase3.py** — extend with one new assertion: after `match_jobs_task` runs for Alice (who applied to Frontend), `GET /jobs/{tech_lead_id}/cross-applicants` returns Alice with `original_job_id = frontend_id` and a non-empty critique. Multi-tenancy check: same query as recruiter B returns either 403 (job not owned) or `[]`.
6. **docs/ai-features/phase-3-cross-job-matching.md** — flip this section's status from "designed" to "complete" when shipped.

### Estimated effort

~2 hours including the smoke test extension and dashboard integration. Mostly mechanical — no new ML, no new schema.

### Status

Complete. Shipped in:

- `app/models.py` — `CrossApplicantResult` Pydantic schema.
- `app/main.py` — `GET /jobs/{job_id}/cross-applicants` endpoint (60/min, ownership-checked).
- `app/static/js/api.js` — `Api.getCrossApplicants(jobId)` helper.
- `app/static/dashboard.html` — new `#crossApplicantsSection` rendered under the applicants table; auto-hides when empty; seeds `applicationMap` so the candidate modal opens correctly across jobs; links the candidate's *original* job title back to that job's applicants view.
- `scripts/smoke_test_phase3.py` — new assertions that the inverse SQL returns Alice for the Tech Lead job with the correct `original_job_id` / `original_job_title`, and that recruiter B's job sees no cross-applicants from recruiter A's pool.
