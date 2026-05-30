// ---------------------------------------------------------------------------
// /assistant — Recruiter assistant chat page (Phase 6).
// ---------------------------------------------------------------------------
//
// Parses the SSE stream from POST /assistant/turn and renders:
//   - user / assistant message bubbles
//   - tool-call chips ("Used search_candidates(query='kubernetes')")
//   - email-draft cards with Send / Discard / Edit buttons (reuses
//     the draft-review modal exposed by candidate-modal.js)
//
// Streaming is implemented with fetch + a ReadableStream reader rather than
// EventSource because EventSource is GET-only and our endpoint POSTs the
// user's message in the body.

(function () {
    if (!localStorage.getItem('access_token')) {
        window.location.href = '/login';
        return;
    }

    const messagesEl = document.getElementById('chatMessages');
    const form       = document.getElementById('assistantForm');
    const input      = document.getElementById('assistantInput');
    const sendBtn    = document.getElementById('assistantSend');
    const newConvBtn = document.getElementById('newConversationBtn');

    let streaming = false;

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (streaming) return;
        const text = input.value.trim();
        if (!text) return;

        // Remove ONLY the one-time initial placeholder, never the prior chat
        // bubbles. (Bug fix: previously we used querySelector('.italic') which
        // matched the thinking span inside every assistant bubble.)
        const placeholder = document.getElementById('initialPlaceholder');
        if (placeholder) placeholder.remove();

        renderUserMessage(text);
        input.value = '';
        await runTurn(text);
    });

    newConvBtn.addEventListener('click', async () => {
        if (streaming) return;
        if (!confirm('Start a new conversation? Your current chat history will be cleared.')) return;
        try {
            await Api.request('/assistant/reset', 'POST');
            messagesEl.innerHTML = `
                <div id="initialPlaceholder" class="text-center text-sm text-slate-400 italic py-8">
                    New conversation started. Try: <em>"Find my top 3 Python candidates"</em>.
                </div>`;
        } catch (_) { /* shown by Api.request */ }
    });

    // -----------------------------------------------------------------
    // Friendly labels for tool-call status — keeps the chat readable
    // without leaking function names + JSON to the recruiter. Curious
    // users can still expand the chip to see the raw call.
    // -----------------------------------------------------------------
    const TOOL_LABELS = {
        list_jobs:                    "Looking up your jobs",
        get_job_details:              "Reading the job posting",
        get_applicants:               "Reading the applicants for this role",
        get_candidate:                "Reading the candidate's profile",
        get_cross_matches:            "Checking other roles that fit this candidate",
        search_candidates:            "Searching your candidate pool",
        ask_about_resume:             "Reading the resume",
        draft_job_description:        "Drafting a job description",
        improve_job_description:      "Revising the job description",
        generate_interview_questions: "Generating interview questions",
        generate_screening_rubric:    "Generating a screening rubric",
        draft_email:                  "Drafting an email",
        list_drafts:                  "Checking previous drafts",
    };

    function friendlyToolLabel(name) {
        return TOOL_LABELS[name] || `Running ${name}`;
    }

    // -----------------------------------------------------------------
    // Rendering helpers
    // -----------------------------------------------------------------

    function renderUserMessage(text) {
        const div = document.createElement('div');
        div.className = 'bg-indigo-50 border border-indigo-100 rounded-lg px-4 py-3 ml-12';
        div.innerHTML = `
            <div class="text-[10px] font-semibold uppercase tracking-wide text-indigo-500 mb-1">You</div>
            <div class="text-sm text-slate-700 whitespace-pre-wrap">${escapeHtml(text)}</div>`;
        messagesEl.appendChild(div);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function renderAssistantContainer() {
        const div = document.createElement('div');
        div.className = 'bg-slate-50 border border-slate-100 rounded-lg px-4 py-3 mr-12 space-y-2';
        // Order matters: chips at top, then any draft cards that the tools
        // produced, then the final synthesis text. This way the LLM's
        // phrasing ("review and send from the card above") matches the
        // visual order on screen.
        div.innerHTML = `
            <div class="text-[10px] font-semibold uppercase tracking-wide text-slate-400 mb-1">Assistant</div>
            <div class="assistant-thinking text-xs italic text-slate-400 hidden"></div>
            <div class="assistant-tool-calls space-y-1"></div>
            <div class="assistant-drafts space-y-2"></div>
            <div class="assistant-text text-sm text-slate-700 whitespace-pre-wrap"></div>`;
        messagesEl.appendChild(div);
        messagesEl.scrollTop = messagesEl.scrollHeight;
        return div;
    }

    function appendToolCallChip(container, payload) {
        const wrap = container.querySelector('.assistant-tool-calls');
        const chip = document.createElement('details');
        chip.className = 'text-xs bg-white border border-slate-200 rounded px-2 py-1.5';
        chip.dataset.toolCallId = payload.tool_call_id || '';
        const friendlyText = friendlyToolLabel(payload.name);
        const argsPreview = JSON.stringify(payload.args || {});
        chip.innerHTML = `
            <summary class="cursor-pointer text-slate-600 flex items-center gap-1.5">
                <span class="status-icon inline-block w-3 h-3 rounded-full border-2 border-indigo-400 border-t-transparent animate-spin"></span>
                <span class="status-text">${escapeHtml(friendlyText)}…</span>
                <span class="ml-auto text-slate-300 text-[10px]">details</span>
            </summary>
            <div class="mt-2 pt-2 border-t border-slate-100 text-[11px] text-slate-500 space-y-1">
                <div class="font-mono"><span class="text-slate-400">tool:</span> ${escapeHtml(payload.name || '')}</div>
                <div class="font-mono break-all"><span class="text-slate-400">args:</span> ${escapeHtml(argsPreview)}</div>
                <div class="result-block hidden">
                    <div class="text-slate-400 mt-1">result:</div>
                    <pre class="result whitespace-pre-wrap break-words bg-slate-50 p-2 rounded mt-0.5"></pre>
                </div>
            </div>`;
        wrap.appendChild(chip);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function setToolResult(container, payload) {
        const wrap = container.querySelector('.assistant-tool-calls');
        const matches = wrap.querySelectorAll('details');
        if (!matches.length) return;
        // Take the LAST chip that doesn't have a real result yet — there's
        // no reliable run_id correlation between on_tool_start and on_tool_end
        // in LangGraph's event stream, so we fall back to "most recent".
        let target = null;
        for (let i = matches.length - 1; i >= 0; i--) {
            const block = matches[i].querySelector('.result-block');
            if (block && block.classList.contains('hidden')) { target = matches[i]; break; }
        }
        if (!target) target = matches[matches.length - 1];

        // Friendly result rendering: swap the spinner for a check or X.
        // Raw tool output stays available inside the expandable details.
        const errored = !!payload.errored;
        const icon = target.querySelector('.status-icon');
        if (icon) {
            icon.className = errored
                ? 'status-icon inline-block w-3 h-3 rounded-full bg-red-500'
                : 'status-icon inline-block w-3 h-3 rounded-full bg-emerald-500';
            icon.innerHTML = '';
        }
        const statusText = target.querySelector('.status-text');
        if (statusText && statusText.textContent.endsWith('…')) {
            const baseLabel = statusText.textContent.slice(0, -1);
            statusText.textContent = errored
                ? `Failed: ${baseLabel}`
                : `Done: ${baseLabel}`;
            if (errored) {
                statusText.classList.add('text-red-700');
                statusText.classList.remove('text-slate-600');
            }
        }
        const resultBlock = target.querySelector('.result-block');
        const resultPre = target.querySelector('.result');
        if (resultBlock && resultPre) {
            resultPre.textContent = payload.summary || '(empty result)';
            resultBlock.classList.remove('hidden');
            // On failure, auto-open the details so the recruiter sees the
            // error without having to click.
            if (errored) target.setAttribute('open', 'open');
        }
    }

    function appendDraftCard(container, draft) {
        const wrap = container.querySelector('.assistant-drafts');
        const card = document.createElement('div');
        card.className = 'mt-2 border border-indigo-200 bg-indigo-50/40 rounded-lg p-3';
        card.dataset.draftId = String(draft.draft_id);
        card.innerHTML = `
            <div class="text-xs text-indigo-700 font-semibold mb-1">
                Draft email · to ${escapeHtml(draft.candidate_name || '')} &lt;${escapeHtml(draft.candidate_email || '')}&gt;
            </div>
            <div class="text-xs text-slate-500 mb-2">Intent: ${escapeHtml(draft.intent || '')}${draft.target_job_id ? ` · Job ID: ${draft.target_job_id}` : ''}</div>
            <div class="bg-white border border-slate-200 rounded p-2 mb-2">
                <div class="text-[11px] text-slate-400 mb-0.5">Subject</div>
                <div class="text-sm font-semibold text-slate-700">${escapeHtml(draft.subject || '')}</div>
            </div>
            <div class="bg-white border border-slate-200 rounded p-2 text-sm text-slate-700 whitespace-pre-wrap mb-2 max-h-48 overflow-y-auto">${escapeHtml(draft.body || '')}</div>
            <div class="flex justify-end gap-2">
                <button data-action="review" class="text-xs font-medium text-indigo-600 hover:text-indigo-800 border border-indigo-200 rounded px-3 py-1 hover:bg-indigo-50 transition">
                    Review &amp; Send
                </button>
            </div>`;
        wrap.appendChild(card);
        card.querySelector('button[data-action="review"]').addEventListener('click', () => {
            // Reuse the draft-review modal from candidate-modal.js — accepts
            // an EmailDraftPublic-shaped object. The agent's tool returns
            // slightly different fields (draft_id vs id, candidate_name top-level
            // instead of joined), so we adapt.
            CandidateModal.openDraftReview({
                id: draft.draft_id,
                subject: draft.subject,
                body: draft.body,
                intent: draft.intent,
                target_job_id: draft.target_job_id,
                target_job_title: null,
                candidate_name: draft.candidate_name,
                candidate_email: draft.candidate_email,
            });
        });
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function setThinking(container, text) {
        const el = container.querySelector('.assistant-thinking');
        if (!text) {
            el.classList.add('hidden');
            el.textContent = '';
        } else {
            el.textContent = text;
            el.classList.remove('hidden');
        }
    }

    function appendToken(container, content) {
        // Hide the thinking placeholder once real tokens start arriving
        setThinking(container, '');
        const textEl = container.querySelector('.assistant-text');
        textEl.textContent = (textEl.textContent || '') + content;
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function appendSystemMessage(container, text) {
        const wrap = container.querySelector('.assistant-text');
        wrap.parentElement.classList.add('bg-amber-50', 'border-amber-200');
        wrap.classList.remove('text-slate-700');
        wrap.classList.add('text-amber-800');
        wrap.textContent = text;
        setThinking(container, '');
    }

    function appendError(container, detail) {
        const wrap = container.querySelector('.assistant-text');
        wrap.parentElement.classList.add('bg-red-50', 'border-red-200');
        wrap.classList.remove('text-slate-700');
        wrap.classList.add('text-red-700');
        wrap.textContent = `(Error: ${detail})`;
        setThinking(container, '');
    }

    // -----------------------------------------------------------------
    // SSE consumption (fetch + ReadableStream, since EventSource is GET-only)
    // -----------------------------------------------------------------

    async function runTurn(userText) {
        const container = renderAssistantContainer();
        setThinking(container, 'Planning…');
        streaming = true;
        sendBtn.disabled = true;
        sendBtn.textContent = 'Working…';

        try {
            const token = localStorage.getItem('access_token');
            const response = await fetch('/assistant/turn', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${token}`,
                },
                body: JSON.stringify({ message: userText }),
            });

            if (response.status === 401) {
                Api.logout();
                return;
            }
            if (!response.ok) {
                let detail = `HTTP ${response.status}`;
                try {
                    const errData = await response.json();
                    detail = errData.detail || detail;
                } catch (_) {}
                appendError(container, detail);
                return;
            }

            // Parse SSE: events separated by blank line, each event has
            // "event: <type>" + "data: <json>" lines.
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });

                let idx;
                while ((idx = buffer.indexOf('\n\n')) >= 0) {
                    const raw = buffer.slice(0, idx);
                    buffer = buffer.slice(idx + 2);
                    handleSseEvent(container, raw);
                }
            }
        } catch (err) {
            appendError(container, err.message || String(err));
        } finally {
            streaming = false;
            sendBtn.disabled = false;
            sendBtn.textContent = 'Send';
            setThinking(container, '');
        }
    }

    function handleSseEvent(container, raw) {
        // raw looks like: "event: token\ndata: {\"content\":\"hi\"}"
        let eventType = 'message';
        let dataStr = '';
        for (const line of raw.split('\n')) {
            if (line.startsWith('event:')) {
                eventType = line.slice(6).trim();
            } else if (line.startsWith('data:')) {
                dataStr += line.slice(5).trim();
            }
        }
        let data = {};
        if (dataStr) {
            try { data = JSON.parse(dataStr); } catch (_) { /* keep empty */ }
        }

        switch (eventType) {
            case 'thinking':       setThinking(container, data.content || ''); break;
            case 'tool_call':      appendToolCallChip(container, data); break;
            case 'tool_result':    setToolResult(container, data); break;
            case 'email_draft':    appendDraftCard(container, data); break;
            case 'token':          appendToken(container, data.content || ''); break;
            case 'system_message': appendSystemMessage(container, data.content || ''); break;
            case 'error':          appendError(container, data.detail || 'unknown error'); break;
            case 'done':           setThinking(container, ''); break;
        }
    }
})();
