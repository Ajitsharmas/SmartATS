# Phase 4 — RAG-Powered Candidate Q&A

The headline AI feature of SmartATS: a chat interface inside each candidate's analysis modal that lets the recruiter ask natural-language questions about that candidate's resume, with grounded answers and citations. Answers stream token-by-token as Gemini generates them.

For the overall roadmap, see [roadmap.md](roadmap.md). Phase 4 depends on the resume embeddings produced by [Phase 1](phase-1-embedding-pipeline.md) and reuses the query-embedding cache from [Phase 2](phase-2-semantic-search.md).

---

## Goal

A recruiter opens a candidate's analysis modal, types *"Has this candidate worked with Kubernetes? Specifically with operators or just basic kubectl?"* and within ~2 seconds gets back a grounded answer like:

> *"Yes — managed a Kubernetes cluster of 40 nodes at Acme Corp (2022-2024), including writing custom operators using the Kubebuilder framework [chunk 3]. The resume does not mention basic kubectl-only work — their experience is at the platform engineering depth [chunk 3]."*

The answer is grounded in **this specific candidate's resume**, with the supporting excerpts visible underneath. If the question can't be answered from the resume, the system says so honestly instead of making something up.

---

## Acceptance criteria

- A new `POST /applications/{id}/chat` endpoint accepts a free-text question + recent conversation history and returns a streamed answer
- The endpoint runs a RAG pipeline: embed question → retrieve top-K relevant resume chunks → generate grounded answer via Gemini → return both the answer text and the citations
- Owner-scoped: a recruiter can only chat about candidates whose parent job they own
- The answer is streamed using **Server-Sent Events (SSE)** so tokens appear progressively in the UI as Gemini generates them
- Cited resume excerpts are returned alongside the answer for verification
- Multi-turn conversations work, with the last six turns preserved as context (Redis-backed sliding window, keyed by session ID)
- Rate-limited at 5 requests / minute per user — checked **before** the stream opens
- Smoke test covers a real RAG round-trip, an honest-refusal case, and the multi-tenancy guard

---

## Decisions

### 1. Streaming — Server-Sent Events (SSE)

Tokens stream from server to client as Gemini emits them. UX matches ChatGPT / Claude / Perplexity: the first token appears in ~500ms, regardless of total response length, so the interface always feels responsive.

Implementation: FastAPI's `StreamingResponse` with `media_type="text/event-stream"`. Each event is a single SSE message with a JSON payload encoding the event type (`token`, `citations`, `done`, `error`).

We use SSE rather than WebSockets because the data only flows server → client — full-duplex isn't needed. SSE is also simpler to consume from the browser and works through Nginx without special configuration.

### 2. Conversation memory — Redis-backed sliding window of 6 turns

Conversation history lives in Redis, keyed by `(user_id, application_id, session_id)`. The frontend tracks only a `session_id` (UUID v4 hex) and sends it with each request; the backend loads prior turns from Redis, runs the LLM, and appends the new turn after streaming completes.

**Window size:** 12 entries (6 user + 6 assistant turns), enforced atomically by `LTRIM` on every write. Typical Q&A about a single resume rarely needs more than three follow-ups; if the conversation outgrows the window, oldest turns simply roll off.

**TTL:** 24 hours, refreshed on every write. Active conversations stay alive; idle ones expire automatically — no cleanup job needed.

**Why Redis and not Postgres:**

| Property | Redis (chosen) | Postgres |
|---|---|---|
| Native append + trim ops | `RPUSH`, `LTRIM` — perfect fit | `INSERT` + window functions or app logic |
| TTL / auto-expiry | First-class via `EXPIRE` | Requires a cron job |
| Write speed | Sub-millisecond | ~5–10 ms with disk fsync |
| Schema overhead | None — just keys | Migration + table + indexes |
| Already in stack | Yes — Celery + rate limiter + query cache | Yes (different purpose) |

For ephemeral, append-only, time-limited data, Redis is what data stores like this are built for. Postgres would be over-engineering.

**Why backend-managed and not frontend-managed:**

- **Survives page refresh** — the recruiter can reload and continue the conversation
- **Less data over the wire** — frontend sends `session_id` only, not the whole history each turn
- **Tamper-resistant** — frontend cannot inject false prior turns to manipulate the LLM
- **Cross-device continuity** — the session ID could be shared across tabs/devices (not surfaced in UI yet, but the data model supports it)

**Graceful degradation:** if Redis is unreachable, `load_history` returns an empty list and `append_turn` silently drops the write. Chat still works without memory — same fallback philosophy as the Phase 2 query cache.

### 3. Retrieval — top-5 chunks per question

For each question we run a vector similarity search restricted to this candidate's resume only, taking the top 5 most relevant chunks.

A typical resume splits into 8–12 chunks at the Phase 1 default of 500-char windows. Five chunks ≈ the half of the resume most relevant to the current question — enough context for the LLM to answer, not so much that the signal drowns in noise.

### 4. Prompt enforcement strategy

The system prompt forces grounded answers and inline citations. The exact wording is in the code (see `app/rag.py`), but the structure is:

```
You are an assistant helping a recruiter understand a candidate's resume.

CRITICAL RULES:
1. Answer ONLY using the resume excerpts in the user message. Never use outside knowledge.
2. If the resume does not contain information to answer the question, say so explicitly:
   "The resume does not mention <topic>." Do NOT speculate.
3. After every factual claim, cite the chunk number it came from in square brackets, e.g. [chunk 2].
4. Keep answers concise and focused on what the resume actually says.
```

Followed by conversation history, then a user message containing:

```
RESUME EXCERPTS:
[chunk 1] ...
[chunk 2] ...
...

QUESTION: <the recruiter's new question>
```

This pattern is the standard production-RAG approach (Notion AI, Perplexity, Anthropic's own Claude Projects all use variants of it). Hallucination resistance comes from:

- Putting rules first, before the LLM sees any data
- Repeating "ONLY using the resume excerpts" prominently
- Requiring inline citations — when the LLM has to point to a chunk for each claim, it's forced to actually find the support
- Providing the explicit honest-refusal phrasing the LLM can use

### 5. Rate limiting — 5 requests / minute per user

Each chat request triggers one Gemini text-generation call (the expensive part) plus one embedding call (cached after first use). Rate limiting protects against runaway cost and queue exhaustion.

The limit is checked **before** the SSE stream opens. If exceeded, the client gets a clean `429` response — not a half-streamed message that errors mid-reply.

| Endpoint | Limit | Key |
|---|---|---|
| `POST /applications/{id}/chat` | 5 / minute | per user |

### 6. LangChain for chain composition

Phase 4 is where LangChain genuinely earns its place in the project. Specifically:

- **Streaming-capable LLM client** — `langchain_google_genai.ChatGoogleGenerativeAI` with `convert_system_message_to_human=False` and streaming enabled provides a Pythonic `stream()` interface that yields `AIMessageChunk` objects as Gemini generates them.
- **Message construction** — `SystemMessage`, `HumanMessage`, `AIMessage` classes make the prompt assembly explicit and readable.
- **Provider swap-out** — if Gemini ever stops working, swapping to OpenAI or Anthropic is changing one class name.

Hand-rolling the same streaming + retry + structured message handling would be 50+ lines of brittle code. LangChain is ~15 lines and well-tested.

### 7. Empty-embeddings edge case

If the candidate's `resume_embedding` table is empty (Phase 1 task failed, never ran, or was skipped due to short text), retrieval returns zero chunks. We do **not** call Gemini with empty context — it would either hallucinate or refuse uselessly.

Instead the endpoint returns a single SSE event with a system-style message:

```json
{
  "type": "system_message",
  "content": "This candidate's resume has not been processed for AI Q&A yet. Try the Retry button on the candidate row, or upload again."
}
```

The frontend renders this differently from a real LLM answer so the recruiter knows it's a status message.

### 8. Citations format

The LLM is instructed to cite chunks inline as `[chunk N]`. The endpoint returns:

- The streamed answer text (with the inline `[chunk N]` references intact)
- A `citations` event after streaming completes, carrying the full text of all retrieved chunks

The frontend shows the answer above and a collapsible citations panel below, so the recruiter can verify any specific claim against the source resume.

### 9. Per-application retrieval (not global)

Phase 2's semantic search ran across the entire applicant pool. Phase 4 runs against **one resume only**. The SQL adds a `WHERE application_id = :application_id` filter.

This is a deliberate scoping difference: Phase 2 answers *"who in my pool matches this query?"* — Phase 4 answers *"what does this specific resume say about X?"*. The retrieval query is much simpler as a result.

### 10. Query embedding cache reused from Phase 2

The question text is embedded via the existing `embed_query_cached()` helper, which uses the Redis-backed 1-hour TTL cache. Common follow-up phrases like *"What about Python?"* are essentially free to embed.

---

## Architecture

```
Recruiter types question in modal (frontend has session_id)
              │
              ▼
POST /applications/{id}/chat   ←── SSE stream opens
              │
              ├── Auth + ownership check (parent_job.owner_id == current_user.id)
              │
              ├── Rate limit (5/min per user) — checked BEFORE stream starts
              │
              ├── Look up resume chunks for this application
              │      │
              │      └── If zero chunks → return system_message event, close stream
              │
              ├── load_history(user_id, application_id, session_id)
              │      │
              │      └── Redis LRANGE — returns last 12 turns (or [] on miss / error)
              │
              ├── embed_query_cached(question)     (Phase 2 Redis cache)
              │
              ├── pgvector similarity search (top-5, this application only)
              │
              ├── Build LangChain messages: SystemMessage + history + final HumanMessage
              │                              with resume excerpts + question
              │
              ├── LangChain → ChatGoogleGenerativeAI.stream(messages)
              │
              ├── For each token: yield as SSE event
              │      │
              │      └── frontend: append token to current assistant message
              │
              ├── After all tokens: yield citations event, then done event
              │      │
              │      └── frontend: render citations panel
              │
              └── append_turn() × 2 — persist user question + assistant reply to Redis
                     │
                     └── Redis RPUSH + LTRIM (cap at 12) + EXPIRE (24h)
```

### Redis usage summary

Redis is now used for four distinct purposes, each on its own key prefix:

| Purpose | Key pattern | TTL | Eviction-eligible? |
|---|---|---|---|
| Celery broker (task queue) | `celery-task-meta-*`, queue keys | none | **No** (no TTL) |
| Rate limiter counters | SlowAPI internal keys | per-window | Yes |
| Query embedding cache | `emb:<hash>` | 1 hour | Yes |
| Chat history (Phase 4) | `chat:<user>:<app>:<session>` | 24 hours | Yes |

Both compose files configure Redis with `maxmemory 256mb`, `maxmemory-policy volatile-lru`, and `appendonly yes`. Under memory pressure Redis evicts the least-recently-used keys *that have a TTL* (chat history, cache, rate counters), while Celery task messages — which have no TTL — are never evicted. This means a memory squeeze gracefully degrades chat history and cache freshness without losing in-flight tasks.

---

## SSE event schema

The endpoint emits a series of newline-delimited events. Each event is a `data: <json>\n\n` block. The JSON has a `type` field discriminating the payload:

| Event type | Payload | Purpose |
|---|---|---|
| `token` | `{"content": "..."}` | One token (or short run) of the LLM's answer |
| `citations` | `{"citations": [{"chunk_index": int, "chunk_text": str, "similarity": float}, ...]}` | Sent after all tokens — the chunks that informed the answer |
| `system_message` | `{"content": "..."}` | A status message (e.g. "resume not processed yet"), shown distinctly from an LLM answer |
| `error` | `{"detail": "..."}` | Recoverable error during streaming (e.g. Gemini 503 mid-stream); frontend shows it and the user can retry |
| `done` | `{}` | Stream is complete; close the reader |

---

## SQL for chunk retrieval

```sql
SELECT
    chunk_index,
    chunk_text,
    1 - (embedding <=> CAST(:query_vector AS vector)) AS similarity
FROM resumeembedding
WHERE application_id = :application_id
ORDER BY embedding <=> CAST(:query_vector AS vector)
LIMIT :top_k;
```

Much simpler than the Phase 2 search SQL:

- **No multi-tenancy concern at SQL level** — already enforced by the endpoint's ownership check (the `application_id` is verified before this query runs).
- **No `ROW_NUMBER` aggregation** — Phase 4 wants every relevant chunk, not "best one per candidate".
- **No threshold filter** — retrieval returns top-K regardless of similarity scores. The LLM decides which retrieved chunks actually support an answer; if all five are weak, the LLM's honest-refusal logic kicks in.

The HNSW index on `resumeembedding.embedding` accelerates the ordering. The `application_id` predicate uses the existing B-tree index on that column to narrow the candidate set before the HNSW walk.

---

## Schemas

```python
class ChatTurn(BaseModel):
    """One stored turn — used internally by app/chat_history.py."""
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    """
    Request body for POST /applications/{id}/chat.

    Conversation history is stored server-side in Redis, keyed by
    (user_id, application_id, session_id). The frontend only sends a
    session_id; the backend loads prior turns from Redis. A fresh
    session_id starts a new conversation; reusing one continues it.
    """
    question: str = Field(min_length=3, max_length=1000)
    session_id: str = Field(min_length=8, max_length=64)


class Citation(BaseModel):
    chunk_index: int
    chunk_text: str
    similarity: float
```

The streaming response itself is not described by a Pydantic schema — it's a series of SSE events documented in the table above.

---

## Files to create / modify

| File | Change |
|---|---|
| `app/models.py` | Add `ChatTurn`, `ChatRequest` (with `session_id`), `Citation` schemas |
| `app/rag.py` | **NEW**: LangChain RAG chain — system prompt, message builder, streaming generator |
| `app/chat_history.py` | **NEW**: Redis-backed conversation history — `load_history`, `append_turn`, `reset_session`, `new_session_id` |
| `app/main.py` | New `POST /applications/{id}/chat` endpoint with auth, rate limit, ownership check, SSE streaming, history load/save |
| `app/static/dashboard.html` | Add chat panel to the analysis modal; track `session_id` (UUID hex) in JS state instead of history array |
| `app/static/js/api.js` | `Api.chatStream(applicationId, question, sessionId, callbacks)` SSE consumer |
| `docker-compose.yaml`, `docker-compose.prod.yaml` | Configure Redis with `maxmemory`, `maxmemory-policy volatile-lru`, and `appendonly yes` |
| `scripts/smoke_test_phase4.py` | End-to-end smoke test, including Redis history append/load/trim/reset assertions |

---

## Failure modes

| Failure | Handling |
|---|---|
| No embeddings for this application | Single `system_message` event then stream closes — no Gemini call |
| Gemini unavailable | `error` event with recruiter-friendly message; stream closes |
| Gemini rate-limited (Google's own 429) | `error` event explaining the upstream cap; stream closes |
| LLM hallucinates outside the context | Mitigated by strict prompt + citation enforcement; recruiter sees the citations and can verify |
| Junk question | Retrieval returns low-similarity chunks; LLM should reply "the resume does not mention ..." per the prompt rules |
| Network drops mid-stream | Frontend treats partial response as final; recruiter can re-ask |
| Concurrent chat requests by the same recruiter | Rate limit caps to 5/min — beyond that, 429 before any stream opens |

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Multi-tenancy: chatting about someone else's candidate | Ownership check (`parent_job.owner_id == current_user.id`) at endpoint entry, before any work happens |
| LLM hallucinates beyond the resume | Strict system prompt requiring inline citations; recruiter sees the citation panel and can verify |
| Cost explosion under load | 5/min rate limit; query-embedding cache (Phase 2) absorbs repeated questions; LangChain swaps to a cheaper model trivially if needed |
| Conversation history loss | History lives in Redis with a 24-hour TTL, refreshed on every write, and survives container restarts via AOF. Loss is only possible if the recruiter explicitly clicks "New conversation" or the TTL expires from idle. Redis being unreachable degrades gracefully — chat continues without prior context |
| SSE connection lifecycle issues behind Nginx | `proxy_buffering off` and `Cache-Control: no-cache` are set in the response so Nginx forwards events immediately |

---

## Scalability notes

- **Per-request latency is dominated by Gemini generation** (~1–3s for typical responses). Retrieval and prompt assembly are sub-100ms.
- **Streaming reduces perceived latency** — first token in ~500ms even on slow responses. Subjectively much faster than a 3-second wait for a fully-formed reply.
- **Stateless FastAPI workers** — conversation state lives in Redis, not in process memory. Any FastAPI worker can serve any chat request. Horizontal scaling is just "add more workers" — no sticky sessions needed.
- **Free-tier ceiling is the practical limit.** Gemini 2.5 Flash free tier: 15 RPM, 1500 requests/day. The 5/min per-user limit means 3 concurrent users can saturate free tier. Paid tier removes this cap entirely with no application changes.
- **Conversation context size** at full memory: 5 chunks × ~150 tokens + 6 history turns × ~50 tokens + question + system prompt ≈ 1500 tokens. Comfortably within Gemini's 1M token context window.

---

## Smoke test — `scripts/smoke_test_phase4.py`

After Phase 4 lands:

1. Create recruiter A, a test job owned by A, and an application
2. Embed a resume with specific, verifiable content (e.g. Python + Kubernetes + Acme Corp work)
3. Call `POST /applications/{id}/chat` with a question that's answerable from the resume: *"What was their main project at Acme Corp?"*
4. Assert: the streamed answer contains Acme Corp content and at least one `[chunk N]` reference
5. Assert: the `citations` event contains the expected chunks
6. Call with a question that is NOT in the resume: *"Has this candidate worked at Microsoft?"*
7. Assert: the answer honestly says "does not mention" / "no mention of" — does NOT hallucinate
8. Create recruiter B and try chatting about recruiter A's candidate
9. Assert: `403` (multi-tenancy)
10. Create an application with NO embeddings and chat against it
11. Assert: a single `system_message` event explaining the resume hasn't been processed
12. Cleanup

Note: testing streaming output programmatically is awkward — the test collects the streamed tokens into a single string and asserts on the final content.

---

## Out of scope for Phase 4

- **Long-term chat history retention** — conversations expire from Redis after 24 hours of inactivity. Preserving them beyond that (e.g. for audit logs, "show me past conversations" UI, cross-recruiter handoff) would require Postgres-backed persistence; deferred.
- **Multi-resume Q&A** — *"compare these 3 candidates"* spans multiple applications. That's the territory of Phase 5 (Recruiter Assistant Agent).
- **Re-ranking the retrieved chunks** — standard for production RAG quality but adds another LLM call. Deferred.
- **Hybrid retrieval (semantic + keyword)** — deferred.
- **Code-aware chunking for tech resumes** — current chunking is fine for plain text. AST-based splitting could improve retrieval quality for resumes with code blocks; deferred.

---

## Running the Phase 4 smoke test

After Phase 4 changes are in place, validate with `scripts/smoke_test_phase4.py`. The test bypasses the SSE HTTP layer (programmatic SSE consumption is awkward) and instead invokes the underlying RAG components directly, asserting on the accumulated response.

### Steps (local dev)

```bash
# Restart the stack to pick up the new endpoint and dependencies
docker compose down
docker compose up -d --build

# Run the smoke test from the local venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/smoke_test_phase4.py
```

Or inside the worker container:

```bash
docker compose exec worker python scripts/smoke_test_phase4.py
```

### Expected output

```
=== Phase 4 smoke test — RAG Q&A ===

OK:   Database is initialised
OK:   Created recruiter, job, application (id=1)
OK:   Embedded resume via Phase 1 pipeline
OK:   Retrieved 5 chunks for the answerable question
OK:   Answer references resume content (matched keywords: ['acme', 'microservice', 'kafka'])
OK:   Answer includes [chunk N] inline citations
OK:   Honest refusal: LLM said it does not have information about Microsoft
OK:   Retrieval correctly returns zero chunks for an application with no embeddings
OK:   Redis chat history append + load round-trip works
OK:   History is isolated by session_id — fresh session sees no prior turns
OK:   Sliding-window trim keeps only the latest 12 entries
OK:   reset_session clears the conversation
OK:   Cleaned up test data via cascades

=== Phase 4 smoke test passed ===
```

If the **honest refusal** assertion fails, that's a hallucination risk — investigate the prompt in `app/rag.py`. The exact keyword match list is intentionally lenient ("does not mention", "no mention of", "doesn't say", etc.) so the test passes on any reasonable refusal phrasing.

### Manually testing streaming UX

The smoke test does not exercise the SSE wire protocol. To verify the UI end-to-end:

1. Open the dashboard, click into a candidate with embeddings
2. Type a question and submit
3. Tokens should appear progressively (not all at once)
4. After the answer completes, the "Cited excerpts" panel should be available with the supporting chunks
5. Ask a follow-up like "what about Python?" — the answer should reflect the prior turn's context

---

## Status

Design approved. Implementation complete. Smoke test ready to run.
