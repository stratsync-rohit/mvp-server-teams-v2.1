"""Tests for creator-supplied report timestamps and render-time alert timing."""

from datetime import datetime, timezone

from src.renderers.teams_risk_card_renderer import TeamsRiskCardRenderer
from src.services.risk_timing import RiskTimingService


def _renderer_at(now: datetime) -> TeamsRiskCardRenderer:
    return TeamsRiskCardRenderer(timing_service=RiskTimingService(lambda: now))


def _timing_section(card):
    return next(
        item for item in card["content"]["body"]
        if item["type"] == "ColumnSet"
        and [column["items"][0]["text"] for column in item["columns"]]
        == ["REPORTED DATE", "ELAPSED TIME"]
    )


def _timing_values(card):
    return [column["items"][1]["text"] for column in _timing_section(card)["columns"]]


def test_hour_checkpoint_uses_last_completed_utc_boundary_and_clamps_negative_values():
    timing = RiskTimingService(lambda: datetime(2026, 1, 2, 12, 59, tzinfo=timezone.utc))

    snapshot = timing.calculate("2026-01-02T10:15:00+00:00", "UTC")
    future_within_hour = timing.calculate("2026-01-02T12:30:00+00:00", "UTC")

    assert snapshot.elapsed_time == "0 days, 1 hour, 45 minutes"
    assert future_within_hour.elapsed_time == "0 days, 0 hours, 0 minutes"


def test_elapsed_time_handles_day_rollover_and_converts_to_creator_timezone():
    timing = RiskTimingService(lambda: datetime(2026, 1, 3, 1, 15, tzinfo=timezone.utc))

    snapshot = timing.calculate("2026-01-01T23:30:00+00:00", "Asia/Kolkata")

    assert snapshot.elapsed_time == "1 day, 1 hour, 30 minutes"
    assert snapshot.reported_date == "2 Jan 2026, 05:00 AM IST"


def test_missing_reported_date_uses_fallback_without_an_invented_duration():
    timing = RiskTimingService()
    snapshot = timing.calculate(None, None)
    card = TeamsRiskCardRenderer(timing_service=timing).render_notification({
        "risk_id": "RSK-LEGACY", "title": "Legacy risk",
    })

    assert snapshot.reported_date == "Not specified"
    assert snapshot.elapsed_time == "Not available"
    assert _timing_values(card) == ["Not specified", "Not available"]


def test_notification_details_and_mitigation_render_the_same_fresh_timing_section():
    renderer = _renderer_at(datetime(2026, 1, 2, 12, 59, tzinfo=timezone.utc))
    risk = {
        "risk_id": "RSK-TIMING",
        "title": "Timing alert",
        "reported_date": "2026-01-02T10:15:00+00:00",
        "reported_timezone": "America/New_York",
        "views": {
            "notification": {"blocks": [{"type": "text", "text": "Notification body"}]},
            "details": {"blocks": [{"type": "text", "text": "Details body"}]},
            "mitigation": {"blocks": [{"type": "text", "text": "Mitigation body"}]},
        },
    }

    cards = [
        renderer.render_notification(risk),
        renderer.render_view(risk, "details"),
        renderer.render_view(risk, "mitigation"),
    ]

    for card in cards:
        assert _timing_values(card) == [
            "2 Jan 2026, 05:15 AM EST", "0 days, 1 hour, 45 minutes"
        ]
        assert card["content"]["version"] == "1.5"
        assert card["content"]["msteams"] == {"width": "Full"}
