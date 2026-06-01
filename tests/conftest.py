"""
Shared pytest fixtures.

Scope policy:
  - These tests are designed to run WITHOUT a database, Redis, MinIO, or
    Gemini API key. They cover pure-function behaviour and lightweight
    FastAPI surface tests.
  - For end-to-end integration coverage (real Postgres + pgvector + Redis +
    Celery), use the smoke tests in scripts/smoke_test_phase*.py.
"""

import os
import sys
from pathlib import Path

import pytest

# Ensure the project root is importable when running `pytest` from any
# directory. Mirrors the pattern used by the smoke tests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Make the runtime config tolerate dev-mode defaults so module imports
# during test collection don't trip the startup secret check. The check
# itself is exercised explicitly in tests/test_security.py.
os.environ.setdefault("SECRET_KEY", "test-suite-only-not-the-default-value")
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")
os.environ.setdefault("APP_BASE_URL", "http://localhost:8000")
# Redirect SlowAPI to in-memory rate-limit storage so TestClient calls work
# without a running Redis. This is set BEFORE app.limiter is imported so the
# Limiter() instance picks up the override at module load time.
os.environ.setdefault("RATE_LIMITER_STORAGE_URL", "memory://")
