/* ---------------------------------------------------------------------------
   Purpose: Central API Wrapper (Final Version)
--------------------------------------------------------------------------- */
//In production, we point to the same domain (port 80)
const API_BASE = "";

// --- HELPERS ---
function escapeHtml(str) {
    return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// --- MODAL ---
// type: 'error' (default) | 'success'
// onClose: optional callback fired when the user clicks OK
function showModal(message, type = 'error', onClose = null) {
    const isSuccess = type === 'success';

    let modal = document.getElementById('_appModal');
    if (!modal || !document.getElementById('_appModalMsg')) {
        if (modal) modal.remove();
        modal = document.createElement('div');
        modal.id = '_appModal';
        modal.innerHTML = `
            <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
                <div class="bg-white rounded-xl shadow-2xl max-w-sm w-full mx-4 p-6">
                    <div class="flex items-start gap-3 mb-4">
                        <div id="_appModalIcon" class="w-10 h-10 rounded-full flex items-center justify-center flex-shrink-0"></div>
                        <div>
                            <h3 id="_appModalTitle" class="font-bold text-slate-800"></h3>
                            <p id="_appModalMsg" class="text-sm text-slate-600 mt-1"></p>
                        </div>
                    </div>
                    <button id="_appModalClose" class="w-full bg-indigo-600 text-white font-bold py-2 rounded-lg hover:bg-indigo-700 transition">OK</button>
                </div>
            </div>`;
        document.body.appendChild(modal);
    }

    // Always replace the close listener so onClose is always current
    const oldBtn = document.getElementById('_appModalClose');
    const newBtn = oldBtn.cloneNode(true);
    oldBtn.replaceWith(newBtn);
    newBtn.addEventListener('click', () => { modal.remove(); if (onClose) onClose(); });

    const iconEl  = document.getElementById('_appModalIcon');
    const titleEl = document.getElementById('_appModalTitle');

    if (isSuccess) {
        iconEl.className = 'w-10 h-10 bg-green-100 rounded-full flex items-center justify-center flex-shrink-0';
        iconEl.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" class="w-5 h-5 text-green-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>`;
        titleEl.textContent = 'Success';
    } else {
        iconEl.className = 'w-10 h-10 bg-red-100 rounded-full flex items-center justify-center flex-shrink-0';
        iconEl.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" class="w-5 h-5 text-red-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`;
        titleEl.textContent = 'Error';
    }

    document.getElementById('_appModalMsg').textContent = message;
}

// Confirmation modal with details table + Submit and Cancel buttons.
// options: { title, details: [{label, value}], confirmText?, note?, onConfirm?, onCancel? }
function showConfirmModal({ title, details, confirmText = 'Submit', note = null, onConfirm, onCancel }) {
    const existing = document.getElementById('_confirmModal');
    if (existing) existing.remove();

    const rows = details.map(d =>
        `<div class="flex justify-between items-center py-2 border-b border-slate-100 last:border-0">
            <span class="text-xs font-bold text-slate-400 uppercase">${escapeHtml(d.label)}</span>
            <span class="text-sm font-medium text-slate-800 text-right max-w-[60%] break-words">${escapeHtml(d.value)}</span>
        </div>`
    ).join('');

    const modal = document.createElement('div');
    modal.id = '_confirmModal';
    modal.innerHTML = `
        <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
            <div class="bg-white rounded-xl shadow-2xl max-w-sm w-full mx-4 p-6">
                <div class="flex items-center gap-2 mb-4">
                    <svg xmlns="http://www.w3.org/2000/svg" class="w-5 h-5 text-indigo-600 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="10"/></svg>
                    <h3 class="font-bold text-slate-800 text-lg">${escapeHtml(title)}</h3>
                </div>
                <div class="bg-slate-50 rounded-lg px-4 py-1 mb-4">${rows}</div>
                ${note ? `<p class="text-xs text-amber-600 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 mb-4">⚠ ${escapeHtml(note)}</p>` : ''}
                <div class="flex gap-3">
                    <button id="_confirmCancel" class="flex-1 py-2 border border-slate-300 text-slate-700 rounded-lg font-medium hover:bg-slate-50 transition">Cancel</button>
                    <button id="_confirmSubmit" class="flex-1 py-2 bg-indigo-600 text-white rounded-lg font-bold hover:bg-indigo-700 transition">${escapeHtml(confirmText)}</button>
                </div>
            </div>
        </div>`;
    document.body.appendChild(modal);

    document.getElementById('_confirmCancel').addEventListener('click', () => { modal.remove(); if (onCancel) onCancel(); });
    document.getElementById('_confirmSubmit').addEventListener('click', () => { modal.remove(); if (onConfirm) onConfirm(); });
}

class Api {
    // --- 1. STORAGE HELPERS ---
    // We use localStorage to persist the JWT (JSON Web Token).
    // This ensures the user stays logged in even if they refresh the page.

    static getToken() {
        return localStorage.getItem("access_token");
    }

    static setToken(token) {
        localStorage.setItem("access_token", token);
    }

    static logout() {
        // To logout, we simply destroy the token and redirect to login.
        localStorage.removeItem("access_token");
        window.location.href = "/login";
    }

    // --- 2. GENERIC REQUEST HANDLER ---
    // This is the "Engine" of our frontend. 
    // Instead of calling fetch() manually in every function, we route everything
    // through this method. It handles Authorization, Headers, and Errors centrally.
    static async request(endpoint, method = "GET", body = null) {
        const headers = {};

        // A. Auto-attach Auth Token
        // If we have a token, we attach it to the "Authorization" header.
        // The Backend (auth.py) looks for "Bearer <token>" to validate identity.
        const token = this.getToken();
        if (token) {
            headers["Authorization"] = `Bearer ${token}`;
        }

        let config = { method, headers };

        // B. Handle Content-Type
        // If we are uploading a file, we MUST use FormData.
        // If we are sending data, we use JSON.
        if (body instanceof FormData) {
            // Browser sets Content-Type to "multipart/form-data; boundary=..." automatically
            // If we manually set Content-Type here, file uploads would fail!
            config.body = body;
        } else {
            // Standard JSON API request
            headers["Content-Type"] = "application/json";
            if (body) config.body = JSON.stringify(body);
        }

        try {
            const response = await fetch(`${API_BASE}${endpoint}`, config);

            // C. Global Error Handling
            // 401 Unauthorized = Token Expired or Invalid.
            // We force a logout immediately to prevent the user from seeing a broken UI.
            if (response.status === 401) {
                this.logout();
                return null;
            }

            if (!response.ok) {
                const errorData = await response.json();
                // FastAPI validation errors (422) return detail as an array of objects;
                // HTTPException errors return detail as a plain string.
                const detail = Array.isArray(errorData.detail)
                    ? errorData.detail.map(e => e.msg).join(' | ')
                    : (errorData.detail || "API Error");
                throw new Error(detail);
            }

            return response.status === 204 ? null : await response.json();
        } catch (error) {
            console.error("API Call Failed:", error);
            showModal(error.message);
            throw error;
        }
    }

    // --- 3. AUTH METHODS ---

    static async login(username, password) {
        // OAuth2 Standard requires data to be sent as "x-www-form-urlencoded"
        // NOT as JSON. This is why we use URLSearchParams instead of JSON.stringify.
        const formData = new URLSearchParams();
        formData.append("username", username);
        formData.append("password", password);

        const response = await fetch(`${API_BASE}/token`, {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: formData,
        });

        if (!response.ok) throw new Error("Invalid Credentials");
        return await response.json();
    }

    static async register(email, password, fullName) {
        // This maps to our UserCreate Pydantic model in the backend
        return await this.request("/register", "POST", {
            email: email,
            password: password,
            full_name: fullName
        });
    }

    // --- 4. FEATURE METHODS ---
    // These are simple wrappers that make the rest of our code readable.
    // usage: await Api.getJobs() vs fetch('http://localhost:8000/jobs'...)

    static async getJobs()   { return await this.request("/jobs"); }
    static async getMyJobs()             { return await this.request("/my-jobs"); }
    static async deleteJob(jobId)        { return await this.request(`/jobs/${jobId}`, "DELETE"); }
    static async updateJob(jobId, data)  { return await this.request(`/jobs/${jobId}`, "PATCH", data); }

    static async postJob(jobData) { return await this.request("/jobs", "POST", jobData); }

    static async getApplications(jobId) {
        return await this.request(`/applications/${jobId}`);
    }

    static async uploadResume(file) {
        if (!(file instanceof File)) {
            showModal("Please upload your resume (PDF) before submitting.");
            return null;
        }
        const formData = new FormData();
        formData.append("file", file);
        return await this.request("/upload", "POST", formData);
    }


    // We now send a full payload (Name, Email, JobID) for processing and not just text.
    // This matches the "ApplicationSubmit" model in models.py
    static async submitApplication(payload) {
        return await this.request("/process", "POST", payload);
    }
}