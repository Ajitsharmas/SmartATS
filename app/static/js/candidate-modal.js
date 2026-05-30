// ---------------------------------------------------------------------------
// Candidate detail modal — shared by every page that lists applications.
// ---------------------------------------------------------------------------
//
// Usage:
//   <script src="/static/js/api.js"></script>
//   <script src="/static/js/candidate-modal.js"></script>
//   …
//   CandidateModal.open(appObject, { onReanalyzed: () => refreshTable() });
//
// `appObject` must include: id, candidate_name, candidate_email, resume_url,
// ai_score, ai_critique.
//
// The modal owns its own RAG chat state and cross-job-match rendering. It
// stays self-contained — the caller passes an opaque `onReanalyzed` callback
// so the page can refresh its own data after the recruiter triggers a
// re-analysis, but otherwise the modal does not reach back into the page.

const CANDIDATE_MODAL_HTML = `
<div id="atsModal" class="hidden fixed inset-0 bg-slate-900/50 flex items-center justify-center z-50 backdrop-blur-sm">
    <div class="bg-white p-8 rounded-xl w-2/3 max-w-2xl shadow-2xl border border-slate-100 max-h-[90vh] overflow-y-auto">
        <div class="flex justify-between items-start mb-6">
            <div>
                <h2 id="atsName" class="text-2xl font-bold text-slate-800">Candidate Name</h2>
                <div class="mt-1 flex items-center space-x-2">
                    <span id="atsScore" class="text-xl font-bold text-indigo-600">--/100</span>
                    <span class="text-sm text-slate-400">AI Match Score</span>
                </div>
                <div id="atsAppliedJob" class="hidden mt-2 text-xs text-slate-500">
                    Applied to <span id="atsAppliedJobTitle" class="font-medium text-slate-600"></span>
                    <span class="text-slate-400 ml-1">· Job ID: <span id="atsAppliedJobId"></span></span>
                </div>
            </div>
            <button id="atsCloseBtn" class="text-slate-400 hover:text-slate-600">
                <svg xmlns="http://www.w3.org/2000/svg" class="w-6 h-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
        </div>

        <div class="bg-slate-50 p-6 rounded-lg border border-slate-100 text-slate-700 leading-relaxed max-h-96 overflow-y-auto whitespace-pre-wrap font-mono text-sm" id="atsContent"></div>

        <!-- Phase 4 — RAG Q&A chat panel. -->
        <div class="mt-6 pt-6 border-t border-slate-100">
            <div class="flex justify-between items-center mb-3">
                <h3 class="text-sm font-bold text-slate-700">Ask about this candidate</h3>
                <button id="atsChatResetBtn" class="text-xs font-medium text-slate-500 hover:text-indigo-600 transition">
                    New conversation
                </button>
            </div>

            <div id="atsChatMessages" class="space-y-3 mb-3 max-h-64 overflow-y-auto">
                <div class="text-xs text-slate-400 italic text-center py-2">
                    Ask any question about this candidate's resume — answers cite the supporting excerpts.
                </div>
            </div>

            <div id="atsChatCitations" class="hidden mb-3 bg-slate-50 border border-slate-100 rounded-lg p-3">
                <button id="atsChatCitationsToggle" class="text-xs font-semibold text-slate-600 hover:text-indigo-600 flex items-center gap-1 mb-2">
                    <span id="atsChatCitationsIcon">▶</span>
                    <span id="atsChatCitationsLabel">Cited excerpts</span>
                </button>
                <div id="atsChatCitationsBody" class="hidden space-y-2 text-xs text-slate-600"></div>
            </div>

            <form id="atsChatForm" class="flex gap-2">
                <input type="text" id="atsChatInput" placeholder="e.g. Has this candidate led teams?"
                    class="flex-1 px-3 py-2 text-sm border border-slate-300 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none transition"
                    minlength="3" maxlength="1000" required>
                <button type="submit" id="atsChatSubmit"
                    class="bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-medium px-4 rounded-lg transition disabled:bg-slate-300">
                    Ask
                </button>
            </form>
        </div>

        <!-- Phase 3 — Cross-job match suggestions for this candidate. -->
        <div class="mt-6 pt-6 border-t border-slate-100">
            <div class="flex justify-between items-center mb-3">
                <h3 class="text-sm font-bold text-slate-700">Also a good fit for</h3>
                <button id="atsRematchBtn" class="text-xs font-medium text-slate-500 hover:text-indigo-600 transition">
                    ↻ Re-check matches
                </button>
            </div>
            <div id="atsMatchesList" class="space-y-2 text-sm"></div>
        </div>

        <div class="mt-6 flex justify-end space-x-3">
            <button id="atsReanalyzeBtn" class="px-4 py-2 bg-white border border-slate-300 text-slate-700 rounded-lg hover:bg-slate-50 font-medium" title="Re-run scoring, embedding, and matching for this candidate using a fresh extraction of the stored PDF.">
                Re-analyze
            </button>
            <a id="downloadResume" href="#" target="_blank" class="px-4 py-2 bg-white border border-slate-300 text-slate-700 rounded-lg hover:bg-slate-50 font-medium">Download Resume</a>
            <button id="atsCloseBtnBottom" class="px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 font-medium">Close</button>
        </div>
    </div>
</div>`;

const CandidateModal = (() => {
    let mounted = false;
    let currentApp = null;
    let chatSessionId = null;
    let chatAppId = null;
    let chatStreaming = false;
    let onReanalyzedCb = null;

    function mount() {
        if (mounted) return;
        const wrap = document.createElement('div');
        wrap.innerHTML = CANDIDATE_MODAL_HTML;
        document.body.appendChild(wrap.firstElementChild);
        wireHandlers();
        mounted = true;
    }

    function wireHandlers() {
        const close = () => document.getElementById('atsModal').classList.add('hidden');
        document.getElementById('atsCloseBtn').addEventListener('click', close);
        document.getElementById('atsCloseBtnBottom').addEventListener('click', close);

        // Re-analyze
        document.getElementById('atsReanalyzeBtn').addEventListener('click', () => {
            const app = currentApp;
            if (!app) return;
            const candidateName = app.candidate_name || 'this candidate';
            showConfirmModal({
                title: 'Re-analyze candidate',
                details: [
                    { label: 'Candidate', value: candidateName },
                    { label: 'Effect', value: 'Re-runs scoring, embedding, and matching' },
                ],
                confirmText: 'Re-analyze',
                note: 'Current critique and matches will be overwritten when the new analysis completes (usually under a minute).',
                onConfirm: async () => {
                    try {
                        await Api.reanalyzeApplication(app.id);
                        close();
                        showModal(
                            `Re-analysis dispatched for ${candidateName}. Refresh in a few seconds to see the updated results.`,
                            'success',
                        );
                        if (typeof onReanalyzedCb === 'function') {
                            setTimeout(onReanalyzedCb, 3000);
                        }
                    } catch (_) { /* error modal shown by Api.request */ }
                },
            });
        });

        // Re-check cross-job matches
        document.getElementById('atsRematchBtn').addEventListener('click', async () => {
            const btn = document.getElementById('atsRematchBtn');
            const app = currentApp;
            if (!app) return;
            btn.textContent = 'Re-checking…';
            btn.disabled = true;
            try {
                await Api.refreshMatches(app.id);
                setTimeout(() => loadMatches(app.id), 1500);
            } catch (_) { /* shown */ }
            finally {
                btn.textContent = '↻ Re-check matches';
                btn.disabled = false;
            }
        });

        // Chat — reset
        document.getElementById('atsChatResetBtn').addEventListener('click', () => {
            if (chatAppId) resetChatState(chatAppId);
        });

        // Chat — citations toggle
        document.getElementById('atsChatCitationsToggle').addEventListener('click', () => {
            const body = document.getElementById('atsChatCitationsBody');
            const icon = document.getElementById('atsChatCitationsIcon');
            const expanded = !body.classList.contains('hidden');
            body.classList.toggle('hidden');
            icon.textContent = expanded ? '▶' : '▼';
        });

        // Chat — submit
        document.getElementById('atsChatForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            if (chatStreaming || !chatAppId) return;
            const input = document.getElementById('atsChatInput');
            const question = input.value.trim();
            if (question.length < 3) return;

            const messagesEl = document.getElementById('atsChatMessages');
            const placeholder = messagesEl.querySelector('.italic');
            if (placeholder) messagesEl.innerHTML = '';

            appendChatMessage('user', question);
            const assistantEl = appendChatMessage('assistant', '');
            const assistantTextEl = assistantEl.querySelector('.chat-text');

            input.value = '';
            chatStreaming = true;
            document.getElementById('atsChatSubmit').disabled = true;
            document.getElementById('atsChatCitations').classList.add('hidden');

            let accumulated = '';

            await Api.chatStream(chatAppId, question, chatSessionId, {
                onToken: (text) => {
                    accumulated += text;
                    assistantTextEl.textContent = accumulated;
                    messagesEl.scrollTop = messagesEl.scrollHeight;
                },
                onCitations: (citations) => renderCitations(citations),
                onSystemMessage: (text) => {
                    assistantTextEl.textContent = text;
                    assistantEl.classList.add('bg-amber-50', 'border-amber-200');
                },
                onError: (detail) => {
                    assistantTextEl.textContent = `(Error: ${detail})`;
                    assistantEl.classList.add('bg-red-50', 'border-red-200');
                },
                onDone: () => {
                    chatStreaming = false;
                    document.getElementById('atsChatSubmit').disabled = false;
                },
            });
        });
    }

    async function loadMatches(appId) {
        const listEl = document.getElementById('atsMatchesList');
        listEl.innerHTML = '<div class="text-xs text-slate-400">Loading…</div>';
        try {
            const matches = await Api.getMatches(appId);
            if (matches.length === 0) {
                listEl.innerHTML = '<div class="text-xs text-slate-400 italic">No other roles in your pool match this candidate well enough to surface.</div>';
                return;
            }
            listEl.innerHTML = matches.map(m => {
                const pct = Math.round(m.similarity * 100);
                let pctClass = "bg-amber-100 text-amber-700";
                if (pct >= 80) pctClass = "bg-emerald-100 text-emerald-700";
                const critiqueBlock = m.critique
                    ? `<div class="text-xs text-slate-500 mt-1 italic">${escapeHtml(m.critique)}</div>`
                    : '';
                return `
                    <div class="px-3 py-2 bg-slate-50 rounded border border-slate-100">
                        <div class="flex justify-between items-center">
                            <div class="min-w-0">
                                <div class="text-slate-700">${escapeHtml(m.job_title)}</div>
                                <div class="text-xs text-slate-400">Job ID: ${m.matched_job_id}</div>
                            </div>
                            <span class="text-xs font-bold px-2 py-0.5 rounded ${pctClass} flex-shrink-0 ml-2">${pct}% match</span>
                        </div>
                        ${critiqueBlock}
                        <div class="mt-2 flex justify-end">
                            <button data-action="draft-invite" data-matched-job-id="${m.matched_job_id}"
                                class="text-xs font-medium text-indigo-600 hover:text-indigo-800 border border-indigo-200 rounded px-2 py-1 hover:bg-indigo-50 transition">
                                Draft invite email →
                            </button>
                        </div>
                    </div>`;
            }).join('');

            // Wire the "Draft invite email" buttons. Each opens the draft
            // preview modal after calling the cross-match-invite endpoint.
            listEl.querySelectorAll('button[data-action="draft-invite"]').forEach(btn => {
                btn.addEventListener('click', () => {
                    const matchedJobId = Number(btn.dataset.matchedJobId);
                    handleDraftInvite(btn, matchedJobId);
                });
            });
        } catch (_) {
            listEl.innerHTML = '<div class="text-xs text-red-500">Could not load matches.</div>';
        }
    }

    // Phase 6 — draft an invite email for a cross-match. Triggers one LLM
    // call server-side, persists the draft, then opens the draft-review
    // modal so the recruiter can edit and send.
    async function handleDraftInvite(btn, matchedJobId) {
        const app = currentApp;
        if (!app) return;

        const originalText = btn.textContent;
        btn.disabled = true;
        btn.textContent = 'Drafting…';

        try {
            const draft = await Api.draftCrossMatchInvite(app.id, matchedJobId);
            openDraftReview(draft);
        } catch (_) {
            // Api.request already showed the modal
        } finally {
            btn.disabled = false;
            btn.textContent = originalText;
        }
    }

    function resetChatState(appId) {
        chatSessionId = (window.crypto && crypto.randomUUID)
            ? crypto.randomUUID().replace(/-/g, "")
            : Math.random().toString(36).slice(2) + Date.now().toString(36);
        chatAppId = appId;
        chatStreaming = false;
        document.getElementById('atsChatMessages').innerHTML = `
            <div class="text-xs text-slate-400 italic text-center py-2">
                Ask any question about this candidate's resume — answers cite the supporting excerpts.
            </div>`;
        document.getElementById('atsChatCitations').classList.add('hidden');
        document.getElementById('atsChatInput').value = '';
        document.getElementById('atsChatSubmit').disabled = false;
    }

    function appendChatMessage(role, text) {
        const messagesEl = document.getElementById('atsChatMessages');
        const div = document.createElement('div');
        const isUser = role === 'user';
        div.className = isUser
            ? 'bg-indigo-50 border border-indigo-100 rounded-lg px-3 py-2 text-sm text-slate-700 ml-8'
            : 'bg-slate-50 border border-slate-100 rounded-lg px-3 py-2 text-sm text-slate-700 mr-8';
        div.innerHTML = `
            <div class="text-[10px] font-semibold uppercase tracking-wide mb-1 ${isUser ? 'text-indigo-500' : 'text-slate-400'}">
                ${isUser ? 'You' : 'Assistant'}
            </div>
            <div class="chat-text whitespace-pre-wrap">${escapeHtml(text)}</div>`;
        messagesEl.appendChild(div);
        messagesEl.scrollTop = messagesEl.scrollHeight;
        return div;
    }

    function renderCitations(citations) {
        if (!citations || citations.length === 0) return;
        const wrap = document.getElementById('atsChatCitations');
        const body = document.getElementById('atsChatCitationsBody');
        const label = document.getElementById('atsChatCitationsLabel');
        label.textContent = `Cited excerpts (${citations.length})`;
        body.innerHTML = citations.map(c => `
            <div class="border-l-2 border-slate-300 pl-3 py-1">
                <div class="text-[10px] font-mono text-slate-400 mb-0.5">chunk ${c.chunk_index} · ${(c.similarity * 100).toFixed(0)}% similarity</div>
                <div>${escapeHtml(c.chunk_text)}</div>
            </div>`).join('');
        wrap.classList.remove('hidden');
    }

    function open(app, opts = {}) {
        if (!app) return;
        if (!mounted) mount();
        currentApp = app;
        onReanalyzedCb = opts.onReanalyzed || null;

        document.getElementById('atsName').innerText = app.candidate_name;
        document.getElementById('atsScore').innerText = `${app.ai_score}/100`;
        document.getElementById('atsContent').textContent = app.ai_critique || "Analysis pending...";
        document.getElementById('downloadResume').href = app.resume_url;

        // "Applied to" header — only shown if the caller passes the applied
        // job's id and title. Tells the recruiter which job the candidate
        // originally applied to (most useful when opening from cross-applicants
        // or search, where the surrounding context isn't the applied job).
        const appliedWrap = document.getElementById('atsAppliedJob');
        if (opts.appliedJob && opts.appliedJob.id != null && opts.appliedJob.title) {
            document.getElementById('atsAppliedJobTitle').textContent = opts.appliedJob.title;
            document.getElementById('atsAppliedJobId').textContent = String(opts.appliedJob.id);
            appliedWrap.classList.remove('hidden');
        } else {
            appliedWrap.classList.add('hidden');
        }

        loadMatches(app.id);
        resetChatState(app.id);

        document.getElementById('atsModal').classList.remove('hidden');
    }

    function close() {
        document.getElementById('atsModal').classList.add('hidden');
    }

    // ---------------------------------------------------------------------
    // Draft-review modal (Phase 6) — used by the "Draft invite email"
    // button on cross-match rows. Sits on top of the candidate modal so the
    // recruiter can review/edit/send without losing context.
    // ---------------------------------------------------------------------

    let draftMounted = false;
    let currentDraftId = null;

    function mountDraftModal() {
        if (draftMounted) return;
        const wrap = document.createElement('div');
        wrap.innerHTML = `
            <div id="draftReviewModal" class="hidden fixed inset-0 bg-slate-900/60 flex items-center justify-center z-[60] backdrop-blur-sm">
                <div class="bg-white rounded-xl shadow-2xl w-full max-w-2xl mx-4 p-6 max-h-[90vh] overflow-y-auto">
                    <div class="flex justify-between items-start mb-4">
                        <div>
                            <h3 class="text-lg font-bold text-slate-800">Review draft email</h3>
                            <p id="draftReviewMeta" class="text-xs text-slate-500 mt-0.5"></p>
                        </div>
                        <button id="draftCloseBtn" class="text-slate-400 hover:text-slate-600">
                            <svg xmlns="http://www.w3.org/2000/svg" class="w-6 h-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                        </button>
                    </div>

                    <p class="text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1.5 mb-3">
                        AI-drafted. Please review carefully before sending.
                    </p>

                    <div class="mb-3">
                        <label class="block text-xs font-bold text-slate-500 uppercase mb-1">Subject</label>
                        <input id="draftSubject" type="text" maxlength="200"
                            class="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none">
                    </div>

                    <div class="mb-3">
                        <label class="block text-xs font-bold text-slate-500 uppercase mb-1">Body</label>
                        <textarea id="draftBody" rows="12"
                            class="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none font-mono"></textarea>
                    </div>

                    <p class="text-[11px] text-slate-400 mb-4">
                        Reply-To will be set to your account email so candidate replies come back to you.
                    </p>

                    <div class="flex justify-end gap-2">
                        <button id="draftDiscardBtn" class="px-4 py-2 bg-white border border-slate-300 text-slate-700 rounded-lg hover:bg-red-50 hover:border-red-200 hover:text-red-700 font-medium text-sm">
                            Discard
                        </button>
                        <button id="draftSendBtn" class="px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 font-medium text-sm">
                            Send
                        </button>
                    </div>
                </div>
            </div>`;
        document.body.appendChild(wrap.firstElementChild);

        const closeDraft = () => document.getElementById('draftReviewModal').classList.add('hidden');
        document.getElementById('draftCloseBtn').addEventListener('click', closeDraft);

        document.getElementById('draftDiscardBtn').addEventListener('click', async () => {
            if (!currentDraftId) return;
            const ok = confirm('Discard this draft? This cannot be undone.');
            if (!ok) return;
            try {
                await Api.discardDraft(currentDraftId);
                showModal('Draft discarded.', 'success');
                closeDraft();
            } catch (_) { /* shown by Api.request */ }
        });

        document.getElementById('draftSendBtn').addEventListener('click', async () => {
            if (!currentDraftId) return;
            const sendBtn = document.getElementById('draftSendBtn');
            // The recruiter may have edited subject/body in the modal — we
            // don't currently persist edits before send, so warn them.
            const originalSubject = sendBtn.dataset.originalSubject || '';
            const originalBody = sendBtn.dataset.originalBody || '';
            const currentSubject = document.getElementById('draftSubject').value;
            const currentBody = document.getElementById('draftBody').value;
            if (currentSubject !== originalSubject || currentBody !== originalBody) {
                const ok = confirm(
                    'You have edited the draft but those edits are not yet persisted. '
                    + 'Sending now will send the ORIGINAL AI-drafted version. Continue?'
                );
                if (!ok) return;
            }
            sendBtn.disabled = true;
            sendBtn.textContent = 'Sending…';
            try {
                await Api.sendDraft(currentDraftId);
                showModal('Email sent.', 'success');
                closeDraft();
            } catch (_) {
                /* shown by Api.request */
            } finally {
                sendBtn.disabled = false;
                sendBtn.textContent = 'Send';
            }
        });

        draftMounted = true;
    }

    function openDraftReview(draft) {
        mountDraftModal();
        currentDraftId = draft.id;

        const meta = `To: ${draft.candidate_name} <${draft.candidate_email}>`
            + (draft.target_job_title ? ` · About: ${draft.target_job_title} (Job ID: ${draft.target_job_id})` : '');
        document.getElementById('draftReviewMeta').textContent = meta;

        document.getElementById('draftSubject').value = draft.subject;
        document.getElementById('draftBody').value = draft.body;

        // Remember the original AI-drafted content so we can warn if the
        // recruiter edits the textareas but tries to send before we add
        // edit-persistence (a future enhancement).
        const sendBtn = document.getElementById('draftSendBtn');
        sendBtn.dataset.originalSubject = draft.subject;
        sendBtn.dataset.originalBody = draft.body;

        document.getElementById('draftReviewModal').classList.remove('hidden');
    }

    return { open, close, openDraftReview };
})();

window.CandidateModal = CandidateModal;
