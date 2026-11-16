"""CloudAdapter boundary for outbound Teams conversation mechanics."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Awaitable, Callable

try:
    from botbuilder.schema import (
        Activity,
        Attachment,
        ChannelAccount,
        ConversationAccount,
        ConversationReference,
    )
except ImportError:  # pragma: no cover
    # Keep the repository's SDK-free unit tests useful. Production images
    # install botbuilder and therefore use the real schema classes above.
    class _SchemaObject:
        def __init__(self, **values: Any) -> None:
            self.__dict__.update(values)

    class Activity(_SchemaObject):
        pass

    class Attachment(_SchemaObject):
        pass

    class ChannelAccount(_SchemaObject):
        pass

    class ConversationAccount(_SchemaObject):
        pass

    class ConversationReference(_SchemaObject):
        def deserialize(self, value: Mapping[str, Any]) -> "ConversationReference":
            self.channel_id = value.get("channelId")
            self.service_url = value.get("serviceUrl")
            conversation = value.get("conversation")
            bot = value.get("bot")
            self.conversation = ConversationAccount(**dict(conversation)) if isinstance(conversation, Mapping) else None
            self.bot = ChannelAccount(**dict(bot)) if isinstance(bot, Mapping) else None
            return self

        def serialize(self) -> dict[str, Any]:
            result: dict[str, Any] = {"channelId": getattr(self, "channel_id", None),
                                      "serviceUrl": getattr(self, "service_url", None)}
            if getattr(self, "conversation", None) is not None:
                result["conversation"] = vars(self.conversation)
            if getattr(self, "bot", None) is not None:
                result["bot"] = vars(self.bot)
            return result

from src.exceptions import ProviderError


class TeamsConversationClient:
    """Keep provider-specific conversation continuation out of repositories."""

    def __init__(self, adapter: Any, bot_id: str | None = None) -> None:
        self.adapter = adapter
        self.bot_id = bot_id

    def build_channel_reference(self, *, tenant_id: str, channel_id: str,
                                service_url: str) -> dict[str, Any] | None:
        """Build a serialized SDK reference targeting one Teams channel."""
        if not tenant_id or not channel_id or not service_url or ConversationReference is None:
            return None
        reference = ConversationReference(
            channel_id="msteams",
            service_url=service_url,
            conversation=ConversationAccount(
                id=channel_id,
                tenant_id=tenant_id,
                conversation_type="channel",
                is_group=True,
            ),
            bot=ChannelAccount(id=self.bot_id) if self.bot_id else None,
        )
        return reference.serialize()

    async def resolve_conversation(self, reference: dict[str, Any]) -> dict[str, Any] | None:
        """Optionally pass route metadata through an adapter-specific resolver.

        The fallback is structural preparation only; it does not claim that
        Microsoft Teams has validated or created the conversation.
        """
        if not reference:
            return None
        resolver = getattr(self.adapter, "resolve_conversation", None)
        if resolver is None:
            return reference
        result = resolver(reference)
        return await result if hasattr(result, "__await__") else result

    async def send_activity(self, reference: dict[str, Any], activity: Any) -> Any:
        """Send an activity using the adapter's conversation continuation hook."""
        if isinstance(reference, dict) and ConversationReference is not None:
            reference = ConversationReference().deserialize(reference)
        activity = self._as_message_activity(activity)
        sender = getattr(self.adapter, "send_to_conversation", None)
        if sender is not None:
            result = sender(reference, activity)
            return await result if hasattr(result, "__await__") else result
        callback: Callable[[Any], Awaitable[Any]] | None = None
        continuer = getattr(self.adapter, "continue_conversation", None)
        if continuer is not None:
            response_holder: dict[str, Any] = {}

            async def callback(turn_context: Any) -> Any:
                response_holder["response"] = await turn_context.send_activity(activity)

            result = continuer(reference, callback, self.bot_id)
            if hasattr(result, "__await__"):
                await result
            return response_holder.get("response")
        raise ProviderError("CloudAdapter has no outbound conversation method")

    async def send_card_in_context(self, turn_context: Any, card: dict[str, Any]) -> Any:
        """Send one Adaptive Card through an already authenticated turn context."""
        activity = self._as_message_activity(card)
        result = turn_context.send_activity(activity)
        return await result if hasattr(result, "__await__") else result

    def build_message_reference(
        self,
        conversation_reference: Mapping[str, Any] | None,
        *,
        message_id: str | None,
        tenant_id: str | None,
        channel_id: str | None,
    ) -> dict[str, Any] | None:
        """Return only the persisted routing fields needed for a future edit."""
        if not isinstance(message_id, str) or not message_id or not isinstance(conversation_reference, Mapping):
            return None
        conversation = conversation_reference.get("conversation")
        if not isinstance(conversation, Mapping):
            return None
        conversation_id = conversation.get("id")
        service_url = conversation_reference.get("serviceUrl") or conversation_reference.get("service_url")
        if not isinstance(conversation_id, str) or not conversation_id or not isinstance(service_url, str) or not service_url:
            return None
        reference = {
            "channelId": conversation_reference.get("channelId") or "msteams",
            "serviceUrl": service_url,
            "conversation": {
                "id": conversation_id,
                **({"tenantId": conversation.get("tenantId")} if conversation.get("tenantId") else {}),
            },
            "bot": {"id": self.bot_id} if self.bot_id else {},
        }
        return {
            "conversation_reference": reference,
            "conversation_id": conversation_id,
            "activity_id": message_id,
            "service_url": service_url,
            "tenant_id": tenant_id,
            "channel_id": channel_id,
            "bot_id": self.bot_id,
        }

    async def update_card(
        self, message_reference: Mapping[str, Any], card: dict[str, Any]
    ) -> Any:
        """Update exactly one bot-authored Teams activity in a proactive turn."""
        activity_id = message_reference.get("activity_id")
        serialized_reference = message_reference.get("conversation_reference")
        conversation_id = message_reference.get("conversation_id")
        if not isinstance(activity_id, str) or not activity_id or not isinstance(serialized_reference, Mapping):
            raise ProviderError("Teams message reference is incomplete")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise ProviderError("Teams message reference has no conversation ID")
        reference = ConversationReference().deserialize(dict(serialized_reference))
        response_holder: dict[str, Any] = {}

        async def callback(turn_context: Any) -> None:
            activity = self._as_message_activity(card)
            activity.id = activity_id
            activity.conversation = ConversationAccount(id=conversation_id)
            response_holder["response"] = await turn_context.update_activity(activity)

        continuer = getattr(self.adapter, "continue_conversation", None)
        if continuer is None:
            raise ProviderError("CloudAdapter has no proactive conversation method")
        result = continuer(reference, callback, self.bot_id)
        if hasattr(result, "__await__"):
            await result
        return response_holder.get("response")

    @staticmethod
    def _as_message_activity(activity: Any) -> Any:
        """Convert the internal card envelope to one Bot Framework message."""
        if isinstance(activity, dict) and activity.get("contentType") and Activity is not None:
            return Activity(type="message", attachments=[Attachment(
                content_type=activity["contentType"], content=activity.get("content", {})
            )])
        return activity
