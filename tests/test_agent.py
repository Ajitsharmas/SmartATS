"""
Unit tests for app/agent.py — pure helpers only.

The LangGraph loop, tool execution, and SSE streaming are exercised by
scripts/smoke_test_phase6.py with a mocked planner LLM. Here we only test
the standalone helpers that don't touch DB / Redis / Gemini.
"""

import pytest

from app.agent import (
    CHAT_HISTORY_MAX_MESSAGES,
    MAX_TOOL_CALLS_PER_TURN,
    MAX_OUTPUT_TOKENS,
    TOOLS,
    _extract_chunk_text,
    _strip_tool_echoes,
)


# ---------------------------------------------------------------------------
# _strip_tool_echoes — defence against model echoing tool JSON in synthesis
# ---------------------------------------------------------------------------

class TestStripToolEchoes:
    """Gemini sometimes pastes the JSON output of a tool call back into its
    final synthesis text. This regex strips markdown code fences from that
    text so the recruiter doesn't see raw JSON in the chat."""

    def test_plain_text_unchanged(self):
        assert _strip_tool_echoes("Hello world.") == "Hello world."

    def test_strips_json_code_fence(self):
        text = '```json\n{"subject": "x"}\n```\nI have drafted an email.'
        assert _strip_tool_echoes(text) == "I have drafted an email."

    def test_strips_plain_code_fence_with_json_payload(self):
        text = '```\n{"a": 1}\n```Done.'
        assert _strip_tool_echoes(text) == "Done."

    def test_keeps_bare_json_with_no_fence(self):
        # No code fence → not a known model-echo pattern → leave alone
        text = 'json\n{"only": "echo"}'
        result = _strip_tool_echoes(text)
        assert '{"only": "echo"}' in result

    def test_returns_empty_when_entire_response_is_echo(self):
        text = '```json\n{"only": "echo"}\n```'
        assert _strip_tool_echoes(text) == ""

    def test_collapses_excess_blank_lines(self):
        text = "Line 1\n\n\n\nLine 2"
        # Three or more blank lines collapse to one blank line
        result = _strip_tool_echoes(text)
        assert "\n\n\n" not in result
        assert "Line 1" in result and "Line 2" in result

    def test_multiple_fenced_blocks_all_removed(self):
        text = '```json\n{"a": 1}\n```\nMiddle text\n```json\n{"b": 2}\n```\nEnd.'
        result = _strip_tool_echoes(text)
        assert "Middle text" in result
        assert "End." in result
        assert "{" not in result  # both JSON payloads stripped

    def test_case_insensitive_json_label(self):
        text = '```JSON\n{"x": 1}\n```\nDrafted.'
        result = _strip_tool_echoes(text)
        assert "Drafted." in result
        assert "{" not in result


# ---------------------------------------------------------------------------
# _extract_chunk_text — multi-modal content coercion
# ---------------------------------------------------------------------------

class TestExtractChunkText:
    """LangChain's AIMessageChunk.content can be a string OR a list of
    multi-modal dicts. JS template-literal concatenation of dicts produced
    [object Object] in the UI. This helper extracts plain text from all
    common shapes."""

    def test_plain_string(self):
        assert _extract_chunk_text("hello") == "hello"

    def test_empty_string(self):
        assert _extract_chunk_text("") == ""

    def test_list_of_text_dicts(self):
        content = [
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world"},
        ]
        assert _extract_chunk_text(content) == "Hello world"

    def test_list_of_raw_strings(self):
        # Edge case: a list of strings instead of dicts
        content = ["Hello ", "world"]
        assert _extract_chunk_text(content) == "Hello world"

    def test_list_with_non_text_parts_dropped(self):
        content = [
            {"type": "text", "text": "answer:"},
            {"type": "image", "url": "https://..."},  # not text — dropped
            {"type": "text", "text": " 42"},
        ]
        assert _extract_chunk_text(content) == "answer: 42"

    def test_list_with_all_non_text_returns_empty(self):
        content = [
            {"type": "image", "url": "https://example.com/x"},
            {"type": "audio", "url": "https://example.com/y"},
        ]
        assert _extract_chunk_text(content) == ""

    def test_unknown_type_returns_empty_string(self):
        # Any other shape — None, int, dict, etc. — yields ""
        assert _extract_chunk_text(None) == ""
        assert _extract_chunk_text(42) == ""
        assert _extract_chunk_text({"text": "should not be extracted from dict"}) == ""


# ---------------------------------------------------------------------------
# Tool palette presence — guards against accidental tool removal
# ---------------------------------------------------------------------------

class TestToolPalette:
    """Phase 6 acceptance criteria require a specific set of tools to be
    registered. A regression that removes one shouldn't ship unnoticed."""

    def test_thirteen_tools_registered(self):
        assert len(TOOLS) == 13

    def test_tier2_tools_present(self):
        names = {t.name for t in TOOLS}
        assert {
            "list_jobs",
            "get_job_details",
            "get_applicants",
            "get_candidate",
            "get_cross_matches",
            "search_candidates",
            "ask_about_resume",
        }.issubset(names)

    def test_tier3_generation_tools_present(self):
        names = {t.name for t in TOOLS}
        assert {
            "draft_job_description",
            "improve_job_description",
            "generate_interview_questions",
            "generate_screening_rubric",
        }.issubset(names)

    def test_tier5_outreach_tools_present(self):
        names = {t.name for t in TOOLS}
        assert {"draft_email", "list_drafts"}.issubset(names)

    def test_no_send_email_tool(self):
        """Phase 6 design rule: the LLM never has direct send capability.
        Sending requires a recruiter UI click via /assistant/drafts/{id}/send."""
        names = {t.name for t in TOOLS}
        assert "send_email" not in names
        assert "send_outreach" not in names


# ---------------------------------------------------------------------------
# Guardrail constants — design invariants that should never quietly change
# ---------------------------------------------------------------------------

class TestGuardrailConstants:
    def test_tool_call_budget_is_reasonable(self):
        # 8 is the documented Phase 6 budget; if someone bumps it past
        # 30 something is probably wrong (cost / latency explosion risk)
        assert 4 <= MAX_TOOL_CALLS_PER_TURN <= 30

    def test_chat_history_window_bounded(self):
        # Sliding window keeps prompt size bounded; if this grew past 64
        # turns we'd burn token budget on history every call
        assert 4 <= CHAT_HISTORY_MAX_MESSAGES <= 64

    def test_output_token_cap_set(self):
        assert MAX_OUTPUT_TOKENS >= 1000  # need enough for tool use + reply
