# Phase 6 — Recruiter Assistant Agent

A conversational agent on the recruiter dashboard that reasons across the data built in Phases 1–5 and drafts candidate outreach. Scope is intentionally narrow: read-only data exploration plus job-description authoring plus draft-only email composition. The agent never sends anything itself — every email goes out only after the recruiter clicks Send.

For the overall roadmap, see [roadmap.md](roadmap.md). Depends on [Phase 2](phase-2-semantic-search.md) (semantic search), [Phase 3](phase-3-cross-job-matching.md) (cross-job matching), [Phase 4](phase-4-rag-qa.md) (RAG chat), and [Phase 5](phase-5-llm-reranking.md) (the rerank + critique substrate).

---

## Why this is needed

Phases 1–5 give the recruiter a powerful but *fragmented* surface: search candidates here, view applicants there, chat about a resume in a modal, see cross-matches in a side section. Common workflows still require manual orchestration — *"find my top 3 Kubernetes candidates, compare them, draft outreach to the strongest"* is five distinct UI gestures today.

The agent makes those workflows a single sentence. It also unlocks two things the static UI cannot:

1. **Cross-data reasoning** — *"which of my roles is Alice the best fit for, and why?"* requires looking at every job, scoring Alice against each, and synthesising the answer. The agent does this in one turn.
2. **Contextual outreach** — drafting personalised emails grounded in both the resume content and the job description. A recruiter writes 10 of these a week; saving them at 5 minutes each is real time back.

Equally important is what this phase is **not**: it is not an autopilot. It does not change application statuses, schedule interviews, or send emails on its own. Every action with side effects requires a recruiter click. We are buying *speed-of-drafting*, not *autonomy*.

---

## Goal

A recruiter types *"draft an email inviting Alice to apply for my Tech Lead role"* into a chat panel. The agent:

1. Looks up Alice's application (and her resume context if needed)
2. Looks up the Tech Lead job posting
3. Drafts a short, personalised email referencing only facts present in Alice's resume and the job posting
4. Returns the draft as an editable card with Send / Discard / Edit buttons
5. Persists the draft in the database

The recruiter reads the draft, edits two words, clicks Send. The email goes out via the existing Resend integration. The send is logged.

In parallel, the recruiter can also **one-click drafts** from the candidate modal's *"Also a good fit for"* section — same draft, same Send pipeline, no chat needed.

---

## Acceptance criteria

- A new `/assistant` page in the recruiter UI with a streaming chat interface
- Single rolling conversation per recruiter (with a "New conversation" button to reset)
- Streaming SSE protocol that surfaces intermediate tool calls and the final answer
- Tool palette covering data lookup (read-only), job-description authoring (generation), and email draft composition (draft-only)
- New `outreach_email` table persists every draft and every send
- Resend integration sends approved drafts from `noreply@<APP_BASE_URL_DOMAIN>` with the recruiter's name in the email body
- Contextual one-click *"Draft invite email"* button on every row of the candidate modal's *"Also a good fit for"* section, pre-filled with the matched job's `/job/{id}` URL
- Multi-tenancy enforced on every tool — recruiters cannot read or write across pools
- Guardrails: max tool calls per turn, validated tool args, hallucination-resistant email prompts
- Smoke test exercising the full loop (chat-initiated draft, one-click draft, send)

---

## Architecture

```
                              ┌──────────────────────────────────────────────┐
                              │  /assistant page  (recruiter chat UI)        │
                              │                                              │
                              │  ┌────────────────────────────────────────┐  │
                              │  │  POST /assistant/turn  (SSE stream)    │  │
                              │  └────────────────────────────────────────┘  │
                              └──────────────────────────────────────────────┘
                                            │
                                            ▼
                              ┌──────────────────────────────────────────────┐
                              │  LangGraph ReAct loop                        │
                              │                                              │
                              │   plan → tool_call → observe → repeat → end  │
                              │                                              │
                              │   max 8 tool calls per turn                  │
                              │   max 4000 output tokens per turn            │
                              └──────────────────────────────────────────────┘
                                            │
                ┌──────────────────────────┼──────────────────────────┐
                ▼                          ▼                          ▼
   ┌───────────────────────┐   ┌────────────────────────┐   ┌──────────────────────┐
   │ Read-only tools       │   │ Generation tools       │   │ Outreach tools       │
   │  (Tier 2)             │   │  (Tier 3)              │   │  (Tier 5)            │
   │                       │   │                        │   │                      │
   │ list_jobs             │   │ draft_job_description  │   │ draft_email          │
   │ get_job_details       │   │ improve_job_description│   │ list_drafts          │
   │ get_applicants        │   │ generate_interview_qs  │   │                      │
   │ get_candidate         │   │ generate_rubric        │   │ ┌──────────────────┐ │
   │ get_cross_matches     │   │                        │   │ │ persists drafts  │ │
   │ search_candidates     │   │ pure LLM, no I/O       │   │ │ in outreach_email│ │
   │ ask_about_resume      │   │                        │   │ │ never auto-sends │ │
   │                       │   │                        │   │ └──────────────────┘ │
   │ all auth-scoped       │   │                        │   │                      │
   └───────────────────────┘   └────────────────────────┘   └──────────────────────┘

   ──── separate UI surface ────

   Candidate modal → "Also a good fit for" → [Draft invite email] button
                                                       │
                                                       ▼
                       POST /applications/{id}/cross-match-invite?matched_job_id=N
                                                       │
                                          (calls draft_email directly,
                                           no agent loop needed)
                                                       │
                                                       ▼
                                              outreach_email row
                                                       │
                                                       ▼
                                          UI shows editable draft modal
                                                       │
                                              [Edit] [Send] [Discard]
                                                       │
                                                       ▼
                                       POST /assistant/drafts/{id}/send
                                                       │
                                                       ▼
                                                  Resend API
```

Two outreach surfaces, one backend. The chat agent calls `draft_email(...)` as a tool when reasoning multi-step; the cross-match-invite button hits the same Python function directly via a focused endpoint when no reasoning is needed. Both write the same row in `outreach_email`. Both go through the same `POST /assistant/drafts/{id}/send` for the actual Resend call.

---

## Decisions

### 1. Framework — LangGraph

We already depend on LangChain (Phase 4 RAG, Phase 5 rerank). LangGraph is the LangChain team's stateful-agent layer and fits naturally. Alternatives considered and rejected:

- **Anthropic tool-use directly** — clean API but adds a second SDK to the stack. Skip.
- **Pydantic-AI** — newer, more opinionated. Solid for new projects, not worth swapping for here.
- **Hand-rolled ReAct loop** — simpler than LangGraph but loses observability, retry, and tool-error-recovery primitives we'd reinvent badly.

The graph itself is intentionally minimal: a single ReAct loop with `tools_condition`-style routing. No multi-agent orchestration, no parallel branches. The complexity is in the tools, not the graph.

### 2. Model — Gemini 2.5 Flash for both planning and synthesis

Same model already used for Phase 5 rerank and Phase 4 chat. Free-tier-friendly until quota is exhausted. Temperature `0.2` for the planner (consistent tool choice), `0.4` for synthesis turns where we want some warmth in email drafts. Tool-calling is supported natively via `google-genai`'s function-declarations API; LangGraph wraps this.

### 3. Tool palette — three categories, eleven tools

**Read-only data lookup (Tier 2):**

| Tool | Wraps | Purpose |
|---|---|---|
| `list_jobs()` | `GET /my-jobs` | All jobs the recruiter owns |
| `get_job_details(job_id)` | `GET /my-jobs` filtered | Full posting: title, description, skills, location, salary |
| `get_applicants(job_id, status=None)` | `GET /applications/{job_id}` | Ranked applicants for one job |
| `get_candidate(application_id)` | DB lookup | Name, email, score, critique for one applicant |
| `get_cross_matches(application_id)` | `GET /applications/{id}/matches` | Phase 3 cross-job match list |
| `search_candidates(query, limit=5)` | `POST /search/candidates` | Phase 2 semantic search |
| `ask_about_resume(application_id, question)` | wraps Phase 4 RAG | Non-streaming, returns final answer + citations |

**Generation (Tier 3):**

| Tool | Inputs | Output |
|---|---|---|
| `draft_job_description(role, key_skills, context="")` | Free-text role + comma list + optional bullet context | Markdown job description |
| `improve_job_description(current, instruction)` | Existing description + edit instruction | Rewritten description |
| `generate_interview_questions(job_id, count=8)` | Pulls job context via `get_job_details` | List of questions |
| `generate_screening_rubric(job_id)` | Pulls job context | Structured rubric (criteria + weights) |

**Outreach (Tier 5):**

| Tool | Inputs | Output / side effect |
|---|---|---|
| `draft_email(application_id, intent, tone="professional", custom_notes="", target_job_id=None)` | `intent ∈ {rejection, interview_invite, offer, follow_up, cross_match_invite, custom}` | Returns `EmailDraft(id, subject, body)`. Persists row in `outreach_email`. `target_job_id` used only for `cross_match_invite` to embed the public job URL. |
| `list_drafts(application_id=None, status="draft")` | Optional filters | Returns previous drafts so the agent can avoid duplicates |

The agent **cannot** send. Send is a separate non-tool endpoint that the UI calls after recruiter approval.

### 4. Authorisation — every tool re-validates ownership

Tools accept `current_user` (the authenticated recruiter) as the first implicit argument. Each tool re-runs the same owner-scope checks the underlying endpoints do (e.g., `JobListing.owner_id == current_user.id`). The agent's planner can hallucinate any integer; the tool layer is the security boundary.

For cross-match-invite specifically: both the application's parent job *and* the `target_job_id` must belong to `current_user`. CrossJobMatch only ever lives within a pool, so if `matched_job_id` is owned, the application is too — but we check both defensively.

### 5. Outreach send flow — recruiter-approved, never automatic

Hard rule: the LLM cannot trigger a network send under any circumstances. The send pipeline is:

```
LLM tool call: draft_email(...)
   │
   ▼
outreach_email row created with status="draft"
   │
   ▼
SSE event "email_draft" → UI renders editable card
   │
   ▼  (recruiter edits, clicks Send)
   │
   ▼
POST /assistant/drafts/{id}/send
   │
   ▼
Resend API call
   │
   ▼
outreach_email row updated: status="sent", sent_at=now()
```

If the recruiter clicks Discard, the row is updated to `status="discarded"` rather than deleted, so we keep the audit trail of what the LLM proposed.

### 6. Sender identity — single shared `noreply@<APP_BASE_URL>`

V1 sends every email from one address (same as the verification flow). The recruiter's name appears in the email body and the `Reply-To` header so candidates know who's reaching out and can reply directly to that recruiter.

We deliberately do not give each recruiter a configurable `From` address. Per-recruiter sender domains would require:
- Per-recruiter Resend domain verification (DKIM, SPF, return-path)
- A UI for the recruiter to configure their sending domain
- Bounce/complaint routing per recruiter

That's a real product feature; it is not v1.

### 7. Conversation memory — single rolling chat per recruiter

One conversation per recruiter, persisted in Redis with the same sliding-window trimming pattern Phase 4 uses for the candidate modal chat. Key: `agent:history:{user_id}` (no per-application scoping — this conversation spans the whole pool). Window: 16 turns max (8 user + 8 assistant). A *"New conversation"* button clears the key.

Multi-session (sidebar with named past chats, ChatGPT-style) is a v2 enhancement. The work is mostly Postgres rows + a sidebar UI; the agent itself doesn't change.

### 8. SSE event protocol — extends Phase 4's pattern

The chat panel needs to render more than just streaming tokens. Event types:

| Event | Payload | UI rendering |
|---|---|---|
| `thinking` | `{ content: str }` | Greyed-out italic placeholder under the assistant label. Emitted once per turn (`"Planning…"`). Per-tool *"Using tool: X…"* events were removed — the chip itself with its friendly label + spinner conveys the same info without leaking raw tool names. |
| `tool_call` | `{ tool_call_id, name, args }` | Collapsible `<details>` chip with a friendly label (e.g. *"Looking up your jobs"* not `list_jobs({})`). Raw `name` + `args` are hidden inside the disclosure for debugging. |
| `tool_result` | `{ tool_call_id, name, summary, errored }` | Flips the chip's spinner to a green dot ("Done: …") on success or a red dot ("Failed: …") when `errored=true`, with the raw result revealed inside the disclosure. The `errored` flag is set when the tool returned a JSON `{"error": "..."}` payload. |
| `token` | `{ content }` | Appended to the assistant bubble's text region. Emitted **only once per chat-model invocation** — the agent buffers chunks internally and flushes the full synthesis at `on_chat_model_end` (live token-by-token streaming was a casualty of the buffering fix, see Post-release fixes below). |
| `email_draft` | `{ draft_id, application_id, subject, body, intent, target_job_id?, candidate_name, candidate_email }` | Inline editable email card with Review & Send button. Visually rendered **above** the synthesis text in the bubble (DOM order matters — see Post-release fixes). |
| `done` | `{}` | End of turn marker. Clears the thinking placeholder. |
| `system_message` | `{ content }` | Amber notice for soft failures — quota exhausted, transient Gemini outage. Reuses Phase 5.1's `_classify_gemini_error` classifier so the message text matches the Phase 4 chat experience. |
| `error` | `{ detail }` | Red notice for genuine bugs. Raw exception text never reaches the user; the server-side `print()` keeps the diagnostic. |

Same SSE plumbing as Phase 4 chat — `text/event-stream`, blank-line separated, JSON-encoded payload.

### 9. Guardrails — non-negotiable

- **Max tool calls per turn**: 8. After this the loop force-exits with a synthesis step.
- **Max output tokens per turn**: 4000.
- **Tool argument validation**: every tool input is a Pydantic schema. Invalid args return `{"error": "..."}` to the model rather than raising — the agent can recover and try different args.
- **Tool result truncation**: any tool returning >2 KB of text is truncated with a *"…(N more items hidden)"* marker, so the model context doesn't bloat across turns.
- **Email prompt rules**: the `draft_email` system prompt explicitly forbids inventing facts. The prompt embeds the resume text and job text directly and instructs *"only reference information present in the provided resume and job posting. Do not invent companies, dates, or accomplishments."* Hallucinated personalisation is the canonical LLM outreach failure mode.
- **Rate limits**: `/assistant/turn` capped at 10/min per user (each turn = several LLM calls); `/assistant/drafts/{id}/send` capped at 30/hour per user.

### 10. Cost projection — paid tier required for real use

- Average turn ≈ 4–6 LLM calls (1 planning + 2–4 tools + 1 synthesis).
- Average tool latency ≈ 1–2 s, sequential per turn.
- Wall clock per turn ≈ 8–15 s, hence streaming intermediate events is mandatory or it feels broken.
- Free-tier 15 RPM caps you at ~3 turns/minute. **Production usage requires the paid Gemini tier.** Documented; not solvable in code.

Caching helps absorb the cost spike:
- **Within-turn tool cache**: tool results are cached in-process for the duration of the turn, keyed by `(tool_name, args_hash)`. So `list_jobs()` called twice in one turn hits Postgres once.
- **Cross-turn tool cache**: tool results cached in Redis with 60 s TTL, keyed by `(user_id, tool_name, args_hash)`. Within a single conversation the recruiter often asks follow-ups about the same data.

---

## Schemas

### Database — new `outreach_email` table

```python
class OutreachEmail(SQLModel, table=True):
    """
    A single AI-drafted outreach email. Persisted at draft time (by the agent
    or the cross-match-invite shortcut) and never deleted — `status` tracks
    lifecycle. Sent only after recruiter clicks Send via the UI.
    """
    id: int | None = Field(default=None, primary_key=True)

    application_id: int = Field(
        sa_column=Column(Integer, ForeignKey("application.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    )
    recruiter_id: int = Field(
        sa_column=Column(Integer, ForeignKey("user.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    )

    # intent ∈ {rejection, interview_invite, offer, follow_up, cross_match_invite, custom}
    intent: str

    # If the intent is cross_match_invite, this is the job we're inviting them
    # to apply to. The public URL https://<host>/job/{target_job_id} is
    # embedded in the email body.
    target_job_id: int | None = Field(
        sa_column=Column(Integer, ForeignKey("joblisting.id", ondelete="SET NULL"),
                         nullable=True)
    )

    subject: str
    body: str = Field(sa_column=Column(Text, nullable=False))

    # draft | sent | discarded
    status: str = Field(default="draft")

    custom_notes: str | None = None  # What the recruiter told the agent

    created_at: datetime = Field(default_factory=datetime.now)
    sent_at: datetime | None = None
```

### API — request/response models

```python
class AssistantTurnRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    # Conversation is server-side keyed by user_id; no session_id field
    # because we ship the single-rolling-chat variant in v1.

class CrossMatchInviteRequest(BaseModel):
    matched_job_id: int

class DraftSendResponse(BaseModel):
    status: str            # "sent"
    sent_at: datetime
    message_id: str | None # Resend message id, if available

class EmailDraftPublic(BaseModel):
    """Response shape for list/get of outreach_email rows."""
    id: int
    application_id: int
    candidate_name: str
    candidate_email: str
    intent: str
    target_job_id: int | None
    target_job_title: str | None
    subject: str
    body: str
    status: str
    created_at: datetime
    sent_at: datetime | None
```

---

## API endpoints

| Endpoint | Method | Purpose | Rate limit |
|---|---|---|---|
| `/assistant/turn` | POST (SSE) | Drive one agent turn. Body = `AssistantTurnRequest`. Streams the events from §8. | 10/min per user |
| `/assistant/reset` | POST | Clears the rolling chat history for `current_user`. | 30/min per user |
| `/assistant/drafts` | GET | List drafts for `current_user`. Optional `?application_id=X` filter. | 60/min per user |
| `/assistant/drafts/{draft_id}/send` | POST | Send via Resend. Updates `status` and `sent_at`. | 30/hour per user |
| `/assistant/drafts/{draft_id}/discard` | POST | Soft-delete (`status = discarded`). | 30/min per user |
| `/applications/{application_id}/cross-match-invite` | POST | One-click contextual draft. Body = `CrossMatchInviteRequest`. Calls `draft_email` directly, no agent loop. Returns `EmailDraftPublic`. | 30/hour per user |

All endpoints protected via the existing `get_current_user` dependency. All return rows scoped to `current_user` only.

---

## UI

### New `/assistant` page

Layout:

```
┌─────────────────────────────────────────────────────────────────┐
│  [SmartATS]                  Jobs · Search · Settings · Assistant │ ← new nav tab
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Assistant                                  [↻ New conversation]│
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │ You: Find my top 3 Python candidates with cloud         │    │
│  │      experience and draft outreach to the strongest.    │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │ ▸ search_candidates(query="Python with cloud", limit=5) │    │
│  │ ▸ get_candidate(application_id=42)                      │    │
│  │ ▸ draft_email(application_id=42, intent="follow_up")    │    │
│  │                                                          │    │
│  │ Assistant: Your top three Python+cloud candidates…       │    │
│  │   1. Alice Chen — 88% match, …                           │    │
│  │   2. Jordan Park — 84% match, …                          │    │
│  │   3. Sam Wu — 79% match, …                               │    │
│  │                                                          │    │
│  │ Draft for Alice Chen ────────────────────────────┐       │    │
│  │ Subject: [editable]                              │       │    │
│  │ Body:    [editable, multiline]                   │       │    │
│  │          ┌───────────┐ ┌────────┐ ┌────────┐    │       │    │
│  │          │  Discard  │ │  Edit  │ │  Send  │    │       │    │
│  │          └───────────┘ └────────┘ └────────┘    │       │    │
│  └──────────────────────────────────────────────────┘       │    │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │ [type a message…]                              [Send]   │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

Files:
- `app/static/assistant.html`
- `app/static/js/assistant-page.js`
- Route: `GET /assistant` serves the page (auth-protected, same pattern as the other recruiter pages)
- Nav: a fourth tab *"Assistant"* added to the shared nav across all recruiter pages

### Candidate modal — cross-match invite button

In the *"Also a good fit for"* section ([candidate-modal.js](../../app/static/js/candidate-modal.js)), each row currently renders:

```
┌──────────────────────────────────────────────────┐
│  Tech Lead                          85% match    │
│  Job ID: 2                                       │
│  Strong overlap on distributed systems, mentorship…│
└──────────────────────────────────────────────────┘
```

Add a small button on each row:

```
┌──────────────────────────────────────────────────┐
│  Tech Lead                          85% match    │
│  Job ID: 2                                       │
│  Strong overlap on distributed systems, mentorship…│
│                          [Draft invite email →]  │
└──────────────────────────────────────────────────┘
```

Click → `POST /applications/{application_id}/cross-match-invite` with `matched_job_id`. Opens an inline draft modal pre-filled with the agent's draft (subject + body), with Send / Discard / Edit buttons. The email body always includes `${APP_BASE_URL}/job/${target_job_id}` as the public link the candidate clicks to view the matched role and apply.

---

## Prompt design

### System prompt for the planner

```
You are a recruiter assistant agent for the SmartATS platform. You help
the authenticated recruiter explore their candidate pool, draft job
descriptions, and compose outreach emails.

You have access to a set of tools. Choose tools carefully — each call
takes 1–2 seconds. Prefer one well-chosen call over many speculative
calls.

Rules:
- Every tool you call is automatically scoped to the current recruiter.
  You cannot access another recruiter's data even if you guess an id.
- Never invent a candidate, job, or fact. If you do not have data,
  call a tool to fetch it.
- When asked to draft an email, you MUST call `draft_email`. Do not
  produce email content in your reply directly — drafts must be
  persisted so the recruiter can edit and send them.
- After at most 8 tool calls per turn, you will be forced to synthesise
  whatever you have. Plan accordingly.
- Cite job ids when you reference jobs in your reply.

Output style: concise, business-tone, no emoji, no fluff.
```

### System prompt for `draft_email`

```
You are drafting an outreach email on behalf of a recruiter. The
recruiter's name is {recruiter_name}. The candidate's name is
{candidate_name}.

CRITICAL: you may only reference information that appears in the
provided RESUME or JOB POSTING below. Do not invent companies,
employers, dates, technologies, or accomplishments. If you don't
have a relevant detail, say something neutral; do not guess.

Intent: {intent}
Tone: {tone}
{custom_notes_block}

RESUME:
{resume_text}

JOB POSTING:
{job_text}

{cross_match_invite_block}

Return strict JSON with NO surrounding text:
{{
  "subject": "<max 80 chars>",
  "body": "<email body in plain text, max 1500 chars>"
}}
```

The `cross_match_invite_block` is injected only when `intent == "cross_match_invite"`:

```
This is a cross-match invitation. The candidate originally applied to a
different role and may not know this role exists. The email body MUST
include exactly one URL on its own line:
  {public_job_url}
End the email with a friendly invitation to click the link to view the
full role description and apply.
```

---

## Failure modes

| Failure | Detection | Response |
|---|---|---|
| LLM hallucinates non-existent application_id | Tool validates against DB; returns `{"error": "no such application"}` to the model | Model retries with different args or apologises in the synthesis |
| LLM tries to call a non-existent tool | LangGraph rejects before execution | Same as above |
| LLM hits max-tool-calls budget | Loop force-exits with a synthesis prompt | UI shows the partial answer plus a *"reached tool budget"* note |
| Gemini quota exhausted mid-turn | `_classify_gemini_error` from Phase 5.1 wraps the exception | SSE emits `system_message` with the same quota message as the AI status check; loop ends |
| Resend send fails | Catch in `/assistant/drafts/{id}/send` | Draft stays `status="draft"`; UI shows error toast; recruiter can retry |
| Recruiter sends the same draft twice | `outreach_email.status != "draft"` check before send | Returns 409 with `{"detail": "already sent"}` |
| Email body contains hallucinated company | Mitigated by prompt rules; can't fully prevent | Recruiter is the safety net — they review every draft. Audit logs let us inspect later. |
| Agent gets stuck in a tool loop (calling same tool repeatedly) | Within-turn tool cache returns cached result | Avoids redundant work; planner sees same result and moves on |
| **Gemini streams JSON args as text content during tool-deciding turns** | `on_chat_model_end`: buffer is flushed only when the final `AIMessage` has no `tool_calls`. Buffered chunks from tool-deciding turns are discarded. | Card renders cleanly; no raw JSON in the chat. Cost: live token streaming for the final synthesis is replaced by a single end-of-turn flush. |
| **Gemini occasionally returns `chunk.content` as a list of multi-modal dicts** | `_extract_chunk_text` coerces `str`, `list[str]`, and `list[{"type": "text", "text": "…"}]` into plain text. | Prevents `[object Object]` leaking into chat. |
| **Model echoes a `draft_email` tool's JSON output back in its synthesis text** | `_strip_tool_echoes()` regex removes markdown JSON fences from the buffered text before flushing. Reinforced by an explicit "NEVER ECHO TOOL OUTPUT" rule in the system prompt. | Card renders once via `email_draft` SSE; synthesis stays prose-only. |
| **Model claims "I've drafted…" on a repeat request without actually calling `draft_email`** (because the prior turn's `"I've drafted…"` text is in Redis-backed chat history) | System prompt rule 1 explicitly requires a fresh `draft_email` call **every time** the user asks. Server-side heuristic warning logs `agent: WARNING … claimed to have drafted… but did not call draft_email this turn`. | Forces a fresh draft + new card. As a manual escape, the user can click *"↻ New conversation"* to clear Redis history. |
| **Tool output not a `ToolMessage` instance in `on_tool_end`** (LangGraph delivers raw return values, not wrapped messages) | `on_tool_end` extraction now handles `str`, `ToolMessage`, and `dict` shapes uniformly via a permissive coercion before JSON-parsing. | `email_draft` SSE event fires reliably; the draft card now renders for any successful `draft_email` call. |
| **Starlette runs the `StreamingResponse` generator in a separate context, so a `contextvar` set in the request handler is lost** | `set_agent_context(user, session)` is now called *inside* the generator body, with a fresh `Session(engine)` bound to the generator's scope. | Tools run with the correct recruiter auth scope; the previous failure mode was all tools silently raising `"agent context not set"` and the turn ending with no visible effect. |
| **Chat window expands the whole page instead of scrolling internally** | Layout uses `height: calc(100vh - 3.5rem)` on `<main>` with `flex flex-col` + `flex-1 min-h-0` on the chat box. The `min-h-0` overrides the default `min-height: auto` on flex items, letting `overflow-y-auto` actually kick in. | Inner messages area scrolls; the input form and page chrome stay anchored. |
| **Draft card rendered visually BELOW the synthesis text despite the LLM saying "card above"** | `renderAssistantContainer` DOM order changed: drafts now appear before text. | LLM's "above"/"below" phrasing now matches visual order. |

---

## Smoke test — `scripts/smoke_test_phase6.py`

After Phase 6 lands:

1. Create a recruiter with two jobs (Frontend + Tech Lead) and one applicant (Alice) on Frontend
2. Run `match_jobs_task` so Alice has a cross-match to Tech Lead
3. Invoke `/assistant/turn` with *"Find candidates for my Tech Lead role"* — assert at least one tool call is made and Alice appears in the response
4. Invoke `/assistant/turn` with *"Draft an interview invitation for Alice for the Tech Lead role"* — assert a draft is persisted in `outreach_email` with `status="draft"`, `intent="interview_invite"`
5. Invoke `POST /applications/{alice_app_id}/cross-match-invite?matched_job_id={tech_lead_id}` — assert another draft persisted, `intent="cross_match_invite"`, body contains the public URL `/job/{tech_lead_id}`
6. Mock Resend; invoke `POST /assistant/drafts/{draft_id}/send` — assert row updated to `status="sent"` and `sent_at` not null
7. Invoke `POST /assistant/drafts/{draft_id}/send` again — assert 409 (idempotent guard)
8. Multi-tenancy: try the same endpoints as a second recruiter — assert 403 or empty response
9. Mock the LLM client to raise — assert SSE emits `system_message` with the quota-exhausted text, no crash
10. Cleanup

---

## Out of scope for v1

- **Tier 4 (read-write internal state)** — `change_status`, `tag_candidate`, `shortlist`. No application status state machine in v1. The agent can draft a rejection email but cannot mark Alice as rejected — the email IS the rejection.
- **Tier 6 (autonomous workflows)** — scheduled jobs, *"every Monday do X"*, Celery-driven agent runs. Out.
- **Per-recruiter sender domains** — single shared `noreply@<APP_BASE_URL>` for v1. Per-recruiter Resend domain config is real product work and not v1 scope.
- **Multi-session conversation history** — single rolling chat per recruiter in v1. ChatGPT-style sidebar is v2.
- **Bounce/complaint webhook from Resend** — drafts marked `sent` won't auto-update if the email bounces. Manual recruiter follow-up only.
- **Email templates / saved styles per recruiter** — recruiter can't yet save *"my preferred rejection style"*. Future enhancement.
- **Approval queues / multi-recruiter review** — single-recruiter sends only.
- **Telemetry on draft quality** — no thumbs-up/thumbs-down on drafts, no aggregate signal back to prompt tuning.

---

## Estimated effort

| Block | Effort |
|---|---|
| `outreach_email` model + migration | 30 min |
| LangGraph agent skeleton + tool palette (Tier 2) | 1 day |
| Tier 3 generation tools | 0.5 day |
| Tier 5 `draft_email` tool + outreach_email persistence | 1 day |
| Resend integration + send endpoint | 0.5 day |
| Cross-match-invite endpoint + UI button + draft modal | 0.5 day |
| `/assistant` page + chat UI with tool-call + email-draft rendering | 1.5 days |
| Rolling chat history in Redis | 0.5 day |
| Rate limiting + auth-scoped tool validation | 0.5 day |
| Smoke test | 0.5 day |
| Doc updates + roadmap | 0.5 day |
| **Total** | **~1 week** |

---

## Risks

- **Free-tier RPM kills the demo experience.** This is the single biggest risk. At 15 RPM cap, one moderately complex turn (5 LLM calls) leaves ~10 RPM for everyone else on the project. Mitigation: ship with explicit guidance to upgrade Gemini tier for production use; the smoke test must validate the *behaviour*, not the throughput.
- **Hallucinated personalisation in emails.** No prompt is bulletproof. Recruiter approval is the safety net. We document this in the UI: *"AI-drafted — please review before sending."*
- **Agent reliability is hard to measure.** Unlike Phase 5 where we had a calibration smoke test (Python-vs-Java < 50), agent quality is fuzzier. We rely on the smoke test catching obvious regressions and on real recruiter feedback for the rest.
- **Multi-tool reasoning quality on Flash.** Gemini 2.5 Flash is competent but not Opus-class for multi-step reasoning. Some complex queries will produce shallow answers. Documented limitation; upgrade-to-Pro is the lever if it matters.
- **Cost on the paid tier.** Once on paid Gemini, a busy recruiter making 50 agent turns a day at ~5 LLM calls each is 250 calls × ~$0.001 ≈ $0.25/day per recruiter. Manageable, but worth monitoring once we have real usage data.

---

## Status

Complete. Shipped in:

**Schema + outreach foundation**
- `app/models.py` — `OutreachEmail` SQLModel table (status, intent, target_job_id, custom_notes), `EmailDraftPublic` API schema, `CrossMatchInviteRequest`, `DraftSendResponse`, `OutreachIntent` and `OutreachStatus` Literals.
- `app/email.py` — new `send_outreach_email()` helper with minimal neutral styling, sets `Reply-To` to the recruiter's email.
- `app/main.py` — `GET /assistant/drafts`, `POST /assistant/drafts/{id}/send`, `POST /assistant/drafts/{id}/discard` with owner-scoping, idempotent send guard (409), Resend failure mapping (502), and rate limits per the decision table.

**Single-shot draft + cross-match invite**
- `app/outreach.py` — new module with `draft_email_for_application()`: top-K resume chunks (Phase 5.2 helper), intent-specific guidance, public URL injection for cross_match_invite, hallucination-resistant prompt, JSON parse with fence tolerance, persists `OutreachEmail` row.
- `app/main.py` — `POST /applications/{application_id}/cross-match-invite` endpoint (30/hour) for the one-click contextual draft.
- `app/static/js/api.js` — `draftCrossMatchInvite`, `sendDraft`, `discardDraft` helpers.
- `app/static/js/candidate-modal.js` — "Draft invite email →" button on every cross-match row; new draft-review modal stacked on top of the candidate modal with editable subject + body, AI-drafted warning banner, Send / Discard / edit-warning guard.

**LangGraph agent**
- `requirements.txt` — added `langgraph>=0.2.0`.
- `app/agent.py` — full agent module:
  - `AgentContext` + contextvar set by the endpoint; tools read it to enforce auth.
  - Rolling chat history in Redis keyed by `agent:history:{user_id}`, 16-message sliding window, 7-day TTL.
  - 13 tools total: 7 Tier 2 (`list_jobs`, `get_job_details`, `get_applicants`, `get_candidate`, `get_cross_matches`, `search_candidates`, `ask_about_resume`) + 4 Tier 3 (`draft_job_description`, `improve_job_description`, `generate_interview_questions`, `generate_screening_rubric`) + 2 Tier 5 (`draft_email`, `list_drafts`).
  - LangGraph state graph: planner ↔ ToolNode loop with hard cap at 8 tool calls/turn; tools return structured errors (`{"error": "..."}`) for self-correction.
  - `run_turn_stream(user_id, message)` async generator that yields SSE-formatted events (`thinking`, `tool_call`, `tool_result`, `email_draft`, `token`, `system_message`, `error`, `done`), translating LangGraph's `astream_events` v2 stream.
  - Quota / unavailable errors classified via `_classify_gemini_error` and emitted as soft `system_message` rather than raw stack traces.
- `app/main.py` — `POST /assistant/turn` SSE endpoint (10/min) wraps `run_turn_stream`; `POST /assistant/reset` (30/min) clears history.

**Frontend**
- `app/static/assistant.html` + `app/static/js/assistant-page.js` — `/assistant` page with chat input, message rendering, tool-call chips (collapsible `<details>`), email-draft cards that route into the existing draft-review modal via `CandidateModal.openDraftReview()`. SSE parsed via `fetch` + `ReadableStream` (POST means EventSource doesn't apply).
- `dashboard.html` / `job.html` / `search.html` / `settings.html` — "Assistant" tab added to the shared nav between Search and Settings.

**Tests + docs**
- `scripts/smoke_test_phase6.py` — exercises the tool palette presence, owner-scoped data access, multi-tenancy refusal across two recruiters, the `draft_email_for_application` cross_match_invite flow (asserts the public URL is embedded), `list_drafts` discovery, status transitions, and that `run_turn_stream` produces a well-formed SSE event stream (planner mocked to avoid burning live Gemini quota).
- `docs/ai-features/phase-6-agent.md` — this file, status flipped to Complete.
- `docs/ai-features/roadmap.md` — Phase 6 row already points at this doc.

### Known follow-ups (deliberately not in v1)

- **Edit-then-send for chat-initiated drafts**: the draft-review modal's textareas are editable but the edits aren't yet persisted before send. The send currently fires the ORIGINAL AI-drafted version; the UI warns the recruiter if they edited. Add a `PATCH /assistant/drafts/{id}` endpoint + persist-on-send flow in v2.
- **Tier 4 (read-write internal state)** — `change_status`, `tag_candidate`, `shortlist`. Out of scope; the draft email serves as the rejection.
- **Per-recruiter sender domains** — single shared `noreply@<APP_BASE_URL>` for v1.
- **Multi-session conversation history** — single rolling chat in v1; ChatGPT-style sidebar is v2.
- **Bounce/complaint webhook from Resend** — drafts marked `sent` won't auto-update if the email bounces.
- **Eval harness** — beyond the smoke test, no formal eval framework. Worth adding before this is used at scale.
