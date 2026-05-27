// ---------------------------------------------------------------------------
// /search — Semantic search across the recruiter's entire applicant pool.
// ---------------------------------------------------------------------------

(function () {
    if (!localStorage.getItem('access_token')) {
        window.location.href = '/login';
        return;
    }

    // Map of application_id → minimal application snapshot, populated as
    // search results render so clicking opens the candidate modal.
    const applicationMap = {};

    const form         = document.getElementById('searchForm');
    const input        = document.getElementById('searchInput');
    const clearBtn     = document.getElementById('searchClearBtn');
    const hintEl       = document.getElementById('searchEmptyHint');
    const loadingEl    = document.getElementById('searchResultsLoading');
    const loadingLabel = document.getElementById('searchResultsLoadingLabel');
    const emptyEl      = document.getElementById('searchResultsEmpty');
    const listEl       = document.getElementById('searchResultsList');

    let lastQuery = '';

    form.addEventListener('submit', (e) => {
        e.preventDefault();
        const query = input.value.trim();
        if (query.length < 3) {
            showModal('Please enter at least 3 characters.');
            return;
        }
        lastQuery = query;
        performSearch(query);
    });

    input.addEventListener('input', (e) => {
        clearBtn.classList.toggle('hidden', e.target.value.length === 0);
    });

    clearBtn.addEventListener('click', () => {
        input.value = '';
        clearBtn.classList.add('hidden');
        listEl.innerHTML = '';
        emptyEl.classList.add('hidden');
        hintEl.classList.remove('hidden');
        lastQuery = '';
    });

    async function performSearch(query) {
        hintEl.classList.add('hidden');
        listEl.innerHTML = '';
        emptyEl.classList.add('hidden');

        // Phase 5.1 — two-stage loading text.
        loadingLabel.textContent = 'Searching applicant pool…';
        loadingEl.classList.remove('hidden');
        const stageTwoTimeout = setTimeout(() => {
            loadingLabel.textContent = 'AI-ranking your candidates…';
        }, 250);

        try {
            const data = await Api.searchCandidates(query, 0, 10);
            clearTimeout(stageTwoTimeout);
            loadingEl.classList.add('hidden');

            if (data.results.length === 0) {
                emptyEl.classList.remove('hidden');
                return;
            }

            renderResults(data.results, data.degraded);
        } catch (_) {
            clearTimeout(stageTwoTimeout);
            loadingEl.classList.add('hidden');
            // error modal shown by Api.request
        }
    }

    function renderResults(results, degraded = false) {
        const degradedBanner = degraded
            ? `<div class="text-xs bg-amber-50 border border-amber-200 text-amber-800 rounded p-2 mb-3">
                   AI re-rank temporarily unavailable — showing vector-similarity matches only. Try again in a moment for higher-precision ranking.
               </div>`
            : '';

        const html = degradedBanner + results.map(r => {
            const pct = Math.round(r.similarity * 100);
            let pctClass = "bg-amber-100 text-amber-700";
            if (pct >= 80) pctClass = "bg-emerald-100 text-emerald-700";
            else if (pct < 65) pctClass = "bg-slate-100 text-slate-600";

            // Seed map so CandidateModal.open() has the application data.
            // Use the search hit's own LLM score + critique (scoped to the
            // search query) rather than placeholder zeros, and tag with the
            // candidate's *applied* job so the modal header surfaces it.
            applicationMap[r.application_id] = {
                id: r.application_id,
                candidate_name: r.candidate_name,
                candidate_email: r.candidate_email,
                resume_url: r.resume_url,
                ai_score: Math.round(r.similarity * 100),
                ai_critique: r.critique || 'Critique not available.',
                applied_job_id: r.job_id,
                applied_job_title: r.job_title,
            };

            const reasonText = r.critique || `"${r.best_match_chunk}"`;
            const reasonClass = r.critique
                ? 'text-sm text-slate-600 border-l-2 border-indigo-200 pl-3'
                : 'text-sm text-slate-600 italic border-l-2 border-slate-200 pl-3 line-clamp-2';

            return `
                <div data-app-id="${r.application_id}" class="result-card border border-slate-200 rounded-lg p-4 hover:border-indigo-400 hover:bg-slate-50 cursor-pointer transition bg-white">
                    <div class="flex justify-between items-start mb-1">
                        <div class="font-bold text-slate-800">${escapeHtml(r.candidate_name)}</div>
                        <span class="text-xs font-bold px-2 py-0.5 rounded ${pctClass}">${pct}% match</span>
                    </div>
                    <div class="text-xs text-slate-400">${escapeHtml(r.candidate_email)} · Applied to <span class="font-medium text-slate-500">${escapeHtml(r.job_title)}</span></div>
                    <div class="text-xs text-slate-400 mb-2">Job ID: ${r.job_id}</div>
                    <div class="${reasonClass}">${escapeHtml(reasonText)}</div>
                </div>`;
        }).join('');

        listEl.innerHTML = html;
        listEl.querySelectorAll('.result-card').forEach(card => {
            card.addEventListener('click', () => {
                const appId = Number(card.dataset.appId);
                const app = applicationMap[appId];
                if (!app) return;
                const appliedJob = (app.applied_job_id != null && app.applied_job_title)
                    ? { id: app.applied_job_id, title: app.applied_job_title }
                    : null;
                CandidateModal.open(app, { appliedJob });
            });
        });
    }
})();
