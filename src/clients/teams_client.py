"""Microsoft Teams SDK calls used by domain services."""

from __future__ import annotations

import logging
from typing import Any

try:
    from botbuilder.core.teams import TeamsInfo
except ImportError:  # pragma: no cover - keeps disabled local imports lightweight.
    TeamsInfo = None  # type: ignore[assignment,misc]

from src.bot.diagnostics.activity_logger import (
    log_teams_debug,
    log_teams_debug_raw,
    sanitize_activity,
)


logger = logging.getLogger(__name__)


class TeamsClient:
    """Thin wrapper around Bot Framework TeamsInfo operations."""

    async def get_team_channels(self, turn_context: Any, team_id: str | None = None) -> list[Any]:
        """Return all channels visible to the authenticated bot installation."""
        if TeamsInfo is None:
            return []
        log_teams_debug("TeamsInfo.get_team_channels start", team_id=team_id)
        channels = await TeamsInfo.get_team_channels(turn_context, team_id or "")
        log_teams_debug_raw(
            "TeamsInfo.get_team_channels SDK response",
            channels or [],
            team_id=team_id,
            channel_count=len(channels or []),
        )
        logger.debug(
            "teams_info_team_channels_returned",
            extra={
                "team_id": team_id,
                "channel_count": len(channels or []),
                "raw_channel_fields": [sanitize_activity(channel) for channel in (channels or [])],
            },
        )
        return channels
