"""Thin Microsoft Teams activity router."""

import logging
from typing import Any

from botbuilder.core import TurnContext
from botbuilder.core.teams import TeamsActivityHandler
from botbuilder.schema import AdaptiveCardInvokeResponse

from src.bot.diagnostics.activity_logger import (
    activity_metadata,
    log_activity,
    log_teams_debug,
)
from src.exceptions import AppError

logger = logging.getLogger(__name__)


class RiskBot(TeamsActivityHandler):
    """Route authenticated activities to injected application services."""

    def __init__(self, installation_service: Any | None = None,
                 teams_service: Any | None = None,
                 reaction_service: Any | None = None,
                 risk_view_service: Any | None = None,
                 notification_service: Any | None = None,
                 processed_activity_repository: Any | None = None,
                 *, debug_activity_logging: bool = False) -> None:
        super().__init__()
        self.installation_service = installation_service
        self.teams_service = teams_service
        self.reaction_service = reaction_service
        self.risk_view_service = risk_view_service
        self.notification_service = notification_service
        self.processed_activity_repository = processed_activity_repository
        self.debug_activity_logging = debug_activity_logging

    async def on_turn(self, turn_context: TurnContext) -> None:
        """Log safe activity metadata, then use the SDK's normal dispatch."""
        activity = turn_context.activity
        metadata = activity_metadata(activity)
        members_added = metadata.get("members_added")
        members_removed = metadata.get("members_removed")
        log_teams_debug(
            "bot handler entered",
            **metadata,
            members_added_present=bool(members_added),
            members_removed_present=bool(members_removed),
        )
        activity_type = metadata.get("activity_type")
        if activity_type == "message":
            log_teams_debug("handling message", **metadata)
        elif activity_type == "conversationUpdate":
            log_teams_debug("handling conversationUpdate", **metadata)
        elif activity_type == "installationUpdate":
            log_teams_debug("handling installationUpdate", **metadata)
        elif activity_type == "invoke":
            log_teams_debug("handling invoke", **metadata)
        elif activity_type == "event":
            log_teams_debug("handling event", **metadata)
        activity_id, activity_type, reaction_dedupe = self._dedupe_identity(activity)
        claimed = True
        if self.processed_activity_repository is not None and activity_id:
            claimed = await self.processed_activity_repository.claim(
                activity_id, activity_type
            )
        if reaction_dedupe is not None:
            log_teams_debug(
                "messageReaction dedupe",
                message_id=reaction_dedupe["message_id"],
                user_id=reaction_dedupe["user_id"],
                reactions_added=reaction_dedupe["reactions_added"],
                reactions_removed=reaction_dedupe["reactions_removed"],
                dedupe_key=activity_id,
                claimed=claimed,
            )
        if not claimed:
            logger.info("bot_activity_duplicate_skipped", extra={"activity_id": activity_id})
            return
        log_activity(activity, debug=self.debug_activity_logging)
        try:
            await super().on_turn(turn_context)
        except Exception as exc:
            if self.processed_activity_repository is not None and activity_id:
                await self.processed_activity_repository.mark_failed(activity_id, str(exc))
            raise
        else:
            if self.processed_activity_repository is not None and activity_id:
                await self.processed_activity_repository.mark_completed(activity_id)

    @staticmethod
    def _dedupe_identity(activity: Any) -> tuple[str | None, str | None, dict[str, Any] | None]:
        """Return the processed-activity key, specializing Teams reaction events.

        Teams uses the reacted-to message ID as ``activity.id`` for every
        reaction.  A normal activity keeps that existing identity; a reaction
        event uses the message, sender, and canonical add/remove payload so
        separate reactions cannot suppress one another.
        """
        data = activity.serialize() if hasattr(activity, "serialize") else vars(activity)
        if not isinstance(data, dict):
            data = {}
        activity_type = getattr(activity, "type", None) or data.get("type")
        raw_activity_id = getattr(activity, "id", None) or data.get("id")
        if activity_type != "messageReaction":
            return raw_activity_id, activity_type, None

        message_id = data.get("replyToId") or data.get("reply_to_id") or raw_activity_id
        sender = data.get("from") or data.get("from_property") or {}
        if not isinstance(sender, dict):
            sender = {}
        user_id = sender.get("aadObjectId") or sender.get("id") or "unknown"
        added = RiskBot._reaction_types(data.get("reactionsAdded") or data.get("reactions_added"))
        removed = RiskBot._reaction_types(data.get("reactionsRemoved") or data.get("reactions_removed"))
        # Ordered canonical lists make SDK ordering irrelevant while retaining
        # every entry in an activity that carries multiple reaction changes.
        key = (
            f"messageReaction:{message_id or 'unknown'}:{user_id}:"
            f"add={','.join(sorted(added))}:remove={','.join(sorted(removed))}"
        )
        return key, activity_type, {
            "message_id": message_id or "unknown",
            "user_id": user_id,
            "reactions_added": sorted(added),
            "reactions_removed": sorted(removed),
        }

    @staticmethod
    def _reaction_types(reactions: Any) -> list[str]:
        """Extract reaction type names from raw or SDK-shaped reaction lists."""
        if not isinstance(reactions, (list, tuple)):
            return []
        result = []
        for reaction in reactions:
            reaction_type = (
                reaction.get("type") if isinstance(reaction, dict)
                else getattr(reaction, "type", None)
            )
            if isinstance(reaction_type, str) and reaction_type:
                result.append(reaction_type)
        return result

    async def on_installation_update_add(self, turn_context: TurnContext) -> None:
        """Persist an installation and initiate channel discovery."""
        logger.info("teams_installation_added")
        log_teams_debug("bot added detected", **activity_metadata(turn_context.activity))
        if self.installation_service is not None:
            await self.installation_service.add(turn_context)

    async def on_installation_update_remove(self, turn_context: TurnContext) -> None:
        """Soft-deactivate an installation."""
        logger.info("teams_installation_removed")
        if self.installation_service is not None:
            await self.installation_service.remove(turn_context)

    async def on_conversation_update_activity(self, turn_context: TurnContext) -> None:
        """Delegate conversation lifecycle handling to the Teams service."""
        logger.info("teams_conversation_updated")
        if self.teams_service is not None:
            await self.teams_service.handle_conversation_update(turn_context)

    async def on_message_activity(self, turn_context: TurnContext) -> None:
        """Observe a message while leaving domain behavior in services."""
        logger.info("teams_message_received")
        await self._register_channel_from_activity(turn_context)

    async def on_message_reaction_activity(self, turn_context: TurnContext) -> None:
        """Delegate reaction add/remove state to the reaction service."""
        logger.info("teams_message_reaction_received")
        await self._register_channel_from_activity(turn_context)
        if self.reaction_service is not None:
            await self.reaction_service.handle(turn_context)

    async def on_adaptive_card_invoke(
        self, turn_context: TurnContext, invoke_value: Any
    ) -> AdaptiveCardInvokeResponse:
        """Validate an identifier-only risk-view action and render its latest view."""
        await self._register_channel_from_activity(turn_context)
        action = getattr(invoke_value, "action", None)
        if not isinstance(action, dict) or action.get("type") != "Action.Execute":
            return self._invoke_error(400, "BAD_RISK_VIEW_ACTION", "Unsupported card action.")
        data = action.get("data")
        if (
            not isinstance(data, dict)
            or action.get("verb") != "show_risk_view"
            or data.get("action") != "show_risk_view"
        ):
            return self._invoke_error(400, "BAD_RISK_VIEW_ACTION", "Unsupported card action.")
        risk_id = data.get("risk_id")
        view_name = data.get("view")
        if not isinstance(risk_id, str) or not risk_id.strip() or not isinstance(view_name, str):
            return self._invoke_error(400, "BAD_RISK_VIEW_ACTION", "Invalid risk view identifiers.")
        if view_name not in {"details", "mitigation"}:
            return self._invoke_error(400, "RISK_VIEW_INVALID", "The requested risk view is not supported.")
        if self.risk_view_service is None:
            return self._invoke_error(500, "RISK_VIEW_UNAVAILABLE", "Risk views are not configured.")
        if self.notification_service is None:
            return self._invoke_error(500, "RISK_VIEW_UNAVAILABLE", "Notification sending is not configured.")
        try:
            card = await self.risk_view_service.render(risk_id, view_name)
            await self.notification_service.send_in_context(
                turn_context,
                card,
                risk_id=risk_id,
                view_type=view_name,
            )
        except AppError as exc:
            return self._invoke_error(exc.status_code, exc.code, exc.message)
        return AdaptiveCardInvokeResponse(
            status_code=200,
            type="application/vnd.microsoft.activity.message",
            value={"text": ""},
        )

    @staticmethod
    def _invoke_error(status_code: int, code: str, message: str) -> AdaptiveCardInvokeResponse:
        """Return a safe Adaptive Card invoke error response."""
        return AdaptiveCardInvokeResponse(
            status_code=status_code,
            type="application/vnd.microsoft.error",
            value={"code": code, "message": message},
        )

    async def _register_channel_from_activity(self, turn_context: TurnContext) -> None:
        """Register channel context without coupling the router to persistence."""
        if self.teams_service is None:
            return
        register = getattr(self.teams_service, "register_channel_from_activity", None)
        if register is not None:
            await register(turn_context)
