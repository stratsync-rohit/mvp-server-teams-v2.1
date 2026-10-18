"""Installation lifecycle business logic."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from src.bot.diagnostics.activity_logger import log_teams_debug, log_teams_debug_raw
from src.repositories.team_installation_repository import TeamInstallationRepository
from src.schemas.installation import DiscoveredChannelResponse, InstallationResponse
from src.schemas.teams import (
    TeamInstallation,
    TeamsContext,
    _as_mapping,
    extract_installation_selected_channel,
)
from src.services.teams_service import TeamsService

logger = logging.getLogger(__name__)


def _installation_value(installation: Any, field: str) -> Any:
    """Read persisted installation metadata from a mapping or model."""
    if isinstance(installation, Mapping):
        return installation.get(field)
    return getattr(installation, field, None)


def _candidate_details(candidate: Any) -> dict[str, Any]:
    """Return safe, display-oriented metadata for an origin-channel candidate."""
    candidate_data = _as_mapping(candidate)
    return {
        "channel_id": candidate_data.get("id"),
        "channel_name": candidate_data.get("name"),
        "available": bool(candidate_data.get("id")),
    }


def _display_channel_name(channel_name: Any, *, is_primary: bool) -> Any:
    """Provide the conventional primary-channel label without changing storage."""
    if is_primary and (not isinstance(channel_name, str) or not channel_name.strip()):
        return "General"
    return channel_name


class InstallationService:
    """Persist authenticated installation events and trigger discovery."""

    def __init__(self, repository: TeamInstallationRepository, teams_service: TeamsService,
                 destination_repository: Any | None = None,
                 destination_service: Any | None = None,
                 discovered_channel_repository: Any | None = None) -> None:
        self.repository = repository
        self.teams_service = teams_service
        self.destination_repository = destination_repository
        self.destination_service = destination_service
        self.discovered_channel_repository = discovered_channel_repository

    async def list_installations(self) -> list[InstallationResponse]:
        """Aggregate every discovered channel with active connection status."""
        log_teams_debug("installation aggregation/update start")
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
            discovered_documents = []
            list_discovered = getattr(self.discovered_channel_repository, "list_for_team", None)
            if list_discovered is not None:
                discovered_documents = await list_discovered(tenant_id, team_id)

            active_destinations = {
                (
                    destination.get("tenant_id", tenant_id),
                    destination.get("team_id", team_id),
                    destination.get("channel_id"),
                ): destination
                for destination in destination_documents
            }
            channels = []
            for discovered in discovered_documents:
                channel_id = discovered.get("channel_id")
                if not channel_id:
                    continue
                channel_tenant_id = discovered.get("tenant_id", tenant_id)
                channel_team_id = discovered.get("team_id", team_id)
                destination = active_destinations.get(
                    (channel_tenant_id, channel_team_id, channel_id)
                )
                is_primary = channel_id == channel_team_id
                raw_channel_name = discovered.get("channel_name") or (
                    destination.get("channel_name") if destination else None
                )
                channels.append(DiscoveredChannelResponse(
                    channel_id=channel_id,
                    channel_name=_display_channel_name(
                        raw_channel_name, is_primary=is_primary
                    ),
                    is_primary=is_primary,
                    is_active=discovered.get("available", True),
                    connected=destination is not None,
                    is_configured=destination is not None,
                    status_label="Configured" if destination is not None else "Available",
                    destination_id=(
                        destination.get("destination_id") if destination else None
                    ),
                    team_id=channel_team_id,
                    team_name=discovered.get("team_name") or installation.get("team_name"),
                    tenant_id=channel_tenant_id,
                    conversation_id=(destination.get("conversation_id") if destination else None)
                    or discovered.get("conversation_id"),
                    service_url=(destination.get("service_url") if destination else None)
                    or discovered.get("service_url"),
                    available=discovered.get("available", True),
                    discovered_at=discovered.get("discovered_at"),
                    updated_at=discovered.get("updated_at"),
                ))
                channel_state = channels[-1]
                log_teams_debug(
                    "installation channel state",
                    channel_name=channel_state.channel_name,
                    channel_id=channel_state.channel_id,
                    available=channel_state.available,
                    connected=channel_state.connected,
                    is_configured=channel_state.is_configured,
                    destination_id=channel_state.destination_id,
                    status_label=channel_state.status_label,
                    is_primary=channel_state.is_primary,
                    tenant_id=channel_state.tenant_id,
                    team_id=channel_state.team_id,
                )
            results.append(InstallationResponse(
                tenant_id=tenant_id,
                team_id=team_id,
                team_name=installation.get("team_name"),
                conversation_id=installation.get("conversation_id"),
                service_url=installation.get("service_url"),
                is_active=installation.get("is_active", True),
                installation_status_label=(
                    "Active" if installation.get("is_active", True) else "Inactive"
                ),
                installed_at=installation.get("installed_at"),
                updated_at=installation.get("updated_at"),
                uninstalled_at=installation.get("uninstalled_at"),
                channels=channels,
            ))
        log_teams_debug("installation aggregation/update complete", installation_count=len(results))
        log_teams_debug_raw("installation aggregation result", results)
        return results

    async def add(self, turn_context: Any) -> None:
        """Upsert installation metadata, then discover its channels."""
        context = TeamsContext.from_activity(turn_context.activity)
        if not context.tenant_id or not context.team_id:
            return
        activity_data = _as_mapping(turn_context.activity)
        channel_data = _as_mapping(
            activity_data.get("channelData") or activity_data.get("channel_data")
        )
        settings = _as_mapping(channel_data.get("settings"))
        log_teams_debug(
            "team installation processing started",
            tenant_id=context.tenant_id,
            team_id=context.team_id,
            team_name=context.team_name,
            channel_id=context.channel_id,
            channel_name=context.channel_name,
            conversation_id=context.conversation_id,
            conversation_type=context.conversation_type,
            service_url=context.service_url,
            activity_type=activity_data.get("type"),
            event_type=channel_data.get("eventType") or channel_data.get("event_type"),
        )
        log_teams_debug("origin channel resolution started", tenant_id=context.tenant_id, team_id=context.team_id)
        for source, candidate in (
            ("settings.selectedChannel", settings.get("selectedChannel")),
            ("settings.selected_channel", settings.get("selected_channel")),
            ("channelData.selectedChannel", channel_data.get("selectedChannel")),
            ("channelData.selected_channel", channel_data.get("selected_channel")),
            ("activity.channelData.channel", channel_data.get("channel")),
        ):
            log_teams_debug("origin channel candidate", source=source, **_candidate_details(candidate))
        get_existing = getattr(self.repository, "get", None)
        existing = (
            await get_existing(context.tenant_id, context.team_id)
            if get_existing is not None
            else None
        )
        log_teams_debug(
            "origin channel candidate",
            source="existing persisted installation channel",
            channel_id=None,
            channel_name=None,
            available=False,
            installation_exists=existing is not None,
        )
        selected_channel = extract_installation_selected_channel(turn_context.activity)
        if selected_channel is None:
            log_teams_debug(
                "origin channel not resolved",
                tenant_id=context.tenant_id,
                team_id=context.team_id,
            )
        else:
            log_teams_debug(
                "origin channel resolved",
                tenant_id=context.tenant_id,
                team_id=context.team_id,
                channel_id=selected_channel.channel_id,
                channel_name=selected_channel.channel_name,
                resolution_source=selected_channel.resolution_source,
                used_for_configuration=False,
            )
        # A team installation owns the team/primary conversation. A later
        # channel-scoped installation-shaped activity must not replace it with
        # a secondary channel conversation.
        preserve_lifecycle = existing is not None and context.channel_id != context.team_id
        if preserve_lifecycle:
            installation = TeamInstallation(
                tenant_id=context.tenant_id,
                team_id=context.team_id,
                team_name=_installation_value(existing, "team_name"),
                conversation_id=_installation_value(existing, "conversation_id"),
                conversation_type=_installation_value(existing, "conversation_type"),
                service_url=_installation_value(existing, "service_url"),
                bot_id=_installation_value(existing, "bot_id"),
                installed_at=_installation_value(existing, "installed_at"),
            )
        else:
            installation = TeamInstallation(
                tenant_id=context.tenant_id,
                team_id=context.team_id,
                team_name=context.team_name,
                conversation_id=context.conversation_id,
                conversation_type=context.conversation_type,
                service_url=context.service_url,
                bot_id=context.recipient_id,
            )
        installation_fields = {
            "tenant_id": installation.tenant_id,
            "team_id": installation.team_id,
            "team_name": installation.team_name,
            "is_active": True,
            "conversation_id": installation.conversation_id,
            "service_url": installation.service_url,
        }
        log_teams_debug("installation upsert start", **installation_fields)
        log_teams_debug_raw("installation before upsert", existing, **installation_fields)
        await self.repository.upsert(installation)
        log_teams_debug("installation upsert complete", **installation_fields)
        log_teams_debug_raw("installation after upsert", installation, **installation_fields)
        logger.info(
            "teams_installation_registered",
            extra={
                "tenant_id": context.tenant_id,
                "team_id": context.team_id,
                "lifecycle_preserved": preserve_lifecycle,
            },
        )
        discovered = await self.teams_service.discover_channels(turn_context, context) or []
        logger.info(
            "teams_channels_discovered",
            extra={
                "tenant_id": context.tenant_id,
                "team_id": context.team_id,
                "channel_count": len(discovered),
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
