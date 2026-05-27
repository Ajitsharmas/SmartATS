// ---------------------------------------------------------------------------
// /jobs/{id} — One job's applicants, plus the Phase 3.1 inverse cross-job view.
// ---------------------------------------------------------------------------

(function () {
    if (!localStorage.getItem('access_token')) {
        window.location.href = '/login';
        return;
    }

    // Pull the job id from the URL: /jobs/42  →  jobId = 42
    const pathParts = window.location.pathname.split('/').filter(Boolean);
    const jobId = Number(pathParts[1]);
    if (!Number.isFinite(jobId)) {
        document.getElementById('jobTitle').textContent = 'Invalid job URL';
        return;
    }

    let currentJob = null;
    const applicationMap = {};

    // ----- initial load -----
    loadJob().then(() => {
        if (currentJob) {
            refreshApplicants();
            populateCrossApplicants();
        }
    });

    document.getElementById('refreshApplicantsBtn').addEventListener('click', () => {
        refreshApplicants();
        populateCrossApplicants();
    });

    // ----- job header -----
    async function loadJob() {
        try {
            const jobs = await Api.getMyJobs();
            currentJob = jobs.find(j => j.id === jobId);
            if (!currentJob) {
                document.getElementById('jobTitle').textContent = 'Job not found';
                document.getElementById('applicantTable').innerHTML =
                    '<tr><td colspan="4" class="text-center py-6 text-slate-400">This job does not exist or is not in your pool.</td></tr>';
                return;
            }
            document.getElementById('jobTitle').textContent = currentJob.title;
            document.getElementById('jobIdLabel').textContent = `Job ID: ${currentJob.id}`;
            document.getElementById('jobLocation').textContent = currentJob.location ? `Location: ${currentJob.location}` : '';
            document.getElementById('jobSalary').textContent = currentJob.salary_range ? `Salary: ${currentJob.salary_range}` : '';

            // Posted date — show only if present and parseable
            const postedEl = document.getElementById('jobPostedAt');
            if (currentJob.created_at) {
                const d = new Date(currentJob.created_at);
                if (!isNaN(d)) {
                    const formatted = d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
                    postedEl.textContent = `Posted: ${formatted}`;
                } else {
                    postedEl.textContent = '';
                }
            } else {
                postedEl.textContent = '';
            }

            document.getElementById('jobDescription').textContent = currentJob.description || '(No description provided.)';

            // Skills rendered as chips. The DB field is a free-form comma-
            // separated string from the post-job form, so we split on commas
            // and trim. Falsy values are filtered out.
            const skillsEl = document.getElementById('jobSkills');
            const skills = (currentJob.skills || '')
                .split(',')
                .map(s => s.trim())
                .filter(Boolean);
            skillsEl.innerHTML = skills.length === 0
                ? '<span class="text-xs text-slate-400 italic">No skills listed.</span>'
                : skills.map(s =>
                    `<span class="text-xs font-medium px-2.5 py-1 bg-indigo-50 text-indigo-700 border border-indigo-100 rounded-full">${escapeHtml(s)}</span>`
                  ).join('');

            document.title = `${currentJob.title} - SmartATS`;
        } catch (_) {
            document.getElementById('jobTitle').textContent = 'Could not load job';
        }
    }

    // ----- applicants table -----
    async function refreshApplicants() {
        const tbody = document.getElementById('applicantTable');
        tbody.innerHTML = '<tr><td colspan="4" class="text-center py-4 text-slate-400">Syncing…</td></tr>';

        try {
            const apps = await Api.getApplications(jobId);

            if (apps.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4" class="text-center py-8 text-slate-400">No applicants yet.</td></tr>';
                return;
            }

            tbody.innerHTML = apps.map(app => {
                // Tag with the applied-job context so the modal can show
                // "Applied to: …" — for regular applicants on this page,
                // that's the current job by definition.
                app.applied_job_id    = currentJob ? currentJob.id    : app.job_id;
                app.applied_job_title = currentJob ? currentJob.title : '';
                applicationMap[app.id] = app;

                let scoreClass = "bg-red-100 text-red-700";
                if (app.ai_score >= 80) scoreClass = "bg-emerald-100 text-emerald-700";
                else if (app.ai_score >= 50) scoreClass = "bg-amber-100 text-amber-700";

                const isFailed = app.status === 'failed';
                let statusCell;
                if (isFailed) {
                    const errors = [app.scoring_error, app.embedding_error, app.matching_error].filter(Boolean).join(' | ');
                    statusCell = `
                        <span class="inline-flex items-center gap-1.5 text-xs font-semibold uppercase text-red-700" title="${errors.replace(/"/g, '&quot;')}">
                            <span class="w-2 h-2 rounded-full bg-red-500"></span>
                            Failed
                        </span>`;
                } else {
                    statusCell = `<span class="text-xs font-mono uppercase text-slate-500">${app.status}</span>`;
                }

                const actionCell = isFailed
                    ? `<button data-action="retry" data-app-id="${app.id}" class="text-red-600 hover:text-red-800 text-sm font-medium border border-red-200 px-3 py-1 rounded hover:bg-red-50 transition">Retry</button>`
                    : `<button data-action="view" data-app-id="${app.id}" class="text-indigo-600 hover:text-indigo-800 text-sm font-medium border border-indigo-200 px-3 py-1 rounded hover:bg-indigo-50 transition">View Analysis</button>`;

                return `
                <tr class="hover:bg-slate-50 transition group border-b border-slate-50">
                    <td class="py-4 pl-2">
                        <span class="font-bold text-sm px-2 py-1 rounded ${scoreClass}">${app.ai_score}</span>
                    </td>
                    <td class="py-4">
                        <div class="font-bold text-slate-700">
                            ${escapeHtml(app.candidate_name)}
                            <span class="match-badge hidden ml-2 text-[10px] font-semibold text-indigo-600 bg-indigo-50 border border-indigo-100 rounded px-1.5 py-0.5 align-middle" data-app-id="${app.id}" title=""></span>
                        </div>
                        <div class="text-xs text-slate-400">${escapeHtml(app.candidate_email)}</div>
                    </td>
                    <td class="py-4">${statusCell}</td>
                    <td class="py-4 text-right pr-2">${actionCell}</td>
                </tr>`;
            }).join('');

            tbody.querySelectorAll('button[data-action]').forEach(btn => {
                const appId = Number(btn.dataset.appId);
                const action = btn.dataset.action;
                btn.addEventListener('click', () => {
                    if (action === 'retry') retryApp(appId);
                    else if (action === 'view') viewAts(appId);
                });
            });

            populateMatchBadges(apps);
        } catch (_) {
            tbody.innerHTML = '<tr><td colspan="4" class="text-center py-6 text-red-500">Could not load applicants.</td></tr>';
        }
    }

    async function retryApp(appId) {
        try {
            await Api.retryApplication(appId);
            showModal('Retry dispatched. Refresh in a few seconds to see updated status.', 'success');
            refreshApplicants();
        } catch (_) { /* shown */ }
    }

    function viewAts(appId) {
        const app = applicationMap[appId];
        if (!app) return;
        const appliedJob = (app.applied_job_id != null && app.applied_job_title)
            ? { id: app.applied_job_id, title: app.applied_job_title }
            : null;
        CandidateModal.open(app, { onReanalyzed: refreshApplicants, appliedJob });
    }

    async function populateMatchBadges(apps) {
        for (const app of apps) {
            if (app.status !== 'processed') continue;
            try {
                const matches = await Api.getMatches(app.id);
                if (matches.length === 0) continue;
                const badge = document.querySelector(`.match-badge[data-app-id="${app.id}"]`);
                if (!badge) continue;
                const titles = matches.map(m => {
                    const head = `${m.job_title} (${Math.round(m.similarity * 100)}%)`;
                    return m.critique ? `${head}\n  → ${m.critique}` : head;
                }).join('\n');
                badge.textContent = `+${matches.length} other match${matches.length === 1 ? '' : 'es'}`;
                badge.title = titles;
                badge.classList.remove('hidden');
            } catch (_) { /* silent */ }
        }
    }

    // ----- Phase 3.1 inverse cross-job view -----
    async function populateCrossApplicants() {
        const section = document.getElementById('crossApplicantsSection');
        const list    = document.getElementById('crossApplicantsList');
        try {
            const rows = await Api.getCrossApplicants(jobId);
            if (!rows || rows.length === 0) {
                section.classList.add('hidden');
                list.innerHTML = '';
                return;
            }

            list.innerHTML = rows.map(r => {
                const pct = Math.round(r.similarity * 100);
                let scoreClass = "bg-red-100 text-red-700";
                if (pct >= 80) scoreClass = "bg-emerald-100 text-emerald-700";
                else if (pct >= 65) scoreClass = "bg-amber-100 text-amber-700";

                // Seed the modal's data with this cross-match's own score and
                // critique (which are scoped to *this* job, not the candidate's
                // original application). If the LLM rerank produced no critique
                // we surface a clean fallback rather than a placeholder string.
                // Also tag with the candidate's *applied* job so the modal can
                // surface "Applied to: …" — important here because the current
                // page is NOT the candidate's applied job.
                if (!applicationMap[r.application_id]) {
                    applicationMap[r.application_id] = {
                        id: r.application_id,
                        candidate_name: r.candidate_name,
                        candidate_email: r.candidate_email,
                        resume_url: r.resume_url,
                        ai_score: Math.round(r.similarity * 100),
                        ai_critique: r.critique || 'Critique not available.',
                        applied_job_id: r.original_job_id,
                        applied_job_title: r.original_job_title,
                    };
                }

                const critiqueHtml = r.critique
                    ? `<p class="text-xs text-slate-500 italic mt-1.5">${escapeHtml(r.critique)}</p>`
                    : '';

                return `
                    <div class="bg-slate-50/60 border border-slate-100 rounded-lg p-4 flex items-start gap-4">
                        <span class="font-bold text-sm px-2 py-1 rounded ${scoreClass} flex-shrink-0">${pct}%</span>
                        <div class="flex-1 min-w-0">
                            <div class="font-bold text-slate-700 truncate">${escapeHtml(r.candidate_name)}</div>
                            <div class="text-xs text-slate-400 mt-0.5">
                                ${escapeHtml(r.candidate_email)} &middot; applied to
                                <a href="/jobs/${r.original_job_id}" class="text-indigo-600 hover:text-indigo-800 hover:underline">${escapeHtml(r.original_job_title)}</a>
                            </div>
                            <div class="text-xs text-slate-400">Job ID: ${r.original_job_id}</div>
                            ${critiqueHtml}
                        </div>
                        <button data-app-id="${r.application_id}" data-action="view-cross" class="text-indigo-600 hover:text-indigo-800 text-sm font-medium border border-indigo-200 px-3 py-1 rounded hover:bg-indigo-50 transition flex-shrink-0">
                            View Analysis
                        </button>
                    </div>`;
            }).join('');

            list.querySelectorAll('button[data-action="view-cross"]').forEach(btn => {
                const appId = Number(btn.dataset.appId);
                btn.addEventListener('click', () => viewAts(appId));
            });

            section.classList.remove('hidden');
        } catch (_) {
            section.classList.add('hidden');
            list.innerHTML = '';
        }
    }

    // ----- edit job -----
    document.getElementById('editJobBtn').addEventListener('click', () => {
        if (!currentJob) return;
        const form = document.getElementById('jobForm');
        form.querySelector('[name="title"]').value        = currentJob.title;
        form.querySelector('[name="description"]').value  = currentJob.description;
        form.querySelector('[name="skills"]').value       = currentJob.skills;
        form.querySelector('[name="location"]').value     = currentJob.location;
        form.querySelector('[name="salary_range"]').value = currentJob.salary_range || '';
        document.getElementById('jobModal').classList.remove('hidden');
    });

    document.getElementById('jobForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const data = Object.fromEntries(new FormData(e.target));
        try {
            await Api.updateJob(jobId, data);
            document.getElementById('jobModal').classList.add('hidden');
            await loadJob();
            refreshApplicants();
            showModal('Job updated. Existing applications are being re-scored and will update shortly.', 'success');
        } catch (_) { /* shown */ }
    });
})();
