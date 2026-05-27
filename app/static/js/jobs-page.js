// ---------------------------------------------------------------------------
// /dashboard — Jobs overview. List, post, edit, delete, bulk re-match.
// ---------------------------------------------------------------------------
//
// Per-job applicants and candidate detail live on /jobs/{id}. This page only
// renders the job list and the post/edit modal.

(function () {
    if (!localStorage.getItem('access_token')) {
        window.location.href = '/login';
        return;
    }

    let jobMap = {};
    let editingJobId = null;

    document.getElementById('newJobBtn').addEventListener('click', openCreateModal);
    document.getElementById('jobModalCancelBtn').addEventListener('click', closeModal);

    document.getElementById('rematchAllBtn').addEventListener('click', () => {
        showConfirmModal({
            title: 'Re-check matches for all applicants',
            details: [{ label: 'Effect', value: 'Queues one match task per applicant in your pool' }],
            confirmText: 'Run bulk re-check',
            note: 'Rate-limited to 1 run per hour. Results take up to a few minutes depending on pool size.',
            onConfirm: async () => {
                try {
                    const result = await Api.refreshAllMatches();
                    showModal(`Queued ${result.tasks_queued} re-check task${result.tasks_queued === 1 ? '' : 's'}. Matches will update within a few minutes.`, 'success');
                } catch (_) { /* shown */ }
            },
        });
    });

    document.getElementById('jobForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const data = Object.fromEntries(new FormData(e.target));
        try {
            if (editingJobId) {
                await Api.updateJob(editingJobId, data);
                closeModal();
                editingJobId = null;
                loadJobs();
                showModal('Job updated. Existing applications are being re-scored and will update shortly.', 'success');
            } else {
                await Api.postJob(data);
                closeModal();
                loadJobs();
            }
            e.target.reset();
        } catch (_) { /* shown */ }
    });

    loadJobs();

    async function loadJobs() {
        const list = document.getElementById('jobsList');
        if (!list) return;

        let jobs;
        try {
            jobs = await Api.getMyJobs();
        } catch (e) {
            console.error('loadJobs: API call failed', e);
            list.innerHTML = '<div class="col-span-full text-red-500 text-center py-12">Could not load jobs.</div>';
            return;
        }

        // Api.request returns null on 401 (after triggering a logout/redirect).
        // Treat any non-array shape as "no jobs to show" rather than crashing
        // the rest of loadJobs on a TypeError.
        if (!Array.isArray(jobs)) {
            console.warn('loadJobs: expected an array, got', jobs);
            list.innerHTML = '<div class="col-span-full text-slate-400 text-center py-12 text-sm">Could not load jobs (your session may have expired).</div>';
            return;
        }

        jobMap = {};
        jobs.forEach(j => jobMap[j.id] = j);

        if (jobs.length === 0) {
            list.innerHTML = `
                <div class="col-span-full text-center py-16">
                    <p class="text-slate-400 text-sm mb-4">No jobs yet.</p>
                    <button id="emptyNewJobBtn" class="bg-indigo-600 text-white px-4 py-2 rounded-lg hover:bg-indigo-700 font-medium text-sm">Post your first job</button>
                </div>`;
            const emptyBtn = document.getElementById('emptyNewJobBtn');
            if (emptyBtn) emptyBtn.addEventListener('click', openCreateModal);
            return;
        }

        list.innerHTML = jobs.map(job => `
            <div class="bg-white p-5 rounded-xl border border-slate-200 shadow-sm hover:border-indigo-400 transition group flex flex-col">
                <a href="/jobs/${job.id}" class="flex-1 -m-5 mb-0 p-5 cursor-pointer">
                    <h3 class="font-bold text-slate-800 group-hover:text-indigo-600">${escapeHtml(job.title)}</h3>
                    <div class="text-xs text-slate-400 mb-1.5">Job ID: ${job.id}</div>
                    <div class="text-xs text-slate-500 mt-1 flex flex-col gap-0.5">
                        <span>${escapeHtml(job.location || '')}</span>
                        <span>${escapeHtml(job.salary_range || '')}</span>
                    </div>
                </a>
                <div class="flex gap-2 mt-4 justify-end pt-3 border-t border-slate-100">
                    <button data-action="edit"   data-job-id="${job.id}" class="text-xs text-slate-500 hover:text-indigo-600 border border-slate-200 rounded px-2 py-1 transition">Edit</button>
                    <button data-action="delete" data-job-id="${job.id}" class="text-xs text-slate-500 hover:text-red-600   border border-slate-200 rounded px-2 py-1 transition">Delete</button>
                    <a href="/jobs/${job.id}" class="text-xs text-indigo-600 hover:text-indigo-800 border border-indigo-200 rounded px-2 py-1 hover:bg-indigo-50 transition">Applicants →</a>
                </div>
            </div>
        `).join('');

        list.querySelectorAll('button[data-action]').forEach(btn => {
            const jobId = Number(btn.dataset.jobId);
            if (btn.dataset.action === 'edit') {
                btn.addEventListener('click', () => openEditModal(jobId));
            } else if (btn.dataset.action === 'delete') {
                btn.addEventListener('click', () => confirmDeleteJob(jobId));
            }
        });
    }

    function openCreateModal() {
        editingJobId = null;
        document.getElementById('jobModalTitle').textContent = 'Post New Job';
        document.getElementById('jobModalSubmitBtn').textContent = 'Post';
        document.getElementById('jobForm').reset();
        document.getElementById('jobModal').classList.remove('hidden');
    }

    function openEditModal(jobId) {
        const job = jobMap[jobId];
        if (!job) return;
        editingJobId = jobId;
        document.getElementById('jobModalTitle').textContent = 'Edit Job Posting';
        document.getElementById('jobModalSubmitBtn').textContent = 'Save Changes';
        const form = document.getElementById('jobForm');
        form.querySelector('[name="title"]').value        = job.title;
        form.querySelector('[name="description"]').value  = job.description;
        form.querySelector('[name="skills"]').value       = job.skills;
        form.querySelector('[name="location"]').value     = job.location;
        form.querySelector('[name="salary_range"]').value = job.salary_range || '';
        document.getElementById('jobModal').classList.remove('hidden');
    }

    function closeModal() {
        editingJobId = null;
        document.getElementById('jobModal').classList.add('hidden');
    }

    function confirmDeleteJob(jobId) {
        const job = jobMap[jobId];
        if (!job) return;
        showConfirmModal({
            title: 'Delete Job Posting',
            details: [{ label: 'Title', value: job.title }],
            confirmText: 'Delete',
            note: 'This will permanently delete the job and all its applications.',
            onConfirm: async () => {
                try {
                    await Api.deleteJob(jobId);
                    loadJobs();
                } catch (_) { /* shown */ }
            },
        });
    }
})();
