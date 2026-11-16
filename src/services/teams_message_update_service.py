"""Authenticated in-place refreshes of bot-authored Teams risk cards."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from src.clients.teams_conversation_client import TeamsConversationClient
from src.exceptions import AppError, ProviderError
from src.renderers.teams_risk_card_renderer import TeamsRiskCardRenderer
from src.services.risk_service import RiskService


class TeamsMessageUpdateService:
    """Regenerate and update exactly the persisted original notification card."""

    def __init__(
        self,
        risk_service: RiskService,
        renderer: TeamsRiskCardRenderer,
        conversation_client: TeamsConversationClient,
        *,
        update_timeout_seconds: float = 25,
    ) -> None:
        self.risk_service = risk_service
        self.renderer = renderer
        self.conversation_client = conversation_client
        self.update_timeout_seconds = update_timeout_seconds

    async def update_notification(self, notification: Mapping[str, Any]) -> dict[str, Any]:
        """Load current risk data and replace the same bot-authored activity."""
        message_reference = notification.get("message_reference")
        risk_id = notification.get("risk_id")
        if not isinstance(message_reference, Mapping):
            raise PermanentTeamsUpdateError("missing Teams message reference")
        if not isinstance(risk_id, str) or not risk_id.strip():
            raise PermanentTeamsUpdateError("notification has no risk ID")
        if message_reference.get("activity_id") != notification.get("teams_message_id"):
            raise PermanentTeamsUpdateError("message reference activity ID does not match notification")
        if message_reference.get("tenant_id") != notification.get("tenant_id"):
            raise PermanentTeamsUpdateError("message reference tenant does not match notification")

        try:
            risk = await self.risk_service.get(risk_id)
        except AppError as exc:
            # Resolved, archived, or missing risks must not be replaced by a
            # new notification. The worker records an actionable terminal state.
            raise PermanentTeamsUpdateError(f"risk is not eligible: {exc.code}") from exc
        card = self.renderer.render_notification(risk)
        try:
            await asyncio.wait_for(
                self.conversation_client.update_card(message_reference, card),
                timeout=self.update_timeout_seconds,
            )
        except Exception as exc:
            if is_permanent_teams_error(exc):
                raise PermanentTeamsUpdateError(str(exc)) from exc
            raise RetryableTeamsUpdateError(str(exc), retry_after=_retry_after(exc)) from exc
        return card


class RetryableTeamsUpdateError(Exception):
    """A transient Teams/provider failure that should use bounded backoff."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentTeamsUpdateError(Exception):
    """An inaccessible/deleted/invalid message reference that must be retired."""


def _retry_after(exc: Exception) -> float | None:
    value = getattr(exc, "retry_after", None)
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    headers = getattr(exc, "headers", None)
    if isinstance(headers, Mapping):
        value = headers.get("Retry-After") or headers.get("retry-after")
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def is_permanent_teams_error(exc: Exception) -> bool:
    """Classify known non-retryable authorization/reference failures."""
    if isinstance(exc, PermanentTeamsUpdateError):
        return True
    status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status_code in {400, 401, 403, 404}:
        return True
    return isinstance(exc, ProviderError) and "reference" in str(exc).lower()
