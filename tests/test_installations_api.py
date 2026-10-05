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


def _discovered_channel(
    tenant_id: str, team_id: str, channel_id: str,
    channel_name: str | None, *, available: bool = True,
) -> dict:
    """Build a persistence-shaped discovered-channel fixture."""
    return {
        "tenant_id": tenant_id,
        "team_id": team_id,
        "channel_id": channel_id,
        "team_name": f"Team {team_id}",
        "channel_name": channel_name,
        "conversation_id": f"discovered-{channel_id}",
        "service_url": "https://smba.example",
        "available": available,
    }


def _discovered_installation_service(discovered_documents, destination_documents):
    class InstallationRepository:
        async def list_active(self):
            return [_installation("tenant-a", "team-1")]

    class DiscoveredRepository:
        async def list_for_team(self, tenant_id, team_id):
            return [
                document for document in discovered_documents
                if document["tenant_id"] == tenant_id and document["team_id"] == team_id
            ]

    class DestinationRepository:
        async def list_active_for_team(self, tenant_id, team_id):
            return [
                document for document in destination_documents
                if document["tenant_id"] == tenant_id
                and document["team_id"] == team_id
                and document.get("is_active", True) is True
            ]

    return InstallationService(
        InstallationRepository(),
        object(),
        DestinationRepository(),
        discovered_channel_repository=DiscoveredRepository(),
    )


def _aggregated_service() -> InstallationService:
    class InstallationRepository:
        async def list_active(self):
            return [_installation("tenant-a", "team-1"), _installation("tenant-b", "team-1")]

    class DestinationRepository:
        async def list_active_for_team(self, tenant_id, team_id):
            return [_destination(tenant_id, team_id, f"channel-{tenant_id}")]

    class DiscoveredRepository:
        async def list_for_team(self, tenant_id, team_id):
            return [{
                "tenant_id": tenant_id,
                "team_id": team_id,
                "channel_id": f"channel-{tenant_id}",
                "channel_name": f"Channel {tenant_id}",
            }]

    return InstallationService(
        InstallationRepository(), object(), DestinationRepository(),
        discovered_channel_repository=DiscoveredRepository(),
    )


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


def test_installations_service_marks_discovered_channels_connected():
    import asyncio

    class InstallationRepository:
        async def list_active(self):
            return [_installation("tenant-a", "team-1")]

    class DestinationRepository:
        async def list_active_for_team(self, tenant_id, team_id):
            assert (tenant_id, team_id) == ("tenant-a", "team-1")
            return [_destination("tenant-a", "team-1", "test2")]

    class DiscoveredRepository:
        async def list_for_team(self, tenant_id, team_id):
            return [
                _discovered_channel(tenant_id, team_id, "test2", "test2"),
                _discovered_channel(tenant_id, team_id, "test3", "test3"),
            ]

    data = asyncio.run(
        InstallationService(
            InstallationRepository(), object(), DestinationRepository(),
            discovered_channel_repository=DiscoveredRepository(),
        ).list_installations()
    )

    assert [(channel.channel_id, channel.connected) for channel in data[0].channels] == [
        ("test2", True), ("test3", False)
    ]


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

    class DiscoveredRepository:
        async def list_for_team(self, tenant_id, team_id):
            return [
                _discovered_channel(tenant_id, team_id, "test2", "test2"),
                _discovered_channel(tenant_id, team_id, "test3", "test3"),
            ]

    data = asyncio.run(
        InstallationService(
            InstallationRepository(), object(), DestinationRepository(),
            discovered_channel_repository=DiscoveredRepository(),
        ).list_installations()
    )

    assert [(channel.channel_id, channel.connected) for channel in data[0].channels] == [
        ("test2", True), ("test3", False)
    ]


def test_installations_returns_unconnected_discovered_channels():
    import asyncio

    discovered = [
        _discovered_channel("tenant-a", "team-1", "team-1", None),
        _discovered_channel("tenant-a", "team-1", "test2", "test2"),
    ]
    data = asyncio.run(_discovered_installation_service(discovered, []).list_installations())

    assert [(channel.channel_id, channel.connected) for channel in data[0].channels] == [
        ("team-1", False), ("test2", False)
    ]
    assert data[0].channels[0].is_primary is True
    assert data[0].channels[1].channel_name == "test2"


def test_installations_marks_only_active_destination_as_connected():
    import asyncio

    discovered = [
        _discovered_channel("tenant-a", "team-1", "team-1", None),
        _discovered_channel("tenant-a", "team-1", "test2", "test2"),
    ]
    data = asyncio.run(_discovered_installation_service(
        discovered, [_destination("tenant-a", "team-1", "test2")]
    ).list_installations())

    assert [(channel.channel_id, channel.connected) for channel in data[0].channels] == [
        ("team-1", False), ("test2", True)
    ]


def test_installations_can_mark_primary_destination_connected():
    import asyncio

    discovered = [
        _discovered_channel("tenant-a", "team-1", "team-1", None),
        _discovered_channel("tenant-a", "team-1", "test2", "test2"),
    ]
    data = asyncio.run(_discovered_installation_service(
        discovered, [_destination("tenant-a", "team-1", "team-1")]
    ).list_installations())

    assert data[0].channels[0].is_primary is True
    assert data[0].channels[0].connected is True
    assert data[0].channels[1].connected is False


def test_installations_keeps_channel_visible_when_destination_is_inactive():
    import asyncio

    discovered = [_discovered_channel("tenant-a", "team-1", "test2", "test2")]
    data = asyncio.run(_discovered_installation_service(
        discovered, [_destination("tenant-a", "team-1", "test2", active=False)]
    ).list_installations())

    assert len(data[0].channels) == 1
    assert data[0].channels[0].channel_id == "test2"
    assert data[0].channels[0].connected is False


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
    assert body["data"][0]["channels"][0]["connected"] is False
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
    assert channel["connected"] is False


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
