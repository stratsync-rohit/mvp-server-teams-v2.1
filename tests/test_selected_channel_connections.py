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
    def __init__(self, include_primary=False):
        self.include_primary = include_primary

    async def get_team_channels(self, turn_context, team_id):
        primary = [SimpleNamespace(id="team-1", name=None)] if self.include_primary else [
            SimpleNamespace(id="general", name="General")
        ]
        return [
            *primary,
            SimpleNamespace(id="test2", name="test2"),
            SimpleNamespace(id="test3", name="test3"),
        ]


class ConversationClient:
    def build_channel_reference(self, **values):
        return {
            "serviceUrl": values["service_url"],
            "conversation": {"id": values["channel_id"]},
        }

    async def resolve_conversation(self, reference):
        return reference


def _activity(selected_channel=None):
    channel_data = {
        "tenant": {"id": "tenant-1"},
        "team": {"id": "team-1", "name": "Risk Team"},
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
        "channel_name": None,
        "is_primary": True,
        "is_active": True,
        "connected": True,
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
async def test_installation_list_returns_discovered_and_connection_status():
    service, _, destinations = _services()

    await service.add(_activity({"id": "test2", "name": "test2"}))
    installations = await service.list_installations()

    assert [(channel.channel_id, channel.connected) for channel in installations[0].channels] == [
        ("general", False), ("test2", False), ("test3", False)
    ]


@pytest.mark.asyncio
async def test_uninstall_deactivates_destinations_without_deleting_them():
    service, _, destinations = _services()

    await service.add(_activity({"id": "test2", "name": "test2"}))
    await service.destination_service.resolve_discovered_channel("tenant-1", "team-1", "test2")
    destination_id = next(iter(destinations.documents))
    await service.remove(_activity())

    assert destinations.documents[destination_id]["is_active"] is False
    assert await destinations.list_active_for_team("tenant-1", "team-1") == []


@pytest.mark.asyncio
async def test_reconnecting_channel_reactivates_its_deterministic_destination():
    service, _, destinations = _services()

    await service.add(_activity({"id": "test2", "name": "test2"}))
    await service.destination_service.resolve_discovered_channel("tenant-1", "team-1", "test2")
    await service.remove(_activity())
    await service.destination_service.resolve_discovered_channel("tenant-1", "team-1", "test2")

    assert len(destinations.documents) == 1
    assert next(iter(destinations.documents.values()))["is_active"] is True
