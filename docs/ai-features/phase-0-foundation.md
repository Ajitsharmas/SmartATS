# Phase 0 — Foundation for AI/RAG Features

This document captures the design and decisions for Phase 0 of the AI features roadmap. Phase 0 establishes the infrastructure that Phases 1–5 will build on. No user-facing features ship in this phase.

For the overall roadmap, see [roadmap.md](roadmap.md).

---

## Goal

Set up the database, dependencies, configuration, and module layout for embedding-based features (semantic search, RAG Q&A, cross-job matching, agent). Validate the foundation with a smoke test before building features on top.

---

## Acceptance criteria

- Postgres container runs the `pgvector` extension, enabled per database
- Two new tables (`resume_embedding`, `job_embedding`) exist with HNSW indexes for cosine similarity search
- Python dependencies for embeddings + LangChain are installed in the web and worker containers
- A smoke test script can call Gemini's `gemini-embedding-001`, store the result in pgvector, and retrieve it via similarity search
- All existing functionality (auth, scoring, job CRUD, etc.) continues to work unchanged

---

## Decisions

### 1. Postgres image — switch to `pgvector/pgvector:pg15`

The official pgvector image, based on Postgres 15 (matching what we already use). It bundles the `pgvector` extension as a shared object so `CREATE EXTENSION vector` works without any additional installation.

**Files affected:**
- `docker-compose.yaml`
- `docker-compose.prod.yaml`
- `app/database.py` — run `CREATE EXTENSION IF NOT EXISTS vector` on startup

**Data preservation:** Existing data in both local and GCP databases is wiped — fresh start. No migration needed.

---

### 2. Database schema — two tables, both chunked

#### `resume_embedding`

| Column | Type | Purpose |
|---|---|---|
| `id` | int (PK) | — |
| `application_id` | int (FK, CASCADE) | Link back to candidate's application |
| `chunk_index` | int | 0, 1, 2... position within the resume |
| `chunk_text` | text | Original text — needed as RAG context |
| `embedding` | vector(768) | The embedding |
| `created_at` | datetime | — |

**Indexes:**
- HNSW on `embedding` using cosine distance (`vector_cosine_ops`)
- B-tree on `application_id` for filtering by candidate

#### `job_embedding`

| Column | Type | Purpose |
|---|---|---|
| `id` | int (PK) | — |
| `job_id` | int (FK, CASCADE) | Link back to job |
| `chunk_index` | int | 0, 1, 2... |
| `chunk_text` | text | Original text |
| `embedding` | vector(768) | — |
| `created_at` | datetime | — |

**Indexes:**
- HNSW on `embedding`

#### Why two tables, not one polymorphic table?

| Reason | Detail |
|---|---|
| Lifecycle differs | Resume embeddings created on resume upload; job embeddings created on job creation/update |
| Cascade differs | Resume → application; job → joblisting |
| Chunking may diverge | Resumes can split by section; jobs may split by responsibility/requirement |
| Query patterns differ | Feature 2 (search) queries resumes only; Feature 3 (cross-job) queries jobs only |

A polymorphic `embeddings(entity_type, entity_id, ...)` table would mix concerns and make queries harder.

#### Why job descriptions are chunked too

Initial design assumed jobs would fit in a single embedding. Reconsidered:

- Senior-role job descriptions can easily exceed 2000+ words
- Embedding models have an input token limit (Gemini's is ~2048 tokens, ~8000 chars)
- Long job descriptions would either truncate (losing detail) or fail outright
- Chunking gives finer-grained matching for Feature 3 (e.g., "this candidate's Kubernetes experience matches the DevOps requirements of Job X")

So `job_embedding` has multiple rows per job, just like `resume_embedding`.

---

### 3. ORM choice — stay with SQLModel, drop down to SQLAlchemy where needed

Considered migrating to raw SQLAlchemy for cleaner pgvector integration. Decision: **stay with SQLModel.**

| | Keep SQLModel | Migrate to SQLAlchemy |
|---|---|---|
| Pgvector support | Works via `sa_column=Column(Vector(768))` | Native |
| Refactor cost | None | 1–2 days of rewriting models |
| Risk of bugs | None | Moderate (lots of touch points) |
| Unified ORM + Pydantic schema | Preserved | Lost — need separate models |
| Time spent shipping AI features | Maximised | Reduced |

**The middle path:** Use SQLModel everywhere, drop down to raw SQLAlchemy / SQL only where SQLModel can't express what we need:
- `sa_column=Column(Vector(768))` for the embedding column
- HNSW index creation via raw SQL in `database.py` startup
- Any future complex query that SQLModel's `select()` can't handle

This is the standard escape pattern for SQLModel: keep the ORM-and-validation-in-one-class ergonomics for the common case, and drop into raw SQLAlchemy / SQL for the things SQLModel cannot express — in our case, the pgvector column type and the HNSW index creation. SQLModel is built directly on SQLAlchemy 2.0, so the escape hatch is always available without leaving the codebase.

---

### 4. Python dependencies to add

Append to `requirements.txt`:

| Package | Purpose |
|---|---|
| `langchain-google-genai` | `GoogleGenerativeAIEmbeddings` client for Gemini embeddings |
| `langchain-core` | Core types and abstractions (used by chains in Feature 1) |
| `langchain` | Higher-level chain helpers (used by RAG chain in Feature 1) |
| `langchain-postgres` | `PGVector` vector store class — integrates pgvector with LangChain retrievers |
| `pgvector` | Python client; provides `pgvector.sqlalchemy.Vector` column type |

Both web and worker containers need these — both will be rebuilt.

---

### 5. Configuration additions

In `app/config.py`:

```python
# AI Embeddings Config
EMBEDDING_MODEL: str = "models/gemini-embedding-001"
EMBEDDING_DIMENSIONS: int = 768

# Chunking strategy
RESUME_CHUNK_SIZE: int = 500       # characters
RESUME_CHUNK_OVERLAP: int = 50
JOB_CHUNK_SIZE: int = 500
JOB_CHUNK_OVERLAP: int = 50
```

Defaults bake in; no `.env` changes required unless tuning later. Separate config for resumes and jobs so they can diverge.

#### Why `gemini-embedding-001` and not `text-embedding-004`

We initially planned to use `text-embedding-004`, but that model is no longer available on Gemini's v1beta API (which `langchain-google-genai` currently uses) — calls return `404 NOT_FOUND`. The current standard embedding model is **`gemini-embedding-001`**, which:

- Supports configurable output dimensionality (768, 1536, or 3072)
- Is the recommended replacement for `text-embedding-004`
- Stays on the same free tier (1500 RPM)

We pass `output_dimensionality=768` to the LangChain client so the model returns 768-dim vectors, matching our pgvector column type. Without this parameter, `gemini-embedding-001` defaults to 3072 dimensions (which would require a wider column and roughly 4× the storage).

---

### 6. Module layout

#### New file — `app/embeddings.py`

A thin module that:
- Initialises `GoogleGenerativeAIEmbeddings` as a singleton with the model `gemini-embedding-001` and `output_dimensionality=768`
- Exposes `embed_text(text) -> list[float]` and `embed_texts(texts) -> list[list[float]]`
- Wraps Gemini failures with a clean `EmbeddingError` exception (analogous to `GeminiUnavailableError` in `ai.py`)

#### Updated — `app/models.py`

Add `ResumeEmbedding` and `JobEmbedding` SQLModel classes with the schema described above.

#### Updated — `app/database.py`

After `SQLModel.metadata.create_all()`, run:
1. `CREATE EXTENSION IF NOT EXISTS vector;`
2. HNSW index creation on both embedding tables (idempotent via `CREATE INDEX IF NOT EXISTS`)

#### New file — `scripts/smoke_test_embeddings.py`

Standalone script to validate the infrastructure. See section 8.

#### Not touched in Phase 0
- `worker.py` — chunking + embedding pipeline arrives in Phase 1
- `main.py` — search/RAG endpoints arrive in Phases 2–5
- Frontend — arrives in later phases

---

### 7. Migration strategy

| Scenario | Action |
|---|---|
| Local dev | Wipe `postgres_data` volume |
| GCP prod | Wipe `postgres_data_prod` volume |

`CREATE EXTENSION` is idempotent. `create_all()` adds new tables but doesn't touch existing ones (existing data wouldn't be touched even if we kept it). HNSW index creation uses `CREATE INDEX IF NOT EXISTS`, so it's idempotent too.

No Alembic introduced — consistent with the project's current "create_all + ALTER TABLE notes" approach.

---

### 8. Smoke test

`scripts/smoke_test_embeddings.py` runs after Phase 0 to validate:

1. Generate an embedding via Gemini → expect `list[float]` of length 768
2. Store it in `resume_embedding` with a placeholder/fake `application_id`
3. Query the most similar embeddings to a related phrase
4. Verify the inserted row is returned with similarity > 0.7
5. Clean up

If any step fails, the issue is caught here — not when building Phase 1 features on top.

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Postgres image swap fails | Image is officially maintained pgvector + Postgres 15; widely used |
| Gemini quota hit during dev testing | Use tiny test inputs; free tier is 1500 RPM |
| SQLModel ↔ pgvector incompatibility | Smoke test catches this early; documented escape via raw SQLAlchemy |
| HNSW index creation slow at runtime | Empty index initially; index build only meaningful at thousands of rows |
| LangChain dependency conflicts | Pin versions; rebuild containers |

---

## Confirmed open questions

| Question | Resolution |
|---|---|
| Data preservation? | Wipe both local and GCP databases |
| HNSW index creation timing? | At app startup, raw SQL, idempotent |
| Back-fill embeddings for existing data? | No — only embed new uploads going forward |
| Stay on SQLModel or migrate to SQLAlchemy? | Stay on SQLModel; drop down where needed |
| Job descriptions chunked? | Yes — schema updated to match resume_embedding pattern |

---

## Running the smoke test

After Phase 0 changes are in place, validate the foundation end-to-end with `scripts/smoke_test_embeddings.py`. The script generates an embedding, stores it in pgvector, queries it back via cosine similarity, and cleans up after itself.

### Steps (local dev)

In the dev setup, `db`, `redis`, `minio`, and `worker` run in Docker; the FastAPI app runs locally on your Mac via `uvicorn`. The smoke test can be run either from your local venv (recommended, matches the uvicorn flow) or from inside the worker container.

From the project root on your Mac:

```bash
# 1. Wipe the existing Postgres volume so the new pgvector image initialises a fresh DB
docker compose down -v

# 2. Rebuild containers with the new dependencies and the pgvector image
docker compose up -d --build

# 3. Install Phase 0 dependencies into your local venv (worker container already has them)
.venv/bin/pip install -r requirements.txt

# 4. Wait a few seconds for Postgres to finish initialising, then run the smoke test
.venv/bin/python scripts/smoke_test_embeddings.py
```

Tip: confirm Postgres is ready before step 4 by checking the logs:
```bash
docker compose logs db | grep "ready to accept connections"
```

**Alternative — run inside the worker container:**

```bash
docker compose exec worker python scripts/smoke_test_embeddings.py
```

Both approaches work. The local venv path is closer to how you run uvicorn day-to-day.

### Expected output

```
=== Phase 0 smoke test — embeddings round-trip ===

OK:   Database is initialised (vector extension + tables + HNSW indexes)
OK:   Embedding generated — 768-dim vector for: 'Senior Python Developer ...'
OK:   Test row inserted (resume_embedding id=1)
OK:   Similarity search returned the inserted row with similarity 0.787
OK:   Test rows cleaned up

=== Smoke test passed ===
```

In between the OK lines, SQLAlchemy logs every SQL statement (BEGIN, CREATE EXTENSION, INSERT, SELECT, DELETE, COMMIT) because `echo=settings.DEBUG` is on. The actual similarity value will vary slightly each run since Gemini embeddings are deterministic per input but the threshold check (>0.7) leaves comfortable margin.

The cleanup performs a single `DELETE FROM joblisting` — the CASCADE constraints on `application.job_id` and `resumeembedding.application_id` propagate the delete automatically.

### If a step fails

The output identifies exactly which step failed so the cause is obvious:

| Failure message | Most likely cause |
|---|---|
| Embedding generation fails | Invalid or missing `GEMINI_API_KEY` in `.env`, Gemini quota hit, or network issue from the worker container |
| Vector dimension mismatch | `EMBEDDING_DIMENSIONS` in `config.py` doesn't match what the model returns. `gemini-embedding-001` returns the value passed in `output_dimensionality`. Confirm it's 768. |
| Similarity below threshold | Possible if the model returned non-text-embedding output — re-check `EMBEDDING_MODEL` |
| `404 NOT_FOUND. models/text-embedding-004 is not found` | The older model has been deprecated. Confirm `EMBEDDING_MODEL = "models/gemini-embedding-001"`. |
| Insertion error mentioning `vector` | The `pgvector` extension didn't enable — confirm Postgres image is `pgvector/pgvector:pg15`, not plain `postgres:15` |
| HNSW index error | Indexes failed to create at startup — check Postgres logs via `docker compose logs db` |

### After the FastAPI dev server restart

The FastAPI `web` service runs locally on your Mac (not in Docker) in the dev setup. After Phase 0 changes, restart your local `uvicorn` process so it picks up the new `embeddings.py` module and updated `database.py`:

```bash
# Kill the running uvicorn (Ctrl+C), then:
uvicorn app.main:app --reload
```

This isn't strictly required for the smoke test (which uses the worker container), but ensures the local web server is in sync.

### Steps (GCP / production)

The smoke test should also be re-run on the GCP VM after deploying Phase 0 there, to confirm the same round-trip works in the production setup:

```bash
# On the GCP VM, in the project root
docker compose -f docker-compose.prod.yaml down -v
docker compose -f docker-compose.prod.yaml up -d --build
docker compose -f docker-compose.prod.yaml exec worker python scripts/smoke_test_embeddings.py
```

---

## Status

Design approved. Implementation complete. Smoke test **passed end-to-end** — embedding generation, pgvector storage, HNSW similarity search, and cascade cleanup all working. Foundation is ready for Phase 1.
