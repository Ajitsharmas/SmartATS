"""
TestClient-based checks on auth + protected route surface.

Scope:
- Protected routes reject anon callers with 401
- Public routes return 200 (or appropriate status)
- Static pages serve

We do NOT exercise the actual login flow here because it requires Postgres
(real user table). That's covered by the smoke tests + manual QA. Login-flow
unit testing would require mocking the SQLModel session, which is fragile
and low-value for our purposes.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from app.main import app
    # raise_server_exceptions=False so route handlers that need infra
    # (DB, Redis) return 500 rather than bubbling the exception. We only
    # care here that the response status is not 401/403 (auth-related).
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Public pages — should serve HTML even without auth
# ---------------------------------------------------------------------------

class TestPublicPages:
    def test_root_serves_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    def test_login_page_serves(self, client):
        r = client.get("/login")
        assert r.status_code == 200

    def test_register_page_serves(self, client):
        r = client.get("/register")
        assert r.status_code == 200

    def test_dashboard_serves_html_for_anon(self, client):
        # Dashboard HTML itself is served unauthenticated — the JS inside
        # redirects to /login if no token. That's the intended design.
        r = client.get("/dashboard")
        assert r.status_code == 200

    def test_assistant_page_serves_html_for_anon(self, client):
        # Same pattern — page renders, JS does the auth check
        r = client.get("/assistant")
        assert r.status_code == 200

    def test_public_job_details_page_serves(self, client):
        # /job/{id} is the candidate-facing public details page
        r = client.get("/job/1")
        assert r.status_code == 200

    def test_recruiter_per_job_page_serves(self, client):
        # /jobs/{id} is the recruiter view; HTML serves anon, JS guards
        r = client.get("/jobs/1")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Protected JSON routes — must reject anon callers
# ---------------------------------------------------------------------------

class TestProtectedJsonRoutes:
    """All these endpoints require a valid Bearer JWT. Without one they
    must return 401 (not 200, not 403, not 500)."""

    def _expect_401(self, response):
        # Some FastAPI auth flows return 403 from missing bearer instead of
        # 401. Either is acceptable as long as the call is rejected.
        assert response.status_code in (401, 403), f"got {response.status_code}: {response.text}"

    def test_my_jobs_requires_auth(self, client):
        self._expect_401(client.get("/my-jobs"))

    def test_post_job_requires_auth(self, client):
        self._expect_401(client.post("/jobs", json={"title": "x", "description": "y", "skills": "z", "location": "w"}))

    def test_get_applicants_requires_auth(self, client):
        self._expect_401(client.get("/applications/1"))

    def test_search_candidates_requires_auth(self, client):
        self._expect_401(client.post("/search/candidates", json={"query": "python"}))

    def test_assistant_turn_requires_auth(self, client):
        self._expect_401(client.post("/assistant/turn", json={"message": "hi"}))

    def test_assistant_drafts_list_requires_auth(self, client):
        self._expect_401(client.get("/assistant/drafts"))

    def test_assistant_drafts_send_requires_auth(self, client):
        self._expect_401(client.post("/assistant/drafts/1/send"))

    def test_cross_match_invite_requires_auth(self, client):
        self._expect_401(client.post(
            "/applications/1/cross-match-invite",
            json={"matched_job_id": 2},
        ))

    def test_reanalyze_requires_auth(self, client):
        self._expect_401(client.post("/applications/1/reanalyze"))

    def test_matches_for_app_requires_auth(self, client):
        self._expect_401(client.get("/applications/1/matches"))

    def test_cross_applicants_requires_auth(self, client):
        self._expect_401(client.get("/jobs/1/cross-applicants"))


# ---------------------------------------------------------------------------
# Public JSON endpoints — these should work without auth
# ---------------------------------------------------------------------------

class TestPublicJsonRoutes:
    def test_jobs_listing_is_public(self, client):
        # The candidate-facing job board reads from this. Should return 200
        # with a JSON array (empty if DB is empty, which it is in tests
        # since we have no real DB — but the route shouldn't 401/403).
        r = client.get("/jobs")
        # Without a DB it may 500 — that's a deployment issue, not a security
        # one. Just verify it's not gated on auth.
        assert r.status_code not in (401, 403)

    def test_health_ai_is_public(self, client):
        # The AI health-probe endpoint is rate-limited but unauthenticated
        # by design (used by the front-end status indicator on every page).
        r = client.get("/health/ai")
        # 200 (provider ok), or 500/503 (provider unavailable). Not 401/403.
        assert r.status_code not in (401, 403)


# ---------------------------------------------------------------------------
# Token issuance with bad credentials
# ---------------------------------------------------------------------------

class TestTokenEndpoint:
    """POST /token without valid credentials should fail cleanly.

    With no Postgres available in unit tests, the call will either:
      - 401 (credentials invalid — happy path)
      - 5xx (DB unreachable — deployment issue, but still not 200)
    The important assertion is "no token issued for empty creds."
    """

    def test_empty_credentials_rejected(self, client):
        r = client.post("/token", data={"username": "", "password": ""})
        assert r.status_code != 200
        # No access_token in body on failure
        if r.headers.get("content-type", "").startswith("application/json"):
            assert "access_token" not in r.json()

    def test_missing_password_rejected(self, client):
        r = client.post("/token", data={"username": "alice@example.com"})
        assert r.status_code != 200
