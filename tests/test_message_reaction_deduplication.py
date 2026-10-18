"""Regression coverage for Teams messageReaction processed-activity keys."""

from types import SimpleNamespace

import pytest

from botbuilder.core.teams import TeamsActivityHandler

from src.bot.handlers.risk_bot import RiskBot
from src.repositories.reaction_repository import ReactionRepository
from src.services.reaction_service import ReactionService


class _ReactionCollection:
    def __init__(self):
        self.documents = {}

    async def update_one(self, query, update, upsert=False):
        key = tuple(sorted(query.items()))
        document = self.documents.setdefault(key, {})
        document.update(update.get("$setOnInsert", {}))
        document.update(update.get("$set", {}))


class _NotificationLookup:
    async def get_by_message_id(self, message_id, tenant_id=None):
        if message_id == "teams-message-X" and tenant_id == "tenant-1":
            return {
                "notification_id": "notification-1",
                "destination_id": "destination-1",
                "tenant_id": tenant_id,
            }
        return None


class _ProcessedActivities:
    def __init__(self):
        self.claimed = set()
        self.claim_calls = []
        self.completed = []

    async def claim(self, activity_id, activity_type):
        self.claim_calls.append((activity_id, activity_type))
        if activity_id in self.claimed:
            return False
        self.claimed.add(activity_id)
        return True

    async def mark_completed(self, activity_id):
        self.completed.append(activity_id)

    async def mark_failed(self, activity_id, error):
        raise AssertionError(error)


def _reaction_activity(*, user="user-1", added=None, removed=None):
    payload = {
        "type": "messageReaction",
        # Teams reuses this ID for all reaction events on a message.
        "id": "teams-message-X",
        "replyToId": "teams-message-X",
        "from": {"aadObjectId": user},
        "channelData": {"tenant": {"id": "tenant-1"}},
        "reactionsAdded": added or [],
        "reactionsRemoved": removed or [],
    }
    return SimpleNamespace(
        id=payload["id"], type=payload["type"], serialize=lambda: payload,
    )


@pytest.mark.asyncio
async def test_message_reactions_have_event_specific_dedupe_and_keep_normal_dedupe(monkeypatch):
    sdk_normal_turns = []

    async def sdk_on_turn(self, turn_context):
        if turn_context.activity.type == "messageReaction":
            await self.on_message_reaction_activity(turn_context)
        else:
            sdk_normal_turns.append(turn_context.activity.id)

    monkeypatch.setattr(TeamsActivityHandler, "on_turn", sdk_on_turn)
    collection = _ReactionCollection()
    processed = _ProcessedActivities()
    bot = RiskBot(
        reaction_service=ReactionService(
            ReactionRepository(collection=collection), _NotificationLookup()
        ),
        processed_activity_repository=processed,
    )

    async def deliver(activity):
        await bot.on_turn(SimpleNamespace(activity=activity))

    await deliver(_reaction_activity(added=[{"type": "heart"}]))
    await deliver(_reaction_activity(added=[{"type": "laugh"}]))
    await deliver(_reaction_activity(added=[{"type": "like"}]))
    await deliver(_reaction_activity(added=[{"type": "2714_heavycheckmark"}]))
    await deliver(_reaction_activity(added=[{"type": "heart"}]))  # retry: skipped
    await deliver(_reaction_activity(removed=[{"type": "laugh"}]))
    await deliver(_reaction_activity(user="user-2", added=[{"type": "heart"}]))
    await deliver(_reaction_activity(added=[
        {"type": "surprised"}, {"type": "sad"},
    ]))

    normal = SimpleNamespace(id="normal-activity-1", type="message", serialize=lambda: {
        "id": "normal-activity-1", "type": "message",
    })
    await deliver(normal)
    await deliver(normal)

    state = {
        (document["user_aad_id"], document["reaction"]): document["is_active"]
        for document in collection.documents.values()
    }
    assert state == {
        ("user-1", "heart"): True,
        ("user-1", "laugh"): False,
        ("user-1", "like"): True,
        ("user-1", "2714_heavycheckmark"): True,
        ("user-1", "surprised"): True,
        ("user-1", "sad"): True,
        ("user-2", "heart"): True,
    }
    assert sdk_normal_turns == ["normal-activity-1"]
    reaction_keys = [key for key, kind in processed.claim_calls if kind == "messageReaction"]
    assert len(reaction_keys) == 8
    assert len(set(reaction_keys)) == 7  # only the exact heart-add retry is duplicate
    assert "messageReaction:teams-message-X:user-1:add=laugh:remove=" in reaction_keys
    assert "messageReaction:teams-message-X:user-1:add=:remove=laugh" in reaction_keys
    assert "messageReaction:teams-message-X:user-1:add=sad,surprised:remove=" in reaction_keys
    assert [key for key, kind in processed.claim_calls if kind == "message"] == [
        "normal-activity-1", "normal-activity-1"
    ]
