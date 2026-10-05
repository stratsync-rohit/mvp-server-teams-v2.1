"""Regression tests for Teams channelRenamed name enrichment."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from src.repositories.destination_repository import DestinationRepository
from src.repositories.discovered_channel_repository import DiscoveredChannelRepository
from src.services.teams_service import TeamsService


_MISSING = object()


class MemoryCollection:
    """Small collection double that distinguishes update from upsert."""

    def __init__(self):
        self.documents = []
        self.updates = []

    async def update_one(self, query, update, upsert=False):
        self.updates.append((deepcopy(query), deepcopy(update), upsert))
        document = next(
            (item for item in self.documents if all(item.get(k) == v for k, v in query.items())),
            None,
        )
        if document is None:
            if not upsert:
                return SimpleNamespace(matched_count=0, modified_count=0)
            document = {key: value for key, value in query.items() if not key.startswith("$")}
            self.documents.append(document)
        document.update(update.get("$set", {}))
        document.update(update.get("$setOnInsert", {}))
        return SimpleNamespace(matched_count=1, modified_count=1)

    async def find_one(self, query):
        return next(
            (item for item in self.documents if all(item.get(k) == v for k, v in query.items())),
            None,
        )


def _activity(channel_id, channel_name_marker=_MISSING):
    channel = {"id": channel_id}
    if channel_name_marker is not _MISSING:
        channel["name"] = channel_name_marker
    return SimpleNamespace(activity={
        "type": "conversationUpdate",
        "channelData": {
            "eventType": "channelRenamed",
            "tenant": {"id": "tenant-1"},
            "team": {"id": "team-1"},
            "channel": channel,
        },
    })

def _service():
    discovered_collection = MemoryCollection()
    destination_collection = MemoryCollection()
    discovered = DiscoveredChannelRepository(collection=discovered_collection)
    destinations = DestinationRepository(collection=destination_collection)
    return (
        TeamsService(object(), discovered, destination_repository=destinations),
        discovered,
        destinations,
        discovered_collection,
        destination_collection,
    )


async def _seed_channel(repository, *, channel_id, channel_name):
    await repository.upsert({
        "tenant_id": "tenant-1",
        "team_id": "team-1",
        "channel_id": channel_id,
        "team_name": "Risk Team",
        "channel_name": channel_name,
        "conversation_id": "conversation-1",
        "service_url": "https://teams.example",
        "available": True,
    })


async def _seed_destination(repository, *, channel_id, channel_name):
    await repository.upsert({
        "destination_id": f"tenant-1:team-1:{channel_id}",
        "tenant_id": "tenant-1",
        "team_id": "team-1",
        "channel_id": channel_id,
        "channel_name": channel_name,
        "conversation_id": "conversation-1",
        "conversation_reference": {"conversation": {"id": "conversation-1"}},
        "service_url": "https://teams.example",
        "is_active": True,
    })


@pytest.mark.asyncio
async def test_primary_rename_updates_discovered_channel_and_destination(caplog):
    service, discovered, destinations, discovered_collection, destination_collection = _service()
    await _seed_channel(discovered, channel_id="team-1", channel_name=None)
    await _seed_destination(destinations, channel_id="team-1", channel_name=None)
    before_discovered = await discovered.get("tenant-1", "team-1", "team-1")
    before_destination = await destinations.get_by_channel("tenant-1", "team-1", "team-1")

    with caplog.at_level("INFO"):
        await service.handle_conversation_update(_activity("team-1", "Company Updates"))

    updated_discovered = await discovered.get("tenant-1", "team-1", "team-1")
    updated_destination = await destinations.get_by_channel("tenant-1", "team-1", "team-1")
    assert updated_discovered["channel_name"] == "Company Updates"
    assert updated_destination["channel_name"] == "Company Updates"
    assert updated_discovered["team_name"] == before_discovered["team_name"]
    assert updated_destination["conversation_reference"] == before_destination["conversation_reference"]
    for collection in (discovered_collection, destination_collection):
        rename_updates = [
            update for _, update, upsert in collection.updates
            if not upsert and "channel_name" in update.get("$set", {})
        ]
        assert rename_updates
        assert all(set(update["$set"]) == {"channel_name", "updated_at"} for update in rename_updates)

    received = next(record for record in caplog.records if record.message == "teams_channel_renamed_received")
    updated = next(record for record in caplog.records if record.message == "teams_channel_name_updated")
    for record in (received, updated):
        assert (record.tenant_id, record.team_id, record.channel_id) == (
            "tenant-1", "team-1", "team-1"
        )
        assert record.old_name is None
        assert record.new_name == "Company Updates"
        assert record.is_primary is True


@pytest.mark.asyncio
async def test_normal_channel_rename_updates_name():
    service, discovered, destinations, _, _ = _service()
    await _seed_channel(discovered, channel_id="test2", channel_name="test2")
    await _seed_destination(destinations, channel_id="test2", channel_name="test2")

    await service.handle_conversation_update(_activity("test2", "alerts"))

    assert (await discovered.get("tenant-1", "team-1", "test2"))["channel_name"] == "alerts"
    assert (await destinations.get_by_channel("tenant-1", "team-1", "test2"))["channel_name"] == "alerts"


@pytest.mark.asyncio
async def test_unconnected_discovered_channel_is_enriched_without_destination_creation():
    service, discovered, destinations, _, destination_collection = _service()
    await _seed_channel(discovered, channel_id="test2", channel_name="test2")

    await service.handle_conversation_update(_activity("test2", "alerts"))

    assert (await discovered.get("tenant-1", "team-1", "test2"))["channel_name"] == "alerts"
    assert await destinations.get_by_channel("tenant-1", "team-1", "test2") is None
    assert destination_collection.documents == []


@pytest.mark.asyncio
async def test_missing_channel_name_does_not_overwrite_existing_name():
    service, discovered, destinations, _, _ = _service()
    await _seed_channel(discovered, channel_id="team-1", channel_name="Company Updates")
    await _seed_destination(destinations, channel_id="team-1", channel_name="Company Updates")

    await service.handle_conversation_update(_activity("team-1", _MISSING))

    assert (await discovered.get("tenant-1", "team-1", "team-1"))["channel_name"] == "Company Updates"
    assert (await destinations.get_by_channel("tenant-1", "team-1", "team-1"))["channel_name"] == "Company Updates"
