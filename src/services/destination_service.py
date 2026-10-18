"""Outbound destination resolution."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from src.bot.diagnostics.activity_logger import log_teams_debug, log_teams_debug_raw
from src.clients.teams_conversation_client import TeamsConversationClient
from src.exceptions import AppError
from src.repositories.discovered_channel_repository import DiscoveredChannelRepository
from src.repositories.destination_repository import DestinationRepository
from src.schemas.destination import Destination, build_destination_id

logger = logging.getLogger(__name__)


def _destination_value(destination: Any, field: str) -> Any:
    """Read a persisted destination field from a mapping or model."""
    if isinstance(destination, Mapping):
        return destination.get(field)
    return getattr(destination, field, None)


class DestinationService:
    """Resolve a provider route before persisting a destination."""

    def __init__(self, repository: DestinationRepository, conversation_client: TeamsConversationClient,
                 discovered_channel_repository: DiscoveredChannelRepository | None = None) -> None:
        self.repository = repository
        self.conversation_client = conversation_client
        self.discovered_channel_repository = discovered_channel_repository

    async def resolve_discovered_channel(self, tenant_id: str, team_id: str,
                                         channel_id: str) -> Destination:
        """Resolve one trusted discovered channel into an idempotent destination."""
        if self.discovered_channel_repository is None:
            raise AppError("DESTINATION_CONFIGURATION_ERROR", "Channel discovery is not configured.", 500)
        channel = await self.discovered_channel_repository.get(tenant_id, team_id, channel_id)
        if channel is None:
            raise AppError("DISCOVERED_CHANNEL_NOT_FOUND", "The Teams channel was not discovered.", 404)
        if not channel.get("available", True):
            raise AppError("DISCOVERED_CHANNEL_UNAVAILABLE", "The Teams channel is unavailable.", 409)

        channel_name = channel.get("channel_name")
        is_primary = channel_id == team_id
        destination_fields = {
            "tenant_id": tenant_id,
            "team_id": team_id,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "conversation_id": channel.get("conversation_id"),
        }
        log_teams_debug("destination resolution start", **destination_fields)
        logger.info(
            "teams_destination_connect_requested",
            extra={
                "tenant_id": tenant_id,
                "team_id": team_id,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "is_primary": is_primary,
            },
        )
        get_existing = getattr(self.repository, "get_by_channel", None)
        existing = (
            await get_existing(tenant_id, team_id, channel_id)
            if get_existing is not None
            else None
        )
        existing_active = _destination_value(existing, "is_active")
        was_inactive = existing is not None and existing_active is False
        existing_destination_id = _destination_value(existing, "destination_id")
        log_teams_debug_raw("destination before resolution", existing, **destination_fields)

        reference = self.conversation_client.build_channel_reference(
            tenant_id=channel.get("tenant_id", ""),
            channel_id=channel.get("channel_id", ""),
            service_url=channel.get("service_url", ""),
        )
        if reference is None:
            raise AppError("DESTINATION_ROUTE_INVALID", "The discovered channel has incomplete routing metadata.", 422)
        resolved = await self.conversation_client.resolve_conversation(reference)
        if resolved is None:
            raise AppError("DESTINATION_ROUTE_INVALID", "The Teams channel route could not be prepared.", 502)

        destination = Destination(
            # Reconfiguration must keep the identifier already persisted for
            # this canonical tenant/team/channel record.  It is the value the
            # UI must later send to DELETE /api/destinations/{destination_id}.
            destination_id=(
                existing_destination_id
                if isinstance(existing_destination_id, str) and existing_destination_id
                else build_destination_id(tenant_id, team_id, channel_id)
            ),
            tenant_id=tenant_id,
            team_id=team_id,
            channel_id=channel_id,
            team_name=channel.get("team_name"),
            channel_name=channel.get("channel_name"),
            conversation_id=resolved.get("conversation", {}).get("id"),
            conversation_reference=resolved,
            service_url=channel.get("service_url"),
            is_active=True,
        )
        await self.repository.upsert(destination)
        configured_fields = {
            **destination_fields,
            "destination_id": destination.destination_id,
        }
        if existing is not None:
            log_teams_debug("existing destination reused", **configured_fields)
        else:
            log_teams_debug("new destination created", **configured_fields)
        log_teams_debug("destination configured", **configured_fields)
        log_teams_debug_raw("destination after resolution", destination, **configured_fields)
        logger.info(
            "teams_destination_reactivated" if was_inactive else "teams_destination_connected",
            extra={
                "tenant_id": tenant_id,
                "team_id": team_id,
                "channel_id": channel_id,
                "channel_name": destination.channel_name,
                "is_primary": is_primary,
            },
        )
        return destination

    async def resolve(self, route: dict[str, Any]) -> Destination | None:
        """Resolve and persist a destination only when routing metadata is valid."""
        required = ("tenant_id", "team_id", "channel_id")
        if any(not route.get(field) for field in required):
            return None
        log_teams_debug(
            "destination resolution start",
            tenant_id=route.get("tenant_id"),
            team_id=route.get("team_id"),
            channel_id=route.get("channel_id"),
            channel_name=route.get("channel_name"),
            conversation_id=route.get("conversation_id"),
        )
        log_teams_debug_raw("destination before resolution", None,
                            tenant_id=route.get("tenant_id"), team_id=route.get("team_id"),
                            channel_id=route.get("channel_id"))
        reference = route.get("conversation_reference") or {
            "serviceUrl": route.get("service_url"),
            "conversation": {"id": route.get("conversation_id")},
            "channelId": "msteams",
        }
        resolved = await self.conversation_client.resolve_conversation(reference)
        if not resolved:
            return None
        data = dict(route)
        data["destination_id"] = build_destination_id(*(data[field] for field in required))
        data["conversation_reference"] = resolved
        data.setdefault("conversation_id", resolved.get("conversation", {}).get("id"))
        destination = Destination(**data)
        await self.repository.upsert(destination)
        log_teams_debug(
            "destination configured",
            tenant_id=destination.tenant_id,
            team_id=destination.team_id,
            channel_id=destination.channel_id,
            channel_name=destination.channel_name,
            destination_id=destination.destination_id,
        )
        log_teams_debug_raw("destination after resolution", destination)
        return destination

    async def get(self, destination_id: str) -> Destination | None:
        """Load a previously resolved destination."""
        data = await self.repository.get(destination_id)
        if isinstance(data, Destination):
            return data
        return Destination(**data) if data else None

    async def deactivate(self, destination_id: str) -> Destination:
        """Disable one configured channel without changing its Team install."""
        destination = await self.get(destination_id)
        if destination is None:
            raise AppError("DESTINATION_NOT_FOUND", "The destination was not found.", 404)
        disable = getattr(self.repository, "disable_for_channel", None)
        mark_inactive = getattr(self.repository, "mark_inactive_for_channel", None)
        if disable is None and mark_inactive is None:
            raise AppError(
                "DESTINATION_CONFIGURATION_ERROR",
                "Destination deactivation is not configured.",
                500,
            )
        deactivate = disable or mark_inactive
        await deactivate(destination.tenant_id, destination.team_id, destination.channel_id)
        logger.info(
            "teams_destination_deactivated",
            extra={
                "tenant_id": destination.tenant_id,
                "team_id": destination.team_id,
                "channel_id": destination.channel_id,
            },
        )
        return destination.model_copy(update={"is_active": False, "is_configured": False})
