# Frontend — Multi-Page Recruiter UI

A refactor of the recruiter dashboard from a single 970-line `dashboard.html` into a small number of focused pages. No new features — same endpoints, same data, same flows — just split apart so each screen does one thing.

For AI-feature roadmap context, see [`ai-features/roadmap.md`](ai-features/roadmap.md). This restructure is orthogonal to phases 0–5 and unblocks the inverse cross-job view from [Phase 3.1](ai-features/phase-3-cross-job-matching.md#update-31--inverse-view-good-matches-from-other-job-applications), which adds yet another section to an already-crowded screen.

---

## Why this is needed

The current `app/static/dashboard.html` is doing too much in one place:

- Left rail — job list, post-job button, semantic-search box, bulk re-match button, AI health indicator
- Right pane — three different views toggled in/out of the same DOM: empty state, search results, per-job applicants table
- Modals — post-job form, candidate detail (with chat, with "also matches" list)

Concrete consequences observed during recent development:

- **Search feels lost.** The search input is a small box in the left rail; results appear in the right pane. The recruiter mentally context-switches between "I'm browsing jobs" and "I'm searching candidates" without any URL or layout cue.
- **No bookmarkable state.** Every view is the same URL (`/dashboard`). A recruiter can't share a link to "Job 42's applicants" with a teammate.
- **Crowded job view.** With [Phase 3.1's inverse cross-job section](ai-features/phase-3-cross-job-matching.md#update-31--inverse-view-good-matches-from-other-job-applications), each job's applicants screen will gain another scrollable section. Stacking that into the existing right pane will push the candidate table off-screen.
- **HTML file is hard to edit.** 970 lines, three views, a modal with chat. Any change risks accidentally affecting an unrelated section's CSS or event handlers.
- **Single-page state lives in JS variables.** Switching views is `classList.toggle("hidden")` plus a bunch of `currentJobId` globals. A page refresh blows away context.

None of this is unfixable in the single-page layout, but every fix piles more logic on top of an already-dense HTML file. Splitting the pages is a one-time cost that pays back on every future change.

---

## Goal

The recruiter experience after this refactor:

- A top nav bar with three tabs: **Jobs**, **Search**, **Settings**
- Each tab is its own URL and its own HTML file, served by FastAPI
- Per-job applicants live at `/jobs/{id}` — a real route the recruiter can bookmark
- Candidate detail (chat, ATS score, matches) opens as a modal *or* a full page at `/applications/{id}` (both work; modal is the default for continuity)
- All endpoints, auth, and data flows are unchanged

No SPA framework. No build step. Each page is a plain HTML file plus per-page JS modules that share `app/static/js/api.js`.

---

## Non-goals

- React / Vue / Svelte. The current vanilla setup is fine for the scale; adding a framework would balloon the diff and require a build pipeline.
- Server-side templating (Jinja). Each page is static HTML; the data is fetched on load via the same `api.js` helpers we already use.
- Visual redesign. Same Tailwind classes, same color palette, same fonts. Only the layout changes.
- Mobile-first redesign. Keep current responsive behaviour; revisit if recruiters ask.

---

## Pages

### 1. `/dashboard` — Jobs overview

The landing page after login. Single purpose: see all jobs, post a new one, drill into one.

- Top nav: **Jobs** (active) / Search / Settings
- Main area: card grid of the recruiter's jobs, each card showing title, location, applicant count, and a "View applicants" link to `/jobs/{id}`
- Primary action button: "Post a new job" (modal — unchanged)
- Bulk re-match button stays here, since it operates on all jobs at once
- AI health indicator stays in the header

Removed from this page (vs current `dashboard.html`):

- Per-job applicants table (moves to `/jobs/{id}`)
- Search box (moves to `/search`)
- Empty-right-pane state (no longer applicable — this page no longer has a right pane)

### 2. `/jobs/{id}` — Single job and applicants

Everything about one job:

- Top nav: Jobs (active, breadcrumb-style) / Search / Settings
- Header: job title, location, skills, salary, with an "Edit" button (opens the existing job modal)
- Section 1: **Applicants for this job** — the existing table, unchanged
- Section 2 (new, from [Phase 3.1](ai-features/phase-3-cross-job-matching.md#update-31--inverse-view-good-matches-from-other-job-applications)): **Good matches from your other job applications** — fetched from `GET /jobs/{id}/cross-applicants`

Clicking an applicant opens the candidate modal (same modal as today).

URL: `/jobs/42` — bookmarkable, shareable, refresh-stable.

### 3. `/search` — Semantic search

A page dedicated to search:

- Top nav: Jobs / **Search** (active) / Settings
- Big search input at the top, full-width, with placeholder text explaining what semantic search does
- Below: search results list (the existing `renderSearchResults` markup, mostly unchanged)
- Loading states from [Phase 5.1](ai-features/phase-5-llm-reranking.md#follow-up-51--search-latency): two-stage spinner ("Searching applicant pool…" → "AI-ranking your candidates…")
- Optional: a small "Recent searches" list saved to localStorage. Defer to a follow-up — not blocking.

The search box is **removed** from the left rail of `/dashboard`. There is exactly one entry point to search: the dedicated page.

### 4. `/settings` — Settings

A thin page for account-level controls:

- Change password
- Email verification status
- Log out

Today these live in header buttons. Moving them out of every page's chrome simplifies the headers and gives an obvious home for future preferences (notification settings, default rate-limit visibility, etc).

### 5. Candidate detail

Stays as a modal opened from any applicant row. Re-using the existing modal means no change to the chat flow, the matches list, or the rescore button. Optionally we add a "Open as page" link inside the modal that takes the recruiter to `/applications/{id}` — same modal contents rendered as a full page — for cases where they want a stable URL to share. Defer to a follow-up.

---

## Routing and serving

Each page is a `FileResponse` from FastAPI, identical to how `/dashboard` is served today:

```python
@app.get("/dashboard", include_in_schema=False)
def dashboard():
    return FileResponse("app/static/dashboard.html", headers=_NO_CACHE)

@app.get("/jobs/{job_id}", include_in_schema=False)
def job_view(job_id: int):
    return FileResponse("app/static/job.html", headers=_NO_CACHE)

@app.get("/search", include_in_schema=False)
def search_page():
    return FileResponse("app/static/search.html", headers=_NO_CACHE)

@app.get("/settings", include_in_schema=False)
def settings_page():
    return FileResponse("app/static/settings.html", headers=_NO_CACHE)
```

The path parameter `job_id` is read client-side from `window.location.pathname` and used by the page's JS to fetch `/jobs/{id}` and `/jobs/{id}/applications`. We deliberately do not template the HTML server-side — keeping pages as static files preserves the current cache and deploy story.

Auth is unchanged. The existing `getToken()` / `redirectIfNoToken()` helpers in `api.js` run on every page's `DOMContentLoaded`.

---

## Shared chrome

A tiny shared layout, kept in pure HTML (no templating). Each page file starts with the same `<head>` block and the same `<nav>` block. Duplicated, not abstracted — three short copies are easier to read than one abstract one, and the diff per page on a chrome change is mechanical.

```html
<nav class="bg-white border-b border-slate-200">
  <div class="max-w-6xl mx-auto px-4 flex items-center justify-between h-14">
    <a href="/dashboard" class="font-bold text-slate-800">SmartATS</a>
    <div class="flex gap-6">
      <a href="/dashboard" class="nav-link" data-route="jobs">Jobs</a>
      <a href="/search"    class="nav-link" data-route="search">Search</a>
      <a href="/settings"  class="nav-link" data-route="settings">Settings</a>
    </div>
    <div class="flex gap-3 items-center">
      <span id="aiStatusDot" class="w-2 h-2 rounded-full bg-slate-300"></span>
      <button id="logoutBtn" class="text-sm text-slate-500">Log out</button>
    </div>
  </div>
</nav>
```

Each page sets the active tab via a `<script>` at the top of `<body>` that reads `data-route` and adds a class.

---

## Shared JavaScript

`app/static/js/api.js` already centralises auth, fetch wrappers, and most endpoint calls. The refactor keeps that module as-is and adds three new per-page entry modules:

- `app/static/js/jobs-page.js` — populates the job grid, wires the post-job modal
- `app/static/js/job-page.js` — fetches `/jobs/{id}` + applicants + cross-applicants, renders the page, owns the candidate modal
- `app/static/js/search-page.js` — owns the search input, loading states, results rendering
- `app/static/js/settings-page.js` — change password form

Each page's `<script src="…">` pulls in `api.js` first, then its own page module. No bundler, no transpilation.

The candidate modal HTML and its associated JS (chat, matches, rescore) is identical across the pages that open it. We extract it once into `app/static/_candidate-modal.html` and inline it into both `job.html` and (if we add full-page candidate view) `application.html` via a build-time concat — actually no: a build step is exactly what we're avoiding. **Simpler**: keep the modal markup as a JS string template in `app/static/js/candidate-modal.js`, exported as a function that returns the HTML and wires the events. Insert it on demand into whichever page opens it. One source of truth, no build step.

---

## What stays in `dashboard.html`

After the refactor, `dashboard.html` becomes the Jobs overview only. Roughly:

- 970 lines → ~250 lines
- Three views collapsed to one
- The right-pane toggle logic deleted entirely
- The search box, applicants table, and per-job sections moved out

Net effect: each remaining page is small enough to read end-to-end without scrolling between sections.

---

## Migration plan

The current `dashboard.html` and its single URL stay working throughout — no broken state during the migration. The order keeps risk low:

1. **Create the shared nav and a stub `/search` page.** New file `app/static/search.html` with the nav, a search input, and an empty results list. New route in `main.py`. The search input on `/search` is wired to the existing `/search/candidates` endpoint via a new `search-page.js`. The existing search box in the left rail of `dashboard.html` is left alone for now.
2. **Create `/jobs/{id}` as a full page.** New file `app/static/job.html` with the applicant-table markup lifted out of `dashboard.html`. New route. New `job-page.js`. Add a "View applicants" link from each job card in the dashboard to `/jobs/{id}`. The right-pane applicant view on `dashboard.html` keeps working as a fallback during the cutover.
3. **Extract the candidate modal into a shared JS module.** `candidate-modal.js` exports an `openCandidateModal(applicationId)` function that injects HTML into `document.body` and wires events. Replace the inline modal markup in `dashboard.html` and `job.html` with a single import.
4. **Move search to `/search` only.** Delete the search box and the search-view section from `dashboard.html`. The dashboard's left rail collapses to just the jobs list and the post-job button.
5. **Move the applicants table out of `dashboard.html`.** Delete the right-pane applicant view from `dashboard.html`. All "view applicants" actions now navigate to `/jobs/{id}`. The dashboard becomes the jobs-overview page described above.
6. **Add `/settings` and move account controls there.** Move "Change Password" out of the header into `/settings`. Add a logout link to the shared nav.
7. **Layer in the Phase 3.1 inverse section on `/jobs/{id}`.** New API call, new rendering block. This is the change that motivated the split — by step 7 it goes into a page that's small enough to absorb it.
8. **Manual QA pass.** Every flow from `docs/ai-features/roadmap.md`'s acceptance criteria still works: post a job, apply (via the public form), score appears, search returns results, rescore works, chat works, matches show, cross-applicants show, password change works, logout works.

Each step is a separate commit. Between any two steps the app is fully functional.

---

## Decisions

### 1. No SPA framework

Vanilla JS + static HTML pages. Three pages of ~250 lines each is far below the complexity threshold where a framework pays off. Adding React/Vue would multiply the diff size by 10x and introduce a build step we don't currently need.

### 2. No server-side templating

Each page is a static `FileResponse`. Data is fetched client-side via `api.js`. This keeps deploys simple (rsync static files) and matches every other page's current pattern.

### 3. URL paths under `/`, not `/dashboard/*`

`/jobs/42` not `/dashboard/jobs/42`. The dashboard *is* the app; there's no public-facing surface that needs the `/dashboard` prefix for disambiguation. The public application form at `/` already establishes that all authenticated UI is at the top level.

### 4. Candidate detail stays as a modal by default

Modals are the right interaction for "I'm in a list, I want to glance at one item, then go back to the list". Forcing a full-page navigation breaks the recruiter's scan flow. An optional `/applications/{id}` full-page version is fine as a follow-up but not required.

### 5. Duplicated nav markup, not templated nav

Three identical `<nav>` blocks across three HTML files. Easier to edit than a JS-injected nav that has to wait for `DOMContentLoaded` and risks a flash of un-styled content. The duplication cost is ~20 lines × 4 files = 80 lines total. Acceptable.

### 6. Settings split from header

Today's header has "Change Password" as a button. Moving it to `/settings` reduces header noise and gives a home for future settings (notification preferences, theme, etc) without making the header heavier.

---

## Risks

- **A broken intermediate state.** If a migration step half-lands (e.g. step 4 is committed but step 5 isn't), the dashboard has both the old and new entry points for applicants. The migration plan above keeps every step self-contained — the new page works before the old path is removed. Audit each step before merging.
- **Auth check duplication.** Every page needs to call `redirectIfNoToken()` on load. If we add a new page later and forget that one line, it renders nothing for unauthenticated users. Mitigate with a comment at the top of `api.js` listing the required boilerplate, and with a check in the QA pass.
- **Bookmarked old URLs.** Recruiters with `/dashboard#some-job` bookmarks won't find the same view after the split. Acceptable — they get a working jobs overview and click through. Document in release notes if we have any.

---

## What we are not changing

- Endpoints. Every `app/main.py` route stays at the same path, returns the same shape. The split is purely on the static-files side.
- Auth. `getToken()`, `redirectIfNoToken()`, the `/login` flow — all unchanged.
- Tailwind config / CSS. Same classes, same theme.
- Mobile responsive behaviour. Same as today.
- The candidate chat flow. Same modal, same SSE endpoint, same history persistence.

---

## Estimated effort

- Step 1 (search page): ~1 hour
- Step 2 (job page): ~1.5 hours
- Step 3 (candidate modal extraction): ~1 hour
- Step 4 (remove search from dashboard): ~30 min
- Step 5 (remove applicants from dashboard): ~30 min
- Step 6 (settings page): ~30 min
- Step 7 (Phase 3.1 inverse section): see [Phase 3.1 plan](ai-features/phase-3-cross-job-matching.md#implementation-plan)
- Step 8 (QA pass): ~1 hour

Total: ~6 hours of focused work, spread across however many sittings make sense.

---

## Status

Complete. Shipped in:

**New static files**
- `app/static/dashboard.html` — slimmed to the jobs-overview page (card grid + post-job modal). ~75 lines of body markup; no per-job applicants or search anymore.
- `app/static/job.html` — per-job page with job header, applicants table, Phase 3.1 inverse cross-applicants section, and the edit-job modal.
- `app/static/search.html` — dedicated semantic-search page with two-stage loading text.
- `app/static/settings.html` — account email, password reset, logout.

**New JS modules** (under `app/static/js/`)
- `candidate-modal.js` — shared modal injected by `CandidateModal.open(app, { onReanalyzed })`. Owns its own RAG chat state, cross-job-match rendering, and re-analyze handler. Used by `job.html` and `search.html` without code duplication.
- `ai-status.js` — wires the nav AI-status button and highlights the active tab (reads `<body data-route="…">`). Safe against `DOMContentLoaded` having already fired.
- `jobs-page.js`, `job-page.js`, `search-page.js`, `settings-page.js` — per-page entry modules.

**Modified files**
- `app/main.py` — new HTML routes `GET /jobs/{job_id}`, `GET /search`, `GET /settings`. The `/jobs/{job_id}` HTML route does not collide with existing API routes because they use different HTTP methods (`DELETE /jobs/{id}`, `PATCH /jobs/{id}`) or are more specific paths (`GET /jobs/{id}/cross-applicants`).
- `app/static/js/api.js` — added `Api.redirectIfNoToken()` page-guard helper.

**Behaviour preserved**
- All endpoint paths, schemas, and auth flows unchanged.
- Candidate-facing `index.html` and the auth pages (`login`, `register`, `verify-email`, `reset-password`) untouched.
- Login still redirects to `/dashboard` (which is now the jobs-overview page rather than the multi-pane SPA).
- Every Phase 5.1, Phase 5.2, and Phase 3.1 feature is live on the new pages: search has two-stage loading text, search rerank uses top-K resume chunks server-side, and `/jobs/{id}` shows the inverse cross-applicants section.

**Future polish (not blocking)**
- Move `CHAT_TOP_K` from a hardcoded constant in `app/main.py` to `app/config.py` for consistency with `RERANK_RESUME_CHUNK_TOP_K`. Hygiene only; no functional change. Already documented as a deferred cleanup in the Phase 4 / Phase 5.2 discussion.
- Optional `/applications/{id}` full-page candidate view, for bookmarkable candidate detail. Defer until a recruiter asks.
