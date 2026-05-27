# Phase 1 — Resume Embedding Pipeline + Task Failure Recovery

This document captures the design and decisions for Phase 1 of the AI features roadmap. Phase 1 builds the resume chunking + embedding pipeline AND introduces a generalised task failure recovery mechanism that retrofits to all existing Celery tasks.

For the overall roadmap, see [roadmap.md](roadmap.md). For the AI infrastructure foundation, see [phase-0-foundation.md](phase-0-foundation.md).

---

## Goals

1. Every uploaded resume is chunked and embedded into pgvector — making the data ready for semantic search (Phase 2), cross-job matching (Phase 3), and RAG Q&A (Phase 4)
2. Provide a clean recovery path for any Celery task that exhausts its retries — no more silent stuck applications

---

## Acceptance criteria

- `embed_resume_task` exists, chunks resume text via `RecursiveCharacterTextSplitter`, batch-embeds via `gemini-embedding-001`, and stores rows in `resume_embedding`
- Triggered automatically from `POST /process` alongside `analyze_resume_task` (parallel dispatch, not chained)
- All three tasks (`analyze_resume_task`, `rescore_application_task`, `embed_resume_task`) implement `on_failure` hooks that populate error columns on `Application` and set `status="failed"`
- `Application` table has new `scoring_error` and `embedding_error` columns
- `POST /applications/{id}/retry` re-dispatches whichever task(s) failed and resets status
- Dashboard surfaces failed applications with a clear visual indicator and per-row retry button
- A smoke test confirms a real resume gets chunked + stored end-to-end

---

## Decisions

### 1. Chunking strategy

`RecursiveCharacterTextSplitter` with `chunk_size=500` characters, `chunk_overlap=50` characters.

| Decision | Rationale |
|---|---|
| Recursive character splitter | Smart fallback through separators `["\n\n", "\n", " ", ""]`, respects paragraph boundaries |
| 500 chars per chunk | ≈ one short paragraph — small enough for precise retrieval, large enough to carry meaning |
| 50-char overlap | Sentences near boundaries appear in both adjacent chunks, preventing retrieval misses |
| Not semantic chunking | Too expensive — would require an extra embedding call per sentence to score similarity |
| Not custom section parser | Brittle across diverse resume formats |

We can swap to semantic or section-based chunking later if retrieval quality is poor. Cheap default first.

### 2. Trigger location — parallel dispatch (Option B)

`POST /process` dispatches both `analyze_resume_task.delay(...)` and `embed_resume_task.delay(...)` in parallel.

**Why not chain from scoring task (Option A)?**

The two tasks are independent — scoring doesn't need embeddings, embedding doesn't need the score. Parallel execution means faster total time. If embedding fails, scoring still succeeds and vice versa — clean loose coupling.

### 3. Re-scoring does NOT re-embed

`rescore_application_task` runs when a recruiter edits a job. The resume text is unchanged in that case, only the job changed. So resume embeddings stay valid — we do not re-dispatch `embed_resume_task` from rescore.

### 4. Idempotency — delete-then-insert at task start

`embed_resume_task` deletes any existing `ResumeEmbedding` rows for the application at the start, then inserts fresh chunks. This makes the task safe to re-run after a partial failure or via the retry endpoint.

Tiny extra cost (one DELETE), real safety guarantee. Same pattern most embedding pipelines use.

### 5. No backfill of existing applications

Existing applications stay without embeddings. They simply won't be retrievable by semantic search until they re-apply. If we want backfill later, a script can iterate over applications without embeddings and dispatch `embed_resume_task` for each. Out of scope for Phase 1.

---

## Failure recovery mechanism

### Problem we are solving

Today, when `analyze_resume_task` or `rescore_application_task` exhausts all retries:

1. Celery logs the failure to stdout
2. The application stays in `status="pending"` with `ai_score=0`, `ai_critique=None`
3. No alert, no UI indication, no path to retry
4. The recruiter has no way to recover

`embed_resume_task` would have the same blind spot. Phase 1 fixes this for all three tasks.

### Approach — manual retry via dashboard

**Recommended approach (Option A in design discussion):**

- New columns on `Application`: `scoring_error: str | None`, `embedding_error: str | None`
- New status value: `"failed"`
- Celery `on_failure` hooks populate the relevant error column and set status to `"failed"` when retries exhaust
- `POST /applications/{id}/retry` re-dispatches whatever failed and resets status
- Dashboard surfaces failed applications with a clear icon + retry button

**Why not Celery Beat auto-retry?**

A scheduled background re-trier would retry forever for permanent failures (deprecated model, revoked API key) — wasting Gemini quota and making debugging harder. Manual retry forces a human to confirm the issue is transient. Worth the trade-off in a demo / low-traffic context. Auto-retry could be added later if scale demands it.

### Status state machine

```
                 ┌──→ processed (success)
                 │
pending ─────────┤
                 │
                 └──→ failed (any task exhausted retries)
                          │
                          └──→ pending (recruiter clicked retry)
                                   │
                                   └──→ ...
```

Note: `status` is a single field. A row can have either or both of `scoring_error` and `embedding_error` populated when `status="failed"`. The retry endpoint clears whichever errors exist and re-dispatches the corresponding tasks.

### `on_failure` hook pattern

Each task defines an `on_failure` that runs when Celery exhausts `max_retries`:

```python
def on_failure(self, exc, task_id, args, kwargs, einfo):
    application_id = kwargs.get("application_id") or args[-1]
    with Session(engine) as session:
        app_record = session.get(Application, application_id)
        if app_record:
            app_record.embedding_error = str(exc)   # or scoring_error
            app_record.status = "failed"
            session.add(app_record)
            session.commit()
```

For Celery tasks defined via the decorator (as we currently do), `on_failure` is added by subclassing `celery.Task` or by passing a `base=` class. We'll use the cleaner `base=` pattern.

---

## Files to create / modify

| File | Change |
|---|---|
| `app/models.py` | Add `scoring_error` and `embedding_error` columns to `Application` |
| `app/worker.py` | Add `embed_resume_task`; add `on_failure` hooks to all three tasks via a shared `TaskWithFailureTracking` base class |
| `app/main.py` | Dispatch `embed_resume_task.delay(...)` from `/process`; add `POST /applications/{id}/retry` endpoint |
| `app/static/dashboard.html` | Show failed status icon + retry button next to each failed application; wire up call to the retry endpoint |
| `app/static/js/api.js` | Add `retryApplication(applicationId)` method |
| `scripts/smoke_test_phase1.py` | New end-to-end smoke test for the embedding pipeline |

---

## Detailed designs

### `embed_resume_task`

```python
class TaskWithFailureTracking(celery.Task):
    """Base class that updates Application status when retries exhaust."""
    error_column: str = "scoring_error"  # subclasses override

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        application_id = kwargs.get("application_id") or args[-1]
        with Session(engine) as session:
            app_record = session.get(Application, application_id)
            if app_record:
                setattr(app_record, self.error_column, str(exc))
                app_record.status = "failed"
                session.add(app_record)
                session.commit()


class EmbedResumeTask(TaskWithFailureTracking):
    error_column = "embedding_error"


@celery_app.task(
    base=EmbedResumeTask,
    name="embed_resume_task",
    autoretry_for=(EmbeddingError,),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=4,
)
def embed_resume_task(text_content: str, application_id: int) -> dict:
    if len(text_content.strip()) < 50:
        return {"status": "skipped", "reason": "text too short"}

    # 1. Idempotency: clear any existing chunks
    with Session(engine) as session:
        existing = session.exec(
            select(ResumeEmbedding).where(ResumeEmbedding.application_id == application_id)
        ).all()
        for row in existing:
            session.delete(row)
        session.commit()

    # 2. Chunk
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.RESUME_CHUNK_SIZE,
        chunk_overlap=settings.RESUME_CHUNK_OVERLAP,
    )
    chunks = splitter.split_text(text_content)

    # 3. Batch-embed
    vectors = embed_texts(chunks)  # raises EmbeddingError → triggers retry

    # 4. Insert
    with Session(engine) as session:
        for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
            session.add(ResumeEmbedding(
                application_id=application_id,
                chunk_index=i,
                chunk_text=chunk,
                embedding=vector,
            ))
        session.commit()

    return {"status": "success", "chunks": len(chunks)}
```

### Retry endpoint

```python
@app.post("/applications/{application_id}/retry", tags=["Applications"])
def retry_application(
    application_id: int,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Re-dispatch failed Celery tasks for an application. Protected.

    Looks at scoring_error and embedding_error columns. For each that is set,
    re-dispatches the corresponding task and clears the error. Resets the
    application status to "pending".
    """
    app_record = session.get(Application, application_id)
    if not app_record:
        raise HTTPException(status_code=404, detail="Application not found.")

    # Confirm the recruiter owns the parent job
    job = session.get(JobListing, app_record.job_id)
    if not job or job.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized.")

    if app_record.scoring_error:
        # Need text content — fetch from MinIO + re-extract
        ...
        analyze_resume_task.delay(text_content, application_id)
        app_record.scoring_error = None

    if app_record.embedding_error:
        ...
        embed_resume_task.delay(text_content, application_id)
        app_record.embedding_error = None

    app_record.status = "pending"
    session.add(app_record)
    session.commit()
    return {"message": "Retry dispatched"}
```

Note: retry needs the resume text. Since we don't store extracted text in the DB, the endpoint fetches the PDF from MinIO and re-extracts it — same pattern `rescore_application_task` already uses.

### Dashboard changes

- New status badge: red dot with "Failed" label
- Per-row "Retry" button visible only when status is "failed"
- Optional: hover/click reveals the actual error message for debugging

---

## Force re-analysis — `POST /applications/{id}/reanalyze`

The Retry button only fires when the task previously **failed** (i.e. `scoring_error`, `embedding_error`, or `matching_error` is set). It does not help in cases where the AI pipeline completed *successfully* but produced wrong or stale results. Real examples:

- An earlier bug truncated the resume text fed to scoring + embedding (the AI tasks returned successfully but with bad input)
- The chunking strategy changed, so old chunks no longer match the current code
- A candidate uploaded a new PDF and you want fresh analysis
- The embedding model changed (e.g. dimension swap)

`POST /applications/{id}/reanalyze` solves this. It is the **unconditional cousin** of `/retry`:

| Behavior | `/retry` | `/reanalyze` |
|---|---|---|
| Requires an error column to be set | Yes | No |
| Re-fetches PDF from MinIO and re-extracts text | Only if scoring or embedding failed | Always |
| Re-dispatches `analyze_resume_task` | Only on scoring error | Always |
| Re-dispatches `embed_resume_task` → `match_jobs_task` | Only on embedding/matching error | Always |
| Clears stale chat history for this candidate | No | **Yes** — invalidates Phase 4 cache |
| Rate limit | (uses the `/retry` limit) | 10 / minute per user |

The dashboard surfaces it as a **"Re-analyze"** button in the candidate analysis modal (alongside Download Resume and Close), with a confirmation step that lists what will be overwritten.

### Cache invalidation on re-analyze

When re-analyzing, several cached / derived states would become stale. The endpoint handles them as follows:

| Cached state | Action |
|---|---|
| Resume embeddings (`resumeembedding` rows) | Overwritten — `embed_resume_task` deletes-then-inserts when it runs |
| Cross-job matches (`cross_job_match` rows) | Overwritten — `match_jobs_task` deletes-then-inserts |
| AI score and critique (columns on `Application`) | Overwritten by `analyze_resume_task` |
| **Chat history (Redis `chat:*:<app_id>:*`)** | **Cleared by `clear_application_chats(application_id)`**. Prior Q&A cited specific resume chunks that no longer exist after re-embedding; continuing those conversations would produce broken citations and stale context |
| Query embedding cache (Redis `emb:*`) | Not touched — keyed by query text, not by application, so re-analyzing one application does not affect it |

The response includes `chat_sessions_cleared` (count of Redis chat keys deleted) so the operation is observable.

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Resume text comes through empty | Skip task if `len(text.strip()) < 50` |
| Resume exceeds embedding batch limit | Unlikely for normal resumes; if hit, Celery retries |
| Both scoring and embedding fail simultaneously | Status correctly reflects `failed`; retry endpoint handles both |
| Recruiter clicks retry while task is still in flight | Idempotent task deletes-then-inserts; minor wasted work, no data corruption |
| MinIO unavailable during retry | Retry endpoint fails cleanly with 5xx; recruiter tries again later |
| Storage growth from chunks | 1 resume = ~10 chunks × ~3KB = 30KB. 10,000 resumes = 300MB. Fits comfortably on 30GB disk. |

---

## Implementation details

### Bug fix in existing tasks

While retrofitting the failure recovery, we discovered a pre-existing bug: both `analyze_resume_task` and `rescore_application_task` wrapped their bodies in a broad `try/except Exception` that returned `{"status": "error", ...}` on any failure. This silently swallowed every exception — including `GeminiUnavailableError` — which meant:

- The `autoretry_for=(GeminiUnavailableError,)` decorator was effectively dead. Celery never saw the exception, so it never retried.
- Tasks "succeeded" from Celery's view despite failing logically, so no `on_failure` hook ever fired.
- Applications stayed `pending` forever after a real failure.

**Fix:** the broad `try/except` blocks were removed from both tasks. Exceptions now propagate up. Celery handles them according to `autoretry_for` (retry the transient ones) and the `TaskWithFailureTracking.on_failure` hook (mark the application failed if retries exhaust).

### Auto-recovery on success

When a previously-failed task succeeds on retry (whether via Celery's automatic retry or via the recruiter clicking Retry):

- `_analyze_and_save` clears `scoring_error` after a successful score write.
- `embed_resume_task` clears `embedding_error` after a successful insert, and if both error columns are now null, restores `status` from `failed` back to `processed` (or `pending` if scoring also hasn't completed yet).

This means the dashboard self-heals as tasks succeed — no manual cleanup required.

### File-by-file summary

| File | Change |
|---|---|
| `app/models.py` | Added `scoring_error` and `embedding_error` columns on `Application`. |
| `app/worker.py` | Full rewrite. Added `TaskWithFailureTracking` base class with `on_failure` hook; `ScoringTask` and `EmbeddingTask` subclasses; removed broad `try/except` bug; new `embed_resume_task` with chunking + idempotent delete-then-insert. |
| `app/main.py` | `/process` now dispatches `analyze_resume_task` and `embed_resume_task` in parallel. Added `POST /applications/{application_id}/retry` endpoint with auth, MinIO PDF re-fetch, text re-extraction, and conditional task dispatch. |
| `app/static/js/api.js` | Added `Api.retryApplication(applicationId)`. |
| `app/static/dashboard.html` | Failed applications show a red dot with "Failed" label (full error message in the hover title) and a red "Retry" button instead of "View Analysis". Added `window.retryApp` handler. |
| `scripts/smoke_test_phase1.py` | End-to-end smoke test with a realistic multi-paragraph resume. |

---

## Running the Phase 1 smoke test

The smoke test creates a temporary job + application, invokes `embed_resume_task` synchronously, asserts the chunks land in pgvector correctly, and cleans up via cascade delete.

### Steps (local dev)

From the project root on your Mac:

```bash
# 1. Wipe any existing Postgres volume so the schema picks up the new
#    scoring_error / embedding_error columns on Application
docker compose down -v

# 2. Rebuild containers — pulls langchain text splitter and refreshed worker code
docker compose up -d --build

# 3. Wait a few seconds for Postgres to be ready
docker compose logs db | grep "ready to accept connections"

# 4. Run the smoke test inside the worker container
docker compose exec worker python scripts/smoke_test_phase1.py
```

### Expected output

```
=== Phase 1 smoke test — resume embedding pipeline ===

OK:   Database is initialised
OK:   Created test job (1) and application (1)
OK:   embed_resume_task returned success with 7 chunks
OK:   DB contains 7 chunks for the test application
OK:   Chunk indices are contiguous 0..N-1
OK:   All chunks have 768-dim embeddings and non-empty text
OK:   Cascade delete removed all chunks via job → application → embedding FKs

=== Phase 1 smoke test passed ===
```

Exact chunk count will vary slightly with how the splitter respects paragraph breaks in the sample resume, but should be between 4 and 10.

### Steps (GCP / production)

Same pattern using the production compose file:

```bash
# On the GCP VM, in the project root
docker compose -f docker-compose.prod.yaml down -v
docker compose -f docker-compose.prod.yaml up -d --build
docker compose -f docker-compose.prod.yaml exec worker python scripts/smoke_test_phase1.py
```

### Failure flow validation (manual)

The automated smoke test does not exercise the failure flow because forcing a real Gemini error requires either disabling the API key (which breaks other tests) or mocking the network. To validate manually:

1. Temporarily set `GEMINI_API_KEY=invalid` in `.env` and restart the worker
2. Apply a resume via the candidate flow
3. Watch the worker logs — `embed_resume_task` will retry 4 times then call `on_failure`
4. Refresh the recruiter dashboard — the application should show a red "Failed" dot
5. Restore the real `GEMINI_API_KEY`, click Retry in the dashboard
6. The task succeeds; the dashboard refreshes to show the proper score and status

---

## Out of scope for Phase 1

- Semantic search endpoint (Phase 2)
- Job description chunking + embedding (Phase 3)
- RAG Q&A (Phase 4)
- Background auto-retry via Celery Beat (deferred — could be added later)
- Embedding backfill script for existing applications (deferred)

---

## Status

Design approved. Implementation complete. Smoke test ready to run.
