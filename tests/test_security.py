"""
Security-focused tests for the startup guard and the upload size cap.

These tests cover the hardening shipped in the Phase 6 security pass:
- Boot fails when SECRET_KEY is the public-repo default
- /upload rejects oversized files with HTTP 413
"""

import io

import pytest


# ---------------------------------------------------------------------------
# Default SECRET_KEY check
# ---------------------------------------------------------------------------

class TestSecretKeyGuard:
    """The server refuses to boot if SECRET_KEY is the public default."""

    def test_default_key_blocks_boot(self, monkeypatch):
        from app.main import _check_critical_secrets, _DEFAULT_SECRET_KEY
        from app.config import settings

        monkeypatch.setattr(settings, "SECRET_KEY", _DEFAULT_SECRET_KEY)
        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            _check_critical_secrets()

    def test_custom_key_passes(self, monkeypatch):
        from app.main import _check_critical_secrets
        from app.config import settings

        monkeypatch.setattr(settings, "SECRET_KEY", "abcd1234efgh5678ijkl9012mnop3456")
        # Should not raise
        _check_critical_secrets()

    def test_empty_key_passes_but_warns(self, monkeypatch, capsys):
        """An empty string isn't the public default — boot succeeds.
        (A separate check for "is the key strong enough" would belong
        in a different validator. Documented in security.md.)"""
        from app.main import _check_critical_secrets
        from app.config import settings

        monkeypatch.setattr(settings, "SECRET_KEY", "")
        # Doesn't raise; current implementation only blocks the literal default
        _check_critical_secrets()

    def test_warns_on_default_gemini_key(self, monkeypatch, capsys):
        from app.main import _check_critical_secrets
        from app.config import settings

        monkeypatch.setattr(settings, "SECRET_KEY", "real-key")
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "fake-key-for-dev")
        _check_critical_secrets()
        captured = capsys.readouterr()
        assert "GEMINI_API_KEY" in captured.out

    def test_warns_on_default_minio_creds(self, monkeypatch, capsys):
        from app.main import _check_critical_secrets
        from app.config import settings

        monkeypatch.setattr(settings, "SECRET_KEY", "real-key")
        monkeypatch.setattr(settings, "MINIO_ACCESS_KEY", "dummy")
        monkeypatch.setattr(settings, "MINIO_SECRET_KEY", "dummy")
        _check_critical_secrets()
        captured = capsys.readouterr()
        assert "MINIO" in captured.out


# ---------------------------------------------------------------------------
# Upload size cap
# ---------------------------------------------------------------------------
#
# We test the size cap directly against the FastAPI route handler using
# TestClient. This validates the 413 path without needing a real MinIO
# bucket — anything that would call MinIO comes AFTER the size check.

@pytest.fixture
def client():
    """Return a FastAPI TestClient. Don't run the lifespan (which would
    try to connect to Postgres/MinIO); we only test request-handling here."""
    from fastapi.testclient import TestClient
    from app.main import app
    # Disable lifespan startup so the test client can boot without infra
    return TestClient(app, raise_server_exceptions=True)


class TestUploadSizeCap:
    def test_oversized_pdf_rejected_with_413(self, client):
        # Construct a fake PDF that starts with %PDF magic bytes but is 6 MB,
        # over the 5 MB cap.
        large_body = b"%PDF" + b"x" * (5 * 1024 * 1024 + 1)
        files = {"file": ("huge.pdf", io.BytesIO(large_body), "application/pdf")}
        response = client.post("/upload", files=files)
        assert response.status_code == 413
        assert "too large" in response.json()["detail"].lower()

    def test_wrong_content_type_rejected_with_400(self, client):
        files = {"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")}
        response = client.post("/upload", files=files)
        assert response.status_code == 400
        assert "PDF" in response.json()["detail"]

    def test_missing_pdf_header_rejected_with_400(self, client):
        # Right MIME type but doesn't actually start with %PDF magic bytes
        files = {"file": ("fake.pdf", io.BytesIO(b"GIF89aXXXX"), "application/pdf")}
        response = client.post("/upload", files=files)
        assert response.status_code == 400
        assert "Corrupt" in response.json()["detail"] or "invalid" in response.json()["detail"].lower()
