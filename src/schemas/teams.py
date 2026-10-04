"""Typed internal contracts for authenticated Teams activity context."""

from __future__ import annotations

from datetime import datetime
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """Convert Bot Framework models and mappings into safe field mappings."""
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "serialize"):
        serialized = value.serialize()
        if isinstance(serialized, Mapping):
            return serialized
    if hasattr(value, "to_dict"):
        converted = value.to_dict()
        if isinstance(converted, Mapping):
            return converted
    if hasattr(value, "__dict__"):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    return {}


class TeamsContext(BaseModel):
    """Trusted routing context extracted after Bot Framework authentication."""

    model_config = ConfigDict(extra="ignore")

    tenant_id: str | None = None
    team_id: str | None = None
    team_name: str | None = None
    channel_id: str | None = None
    channel_name: str | None = None
    conversation_id: str | None = None
    conversation_type: str | None = None
    service_url: str | None = None
    recipient_id: str | None = None
    sender_id: str | None = None

    @classmethod
    def from_activity(cls, activity: Any) -> "TeamsContext":
        """Extract Teams routing metadata from an SDK activity or raw mapping."""
        data = _as_mapping(activity)
        channel_data = _as_mapping(data.get("channelData") or data.get("channel_data"))
        conversation = _as_mapping(data.get("conversation"))
        sender = _as_mapping(data.get("from") or data.get("from_property"))
        recipient = _as_mapping(data.get("recipient"))
        tenant = _as_mapping(channel_data.get("tenant"))
        team = _as_mapping(channel_data.get("team"))
        channel = _as_mapping(channel_data.get("channel"))
        return cls(
            tenant_id=tenant.get("id") or conversation.get("tenantId"),
            team_id=team.get("id"),
            team_name=team.get("name"),
            channel_id=channel.get("id"),
            channel_name=channel.get("name"),
            conversation_id=conversation.get("id"),
            conversation_type=conversation.get("conversationType"),
            service_url=data.get("serviceUrl") or data.get("service_url"),
            recipient_id=recipient.get("id"),
            sender_id=sender.get("id"),
        )


class InstallationSelectedChannel(BaseModel):
    """Channel metadata explicitly supplied by an installation activity."""

    model_config = ConfigDict(extra="ignore")

    tenant_id: str | None = None
    team_id: str | None = None
    team_name: str | None = None
    channel_id: str
    channel_name: str
    conversation_id: str | None = None
    service_url: str | None = None
    resolution_source: str


def _field(value: Any, *names: str) -> Any:
    """Read a field from either a mapping or a Bot Framework model."""
    mapped = _as_mapping(value)
    for name in names:
        if name in mapped:
            return mapped[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _normalized_string(value: Any) -> str | None:
    """Normalize a provider string without coercing arbitrary values."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def extract_installation_selected_channel(
    activity: Any,
) -> InstallationSelectedChannel | None:
    """Extract an explicitly selected channel from an installation activity.

    Teams has emitted this metadata in several shapes over time.  The order
    below is intentional and keeps this extraction isolated from the normal
    ``TeamsContext`` activity routing path.
    """
    data = _as_mapping(activity)
    channel_data = _field(data, "channelData", "channel_data")
    channel_data = _as_mapping(channel_data)
    settings = _field(channel_data, "settings")

    candidates = (
        ("settings.selectedChannel", _field(settings, "selectedChannel")),
        ("settings.selected_channel", _field(settings, "selected_channel")),
        ("channelData.selectedChannel", _field(channel_data, "selectedChannel")),
        ("channelData.selected_channel", _field(channel_data, "selected_channel")),
        ("channelData.channel", _field(channel_data, "channel")),
    )

    conversation = _field(data, "conversation")
    conversation_id = _normalized_string(_field(conversation, "id"))
    conversation_name = _normalized_string(_field(conversation, "name"))
    conversation_type = _normalized_string(
        _field(conversation, "conversationType", "conversation_type")
    )
    channel_data_channel = _field(channel_data, "channel")

    for source, candidate in candidates:
        channel_id = _normalized_string(_field(candidate, "id"))
        if not channel_id:
            continue
        channel_name = _normalized_string(_field(candidate, "name"))

        if channel_name is None and conversation_type and conversation_type.lower() == "channel":
            candidate_conversation_id = _normalized_string(
                _field(candidate, "conversationId", "conversation_id")
            )
            conversation_channel_id = _normalized_string(
                _field(conversation, "channelId", "channel_id")
            )
            associated = (
                conversation_id == channel_id
                or candidate_conversation_id == conversation_id
                or conversation_channel_id == channel_id
                or _normalized_string(_field(channel_data_channel, "id")) == channel_id
            )
            if conversation_id and conversation_name and associated:
                channel_name = conversation_name

        if not channel_name:
            continue

        return InstallationSelectedChannel(
            tenant_id=_normalized_string(
                _field(_field(channel_data, "tenant"), "id")
            ) or _normalized_string(_field(conversation, "tenantId", "tenant_id")),
            team_id=_normalized_string(_field(_field(channel_data, "team"), "id")),
            team_name=_normalized_string(_field(_field(channel_data, "team"), "name")),
            channel_id=channel_id,
            channel_name=channel_name,
            conversation_id=conversation_id,
            service_url=_normalized_string(_field(data, "serviceUrl", "service_url")),
            resolution_source=source,
        )

    return None


class TeamInstallation(BaseModel):
    """Persisted lifecycle state for one Teams bot installation."""

    model_config = ConfigDict(extra="ignore")

    tenant_id: str
    team_id: str
    team_name: str | None = None
    conversation_id: str | None = None
    conversation_type: str | None = None
    service_url: str | None = None
    bot_id: str | None = None
    is_active: bool = True
    installed_at: datetime | None = None
    updated_at: datetime | None = None
    uninstalled_at: datetime | None = None


class DiscoveredChannel(BaseModel):
    """A channel discovered from a trusted team installation."""

    model_config = ConfigDict(extra="ignore")

    tenant_id: str
    team_id: str
    channel_id: str
    team_name: str | None = None
    channel_name: str | None = None
    conversation_id: str | None = None
    service_url: str | None = None
    available: bool = True
    discovered_at: datetime | None = None
    updated_at: datetime | None = None
