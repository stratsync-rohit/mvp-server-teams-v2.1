"""Unit tests for safe hourly in-place Teams card refreshes."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.clients.teams_conversation_client import TeamsConversationClient
from src.schemas.destination import Destination
from src.services.notification_service import NotificationService
from src.services.teams_message_update_service import (
    PermanentTeamsUpdateError,
    TeamsMessageUpdateService,
)
from src.workers.hourly_card_update_worker import HourlyCardUpdateWorker


def _reference():
    return {
        "conversation_reference": {
            "channelId": "msteams", "serviceUrl": "https://service.example",
            "conversation": {"id": "conversation-1", "tenantId": "tenant-1"},
            "bot": {"id": "bot-1"},
        },
        "conversation_id": "conversation-1",
        "activity_id": "message-1",
        "service_url": "https://service.example",
        "tenant_id": "tenant-1",
        "channel_id": "channel-1",
        "bot_id": "bot-1",
    }


@pytest.mark.asyncio
async def test_successful_initial_send_persists_sanitized_exact_message_reference():
    class Repository:
        def __init__(self): self.created = []; self.sent = []
        async def create(self, record): self.created.append(record)
        async def mark_sent_with_reference(self, notification_id, message_id, reference):
            self.sent.append((notification_id, message_id, reference))
        async def mark_failed(self, *args): raise AssertionError("send succeeds")

    class Destinations:
        async def get(self, destination_id):
            return Destination(destination_id=destination_id, tenant_id="tenant-1", team_id="team-1",
                channel_id="channel-1", conversation_reference={
                    "channelId": "msteams", "serviceUrl": "https://service.example",
                    "conversation": {"id": "conversation-1"}, "secret": "must-not-persist",
                })

    class Client:
        async def send_activity(self, reference, card): return {"id": "message-1"}
        def build_message_reference(self, reference, **kwargs):
            return TeamsConversationClient(object(), "bot-1").build_message_reference(reference, **kwargs)

    repository = Repository()
    await NotificationService(repository, Destinations(), Client()).send(
        "tenant-1:team-1:channel-1", {"content": {}}, risk_id="RSK-1"
    )

    assert repository.created[0]["status"] == "pending"
    persisted = repository.sent[0][2]
    assert persisted["activity_id"] == "message-1"
    assert persisted["conversation_id"] == "conversation-1"
    assert persisted["tenant_id"] == "tenant-1"
    assert "secret" not in str(persisted)


@pytest.mark.asyncio
async def test_authenticated_update_reuses_original_activity_and_conversation_without_sending():
    updates = []

    class TurnContext:
        async def update_activity(self, activity):
            updates.append(activity)
            return SimpleNamespace(id=activity.id)

    class Adapter:
        async def continue_conversation(self, reference, callback, bot_id):
            assert reference.conversation.id == "conversation-1"
            assert bot_id == "bot-1"
            await callback(TurnContext())

    client = TeamsConversationClient(Adapter(), "bot-1")
    await client.update_card(_reference(), {
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {"type": "AdaptiveCard", "version": "1.5", "msteams": {"width": "Full"}, "body": []},
    })

    assert len(updates) == 1
    assert updates[0].id == "message-1"
    assert updates[0].conversation.id == "conversation-1"
    assert updates[0].attachments[0].content["msteams"] == {"width": "Full"}


@pytest.mark.asyncio
async def test_update_service_regenerates_full_notification_card_and_preserves_actions():
    class RiskService:
        async def get(self, risk_id):
            assert risk_id == "RSK-1"
            return {"risk_id": risk_id, "title": "Current", "reported_date": "2024-01-01T00:00:00+00:00",
                    "reported_timezone": "UTC", "views": {"notification": {"blocks": []},
                    "details": {"blocks": [{"type": "text", "text": "Details"}]}}}

    class Renderer:
        def render_notification(self, risk):
            return {"content": {"type": "AdaptiveCard", "version": "1.5", "msteams": {"width": "Full"},
                                "actions": [{"type": "Action.Execute", "verb": "show_risk_view"}], "body": []}}

    class Client:
        def __init__(self): self.calls = []
        async def update_card(self, reference, card): self.calls.append((reference, card))

    client = Client()
    card = await TeamsMessageUpdateService(RiskService(), Renderer(), client).update_notification({
        "notification_id": "n1", "risk_id": "RSK-1", "tenant_id": "tenant-1",
        "teams_message_id": "message-1", "message_reference": _reference(),
    })

    assert client.calls == [(_reference(), card)]
    assert card["content"]["actions"][0]["verb"] == "show_risk_view"


@pytest.mark.asyncio
async def test_update_service_retires_missing_or_cross_tenant_references_without_posting():
    service = TeamsMessageUpdateService(object(), object(), object())
    with pytest.raises(PermanentTeamsUpdateError):
        await service.update_notification({"risk_id": "RSK-1", "tenant_id": "tenant-1"})
    reference = _reference()
    reference["tenant_id"] = "another-tenant"
    with pytest.raises(PermanentTeamsUpdateError):
        await service.update_notification({
            "risk_id": "RSK-1", "tenant_id": "tenant-1", "teams_message_id": "message-1",
            "message_reference": reference,
        })


@pytest.mark.asyncio
async def test_worker_claims_once_per_checkpoint_and_does_not_create_new_messages():
    checkpoint = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
    notification = {"notification_id": "n1", "failure_count": 0}

    class Repository:
        def __init__(self): self.claimed = False; self.successes = []
        async def list_eligible_updates(self, received, *, limit):
            assert received == checkpoint
            return [notification]
        async def claim_update(self, notification_id, received, owner):
            if self.claimed: return None
            self.claimed = True
            return notification
        async def mark_update_success(self, notification_id, received, card):
            self.successes.append((notification_id, received, card))
        async def mark_update_failure(self, *args, **kwargs):
            raise AssertionError("successful update must not fail")

    class Leases:
        async def acquire(self, *args): return True
        async def release(self, *args): pass
        async def heartbeat(self, *args): pass

    class Updater:
        async def update_notification(self, claimed):
            return {"content": {"type": "AdaptiveCard", "version": "1.5", "body": []}}

    repository = Repository()
    worker = HourlyCardUpdateWorker(repository, Updater(), Leases(), owner="worker-1")
    assert await worker.run_once(checkpoint) == 1
    assert await worker.run_once(checkpoint) == 0
    assert [entry[:2] for entry in repository.successes] == [("n1", checkpoint)]
