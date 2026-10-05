"""Tests for the safe Teams installations read API."""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from src.api.dependencies import Container
from src.application import create_app
from src.schemas.installation import DiscoveredChannelResponse, InstallationResponse
from src.services.installation_service import InstallationService


def _installation(tenant_id: str, team_id: str) -> dict:
    """Build a persistence-shaped installation fixture."""
    timestamp = datetime(2026, 9, 29, tzinfo=timezone.utc)
    return {
        "_id": "must-not-leak",
        "tenant_id": tenant_id,
        "team_id": team_id,
        "team_name": f"Team {team_id}",
        "conversation_id": f"conversation-{team_id}",
        "service_url": "https://smba.example",
        "is_active": True,
        "installed_at": timestamp,
        "updated_at": timestamp,
        "uninstalled_at": None,
        "microsoft_app_password": "must-not-leak",
    }


def _destination(tenant_id: str, team_id: str, channel_id: str, *, active: bool = True) -> dict:
    """Build a persistence-shaped destination fixture."""
    timestamp = datetime(2026, 9, 29, tzinfo=timezone.utc)
    return {
        "_id": "must-not-leak",
        "tenant_id": tenant_id,
        "team_id": team_id,
        "channel_id": channel_id,
        "channel_name": f"Channel {channel_id}",
        "team_name": f"Team {team_id}",
        "conversation_id": f"conversation-{team_id}",
        "service_url": "https://smba.example",
        "is_active": active,
        "updated_at": timestamp,
        "conversation_reference": {"secret": "must-not-leak"},
    }


def _aggregated_service() -> InstallationService:
    class InstallationRepository:
        async def list_active(self):
            return [_installation("tenant-a", "team-1"), _installation("tenant-b", "team-1")]

    class DestinationRepository:
        async def list_active_for_team(self, tenant_id, team_id):
            return [_destination(tenant_id, team_id, f"channel-{tenant_id}")]

    return InstallationService(InstallationRepository(), object(), DestinationRepository())


def test_installations_service_groups_channels_by_tenant_and_team():
    import asyncio

    data = asyncio.run(_aggregated_service().list_installations())

    assert len(data) == 2
    assert data[0].team_name == "Team team-1"
    assert data[0].channels[0].tenant_id == "tenant-a"
    assert data[1].channels[0].tenant_id == "tenant-b"
    assert data[0].channels[0].channel_id != data[1].channels[0].channel_id


def test_installations_service_without_channel_repository_still_returns_installations():
    import asyncio

    class InstallationRepository:
        async def list_active(self):
            return [_installation("tenant-a", "team-1")]

    data = asyncio.run(
        InstallationService(InstallationRepository(), object()).list_installations()
    )

    assert len(data) == 1
    assert data[0].team_name == "Team team-1"
    assert data[0].channels == []


def test_installations_service_uses_active_installations_only():
    import asyncio

    active = _installation("tenant-a", "active-team")
    inactive = _installation("tenant-a", "deleted-team")
    inactive["is_active"] = False

    class InstallationRepository:
        async def list_active(self):
            return [active]

        async def list_all(self):
            return [active, inactive]

    data = asyncio.run(
        InstallationService(InstallationRepository(), object()).list_installations()
    )

    assert [item.team_id for item in data] == ["active-team"]


def test_installations_service_reads_connected_destinations_only():
    import asyncio

    class InstallationRepository:
        async def list_active(self):
            return [_installation("tenant-a", "team-1")]

    class DestinationRepository:
        async def list_active_for_team(self, tenant_id, team_id):
            assert (tenant_id, team_id) == ("tenant-a", "team-1")
            return [_destination("tenant-a", "team-1", "test2")]

        async def list_for_team(self, tenant_id, team_id):
            raise AssertionError("discovered channels must not feed connected-channel API")

    data = asyncio.run(
        InstallationService(
            InstallationRepository(), object(), DestinationRepository()
        ).list_installations()
    )

    assert [channel.channel_id for channel in data[0].channels] == ["test2"]


def test_installations_service_excludes_inactive_destinations():
    import asyncio

    class InstallationRepository:
        async def list_active(self):
            return [_installation("tenant-a", "team-1")]

    class DestinationRepository:
        async def list_active_for_team(self, tenant_id, team_id):
            documents = [
                _destination("tenant-a", "team-1", "test2"),
                _destination("tenant-a", "team-1", "test3", active=False),
            ]
            return [document for document in documents if document["is_active"] is True]

    data = asyncio.run(
        InstallationService(
            InstallationRepository(), object(), DestinationRepository()
        ).list_installations()
    )

    assert [channel.channel_id for channel in data[0].channels] == ["test2"]


def test_get_installations_returns_safe_enveloped_data():
    class InstallationServiceDouble:
        async def list_installations(self):
            return [InstallationResponse(**{
                "tenant_id": "tenant-a", "team_id": "team-1", "team_name": "Bot test",
                "channels": [DiscoveredChannelResponse(
                    tenant_id="tenant-a", team_id="team-1", channel_id="test2",
                    channel_name="test2",
                )],
            })]

    app = create_app()
    app.state.container = Container(
        adapter=object(), bot=object(), installation_service=InstallationServiceDouble()
    )
    response = TestClient(app).get("/api/installations")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"][0]["team_name"] == "Bot test"
    assert [channel["channel_id"] for channel in body["data"][0]["channels"]] == ["test2"]
    assert body["error"] is None

    def keys(value):
        if isinstance(value, dict):
            return set(value) | {key for child in value.values() for key in keys(child)}
        if isinstance(value, list):
            return {key for child in value for key in keys(child)}
        return set()

    response_keys = keys(body)
    assert "_id" not in response_keys
    assert "microsoft_app_password" not in response_keys
    assert "access_token" not in response_keys


def test_get_installations_returns_primary_destination_metadata():
    class InstallationServiceDouble:
        async def list_installations(self):
            return [InstallationResponse(**{
                "tenant_id": "tenant-a", "team_id": "team-a", "team_name": "Bot test",
                "channels": [DiscoveredChannelResponse(
                    tenant_id="tenant-a", team_id="team-a", channel_id="team-a",
                    channel_name=None, is_primary=True, is_active=True,
                )],
            })]

    app = create_app()
    app.state.container = Container(
        adapter=object(), bot=object(), installation_service=InstallationServiceDouble()
    )
    response = TestClient(app).get("/api/installations")

    assert response.status_code == 200
    channel = response.json()["data"][0]["channels"][0]
    assert channel["channel_id"] == "team-a"
    assert channel["channel_name"] is None
    assert channel["is_primary"] is True
    assert channel["is_active"] is True


def test_get_installations_returns_empty_data_when_no_installations():
    class EmptyInstallationService:
        async def list_installations(self):
            return []

    app = create_app()
    app.state.container = Container(
        adapter=object(), bot=object(), installation_service=EmptyInstallationService()
    )
    response = TestClient(app).get("/api/installations")

    assert response.status_code == 200
    assert response.json() == {"success": True, "data": [], "error": None}
