"""Installation lifecycle business logic."""

from __future__ import annotations

import logging
from typing import Any

from src.repositories.team_installation_repository import TeamInstallationRepository
from src.schemas.installation import DiscoveredChannelResponse, InstallationResponse
from src.schemas.teams import (
    TeamInstallation,
    TeamsContext,
    extract_installation_selected_channel,
)
from src.services.teams_service import TeamsService

logger = logging.getLogger(__name__)


class InstallationService:
    """Persist authenticated installation events and trigger discovery."""

    def __init__(self, repository: TeamInstallationRepository, teams_service: TeamsService,
                 destination_repository: Any | None = None,
                 destination_service: Any | None = None) -> None:
        self.repository = repository
        self.teams_service = teams_service
        self.destination_repository = destination_repository
        self.destination_service = destination_service

    async def list_installations(self) -> list[InstallationResponse]:
        """Aggregate safe installation DTOs with active connected destinations."""
        installations = await self.repository.list_active()
        results: list[InstallationResponse] = []
        for installation in installations:
            tenant_id = installation.get("tenant_id")
            team_id = installation.get("team_id")
            if not tenant_id or not team_id:
                continue
            destination_documents = []
            if self.destination_repository is not None:
                destination_documents = await self.destination_repository.list_active_for_team(
                    tenant_id, team_id
                )
            channels = [DiscoveredChannelResponse(
                channel_id=destination["channel_id"],
                channel_name=destination.get("channel_name"),
                is_primary=destination["channel_id"] == destination["team_id"],
                is_active=destination.get("is_active", True),
                team_id=destination["team_id"],
                team_name=destination.get("team_name") or installation.get("team_name"),
                tenant_id=destination["tenant_id"],
                conversation_id=destination.get("conversation_id"),
                service_url=destination.get("service_url"),
                available=destination.get("available", destination.get("is_active", True)),
                discovered_at=destination.get("discovered_at"),
                updated_at=destination.get("updated_at"),
            ) for destination in destination_documents]
            results.append(InstallationResponse(
                tenant_id=tenant_id,
                team_id=team_id,
                team_name=installation.get("team_name"),
                conversation_id=installation.get("conversation_id"),
                service_url=installation.get("service_url"),
                is_active=installation.get("is_active", True),
                installed_at=installation.get("installed_at"),
                updated_at=installation.get("updated_at"),
                uninstalled_at=installation.get("uninstalled_at"),
                channels=channels,
            ))
        return results

    async def add(self, turn_context: Any) -> None:
        """Upsert installation metadata, then discover its channels."""
        context = TeamsContext.from_activity(turn_context.activity)
        if not context.tenant_id or not context.team_id:
            return
        await self.repository.upsert(TeamInstallation(
            tenant_id=context.tenant_id,
            team_id=context.team_id,
            team_name=context.team_name,
            conversation_id=context.conversation_id,
            conversation_type=context.conversation_type,
            service_url=context.service_url,
            bot_id=context.recipient_id,
        ))
        await self.teams_service.discover_channels(turn_context, context)

        selected_channel = extract_installation_selected_channel(turn_context.activity)
        if selected_channel is None:
            logger.info(
                "teams_install_selected_channel_missing",
                extra={
                    "tenant_id": context.tenant_id,
                    "team_id": context.team_id,
                    "channel_id": None,
                    "channel_name": None,
                    "resolution_source": None,
                },
            )
            return

        logger.info(
            "teams_install_selected_channel_found",
            extra={
                "tenant_id": selected_channel.tenant_id or context.tenant_id,
                "team_id": selected_channel.team_id or context.team_id,
                "channel_id": selected_channel.channel_id,
                "channel_name": selected_channel.channel_name,
                "resolution_source": selected_channel.resolution_source,
            },
        )
        register = getattr(self.teams_service, "register_selected_install_channel", None)
        if register is None:
            return
        registered_channel = await register(selected_channel)
        if registered_channel is None or self.destination_service is None:
            if registered_channel is None and self.destination_service is not None:
                logger.error(
                    "teams_install_destination_resolution_failed",
                    extra={
                        "tenant_id": selected_channel.tenant_id or context.tenant_id,
                        "team_id": selected_channel.team_id or context.team_id,
                        "channel_id": selected_channel.channel_id,
                        "channel_name": selected_channel.channel_name,
                    },
                )
            return

        try:
            destination = await self.destination_service.resolve_discovered_channel(
                registered_channel.tenant_id,
                registered_channel.team_id,
                registered_channel.channel_id,
            )
        except Exception:
            logger.exception(
                "teams_install_destination_resolution_failed",
                extra={
                    "tenant_id": registered_channel.tenant_id,
                    "team_id": registered_channel.team_id,
                    "channel_id": registered_channel.channel_id,
                    "channel_name": registered_channel.channel_name,
                },
            )
            return

        logger.info(
            "teams_install_destination_resolved",
            extra={
                "tenant_id": destination.tenant_id,
                "team_id": destination.team_id,
                "channel_id": destination.channel_id,
                "channel_name": destination.channel_name,
            },
        )

    async def remove(self, turn_context: Any) -> None:
        """Soft-deactivate an installation from an authenticated activity."""
        context = TeamsContext.from_activity(turn_context.activity)
        if context.tenant_id and context.team_id:
            await self.repository.mark_inactive(context.tenant_id, context.team_id)
            mark_destinations_inactive = getattr(
                self.destination_repository, "mark_inactive_for_team", None
            )
            if mark_destinations_inactive is not None:
                await mark_destinations_inactive(context.tenant_id, context.team_id)
