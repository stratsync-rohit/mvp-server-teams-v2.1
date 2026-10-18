"""Team channel discovery business logic."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from src.clients.teams_client import TeamsClient
from src.bot.diagnostics.activity_logger import log_teams_debug, log_teams_debug_raw
from src.repositories.discovered_channel_repository import DiscoveredChannelRepository
from src.repositories.team_installation_repository import TeamInstallationRepository
from src.schemas.teams import (
    DiscoveredChannel,
    InstallationSelectedChannel,
    TeamsContext,
    _as_mapping,
)

logger = logging.getLogger(__name__)


def _value(item: Any, *names: str) -> Any:
    """Read a field from an SDK object or mapping."""
    for name in names:
        if isinstance(item, Mapping) and name in item:
            return item[name]
        if hasattr(item, name):
            return getattr(item, name)
    return None


def _clean_channel_name(value: Any) -> str | None:
    """Return a non-empty channel name without inventing one."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _resolve_channel_name(
    channel: Any,
    channel_id: str,
    team_id: str,
    persisted_name: Any = None,
    *trusted_names: Any,
) -> str | None:
    """Resolve a channel name from trusted values, never from IDs alone.

    ``channel_id`` and ``team_id`` remain part of the call contract for the
    logical channel identity, but their equality is not evidence of a display
    name.  The values are deliberately not used for name resolution.
    """
    del channel_id, team_id
    provider_names = tuple(
        _clean_channel_name(_value(channel, field))
        for field in ("name", "display_name", "displayName", "channel_name", "channelName")
    )
    for candidate in (*provider_names, persisted_name, *trusted_names):
        channel_name = _clean_channel_name(candidate)
        if channel_name:
            return channel_name
    return None


class TeamsService:
    """Discover and persist channels through the Teams client boundary."""

    def __init__(self, client: TeamsClient, channel_repository: DiscoveredChannelRepository,
                 installation_repository: TeamInstallationRepository | None = None,
                 destination_repository: Any | None = None,
                 destination_service: Any | None = None) -> None:
        self.client = client
        self.channel_repository = channel_repository
        self.installation_repository = installation_repository
        self.destination_repository = destination_repository
        self.destination_service = destination_service

    async def _upsert_discovered_channel(
        self, channel: DiscoveredChannel, *, source: str
    ) -> None:
        """Persist a discovered channel while exposing its safe diagnostic lifecycle."""
        fields = {
            "tenant_id": channel.tenant_id,
            "team_id": channel.team_id,
            "channel_id": channel.channel_id,
            "channel_name": channel.channel_name,
            "conversation_id": channel.conversation_id,
            "available": channel.available,
            "discovery_source": source,
        }
        log_teams_debug("discovered channel upsert start", **fields)
        log_teams_debug_raw("discovered channel before upsert", channel, **fields)
        await self.channel_repository.upsert(channel)
        log_teams_debug("discovered channel upsert complete", **fields)
        log_teams_debug_raw("discovered channel after upsert", channel, **fields)

    async def _resolve_team_name(self, context: TeamsContext) -> str | None:
        """Resolve a missing team name from the persisted installation context."""
        if isinstance(context.team_name, str) and context.team_name.strip():
            return context.team_name
        if self.installation_repository is None or not context.tenant_id or not context.team_id:
            return None
        installation = await self.installation_repository.get(context.tenant_id, context.team_id)
        team_name = installation.get("team_name") if installation else None
        return team_name if isinstance(team_name, str) and team_name.strip() else None

    async def _existing_channel_name(
        self, tenant_id: str, team_id: str, channel_id: str
    ) -> str | None:
        """Read an existing name so a sparse provider response cannot erase it."""
        get_channel = getattr(self.channel_repository, "get", None)
        if get_channel is None:
            return None
        existing = await get_channel(tenant_id, team_id, channel_id)
        return _clean_channel_name(_value(existing, "channel_name"))

    async def discover_channels(self, turn_context: Any, context: TeamsContext) -> list[DiscoveredChannel]:
        """Discover every channel and upsert by tenant/team/channel identity."""
        if not context.tenant_id or not context.team_id:
            return []
        team_name = await self._resolve_team_name(context)
        logger.info(
            "teams_channel_discovery_started",
            extra={
                "tenant_id": context.tenant_id,
                "team_id": context.team_id,
            },
        )
        log_teams_debug(
            "discovering team channels",
            tenant_id=context.tenant_id,
            team_id=context.team_id,
        )
        channels = await self.client.get_team_channels(turn_context, context.team_id)
        channels = channels or []
        logger.info(
            "teams_channel_discovery_completed",
            extra={
                "tenant_id": context.tenant_id,
                "team_id": context.team_id,
                "channel_count": len(channels),
            },
        )
        channel_summaries = [
            {
                "channel_id": _value(channel, "id"),
                "channel_name": _value(channel, "name", "display_name", "displayName"),
            }
            for channel in channels
        ]
        log_teams_debug(
            "team channels discovered",
            tenant_id=context.tenant_id,
            team_id=context.team_id,
            channel_count=len(channels),
            channels=channel_summaries,
        )
        log_teams_debug_raw(
            "TeamsInfo.get_team_channels response",
            channels,
            tenant_id=context.tenant_id,
            team_id=context.team_id,
        )
        discovered: list[DiscoveredChannel] = []
        for channel in channels:
            channel_id = _value(channel, "id")
            if not channel_id:
                continue
            persisted_name = await self._existing_channel_name(
                context.tenant_id, context.team_id, channel_id
            )
            channel_name = _resolve_channel_name(
                channel, channel_id, context.team_id, persisted_name
            )
            logger.info(
                "teams_channel_discovered",
                extra={
                    "tenant_id": context.tenant_id,
                    "team_id": context.team_id,
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                },
            )
            model = DiscoveredChannel(
                tenant_id=context.tenant_id,
                team_id=context.team_id,
                channel_id=channel_id,
                team_name=team_name,
                channel_name=channel_name,
                conversation_id=context.conversation_id,
                service_url=context.service_url,
                available=True,
            )
            await self._upsert_discovered_channel(model, source="TeamsInfo.get_team_channels")
            discovered.append(model)
        return discovered

    async def register_selected_install_channel(
        self, selected: InstallationSelectedChannel
    ) -> DiscoveredChannel | None:
        """Persist the channel explicitly selected during installation."""
        tenant_id = _clean_channel_name(selected.tenant_id)
        team_id = _clean_channel_name(selected.team_id)
        channel_id = _clean_channel_name(selected.channel_id)
        channel_name = _clean_channel_name(selected.channel_name)
        if not tenant_id or not team_id or not channel_id:
            return None

        context = TeamsContext(
            tenant_id=tenant_id,
            team_id=team_id,
            team_name=selected.team_name,
            conversation_id=selected.conversation_id,
            service_url=selected.service_url,
        )
        team_name = await self._resolve_team_name(context)
        channel = DiscoveredChannel(
            tenant_id=tenant_id,
            team_id=team_id,
            channel_id=channel_id,
            team_name=team_name,
            channel_name=channel_name,
            conversation_id=selected.conversation_id,
            service_url=selected.service_url,
            available=True,
        )
        await self._upsert_discovered_channel(
            channel, source="installation_selected_channel"
        )
        logger.info(
            "teams_install_selected_channel_registered",
            extra={
                "tenant_id": channel.tenant_id,
                "team_id": channel.team_id,
                "channel_id": channel.channel_id,
                "channel_name": channel.channel_name,
                "resolution_source": selected.resolution_source,
            },
        )
        return channel

    async def register_channel_from_activity(
        self, turn_context: Any
    ) -> DiscoveredChannel | None:
        """Register one channel from a genuine channel-scoped activity."""
        activity = turn_context.activity
        activity_data = _as_mapping(activity)
        activity_type = activity_data.get("type") or getattr(activity, "type", None)
        context = TeamsContext.from_activity(activity)
        channel_data = _as_mapping(
            activity_data.get("channelData") or activity_data.get("channel_data")
        )
        channel_is_scoped = (
            bool(channel_data.get("channel"))
            and isinstance(context.conversation_type, str)
            and context.conversation_type.lower() == "channel"
        )
        if (
            activity_type == "installationUpdate"
            or not channel_is_scoped
            or not context.tenant_id
            or not context.team_id
            or not context.channel_id
        ):
            return None

        team_name = await self._resolve_team_name(context)
        persisted_name = await self._existing_channel_name(
            context.tenant_id, context.team_id, context.channel_id
        )
        channel_name = _resolve_channel_name(
            {"name": context.channel_name},
            context.channel_id,
            context.team_id,
            persisted_name,
        )
        channel = DiscoveredChannel(
            tenant_id=context.tenant_id,
            team_id=context.team_id,
            channel_id=context.channel_id,
            team_name=team_name,
            channel_name=channel_name,
            conversation_id=context.conversation_id,
            service_url=context.service_url,
            available=True,
        )
        await self._upsert_discovered_channel(channel, source="activity")
        logger.info(
            "teams_channel_activity_registered",
            extra={
                "tenant_id": channel.tenant_id,
                "team_id": channel.team_id,
                "channel_id": channel.channel_id,
                "channel_name": channel.channel_name,
            },
        )
        return channel

    async def handle_conversation_update(self, turn_context: Any) -> DiscoveredChannel | None:
        """Handle channel rename/create and team-deleted Teams lifecycle events."""
        activity = turn_context.activity
        activity_data = _as_mapping(activity)
        activity_type = activity_data.get("type") or getattr(activity, "type", None)
        channel_data = _as_mapping(
            activity_data.get("channelData") or activity_data.get("channel_data")
        )
        event_type = channel_data.get("eventType") or channel_data.get("event_type")
        if activity_type != "conversationUpdate":
            return None

        context = TeamsContext.from_activity(activity)
        if event_type == "channelRenamed":
            return await self._handle_channel_renamed(context, channel_data)
        if event_type == "channelMemberAdded":
            return await self._handle_channel_member_added(
                activity_data, context, channel_data
            )
        if event_type == "channelMemberRemoved":
            return await self._handle_channel_member_removed(
                activity_data, context, channel_data
            )
        if event_type == "teamMemberAdded":
            logger.info(
                "teams_team_member_event_ignored_for_destination",
                extra={
                    "tenant_id": _clean_channel_name(context.tenant_id),
                    "team_id": _clean_channel_name(context.team_id),
                    "channel_id": None,
                    "channel_name": None,
                    "is_primary": False,
                },
            )
            return None

        if event_type == "teamDeleted":
            if not context.tenant_id or not context.team_id:
                logger.info(
                    "teams_team_deleted_skipped",
                    extra={
                        "tenant_id": context.tenant_id,
                        "team_id": context.team_id,
                        "team_name": context.team_name,
                    },
                )
                return None
            logger.info(
                "teams_team_deleted",
                extra={
                    "tenant_id": context.tenant_id,
                    "team_id": context.team_id,
                    "team_name": context.team_name,
                },
            )
            if self.installation_repository is not None:
                await self.installation_repository.mark_inactive(
                    context.tenant_id, context.team_id
                )
            mark_destinations_inactive = getattr(
                self.destination_repository, "mark_inactive_for_team", None
            )
            if mark_destinations_inactive is not None:
                await mark_destinations_inactive(context.tenant_id, context.team_id)
            return None

        if event_type != "channelCreated":
            return None

        if not context.tenant_id or not context.team_id or not context.channel_id:
            logger.info(
                "teams_channel_created_skipped",
                extra={
                    "tenant_id": context.tenant_id,
                    "team_id": context.team_id,
                    "channel_id": context.channel_id,
                },
            )
            return None

        logger.info(
            "teams_channel_created_received",
            extra={
                "tenant_id": context.tenant_id,
                "team_id": context.team_id,
                "channel_id": context.channel_id,
            },
        )
        team_name = await self._resolve_team_name(context)
        channel_data = _as_mapping(
            _as_mapping(activity_data.get("channelData") or activity_data.get("channel_data")).get("channel")
        )
        persisted_name = await self._existing_channel_name(
            context.tenant_id, context.team_id, context.channel_id
        )
        channel_name = _resolve_channel_name(
            channel_data,
            context.channel_id,
            context.team_id,
            persisted_name,
            context.channel_name,
        )
        channel = DiscoveredChannel(
            tenant_id=context.tenant_id,
            team_id=context.team_id,
            channel_id=context.channel_id,
            team_name=team_name,
            channel_name=channel_name,
            conversation_id=context.conversation_id,
            service_url=context.service_url,
            available=True,
        )
        await self._upsert_discovered_channel(channel, source="channelCreated")
        logger.info(
            "teams_discovered_channel_upserted",
            extra={
                "tenant_id": channel.tenant_id,
                "team_id": channel.team_id,
                "channel_id": channel.channel_id,
            },
        )
        return channel

    async def _handle_channel_renamed(
        self, context: TeamsContext, channel_data: Mapping[str, Any]
    ) -> None:
        """Enrich existing channel records from Teams' authoritative rename event."""
        channel = _as_mapping(channel_data.get("channel"))
        tenant_id = _clean_channel_name(context.tenant_id)
        team_id = _clean_channel_name(context.team_id)
        channel_id = _clean_channel_name(_value(channel, "id"))
        channel_name = _clean_channel_name(_value(channel, "name"))

        is_primary = bool(channel_id and team_id and channel_id == team_id)
        if not tenant_id or not team_id or not channel_id or not channel_name:
            logger.info(
                "teams_channel_renamed_skipped",
                extra={
                    "tenant_id": tenant_id,
                    "team_id": team_id,
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "is_primary": is_primary,
                },
            )
            return None

        get_discovered = getattr(self.channel_repository, "get", None)
        discovered = (
            await get_discovered(tenant_id, team_id, channel_id)
            if get_discovered is not None
            else None
        )
        destination = None
        get_destination = getattr(self.destination_repository, "get_by_channel", None)
        if get_destination is not None:
            destination = await get_destination(tenant_id, team_id, channel_id)

        old_name = _clean_channel_name(_value(discovered, "channel_name"))
        if old_name is None:
            old_name = _clean_channel_name(_value(destination, "channel_name"))

        logger.info(
            "teams_channel_renamed_received",
            extra={
                "tenant_id": tenant_id,
                "team_id": team_id,
                "channel_id": channel_id,
                "old_name": old_name,
                "new_name": channel_name,
                "is_primary": is_primary,
            },
        )

        updated = False
        update_discovered = getattr(self.channel_repository, "update_channel_name", None)
        if discovered is not None and update_discovered is not None:
            await update_discovered(tenant_id, team_id, channel_id, channel_name)
            updated = True

        update_destination = getattr(self.destination_repository, "update_channel_name", None)
        if destination is not None and update_destination is not None:
            await update_destination(tenant_id, team_id, channel_id, channel_name)
            updated = True

        if updated:
            logger.info(
                "teams_channel_name_updated",
                extra={
                    "tenant_id": tenant_id,
                    "team_id": team_id,
                    "channel_id": channel_id,
                    "old_name": old_name,
                    "new_name": channel_name,
                    "is_primary": is_primary,
                },
            )
        return None

    async def _handle_channel_member_added(
        self,
        activity_data: Mapping[str, Any],
        context: TeamsContext,
        channel_data: Mapping[str, Any],
    ) -> None:
        """Record bot channel context as discovery, never as configuration.

        Teams can report the bot as a member while the app is installed at
        team scope.  That makes the channel available to StratSync, but is
        not an explicit user choice of a notification destination.
        """
        channel = _as_mapping(channel_data.get("channel"))
        tenant_id = _clean_channel_name(context.tenant_id)
        team_id = _clean_channel_name(context.team_id)
        channel_id = _clean_channel_name(_value(channel, "id"))
        channel_name = _clean_channel_name(_value(channel, "name"))
        is_primary = bool(channel_id and team_id and channel_id == team_id)

        logger.info(
            "teams_channel_member_added_received",
            extra={
                "tenant_id": tenant_id,
                "team_id": team_id,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "is_primary": is_primary,
            },
        )
        if not tenant_id or not team_id or not channel_id:
            return None
        member_ids = [
            _clean_channel_name(_value(member, "id"))
            for member in (activity_data.get("membersAdded") or activity_data.get("members_added") or [])
        ]
        bot_was_added = self._bot_is_member(activity_data, context.recipient_id, "membersAdded")
        log_teams_debug(
            "bot membership check",
            bot_id=context.recipient_id,
            added_member_ids=member_ids,
            bot_was_added=bot_was_added,
            members_added_exists=bool(member_ids),
            tenant_id=tenant_id,
            team_id=team_id,
            channel_id=channel_id,
            channel_name=channel_name,
        )
        if not bot_was_added:
            return None

        logger.info(
            "teams_bot_added_to_channel",
            extra={
                "tenant_id": tenant_id,
                "team_id": team_id,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "is_primary": is_primary,
            },
        )

        get_channel = getattr(self.channel_repository, "get", None)
        existing = (
            await get_channel(tenant_id, team_id, channel_id)
            if get_channel is not None
            else None
        )
        existing_name = _clean_channel_name(_value(existing, "channel_name"))
        team_name = await self._resolve_team_name(context)
        channel = DiscoveredChannel(
            tenant_id=tenant_id,
            team_id=team_id,
            channel_id=channel_id,
            team_name=team_name,
            channel_name=channel_name or existing_name,
            conversation_id=context.conversation_id,
            service_url=context.service_url,
            available=True,
        )
        await self._upsert_discovered_channel(channel, source="channelMemberAdded")

        logger.info(
            "teams_bot_membership_channel_discovered",
            extra={
                "tenant_id": tenant_id,
                "team_id": team_id,
                "channel_id": channel_id,
                "channel_name": channel.channel_name,
                "is_primary": is_primary,
            },
        )
        return None

    async def _handle_channel_member_removed(
        self,
        activity_data: Mapping[str, Any],
        context: TeamsContext,
        channel_data: Mapping[str, Any],
    ) -> None:
        """Deactivate only the bot's destination for the removed channel."""
        channel = _as_mapping(channel_data.get("channel"))
        tenant_id = _clean_channel_name(context.tenant_id)
        team_id = _clean_channel_name(context.team_id)
        channel_id = _clean_channel_name(_value(channel, "id"))
        channel_name = _clean_channel_name(_value(channel, "name"))
        is_primary = bool(channel_id and team_id and channel_id == team_id)

        logger.info(
            "teams_channel_member_removed_received",
            extra={
                "tenant_id": tenant_id,
                "team_id": team_id,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "is_primary": is_primary,
            },
        )
        if not tenant_id or not team_id or not channel_id:
            return None
        if not self._bot_is_member(activity_data, context.recipient_id, "membersRemoved"):
            return None

        logger.info(
            "teams_bot_removed_from_channel",
            extra={
                "tenant_id": tenant_id,
                "team_id": team_id,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "is_primary": is_primary,
            },
        )

        get_destination = getattr(self.destination_repository, "get_by_channel", None)
        destination = (
            await get_destination(tenant_id, team_id, channel_id)
            if get_destination is not None
            else None
        )
        if destination is None:
            return None
        mark_inactive = getattr(
            self.destination_repository, "mark_inactive_for_channel", None
        )
        if mark_inactive is None:
            return None
        await mark_inactive(tenant_id, team_id, channel_id)
        logger.info(
            "teams_channel_destination_deactivated",
            extra={
                "tenant_id": tenant_id,
                "team_id": team_id,
                "channel_id": channel_id,
                "channel_name": channel_name or _value(destination, "channel_name"),
                "is_primary": is_primary,
            },
        )
        return None

    @staticmethod
    def _bot_is_member(
        activity_data: Mapping[str, Any], recipient_id: str | None, field: str
    ) -> bool:
        """Return whether the activity's member list contains the bot recipient."""
        recipient_id = _clean_channel_name(recipient_id)
        if not recipient_id:
            return False
        members = activity_data.get(field) or activity_data.get(
            "members_added" if field == "membersAdded" else "members_removed"
        )
        if not isinstance(members, (list, tuple)):
            return False
        return any(
            _clean_channel_name(_value(member, "id")) == recipient_id
            for member in members
        )
