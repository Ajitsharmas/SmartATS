/* ---------------------------------------------------------------------------
   Purpose: Central API Wrapper (Final Version)
--------------------------------------------------------------------------- */
const API_BASE = "http://localhost:8000";

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
                throw new Error(errorData.detail || "API Error");
            }

            return await response.json();
        } catch (error) {
            console.error("API Call Failed:", error);
            // In a real app, we would show a Toast notification instead of alert()
            alert(error.message);
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

    static async getJobs() { return await this.request("/jobs"); }

    static async postJob(jobData) { return await this.request("/jobs", "POST", jobData); }

    static async getApplications(jobId) {
        return await this.request(`/applications/${jobId}`);
    }

    static async uploadResume(file) {
        // We wrap the file in FormData so the browser streams it correctly.
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