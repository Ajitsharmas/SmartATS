# SmartATS — Architecture

Reference architecture for the SmartATS application. Diagrams are Mermaid (renders natively on GitHub). Each section is self-contained; jump to whichever flow you're debugging.

For per-phase design rationale (the *why* behind each AI feature), see the docs in [`docs/ai-features/`](ai-features/). This file is the *what* and *how* — the structure of the running system.

---

## 1. Elevator pitch

SmartATS is an AI-powered applicant tracking system. Recruiters post jobs; candidates upload resumes; the platform scores fit, lets recruiters semantically search the pool, surfaces cross-job matches, answers natural-language questions about resumes (RAG), and drafts personalised outreach emails through a LangGraph-based agent. Everything runs on a single GCP free-tier VM via Docker Compose; the only paid surface is a Gemini API key (free tier sufficient for small-scale use).

---

## 2. High-level system architecture

```mermaid
flowchart TB
    subgraph internet["Internet"]
        Candidate["Candidate Browser"]
        Recruiter["Recruiter Browser"]
    end

    subgraph vm["GCP Compute Engine VM (e2-micro)"]
        subgraph compose["Docker Compose Network"]
            Nginx["Nginx container<br/>:80 + :443<br/>TLS termination ·<br/>connection + req rate limits ·<br/>reverse proxy"]
            Certbot["Certbot container<br/>(ACME cert renewal)"]
            API["FastAPI / uvicorn"]
            Worker["Celery Worker"]
            DB[("Postgres + pgvector")]
            Redis[("Redis<br/>broker · cache ·<br/>rate-limit counters")]
            Storage[("MinIO<br/>S3-compatible<br/>resume PDF storage")]
        end
    end

    subgraph external["External Services"]
        Gemini["Gemini API<br/>(LLM + embeddings)"]
        Resend["Resend<br/>(transactional email)"]
        LE["Let's Encrypt<br/>(ACME servers)"]
    end

    Candidate -->|HTTPS| Nginx
    Recruiter -->|HTTPS| Nginx
    Nginx --> API
    API <--> DB
    API <--> Redis
    API <--> Storage
    API -->|model calls| Gemini
    API -->|send transactional| Resend
    API -->|enqueue task| Redis
    Worker -->|dequeue task| Redis
    Worker <--> DB
    Worker <--> Storage
    Worker -->|embeddings + LLM| Gemini
    Worker -->|outreach send| Resend
    Certbot -.->|HTTP-01 challenge :80| LE
    Certbot -.->|shared cert volume| Nginx
```

**The single host runs everything**, including TLS termination — Nginx and Certbot are both containers in the `docker-compose.prod.yaml` stack, sharing a Docker volume for the Let's Encrypt certificates. No multi-node orchestration, no managed databases, no Kubernetes. The bottleneck is Gemini's free-tier quota (15 RPM, 1500/day) before it is CPU or RAM. This shape is deliberate: see [`docs/ai-features/roadmap.md`](ai-features/roadmap.md) for the constraint analysis.

---

## 3. Component responsibilities

| Component | Responsibility | Key files |
|---|---|---|
| **Nginx** | TLS termination, connection limits, request rate limits per IP, reverse proxy to FastAPI | [`docs/ddos-resistance.md`](ddos-resistance.md), [`docs/https-ssl.md`](https-ssl.md) |
| **FastAPI / uvicorn** | HTTP request handling, SSE streaming, auth (JWT), application-level rate limits (SlowAPI), tool execution for Phase 6 agent | [`app/main.py`](../app/main.py), [`app/auth.py`](../app/auth.py), [`app/limiter.py`](../app/limiter.py) |
| **Celery Worker** | Long-running tasks: resume parsing + scoring + embedding, job embedding, cross-job matching | [`app/worker.py`](../app/worker.py) |
| **Postgres + pgvector** | Relational data + vector embeddings; vector cosine search via HNSW indexes | [`app/database.py`](../app/database.py), [`app/models.py`](../app/models.py) |
| **Redis** | Celery broker + result backend, query/rerank caches, rate-limit counters, chat history (Phase 4 + Phase 6), short-lived session state | [`app/embeddings.py`](../app/embeddings.py), [`app/rerank.py`](../app/rerank.py), [`app/agent.py`](../app/agent.py) |
| **MinIO** | S3-compatible object storage for resume PDFs (internal-only, accessed via `/download/{key}` proxy) | [`app/utils.py`](../app/utils.py) |
| **Gemini** | `gemini-2.5-flash` for scoring / rerank / RAG / agent / outreach; `gemini-embedding-001` for vectors. Model name overridable via `LLM_MODEL_NAME` env var. | [`app/ai.py`](../app/ai.py), [`app/embeddings.py`](../app/embeddings.py), [`app/rerank.py`](../app/rerank.py), [`app/rag.py`](../app/rag.py), [`app/outreach.py`](../app/outreach.py), [`app/agent.py`](../app/agent.py) |
| **Resend** | Transactional email (verification, password reset, outreach to candidates). Recruiter-approved sends only. | [`app/email.py`](../app/email.py) |

---

## 4. Data model

```mermaid
erDiagram
    USER ||--o{ JOBLISTING : "owns"
    USER ||--o{ OUTREACHEMAIL : "drafted by"
    JOBLISTING ||--o{ APPLICATION : "receives"
    JOBLISTING ||--o{ JOBEMBEDDING : "chunked into"
    APPLICATION ||--o{ RESUMEEMBEDDING : "chunked into"
    APPLICATION ||--o{ CROSSJOBMATCH : "subject of"
    APPLICATION ||--o{ OUTREACHEMAIL : "subject of"
    JOBLISTING ||--o{ CROSSJOBMATCH : "matched against"
    JOBLISTING ||--o{ OUTREACHEMAIL : "target of"

    USER {
        int id PK
        string email UK
        string hashed_password
        string full_name
        bool is_active
        bool is_verified
    }
    JOBLISTING {
        int id PK
        int owner_id FK
        string title
        string description
        string skills
        string location
        string salary_range
        datetime created_at
        text embedding_error
    }
    APPLICATION {
        int id PK
        int job_id FK
        string candidate_email
        string candidate_name
        string resume_url
        int ai_score
        text ai_critique
        string status
        text scoring_error
        text embedding_error
        text matching_error
    }
    RESUMEEMBEDDING {
        int id PK
        int application_id FK
        int chunk_index
        text chunk_text
        vector_768 embedding
    }
    JOBEMBEDDING {
        int id PK
        int job_id FK
        int chunk_index
        text chunk_text
        vector_768 embedding
    }
    CROSSJOBMATCH {
        int id PK
        int application_id FK
        int matched_job_id FK
        float similarity
        text critique
    }
    OUTREACHEMAIL {
        int id PK
        int application_id FK
        int recruiter_id FK
        int target_job_id FK
        string intent
        string subject
        text body
        string status
        text custom_notes
        datetime created_at
        datetime sent_at
    }
```

**Cascade behaviour**: deleting a `JobListing` cascades to its `Application` rows and their `ResumeEmbedding`s + `CrossJobMatch`es + `OutreachEmail`s. Deleting a `User` cascades through their jobs. `OutreachEmail.target_job_id` is `ON DELETE SET NULL` so audit rows survive job deletions.

**Vector storage**: `vector_768` is `pgvector`'s 768-dimensional float array, matching the dimension we request from `gemini-embedding-001`. HNSW indexes on the `embedding` columns are created in `app/database.py` at startup (SQLModel can't express HNSW declaratively).

---

## 5. Request flows — sequence diagrams

### 5.1 Candidate applies (resume upload + scoring + matching)

```mermaid
sequenceDiagram
    actor C as Candidate
    participant N as Nginx
    participant API as FastAPI
    participant M as MinIO
    participant R as Redis
    participant W as Celery Worker
    participant G as Gemini
    participant DB as Postgres

    C->>N: POST /upload (resume.pdf)
    N->>API: forward
    API->>API: validate PDF magic bytes + size cap (5 MB)
    API->>M: PUT object (UUID key)
    API->>API: pypdf extract text
    API-->>C: { file_url, extracted_text }

    C->>N: POST /process { text, job_id, name, email, resume_url }
    N->>API: forward
    API->>DB: INSERT Application (status=pending)
    API->>R: enqueue chain[analyze_resume_task, embed_resume_task → match_jobs_task]
    API-->>C: { application_id, task_id }

    par Scoring (independent)
        W->>R: dequeue analyze_resume_task
        W->>DB: fetch job + application
        W->>G: full resume + job → score JSON
        G-->>W: { score, critique }
        W->>DB: UPDATE Application
        W->>R: (notify candidate via Resend - optional)
    and Embedding (chained to matching)
        W->>R: dequeue embed_resume_task
        W->>W: RecursiveCharacterTextSplitter → ~10 chunks
        W->>G: chunks → embeddings (batched)
        G-->>W: 768-dim vectors
        W->>DB: INSERT ResumeEmbedding rows
        W->>R: enqueue match_jobs_task
    end

    W->>R: dequeue match_jobs_task
    W->>DB: pgvector bidirectional pre-filter (top-K=10 candidate jobs)
    DB-->>W: candidate jobs

    loop For each candidate job
        W->>DB: build top-K resume chunks for THIS job (Phase 5.2)
        W->>G: rerank (job text + chunks) → score 0-100 + critique
        G-->>W: { score, critique }
    end

    W->>DB: INSERT CrossJobMatch rows (filtered by MATCH_LLM_MIN_SCORE)
```

See [`docs/ai-features/phase-1-embedding-pipeline.md`](ai-features/phase-1-embedding-pipeline.md), [`phase-3-cross-job-matching.md`](ai-features/phase-3-cross-job-matching.md), [`phase-5-llm-reranking.md`](ai-features/phase-5-llm-reranking.md).

---

### 5.2 Recruiter login + dashboard load

```mermaid
sequenceDiagram
    actor R as Recruiter
    participant API as FastAPI
    participant DB as Postgres
    participant Cache as Redis

    R->>API: POST /token (form { username, password })
    API->>DB: SELECT User WHERE email = ?
    API->>API: argon2 verify password
    API->>API: sign JWT (HS256, sub=user.id, 30-min TTL)
    API-->>R: { access_token }

    Note over R: stores token in localStorage

    R->>API: GET /dashboard (HTML)
    API-->>R: dashboard.html

    R->>API: GET /my-jobs (Authorization: Bearer ...)
    API->>API: verify JWT signature + extract sub
    API->>DB: SELECT JobListing WHERE owner_id = current_user.id
    DB-->>API: jobs
    API-->>R: [JobListing, ...]
```

JWT validation happens via the `get_current_user` dependency on every protected endpoint. See [`app/auth.py`](../app/auth.py).

---

### 5.3 Semantic search (Phase 2 + Phase 5 rerank)

```mermaid
sequenceDiagram
    actor R as Recruiter
    participant API as FastAPI
    participant Cache as Redis Cache
    participant DB as Postgres+pgvector
    participant G as Gemini

    R->>API: POST /search/candidates { query }
    API->>API: rate limit check (5/min/user)

    rect rgb(245, 245, 245)
    Note over API,G: Stage 1 — query embedding (cached)
    API->>Cache: GET query_emb:{sha256(query)}
    alt cache hit
        Cache-->>API: vector
    else cache miss
        API->>G: embed_query(query)
        G-->>API: vector
        API->>Cache: SETEX (1h)
    end
    end

    rect rgb(245, 245, 245)
    Note over API,DB: Stage 2 — pgvector pre-filter (top-K=10 across recruiter's pool)
    API->>DB: SEARCH_SQL with query_vector + owner_id
    DB-->>API: 10 candidates (by cosine)
    end

    API->>DB: fetch top-K resume chunks per candidate (Phase 5.2)

    rect rgb(245, 245, 245)
    Note over API,G: Stage 3 — LLM rerank (parallel)
    par For each of 10 candidates (asyncio.gather)
        API->>Cache: GET rerank:{app_id}:{hash(query+chunks)}
        alt cache hit
            Cache-->>API: { score, critique }
        else cache miss
            API->>G: rerank_one(query, chunks)
            G-->>API: { score, critique }
            API->>Cache: SETEX (1h)
        end
    end
    end

    API->>API: filter score < threshold, sort desc
    API-->>R: { results, degraded }
```

See [`docs/ai-features/phase-2-semantic-search.md`](ai-features/phase-2-semantic-search.md) + [`phase-5-llm-reranking.md`](ai-features/phase-5-llm-reranking.md).

---

### 5.4 RAG chat (Phase 4)

```mermaid
sequenceDiagram
    actor R as Recruiter
    participant API as FastAPI
    participant Cache as Redis
    participant DB as Postgres+pgvector
    participant G as Gemini

    R->>API: POST /applications/{id}/chat<br/>(SSE) { question, session_id }
    API->>API: verify auth + ownership
    API->>Cache: load chat history (user_id, app_id, session_id)
    Cache-->>API: prior turns

    API->>Cache: GET query embedding cache
    alt miss
        API->>G: embed(question)
        G-->>API: vector
        API->>Cache: SETEX (1h)
    end

    API->>DB: CHAT_RETRIEVAL_SQL — top-5 chunks for this application
    DB-->>API: chunks
    API-->>R: SSE wait...

    API->>G: stream RAG answer<br/>(system + history + UNTRUSTED_RESUME_EXCERPT chunks + question)

    loop For each token from Gemini
        G-->>API: chunk.content
        API-->>R: SSE event "token" { content }
    end

    API-->>R: SSE event "citations" { citations: [...] }
    API->>Cache: append both turns to history (sliding window 12)
    API-->>R: SSE event "done"
```

See [`docs/ai-features/phase-4-rag-qa.md`](ai-features/phase-4-rag-qa.md).

---

### 5.5 Phase 6 agent turn (LangGraph ReAct loop)

```mermaid
sequenceDiagram
    actor R as Recruiter
    participant API as FastAPI
    participant LG as LangGraph
    participant Planner as Planner Node<br/>(Gemini bound to tools)
    participant TN as ToolNode
    participant T as Tool Function
    participant Cache as Redis
    participant DB as Postgres
    participant G as Gemini

    R->>API: POST /assistant/turn (SSE) { message }
    API->>API: open dedicated Session(engine)
    API->>API: set_agent_context(user, session) INSIDE generator
    API->>Cache: load_history(user_id) → 16 messages
    API-->>R: SSE "thinking: Planning…"

    loop ReAct loop (max 8 tool calls per turn)
        LG->>Planner: invoke(state.messages)
        Planner->>G: chat with bound TOOLS
        G-->>Planner: AIMessage (text and/or tool_calls)

        alt model emitted tool_calls
            LG-->>API: on_tool_start event per call
            API-->>R: SSE "tool_call" { name, args }
            LG->>TN: execute each tool
            TN->>T: invoke(args)
            T->>T: read contextvar → user, session
            T->>DB: query with owner_id == user.id
            DB-->>T: rows
            T-->>TN: JSON string (or {"error":"..."})
            TN-->>LG: ToolMessage
            LG-->>API: on_tool_end event
            API-->>R: SSE "tool_result" + (if draft_email) "email_draft"
            Note over LG: loop back to planner
        else final synthesis (no tool_calls)
            LG-->>API: on_chat_model_end event
            API->>API: _strip_tool_echoes(buffered text)
            API-->>R: SSE "token" { content }
            Note over LG: END
        end
    end

    API->>Cache: save_history (filtered to user + final assistant text only)
    API-->>R: SSE "done"
```

The contextvar setup happens **inside** the generator (not in the endpoint body) because Starlette runs the generator in its own asyncio context — see [`docs/ai-features/phase-6-agent.md`](ai-features/phase-6-agent.md) failure modes table.

See also [`notes/agent-walkthrough.md`](../notes/agent-walkthrough.md) for line-by-line agent.py explanation (gitignored personal notes).

---

### 5.6 Outreach draft + send (Phase 6)

```mermaid
sequenceDiagram
    actor R as Recruiter
    participant UI as Browser UI
    participant API as FastAPI
    participant Outreach as outreach.py
    participant DB as Postgres
    participant G as Gemini
    participant Res as Resend

    Note over R,Res: STAGE 1 — DRAFT (two entry points, same backend code)
    alt One-click contextual draft
        R->>UI: click "Draft invite email →" in cross-match row
        UI->>API: POST /applications/{id}/cross-match-invite { matched_job_id }
    else Chat agent
        R->>UI: type "draft rejection for app 42"
        UI->>API: POST /assistant/turn { message }
        Note over API: agent calls draft_email tool
    end

    API->>API: verify ownership of application + target_job
    API->>Outreach: draft_email_for_application(...)
    Outreach->>DB: _build_top_resume_chunks (Phase 5.2, top-8)
    DB-->>Outreach: chunks
    Outreach->>G: prompt with<br/><UNTRUSTED_RESUME>chunks</UNTRUSTED_RESUME><br/>+ JOB POSTING + intent rules
    G-->>Outreach: JSON { subject, body }
    Outreach->>Outreach: _parse_response (fence-tolerant)
    Outreach->>Outreach: _filter_email_body — strip non-APP_BASE_URL URLs
    Note over Outreach: SECURITY log if URLs stripped
    Outreach->>DB: INSERT outreach_email (status=draft)
    DB-->>Outreach: draft row
    Outreach-->>API: draft
    API-->>UI: { draft_id, subject, body, ... }
    UI-->>R: editable card with Send / Discard buttons

    Note over R,Res: STAGE 2 — SEND (manual approval, never automatic)
    R->>UI: click Send
    UI->>API: POST /assistant/drafts/{id}/send
    API->>DB: SELECT draft + verify recruiter_id + status='draft'
    alt draft already sent
        API-->>UI: 409 "Already sent"
    end
    API->>DB: SELECT application for candidate email
    API->>Res: send (from=noreply@..., to=candidate, reply_to=recruiter)
    alt Resend rejects
        Res-->>API: ResendError
        API-->>UI: 502 (draft stays in 'draft' for retry)
    end
    Res-->>API: { message_id }
    API->>DB: UPDATE outreach_email SET status='sent', sent_at=now()
    API-->>UI: { status: sent, sent_at, message_id }
    UI-->>R: success toast
```

See [`docs/ai-features/phase-6-agent.md`](ai-features/phase-6-agent.md), [`notes/outreach-walkthrough.md`](../notes/outreach-walkthrough.md).

---

## 6. Data flow — at a glance

```mermaid
flowchart LR
    subgraph in["Inputs"]
        Resume["Resume PDF<br/>(candidate)"]
        Job["Job posting<br/>(recruiter)"]
        Query["Search / chat input<br/>(recruiter)"]
    end

    subgraph parse["Parse"]
        PDFExtract["pypdf<br/>text extract"]
        Splitter["RecursiveCharacterTextSplitter<br/>~600-char chunks, 50 overlap"]
    end

    subgraph embed["Embed"]
        Embed["gemini-embedding-001<br/>768-dim vectors"]
    end

    subgraph store["Store"]
        PG[("Postgres<br/>relational + pgvector")]
        Mini[("MinIO<br/>PDF blobs")]
    end

    subgraph retrieve["Retrieve"]
        Vec["pgvector HNSW<br/>cosine similarity"]
        TopK["top-K chunks helper<br/>(Phase 5.2)"]
    end

    subgraph reason["Reason"]
        Score["Phase 1 scoring"]
        Rerank["Phase 5 rerank"]
        RAG["Phase 4 RAG"]
        Agent["Phase 6 agent"]
        Draft["draft_email"]
    end

    subgraph out["Outputs"]
        UIScore["AI score + critique<br/>(UI badge)"]
        UIResults["Search results<br/>(UI cards)"]
        UIChat["Chat answer<br/>(SSE stream)"]
        UICard["Email draft card<br/>(UI editable)"]
        Sent["Resend → candidate inbox<br/>(recruiter-approved only)"]
    end

    Resume --> PDFExtract --> Splitter --> Embed --> PG
    Resume --> Mini
    Job --> Splitter
    Query --> Embed

    PG --> Vec --> TopK
    TopK --> Score & Rerank & RAG & Agent & Draft

    Score --> UIScore
    Rerank --> UIResults
    RAG --> UIChat
    Agent --> UICard
    Draft --> UICard
    UICard -->|recruiter clicks Send| Sent
```

---

## 7. Async task pipeline

When a candidate submits an application, three tasks run in this dependency order:

```mermaid
flowchart LR
    Submit["POST /process"]
    Submit --> Analyze["analyze_resume_task<br/>(Phase 1 scoring)"]
    Submit --> Embed["embed_resume_task<br/>(Phase 1 embedding)"]
    Embed --> Match["match_jobs_task<br/>(Phase 3 cross-job)"]

    Analyze -.->|independent| Done1["status: processed<br/>(if scoring succeeds)"]
    Match -.->|completes after| Done2["cross_job_match rows"]

    style Analyze fill:#e8f5e9
    style Embed fill:#e3f2fd
    style Match fill:#fff3e0
```

`analyze_resume_task` and `embed_resume_task` are dispatched **in parallel** at submission. `match_jobs_task` is **chained** after `embed_resume_task` because it depends on `resume_embedding` rows existing.

Each task uses Celery's `autoretry_for=(GeminiUnavailableError,)` with exponential backoff. Permanent failures land in the application's `scoring_error` / `embedding_error` / `matching_error` columns, and the dashboard shows a Retry button.

See [`docs/ai-features/phase-1-embedding-pipeline.md`](ai-features/phase-1-embedding-pipeline.md).

---

## 8. AI inference pipeline — per-phase LLM map

| Phase | Trigger | LLM call(s) | Cached? |
|---|---|---|---|
| 1 — Initial scoring | candidate applies | 1× gemini-2.5-flash on full resume + job | No |
| 1 — Resume embedding | candidate applies | N × gemini-embedding-001 (chunked) | No (persisted to pgvector) |
| 1 — Job embedding | recruiter creates job | N × gemini-embedding-001 (chunked) | No (persisted to pgvector) |
| 2 — Search rerank | recruiter searches | 1× embed query + up to 10× rerank | Both cached in Redis (1 h TTL) |
| 3 — Cross-job match | application embeds finished | Up to 10× rerank per task | Rerank cached in Redis (1 h TTL) |
| 4 — RAG chat | recruiter asks | 1× embed question + 1× streaming RAG | Question embed cached |
| 5 — Rerank | called by Phases 2 + 3 | (see above) | Per (app_id, query, chunks) hash |
| 6 — Agent turn | recruiter sends message | 1× per planner step + 1× per tool that calls LLM | History in Redis 7-day TTL |
| 6 — Outreach draft | agent or one-click | 1× gemini-2.5-flash with strict JSON output | No |

**Model is configured via `LLM_MODEL_NAME` env var** (default `gemini-2.5-flash`). Operators can switch model versions without code changes.

---

## 9. Security boundaries

```mermaid
flowchart LR
    subgraph trust0["Trust level 0 — untrusted internet"]
        Anon["Anonymous candidate"]
    end

    subgraph trust1["Trust level 1 — authenticated"]
        Recruiter["Authenticated recruiter<br/>(JWT verified)"]
    end

    subgraph trust2["Trust level 2 — internal services"]
        Tools["Phase 6 agent tools<br/>(scope by current_user.id)"]
        Worker["Celery worker<br/>(scope by application.id)"]
    end

    subgraph trust3["Trust level 3 — trusted data"]
        DB["Postgres rows<br/>(after ownership check)"]
        JobPost["Job posting<br/>(recruiter-written)"]
    end

    subgraph trust_minus["Trust level -1 — actively hostile"]
        ResumeContent["Resume PDF content<br/>(candidate-supplied)"]
    end

    Anon -->|POST /upload + /process| FastAPIBoundary{"FastAPI<br/>input validation<br/>rate limit"}
    Recruiter -->|JWT bearer token| FastAPIBoundary
    FastAPIBoundary --> Tools
    FastAPIBoundary --> Worker
    Tools --> DB
    Worker --> DB

    ResumeContent -.->|"&lt;UNTRUSTED_RESUME&gt;<br/>tag wrap"| LLMPrompt["LLM prompt"]
    JobPost -->|"trusted block"| LLMPrompt
    LLMPrompt --> OutputFilter{"_filter_email_body<br/>URL allow-list"}
    OutputFilter -->|cleaned| Drafts[("outreach_email")]
    OutputFilter -->|"flagged URLs<br/>logged"| Audit["SECURITY log"]

    style ResumeContent fill:#ffcdd2
    style Anon fill:#ffe0b2
    style Recruiter fill:#fff9c4
    style LLMPrompt fill:#fff3e0
    style OutputFilter fill:#c8e6c9
```

**Three lines of defence against prompt injection in candidate resumes:**

1. **Tag-isolation in the prompt** — candidate content wrapped in `<UNTRUSTED_RESUME>` tags; system prompts have explicit rules to treat tag contents as data only.
2. **Output filtering** — `_filter_email_body()` strips any URL not on `APP_BASE_URL`'s host before persisting drafts.
3. **Recruiter approval** — the LLM never sends. Every outreach email requires an explicit human click.

See [`docs/security.md`](security.md) for the full threat model, mitigation matrix, and changelog.

---

## 10. Deployment topology — GCP free-tier

```mermaid
flowchart TB
    subgraph internet["Public Internet"]
        UserBrowser["User Browser"]
        DNS["DNS A record<br/>→ static IP"]
        ACME["Let's Encrypt<br/>ACME servers"]
    end

    subgraph gcp["GCP us-central1 (free tier)"]
        StaticIP["Static external IP<br/>(reserved + attached = free)"]
        Firewall["VPC firewall<br/>allow 80, 443, 22 (locked-down source ranges)"]

        subgraph vm["Compute Engine e2-micro<br/>(1 vCPU, 1 GB RAM, free tier)"]
            HostOS["Debian 12<br/>2 GB swap<br/>Docker Engine"]

            subgraph dockerC["Docker Compose<br/>(docker-compose.prod.yaml)"]
                nginx["nginx container<br/>publishes :80, :443<br/>TLS terminator + req rate limits"]
                certbot["certbot container<br/>(ACME renewal)"]
                api["fastapi container<br/>:8000 internal"]
                worker["celery worker container"]
                pg["postgres + pgvector<br/>:5432 internal"]
                redis["redis<br/>:6379 internal"]
                minio["minio<br/>:9000 internal"]
                certvol[("certbot/conf<br/>+ webroot volumes<br/>(shared)")]
            end
        end
    end

    subgraph external_apis["External APIs (outbound HTTPS only)"]
        Gem["Gemini API"]
        Res["Resend API"]
    end

    UserBrowser -->|HTTPS :443| StaticIP
    UserBrowser -.->|DNS resolve| DNS
    StaticIP --> Firewall --> nginx
    nginx --> api
    api --> pg
    api --> redis
    api --> minio
    worker --> pg
    worker --> redis
    worker --> minio
    api -.->|outbound| Gem
    api -.->|outbound| Res
    worker -.->|outbound| Gem
    worker -.->|outbound| Res
    certbot -.->|HTTP-01 challenge :80| ACME
    certbot --- certvol
    nginx --- certvol
```

**Why this shape**:

- Single-host because the bottleneck is Gemini's RPM quota, not compute.
- e2-micro is free indefinitely (US regions only — see [`docs/deployment-gcp.md`](deployment-gcp.md)).
- 2 GB swap because 1 GB RAM is tight with all containers running.
- **Nginx and Certbot both live inside `docker-compose.prod.yaml`** and share a Docker volume for the Let's Encrypt cert + webroot directory. Certbot writes the challenge file into the webroot volume; Nginx serves it on `:80` at the `/.well-known/acme-challenge/` path, so the HTTP-01 challenge resolves without bouncing outside the Compose network. See [`docs/https-ssl.md`](https-ssl.md).
- All non-Nginx containers listen on internal Compose-network addresses only; only the `nginx` container publishes `:80` and `:443` to the host.
- VPC firewall locks SSH (port 22) to the operator's IP range; only 80 / 443 open to the world.

---

## 11. Scaling considerations

Realistic next-step ladder when the free tier saturates:

| Symptom | Next step | Effort |
|---|---|---|
| Gemini quota exhausted daily | Upgrade Gemini to paid tier | Env var change |
| FastAPI CPU bound | `uvicorn --workers N` (single VM still) | docker-compose tweak |
| Celery worker queue depth growing | `celery --autoscale=6,2` (within container) | docker-compose tweak |
| Postgres connection pool exhausted | Add PgBouncer in front | New container in compose |
| Single VM CPU saturated | Bump e2-micro → e2-small / e2-medium | Resize, reboot |
| Need geographic redundancy | GCP Managed Instance Group + Load Balancer | Real infra work |
| Multiple worker hosts needed | Scale workers in MIG + Redis-backed shared broker | Real infra work |
| Genuine "platform" with multiple teams | Then and only then consider GKE / K8s | Weeks of work |

**Don't reach for Kubernetes until you're past at least three of these steps.** The autoscaling story is "Celery `--autoscale` for worker concurrency, MIG with custom metrics for HTTP, paid Gemini for LLM throughput." See [`docs/scaling-workers.md`](scaling-workers.md).

---

## 12. Glossary of phases

| Phase | What | Doc |
|---|---|---|
| 0 | Foundation: pgvector, embedding tables, Gemini client | [`phase-0-foundation.md`](ai-features/phase-0-foundation.md) |
| 1 | Resume + job chunking and embedding pipeline | [`phase-1-embedding-pipeline.md`](ai-features/phase-1-embedding-pipeline.md) |
| 2 | Semantic search across recruiter's pool | [`phase-2-semantic-search.md`](ai-features/phase-2-semantic-search.md) |
| 3 | Cross-job matching with bidirectional cosine | [`phase-3-cross-job-matching.md`](ai-features/phase-3-cross-job-matching.md) |
| 3.1 | Inverse cross-job view on per-job page | same doc, Update 3.1 |
| 4 | RAG-powered Q&A chat over a resume | [`phase-4-rag-qa.md`](ai-features/phase-4-rag-qa.md) |
| 5 | LLM rerank on top of pgvector (Phases 2 + 3) | [`phase-5-llm-reranking.md`](ai-features/phase-5-llm-reranking.md) |
| 5.1 | Search latency follow-up | same doc, Follow-up 5.1 |
| 5.2 | Top-K resume chunks to rerank | same doc, Follow-up 5.2 |
| 6 | Recruiter assistant agent + outreach | [`phase-6-agent.md`](ai-features/phase-6-agent.md) |

---

## 13. Where to read next

- **Designing a new AI feature**: start with [`phase-5-llm-reranking.md`](ai-features/phase-5-llm-reranking.md) — its decision-table format is the convention.
- **Operating this in production**: [`docs/deployment-gcp.md`](deployment-gcp.md), [`docs/https-ssl.md`](https-ssl.md), [`docs/scaling-workers.md`](scaling-workers.md), [`docs/security.md`](security.md).
- **Understanding the agent**: [`notes/agent-walkthrough.md`](../notes/agent-walkthrough.md) (gitignored personal notes — read after the Phase 6 doc).
- **Debugging an SSE issue**: section 5.5 (agent) or 5.4 (RAG) sequence diagrams above, then the relevant phase doc's failure-modes table.
