"""Timezone-safe timing calculations for risk alerts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class RiskTiming:
    """Display-ready timing values calculated from a reported instant."""

    reported_date: str
    elapsed_time: str
    reported_at_utc: datetime | None


class RiskTimingService:
    """Calculate timing independently of an Adaptive Card host or layout."""

    def __init__(self, now_provider: Callable[[], datetime] | None = None) -> None:
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def calculate(self, reported_date: Any, reported_timezone: Any) -> RiskTiming:
        """Return alert-local time and elapsed time through the last UTC hour.

        The checkpoint is the latest completed clock-hour boundary, rather
        than the instant the card happened to be rendered. This makes the
        value stable within an hour and suitable for future scheduled refreshes.
        """
        reported_at_utc = self.coerce_utc(reported_date)
        display_timezone = self.resolve_timezone(reported_timezone)
        if reported_at_utc is None or display_timezone is None:
            return RiskTiming("Not specified", "Not available", None)

        now_utc = self.coerce_utc(self._now_provider())
        if now_utc is None:  # Defensive only; built-in provider is always aware.
            now_utc = datetime.now(timezone.utc)
        checkpoint = self.hour_checkpoint(now_utc)
        elapsed_seconds = max(0, int((checkpoint - reported_at_utc).total_seconds()))
        elapsed = self.format_elapsed(elapsed_seconds)
        return RiskTiming(
            self.format_reported_date(reported_at_utc, display_timezone),
            elapsed,
            reported_at_utc,
        )

    @staticmethod
    def hour_checkpoint(value: datetime) -> datetime:
        """Return the completed UTC clock-hour boundary for an aware instant."""
        utc_value = RiskTimingService.coerce_utc(value)
        if utc_value is None:
            raise ValueError("value must be a timezone-aware datetime")
        return utc_value.replace(minute=0, second=0, microsecond=0)

    @staticmethod
    def coerce_utc(value: Any) -> datetime | None:
        """Parse an aware datetime or ISO-8601 string as an instant in UTC."""
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                return None
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)

    @staticmethod
    def resolve_timezone(value: Any) -> ZoneInfo | None:
        """Resolve an IANA timezone without silently changing the alert's zone."""
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return ZoneInfo(value.strip())
        except (ValueError, ZoneInfoNotFoundError):
            return None

    @staticmethod
    def format_elapsed(total_seconds: int) -> str:
        """Format a non-negative duration consistently for all views."""
        minutes = total_seconds // 60
        days, remainder = divmod(minutes, 24 * 60)
        hours, minutes = divmod(remainder, 60)
        return ", ".join((
            f"{days} day{'s' if days != 1 else ''}",
            f"{hours} hour{'s' if hours != 1 else ''}",
            f"{minutes} minute{'s' if minutes != 1 else ''}",
        ))

    @staticmethod
    def format_reported_date(value: datetime, display_timezone: ZoneInfo) -> str:
        """Render the stored UTC instant in the creator-configured timezone."""
        local = value.astimezone(display_timezone)
        abbreviation = local.tzname() or display_timezone.key
        return f"{local.day} {local.strftime('%b %Y')}, {local.strftime('%I:%M %p')} {abbreviation}"
