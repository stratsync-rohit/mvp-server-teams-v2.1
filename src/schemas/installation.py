"""Safe API response DTOs for Teams installations and discovered channels."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DiscoveredChannelResponse(BaseModel):
    """Public-safe available channel metadata and destination state."""

    model_config = ConfigDict(extra="ignore")

    channel_id: str
    channel_name: str | None = None
    is_primary: bool = False
    is_active: bool = True
    # ``connected`` is retained for API compatibility.  New clients should
    # use ``is_configured``/``status_label`` so discovery is never rendered as
    # an installation or destination.
    connected: bool = False
    is_configured: bool = False
    status_label: str = "Available"
    destination_id: str | None = None
    team_id: str
    team_name: str | None = None
    tenant_id: str
    conversation_id: str | None = None
    service_url: str | None = None
    available: bool = True
    discovered_at: datetime | None = None
    updated_at: datetime | None = None


class InstallationResponse(BaseModel):
    """Public-safe installation metadata with its discovered channels."""

    model_config = ConfigDict(extra="ignore")

    tenant_id: str
    team_id: str
    team_name: str | None = None
    conversation_id: str | None = None
    service_url: str | None = None
    is_active: bool = True
    installation_status_label: str = "Active"
    installed_at: datetime | None = None
    updated_at: datetime | None = None
    uninstalled_at: datetime | None = None
    channels: list[DiscoveredChannelResponse] = Field(default_factory=list)


class InstallationsResponse(BaseModel):
    """Envelope returned by the installations read API."""

    success: bool
    data: list[InstallationResponse]
    error: object | None = None
