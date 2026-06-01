"""
Unit tests for app/models.py — Pydantic validation rules.

Most of `models.py` is straight schema definitions. The interesting bits are:
- JobListing title/description length constraints
- JobListing salary_range validator (no negative)
- JobListingUpdate validator (same rule, applied on PATCH)
- SearchQuery length bounds
- UserCreate / UserBase email validation
"""

import pytest
from pydantic import ValidationError

from app.models import (
    Application,
    ChatRequest,
    CrossMatchInviteRequest,
    JobListing,
    JobListingUpdate,
    SearchQuery,
    UserCreate,
)


# ---------------------------------------------------------------------------
# JobListing — title, description, salary validator
# ---------------------------------------------------------------------------

class TestJobListing:
    def _base(self, **over):
        defaults = dict(
            title="Senior Backend Engineer",
            description="We are hiring a senior engineer with strong Python skills.",
            skills="python, fastapi, postgres",
            location="remote",
        )
        defaults.update(over)
        return defaults

    def test_minimal_valid_job(self):
        job = JobListing(**self._base())
        assert job.title == "Senior Backend Engineer"

    def test_title_too_short_rejected(self):
        with pytest.raises(ValidationError):
            JobListing(**self._base(title="Dev"))  # 3 chars, min 5

    def test_title_too_long_rejected(self):
        with pytest.raises(ValidationError):
            JobListing(**self._base(title="x" * 101))  # max 100

    def test_description_too_short_rejected(self):
        with pytest.raises(ValidationError):
            JobListing(**self._base(description="too short"))  # 9 chars, min 10

    def test_negative_salary_rejected(self):
        # The custom validator forbids leading "-"
        with pytest.raises(ValidationError, match="negative"):
            JobListing(**self._base(salary_range="-500"))

    def test_negative_salary_rejected_with_whitespace(self):
        with pytest.raises(ValidationError, match="negative"):
            JobListing(**self._base(salary_range="  -500  "))

    def test_positive_salary_accepted(self):
        job = JobListing(**self._base(salary_range="$100k-$120k"))
        assert job.salary_range == "$100k-$120k"

    def test_null_salary_accepted(self):
        job = JobListing(**self._base(salary_range=None))
        assert job.salary_range is None


# ---------------------------------------------------------------------------
# JobListingUpdate — same salary rule, but partial update
# ---------------------------------------------------------------------------

class TestJobListingUpdate:
    def test_empty_update_is_valid(self):
        # All fields optional → empty object is a no-op update
        upd = JobListingUpdate()
        assert upd.title is None

    def test_partial_update_accepts_one_field(self):
        upd = JobListingUpdate(title="New Senior Backend Engineer")
        assert upd.title == "New Senior Backend Engineer"
        assert upd.description is None

    def test_negative_salary_on_update_rejected(self):
        with pytest.raises(ValidationError, match="negative"):
            JobListingUpdate(salary_range="-100")

    def test_title_min_length_enforced_on_update(self):
        with pytest.raises(ValidationError):
            JobListingUpdate(title="x")  # 1 char, min 5


# ---------------------------------------------------------------------------
# SearchQuery — bounds on the recruiter search input
# ---------------------------------------------------------------------------

class TestSearchQuery:
    def test_normal_query_valid(self):
        q = SearchQuery(query="Senior Python engineer")
        assert q.limit == 10  # default
        assert q.offset == 0

    def test_query_too_short_rejected(self):
        with pytest.raises(ValidationError):
            SearchQuery(query="ok")  # 2 chars, min 3

    def test_query_too_long_rejected(self):
        with pytest.raises(ValidationError):
            SearchQuery(query="x" * 501)  # max 500

    def test_limit_bounds(self):
        with pytest.raises(ValidationError):
            SearchQuery(query="ok ok", limit=0)
        with pytest.raises(ValidationError):
            SearchQuery(query="ok ok", limit=51)  # max 50

    def test_offset_cannot_be_negative(self):
        with pytest.raises(ValidationError):
            SearchQuery(query="ok ok", offset=-1)


# ---------------------------------------------------------------------------
# UserCreate — email format
# ---------------------------------------------------------------------------

class TestUserCreate:
    def test_valid_user(self):
        u = UserCreate(email="alice@example.com", password="hunter2hunter2")
        assert u.email == "alice@example.com"

    def test_bad_email_rejected(self):
        with pytest.raises(ValidationError):
            UserCreate(email="not-an-email", password="ok")


# ---------------------------------------------------------------------------
# ChatRequest — Phase 4 chat input bounds
# ---------------------------------------------------------------------------

class TestChatRequest:
    def test_valid_request(self):
        req = ChatRequest(
            question="Has this candidate led teams?",
            session_id="abcd1234efgh5678",
        )
        assert req.question.startswith("Has")

    def test_question_too_short_rejected(self):
        with pytest.raises(ValidationError):
            ChatRequest(question="hi", session_id="abcd1234efgh5678")

    def test_session_id_too_short_rejected(self):
        with pytest.raises(ValidationError):
            ChatRequest(question="Has this candidate worked with kafka?", session_id="short")


# ---------------------------------------------------------------------------
# CrossMatchInviteRequest — Phase 6 contextual draft body
# ---------------------------------------------------------------------------

class TestCrossMatchInviteRequest:
    def test_valid_request(self):
        req = CrossMatchInviteRequest(matched_job_id=42)
        assert req.matched_job_id == 42

    def test_missing_field_rejected(self):
        with pytest.raises(ValidationError):
            CrossMatchInviteRequest()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Application — minimal valid construction
# ---------------------------------------------------------------------------

class TestApplication:
    def test_minimal_application(self):
        app = Application(
            job_id=1,
            candidate_email="alice@example.com",
            candidate_name="Alice Chen",
            resume_url="/download/alice.pdf",
        )
        assert app.status == "pending"
        assert app.ai_score == 0  # default
        assert app.ai_critique is None
