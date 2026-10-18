"""Tests for separating Teams discovery from explicit connected destinations."""

from types import SimpleNamespace

import pytest

from src.services.destination_service import DestinationService
from src.services.installation_service import InstallationService
from src.services.teams_service import TeamsService


class InstallationRepository:
    def __init__(self):
        self.documents = {}

    async def upsert(self, installation):
        self.documents[(installation.tenant_id, installation.team_id)] = installation.model_dump()

    async def list_active(self):
        return [document for document in self.documents.values() if document["is_active"]]

    async def mark_inactive(self, tenant_id, team_id):
        self.documents[(tenant_id, team_id)]["is_active"] = False


class DiscoveredRepository:
    def __init__(self):
        self.documents = {}

    async def upsert(self, channel):
        data = channel.model_dump()
        self.documents[(data["tenant_id"], data["team_id"], data["channel_id"])] = data

    async def get(self, tenant_id, team_id, channel_id):
        return self.documents.get((tenant_id, team_id, channel_id))

    async def list_for_team(self, tenant_id, team_id):
        return [
            document for document in self.documents.values()
            if document["tenant_id"] == tenant_id
            and document["team_id"] == team_id
        ]


class DestinationRepository:
    def __init__(self):
        self.documents = {}

    async def upsert(self, destination):
        data = destination.model_dump()
        self.documents[data["destination_id"]] = data

    async def get(self, destination_id):
        return self.documents.get(destination_id)

    async def disable_for_channel(self, tenant_id, team_id, channel_id):
        destination = self.documents[f"{tenant_id}:{team_id}:{channel_id}"]
        destination["is_configured"] = False
        destination["is_active"] = False

    async def list_active_for_team(self, tenant_id, team_id):
        return [
            document for document in self.documents.values()
            if document["tenant_id"] == tenant_id
            and document["team_id"] == team_id
            and document["is_active"] is True
        ]

    async def mark_inactive_for_team(self, tenant_id, team_id):
        for document in self.documents.values():
            if document["tenant_id"] == tenant_id and document["team_id"] == team_id:
                document["is_active"] = False


class TeamsClient:
    def __init__(self, include_primary=False, channels=None):
        self.include_primary = include_primary
        self.channels = channels

    async def get_team_channels(self, turn_context, team_id):
        primary = [SimpleNamespace(id="team-1", name=None)] if self.include_primary else [
            SimpleNamespace(id="general", name="General")
        ]
        default_channels = [
            *primary,
            SimpleNamespace(id="test2", name="test2"),
            SimpleNamespace(id="test3", name="test3"),
        ]
        return self.channels if self.channels is not None else default_channels


class ConversationClient:
    def build_channel_reference(self, **values):
        return {
            "serviceUrl": values["service_url"],
            "conversation": {"id": values["channel_id"]},
        }

    async def resolve_conversation(self, reference):
        return reference


def _activity(selected_channel=None, *, tenant_id="tenant-1", team_id="team-1"):
    channel_data = {
        "tenant": {"id": tenant_id},
        "team": {
            "id": team_id,
            "name": "Risk Team" if team_id == "team-1" else f"Risk Team {team_id}",
        },
    }
    if selected_channel is not None:
        channel_data["settings"] = {"selectedChannel": selected_channel}
    return SimpleNamespace(activity={
        "type": "installationUpdate",
        "serviceUrl": "https://teams.example",
        "conversation": {"id": "installation-conversation"},
        "channelData": channel_data,
    })


def _services(*, include_primary=False):
    installation_repository = InstallationRepository()
    discovered_repository = DiscoveredRepository()
    destination_repository = DestinationRepository()
    teams_service = TeamsService(
        TeamsClient(include_primary=include_primary), discovered_repository, installation_repository,
        destination_repository,
    )
    destination_service = DestinationService(
        destination_repository, ConversationClient(), discovered_repository
    )
    installation_service = InstallationService(
        installation_repository,
        teams_service,
        destination_repository,
        destination_service,
        discovered_repository,
    )
    return installation_service, discovered_repository, destination_repository


@pytest.mark.asyncio
async def test_install_discovers_every_channel_without_creating_destination():
    service, discovered, destinations = _services()

    await service.add(_activity({"id": "test2", "name": "test2"}))

    assert set(channel[2] for channel in discovered.documents) == {"general", "test2", "test3"}
    assert destinations.documents == {}


@pytest.mark.asyncio
async def test_team_installation_with_four_channels_creates_one_install_and_no_destinations():
    installation_repository = InstallationRepository()
    discovered_repository = DiscoveredRepository()
    destination_repository = DestinationRepository()
    teams_service = TeamsService(
        TeamsClient(channels=[
            SimpleNamespace(id="risk", name="Risk"),
            SimpleNamespace(id="test", name="test"),
            SimpleNamespace(id="finance", name="Finance"),
            SimpleNamespace(id="general", name="General"),
        ]),
        discovered_repository,
        installation_repository,
        destination_repository,
    )
    service = InstallationService(
        installation_repository, teams_service, destination_repository,
        DestinationService(destination_repository, ConversationClient(), discovered_repository),
        discovered_repository,
    )

    await service.add(_activity())

    assert list(installation_repository.documents) == [("tenant-1", "team-1")]
    assert {key[2] for key in discovered_repository.documents} == {
        "risk", "test", "finance", "general"
    }
    assert destination_repository.documents == {}


@pytest.mark.asyncio
async def test_install_without_selected_channel_creates_no_destination():
    service, discovered, destinations = _services()

    await service.add(_activity())

    assert len(discovered.documents) == 3
    assert destinations.documents == {}


@pytest.mark.asyncio
async def test_primary_install_discovers_primary_without_creating_destination():
    service, discovered, destinations = _services(include_primary=True)

    await service.add(_activity({"id": "team-1"}))

    assert len(discovered.documents) == 3
    assert destinations.documents == {}


@pytest.mark.asyncio
async def test_installations_api_data_marks_primary_destination_without_inventing_name():
    service, _, _ = _services(include_primary=True)

    await service.add(_activity({"id": "team-1"}))
    await service.destination_service.resolve_discovered_channel("tenant-1", "team-1", "team-1")
    installation = (await service.list_installations())[0]

    assert [channel.model_dump() for channel in installation.channels] == [{
        "channel_id": "team-1",
        "channel_name": "General",
        "is_primary": True,
        "is_active": True,
        "connected": True,
        "is_configured": True,
        "status_label": "Configured",
        "destination_id": "tenant-1:team-1:team-1",
        "team_id": "team-1",
        "team_name": "Risk Team",
        "tenant_id": "tenant-1",
        "conversation_id": "team-1",
        "service_url": "https://teams.example",
        "available": True,
        "discovered_at": None,
        "updated_at": None,
    }, {
        "channel_id": "test2",
        "channel_name": "test2",
        "is_primary": False,
        "is_active": True,
        "connected": False,
        "is_configured": False,
        "status_label": "Available",
        "destination_id": None,
        "team_id": "team-1",
        "team_name": "Risk Team",
        "tenant_id": "tenant-1",
        "conversation_id": "installation-conversation",
        "service_url": "https://teams.example",
        "available": True,
        "discovered_at": None,
        "updated_at": None,
    }, {
        "channel_id": "test3",
        "channel_name": "test3",
        "is_primary": False,
        "is_active": True,
        "connected": False,
        "is_configured": False,
        "status_label": "Available",
        "destination_id": None,
        "team_id": "team-1",
        "team_name": "Risk Team",
        "tenant_id": "tenant-1",
        "conversation_id": "installation-conversation",
        "service_url": "https://teams.example",
        "available": True,
        "discovered_at": None,
        "updated_at": None,
    }]


@pytest.mark.asyncio
async def test_normal_install_does_not_create_non_primary_destination():
    service, _, destinations = _services()

    await service.add(_activity({"id": "test2", "name": "test2"}))
    installation = (await service.list_installations())[0]

    assert destinations.documents == {}
    assert [(channel.channel_id, channel.connected) for channel in installation.channels] == [
        ("general", False), ("test2", False), ("test3", False)
    ]


@pytest.mark.asyncio
async def test_explicitly_resolving_another_channel_adds_only_that_destination():
    service, _, destinations = _services()

    await service.add(_activity({"id": "test2", "name": "test2"}))
    destination_service = service.destination_service
    await destination_service.resolve_discovered_channel("tenant-1", "team-1", "test3")

    connected = await destinations.list_active_for_team("tenant-1", "team-1")
    assert {document["channel_id"] for document in connected} == {"test3"}


@pytest.mark.asyncio
async def test_selecting_two_channels_creates_exactly_two_destinations():
    service, _, destinations = _services()

    await service.add(_activity())
    await service.destination_service.resolve_discovered_channel("tenant-1", "team-1", "test2")
    await service.destination_service.resolve_discovered_channel("tenant-1", "team-1", "test3")

    configured = await destinations.list_active_for_team("tenant-1", "team-1")
    assert {destination["channel_id"] for destination in configured} == {"test2", "test3"}


@pytest.mark.asyncio
async def test_multiple_team_destinations_remain_isolated():
    service, _, destinations = _services()

    await service.add(_activity(team_id="team-1"))
    await service.add(_activity(team_id="team-2"))
    await service.destination_service.resolve_discovered_channel("tenant-1", "team-1", "test2")
    await service.destination_service.resolve_discovered_channel("tenant-1", "team-2", "test3")

    assert {item["channel_id"] for item in await destinations.list_active_for_team("tenant-1", "team-1")} == {"test2"}
    assert {item["channel_id"] for item in await destinations.list_active_for_team("tenant-1", "team-2")} == {"test3"}


@pytest.mark.asyncio
async def test_installation_list_returns_discovered_and_connection_status():
    service, _, destinations = _services()

    await service.add(_activity({"id": "test2", "name": "test2"}))
    installations = await service.list_installations()

    assert [(channel.channel_id, channel.connected) for channel in installations[0].channels] == [
        ("general", False), ("test2", False), ("test3", False)
    ]


@pytest.mark.asyncio
async def test_configure_then_disconnect_refreshes_channel_state_without_uninstalling_team():
    service, _, _ = _services()
    await service.add(_activity())

    before = (await service.list_installations())[0]
    assert before.installation_status_label == "Active"
    assert [(channel.channel_name, channel.status_label) for channel in before.channels] == [
        ("General", "Available"), ("test2", "Available"), ("test3", "Available")
    ]

    configured = await service.destination_service.resolve_discovered_channel(
        "tenant-1", "team-1", "test2"
    )
    after_configure = (await service.list_installations())[0]
    test2 = next(channel for channel in after_configure.channels if channel.channel_id == "test2")
    test3 = next(channel for channel in after_configure.channels if channel.channel_id == "test3")
    assert (test2.status_label, test2.is_configured, test2.destination_id) == (
        "Configured", True, configured.destination_id
    )
    assert (test3.status_label, test3.is_configured) == ("Available", False)

    await service.destination_service.deactivate(configured.destination_id)
    after_disconnect = (await service.list_installations())[0]
    test2 = next(channel for channel in after_disconnect.channels if channel.channel_id == "test2")
    assert after_disconnect.installation_status_label == "Active"
    assert (test2.status_label, test2.is_configured, test2.destination_id) == (
        "Available", False, None
    )


@pytest.mark.asyncio
async def test_uninstall_deactivates_destinations_without_deleting_them():
    service, _, destinations = _services()

    await service.add(_activity({"id": "test2", "name": "test2"}))
    await service.add(_activity({"id": "test3", "name": "test3"}, team_id="team-2"))
    await service.destination_service.resolve_discovered_channel("tenant-1", "team-1", "test2")
    await service.destination_service.resolve_discovered_channel("tenant-1", "team-2", "test3")
    destination_id = "tenant-1:team-1:test2"
    await service.remove(_activity())

    assert destinations.documents[destination_id]["is_active"] is False
    assert await destinations.list_active_for_team("tenant-1", "team-1") == []
    assert {item["channel_id"] for item in await destinations.list_active_for_team("tenant-1", "team-2")} == {"test3"}


@pytest.mark.asyncio
async def test_reconnecting_channel_reactivates_its_deterministic_destination():
    service, _, destinations = _services()

    await service.add(_activity({"id": "test2", "name": "test2"}))
    await service.destination_service.resolve_discovered_channel("tenant-1", "team-1", "test2")
    await service.remove(_activity())
    await service.destination_service.resolve_discovered_channel("tenant-1", "team-1", "test2")

    assert len(destinations.documents) == 1
    assert next(iter(destinations.documents.values()))["is_active"] is True
