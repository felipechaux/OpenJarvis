"""Tests for the digest_collect tool."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from openjarvis.connectors._stubs import Document
from openjarvis.core.registry import ConnectorRegistry, ToolRegistry


def test_digest_collect_registered():
    from openjarvis.tools.digest_collect import DigestCollectTool

    ToolRegistry.register_value("digest_collect", DigestCollectTool)
    assert ToolRegistry.contains("digest_collect")


def test_digest_collect_executes():
    from openjarvis.tools.digest_collect import DigestCollectTool

    tool = DigestCollectTool()

    mock_docs = [
        Document(
            doc_id="test-1",
            source="gmail",
            doc_type="email",
            content="Meeting at 3pm",
            title="Team standup",
            author="alice@example.com",
            timestamp=datetime(2026, 4, 1, 10, 0),
        )
    ]

    mock_connector = MagicMock()
    mock_connector.return_value.is_connected.return_value = True
    mock_connector.return_value.sync.return_value = mock_docs

    with patch.object(ConnectorRegistry, "contains", return_value=True):
        with patch.object(ConnectorRegistry, "get", return_value=mock_connector):
            result = tool.execute(sources=["gmail"], hours_back=24)

    assert result.success is True
    assert "=== MESSAGES ===" in result.content
    assert "[gmail] From: alice@example.com" in result.content
    assert "Team standup" in result.content
    assert result.metadata["total_items"] == 1


def test_digest_collect_missing_connector():
    from openjarvis.tools.digest_collect import DigestCollectTool

    tool = DigestCollectTool()

    with patch.object(ConnectorRegistry, "contains", return_value=False):
        result = tool.execute(sources=["nonexistent"])

    assert result.success is True  # Partial success
    assert "not available" in result.content


def test_digest_collect_surfaces_disconnected_source():
    """A dead connector must read as an error, not as "no recent data"."""
    from openjarvis.tools.digest_collect import DigestCollectTool

    tool = DigestCollectTool()
    mock_connector = MagicMock()
    mock_connector.return_value.is_connected.return_value = False

    with patch.object(ConnectorRegistry, "contains", return_value=True):
        with patch.object(ConnectorRegistry, "get", return_value=mock_connector):
            result = tool.execute(sources=["gcalendar"])

    assert "CONNECTION ERRORS" in result.content
    assert "gcalendar" in result.content
    assert "(No recent data found)" not in result.content
    assert result.metadata["sources_failed"]


def test_format_gcalendar_all_day_and_timezone():
    from datetime import timedelta, timezone

    from openjarvis.tools.digest_collect import _format_gcalendar

    all_day = Document(
        doc_id="gcalendar:1",
        source="gcalendar",
        doc_type="event",
        content="",
        title="Cumpleaños",
        timestamp=datetime.now().replace(hour=0, minute=0, second=0, microsecond=0),
        metadata={"all_day": True},
    )
    assert "(all day)" in _format_gcalendar(all_day)
    assert "Today" in _format_gcalendar(all_day)

    # A UTC event is rendered in local time, not as its raw UTC clock time.
    start_utc = datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)
    utc_event = Document(
        doc_id="gcalendar:2",
        source="gcalendar",
        doc_type="event",
        content="",
        title="Standup",
        timestamp=start_utc,
    )
    local = start_utc.astimezone()
    assert local.strftime("%-I:%M %p") in _format_gcalendar(utc_event)
    assert isinstance(local.utcoffset(), timedelta)


def test_time_ago_converts_aware_timestamps_to_local():
    """A UTC timestamp 5h old must not read as 'just now' in UTC-5."""
    from datetime import timedelta, timezone

    from openjarvis.tools.digest_collect import _time_ago

    five_hours_ago = datetime.now(timezone.utc) - timedelta(hours=5, minutes=5)
    assert _time_ago(five_hours_ago) == "5h ago"


def test_format_gmail_includes_inbox_tab():
    from openjarvis.tools.digest_collect import _format_gmail

    doc = Document(
        doc_id="gmail:1",
        source="gmail",
        doc_type="email",
        content="",
        title="Job alert",
        author="jobs@example.com",
        timestamp=datetime.now(),
        metadata={"labels": ["UNREAD", "CATEGORY_UPDATES", "INBOX"]},
    )
    assert "[tab: Updates]" in _format_gmail(doc)
