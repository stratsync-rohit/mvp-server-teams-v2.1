"""Regression tests for Teams channel member lifecycle events."""

from types import SimpleNamespace

import pytest

from src.repositories.destination_repository import DestinationRepository
from src.repositories.discovered_channel_repository import DiscoveredChannelRepository
from src.repositories.team_installation_repository import TeamInstallationRepository
from src.schemas.teams import TeamInstallation
from src.services.destination_service import DestinationService
from src.services.installation_service import InstallationService
from src.services.teams_service import TeamsService


class MemoryCollection:
    """Minimal async collection double for repository behavior tests."""

    def __init__(self):
        self.documents = []

    async def update_one(self, query, update, upsert=False):
        document = next(
            (item for item in self.documents if all(item.get(k) == v for k, v in query.items())),
            None,
        )
        if document is None:
            if not upsert:
                return SimpleNamespace(matched_count=0)
            document = dict(query)
            self.documents.append(document)
        document.update(update.get("$set", {}))
        document.update(update.get("$setOnInsert", {}))
        return SimpleNamespace(matched_count=1)

    async def find_one(self, query):
        return next(
            (item for item in self.documents if all(item.get(k) == v for k, v in query.items())),
            None,
        )

    def find(self, query):
        return [
            item for item in self.documents
            if all(item.get(k) == v for k, v in query.items())
        ]


class ConversationClient:
    """Provider route double used by DestinationService."""

    def build_channel_reference(self, **values):
        return {
            "serviceUrl": values["service_url"],
            "conversation": {"id": values["channel_id"]},
        }

    async def resolve_conversation(self, reference):
        return reference


def _event(channel_id, channel_name, *, member_id, event_type="channelMemberAdded"):
    member_field = "membersAdded" if event_type == "channelMemberAdded" else "membersRemoved"
    return SimpleNamespace(activity={
        "type": "conversationUpdate",
        "serviceUrl": "https://teams.example",
        "conversation": {"id": f"conversation-{channel_id}", "conversationType": "channel"},
        "recipient": {"id": "bot-id"},
        member_field: [{"id": member_id}],
        "channelData": {
            "eventType": event_type,
            "tenant": {"id": "tenant-1"},
            "team": {"id": "team-1", "name": "Risk Team"},
            "channel": {"id": channel_id, "name": channel_name},
        },
    })


def _services():
    installation_collection = MemoryCollection()
    discovered_collection = MemoryCollection()
    destination_collection = MemoryCollection()
    installation_repository = TeamInstallationRepository(collection=installation_collection)
    discovered_repository = DiscoveredChannelRepository(collection=discovered_collection)
    destination_repository = DestinationRepository(collection=destination_collection)
    destination_service = DestinationService(
        destination_repository, ConversationClient(), discovered_repository
    )
    teams_service = TeamsService(
        object(),
        discovered_repository,
        installation_repository,
        destination_repository,
        destination_service,
    )
    installation_service = InstallationService(
        installation_repository,
        teams_service,
        destination_repository,
        destination_service,
        discovered_repository,
    )
    return teams_service, installation_service, discovered_repository, destination_repository


async def _seed_installation(installation_repository):
    await installation_repository.upsert(TeamInstallation(
        tenant_id="tenant-1",
        team_id="team-1",
        team_name="Risk Team",
        conversation_id="team-conversation-id",
        service_url="https://teams.example",
    ))


async def _seed_channel(discovered_repository, channel_id, channel_name):
    await discovered_repository.upsert({
        "tenant_id": "tenant-1",
        "team_id": "team-1",
        "channel_id": channel_id,
        "team_name": "Risk Team",
        "channel_name": channel_name,
        "service_url": "https://teams.example",
        "available": True,
    })


@pytest.mark.asyncio
async def test_bot_added_to_secondary_channel_creates_only_that_destination_and_get_returns_both(caplog):
    teams, installations, discovered, destinations = _services()
    await _seed_installation(teams.installation_repository)
    await _seed_channel(discovered, "team-1", "rohit")
    await teams.destination_service.resolve_discovered_channel("tenant-1", "team-1", "team-1")

    with caplog.at_level("INFO"):
        await teams.handle_conversation_update(
            _event("tet23-id", "tet23", member_id="bot-id")
        )

    connected = await destinations.list_active_for_team("tenant-1", "team-1")
    assert {item["channel_id"] for item in connected} == {"team-1", "tet23-id"}
    assert (await discovered.get("tenant-1", "team-1", "tet23-id"))["channel_name"] == "tet23"
    channels = (await installations.list_installations())[0].channels
    assert {(channel.channel_id, channel.is_primary) for channel in channels} == {
        ("team-1", True), ("tet23-id", False)
    }
    assert "teams_channel_member_added_received" in caplog.messages
    assert "teams_bot_added_to_channel" in caplog.messages
    assert "teams_channel_destination_resolved" in caplog.messages
    assert "teams_destination_connect_requested" in caplog.messages
    assert "teams_destination_connected" in caplog.messages


@pytest.mark.asyncio
async def test_human_member_added_does_not_create_destination():
    teams, _, discovered, destinations = _services()

    await teams.handle_conversation_update(
        _event("tet23-id", "tet23", member_id="human-id")
    )

    assert await discovered.get("tenant-1", "team-1", "tet23-id") is None
    assert await destinations.list_active_for_team("tenant-1", "team-1") == []


@pytest.mark.asyncio
async def test_repeated_channel_member_added_is_idempotent():
    teams, _, _, destinations = _services()
    event = _event("tet23-id", "tet23", member_id="bot-id")

    await teams.handle_conversation_update(event)
    await teams.handle_conversation_update(event)

    assert len(destinations.collection.documents) == 1
    assert (await destinations.list_active_for_team("tenant-1", "team-1"))[0]["channel_id"] == "tet23-id"


@pytest.mark.asyncio
async def test_channel_member_added_reactivates_existing_destination(caplog):
    teams, _, discovered, destinations = _services()
    await _seed_channel(discovered, "tet23-id", "tet23")
    await teams.destination_service.resolve_discovered_channel("tenant-1", "team-1", "tet23-id")
    await destinations.mark_inactive_for_channel("tenant-1", "team-1", "tet23-id")

    with caplog.at_level("INFO"):
        await teams.handle_conversation_update(
            _event("tet23-id", "tet23", member_id="bot-id")
        )

    destination = await destinations.get_by_channel("tenant-1", "team-1", "tet23-id")
    assert destination["is_active"] is True
    assert "teams_destination_reactivated" in caplog.messages


@pytest.mark.asyncio
async def test_bot_removed_deactivates_only_that_channel_and_keeps_team_active(caplog):
    teams, installations, discovered, destinations = _services()
    await _seed_installation(teams.installation_repository)
    for channel_id, channel_name in (("team-1", "rohit"), ("tet23-id", "tet23")):
        await _seed_channel(discovered, channel_id, channel_name)
        await teams.destination_service.resolve_discovered_channel(
            "tenant-1", "team-1", channel_id
        )

    with caplog.at_level("INFO"):
        await teams.handle_conversation_update(
            _event(
                "tet23-id", "tet23", member_id="bot-id",
                event_type="channelMemberRemoved",
            )
        )

    primary = await destinations.get_by_channel("tenant-1", "team-1", "team-1")
    secondary = await destinations.get_by_channel("tenant-1", "team-1", "tet23-id")
    installation = await teams.installation_repository.get("tenant-1", "team-1")
    assert primary["is_active"] is True
    assert secondary["is_active"] is False
    assert installation["is_active"] is True
    assert "teams_channel_member_removed_received" in caplog.messages
    assert "teams_bot_removed_from_channel" in caplog.messages
    assert "teams_channel_destination_deactivated" in caplog.messages


@pytest.mark.asyncio
async def test_human_member_removed_does_not_deactivate_destination():
    teams, _, discovered, destinations = _services()
    await _seed_channel(discovered, "tet23-id", "tet23")
    await teams.destination_service.resolve_discovered_channel("tenant-1", "team-1", "tet23-id")

    await teams.handle_conversation_update(
        _event(
            "tet23-id", "tet23", member_id="human-id",
            event_type="channelMemberRemoved",
        )
    )

    destination = await destinations.get_by_channel("tenant-1", "team-1", "tet23-id")
    assert destination["is_active"] is True


@pytest.mark.asyncio
async def test_team_member_added_without_channel_id_is_ignored_for_destination(caplog):
    teams, _, _, destinations = _services()
    event = SimpleNamespace(activity={
        "type": "conversationUpdate",
        "recipient": {"id": "bot-id"},
        "membersAdded": [{"id": "bot-id"}],
        "channelData": {
            "eventType": "teamMemberAdded",
            "tenant": {"id": "tenant-1"},
            "team": {"id": "team-1", "name": "Risk Team"},
        },
    })

    with caplog.at_level("INFO"):
        await teams.handle_conversation_update(event)

    assert destinations.collection.documents == []
    assert "teams_team_member_event_ignored_for_destination" in caplog.messages


@pytest.mark.asyncio
async def test_secondary_installation_activity_does_not_overwrite_team_conversation():
    teams, _, _, _ = _services()

    class DiscoveryStub:
        async def discover_channels(self, turn_context, context):
            return []

    installation_service = InstallationService(
        teams.installation_repository,
        DiscoveryStub(),
    )
    await installation_service.add(SimpleNamespace(activity={
        "type": "installationUpdate",
        "conversation": {"id": "team-conversation-id"},
        "channelData": {
            "tenant": {"id": "tenant-1"},
            "team": {"id": "team-1", "name": "Risk Team"},
        },
    }))
    await installation_service.add(SimpleNamespace(activity={
        "type": "installationUpdate",
        "serviceUrl": "https://teams.example",
        "conversation": {"id": "tet23-channel-id"},
        "channelData": {
            "tenant": {"id": "tenant-1"},
            "team": {"id": "team-1", "name": "Risk Team"},
            "channel": {"id": "tet23-id", "name": "tet23"},
        },
    }))

    installation = await teams.installation_repository.get("tenant-1", "team-1")
    assert installation["conversation_id"] == "team-conversation-id"
    assert installation["service_url"] is None


@pytest.mark.asyncio
async def test_explicit_secondary_connection_does_not_mutate_team_installation():
    teams, _, discovered, destinations = _services()
    await _seed_installation(teams.installation_repository)
    await _seed_channel(discovered, "tet23-id", "tet23")

    await teams.destination_service.resolve_discovered_channel(
        "tenant-1", "team-1", "tet23-id"
    )

    installation = await teams.installation_repository.get("tenant-1", "team-1")
    destination = await destinations.get_by_channel("tenant-1", "team-1", "tet23-id")
    assert installation["conversation_id"] == "team-conversation-id"
    assert installation["service_url"] == "https://teams.example"
    assert destination["conversation_id"] == "tet23-id"
    assert destination["service_url"] == "https://teams.example"
