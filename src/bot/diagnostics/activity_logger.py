"""Structured, secret-safe diagnostics for Bot Framework activities."""

from __future__ import annotations

import logging
from datetime import date, datetime
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

_SENSITIVE_KEY_PARTS = (
    "authorization", "token", "jwt", "secret", "password", "credential",
    "apikey", "cookie",
)
_MAX_STRING_LENGTH = 4_000
_MAX_COLLECTION_ITEMS = 100


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """Convert an SDK model or mapping into a shallow mapping safely."""
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "serialize"):
        serialized = value.serialize()
        if isinstance(serialized, Mapping):
            return serialized
    if hasattr(value, "__dict__"):
        return {
            key: item
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return {}


def _is_sensitive_key(key: str) -> bool:
    """Return whether a field name may contain credentials or session data."""
    normalized = "".join(character for character in key.lower() if character.isalnum())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def sanitize_activity(value: Any) -> dict[str, Any]:
    """Return a JSON-safe, bounded and credential-redacted debug snapshot."""
    seen: set[int] = set()

    def sanitize(item: Any, key: str = "") -> Any:
        if _is_sensitive_key(key):
            return "<redacted>"
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, str):
            if len(item) <= _MAX_STRING_LENGTH:
                return item
            return f"{item[:_MAX_STRING_LENGTH]}...<truncated>"
        if isinstance(item, (datetime, date)):
            return item.isoformat()
        item_id = id(item)
        if item_id in seen:
            return "<circular>"
        if isinstance(item, Mapping):
            seen.add(item_id)
            try:
                entries = list(item.items())
                result = {
                    str(k): sanitize(v, str(k))
                    for k, v in entries[:_MAX_COLLECTION_ITEMS]
                }
                if len(entries) > _MAX_COLLECTION_ITEMS:
                    result["<truncated>"] = f"{len(entries) - _MAX_COLLECTION_ITEMS} entries omitted"
                return result
            finally:
                seen.discard(item_id)
        if isinstance(item, (list, tuple)):
            seen.add(item_id)
            try:
                result = [sanitize(v) for v in item[:_MAX_COLLECTION_ITEMS]]
                if len(item) > _MAX_COLLECTION_ITEMS:
                    result.append(f"<truncated: {len(item) - _MAX_COLLECTION_ITEMS} items omitted>")
                return result
            finally:
                seen.discard(item_id)
        if hasattr(item, "serialize"):
            try:
                return sanitize(item.serialize(), key)
            except (AttributeError, TypeError, ValueError):
                return "<unserializable>"
        if hasattr(item, "model_dump"):
            try:
                return sanitize(item.model_dump(), key)
            except (AttributeError, TypeError, ValueError):
                return "<unserializable>"
        if hasattr(item, "__dict__") and not isinstance(item, type):
            return sanitize(_as_mapping(item), key)
        return f"<{type(item).__name__}>"

    return sanitize(_as_mapping(value))


def sanitized_snapshot(value: Any) -> Any:
    """Sanitize any debug value, including scalars and collection roots."""
    return sanitize_activity({"snapshot": value})["snapshot"]


def log_teams_debug(message: str, **extra: Any) -> None:
    """Emit an installation diagnostic at INFO so it is visible in Docker logs."""
    logger.info(f"[TEAMS DEBUG] {message}", extra=extra)


def log_teams_debug_raw(message: str, value: Any, **extra: Any) -> None:
    """Emit one safe raw diagnostic without risking request processing."""
    try:
        logger.info(
            f"[TEAMS DEBUG RAW] {message}",
            extra={**extra, "snapshot": sanitized_snapshot(value)},
        )
    except Exception:  # pragma: no cover - diagnostics must never affect the bot.
        logger.exception("teams_debug_snapshot_failed", extra={"snapshot_name": message})


def _nested(value: Mapping[str, Any], *keys: str) -> Any:
    """Read a nested field from either a raw activity or serialized SDK data."""
    current: Any = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def activity_metadata(activity: Any) -> dict[str, Any]:
    """Extract useful non-secret fields from an activity for operational logs."""
    data = _as_mapping(activity)
    channel_data = data.get("channelData") or data.get("channel_data") or {}
    conversation = data.get("conversation") or {}
    recipient = data.get("recipient") or {}
    sender = data.get("from") or data.get("from_property") or {}
    tenant = channel_data.get("tenant") or {}
    team = channel_data.get("team") or {}
    channel = channel_data.get("channel") or {}
    return {
        "activity_type": data.get("type"),
        "activity_id": data.get("id"),
        "timestamp": data.get("timestamp"),
        "service_url": data.get("serviceUrl") or data.get("service_url"),
        "conversation_id": conversation.get("id"),
        "conversation_type": conversation.get("conversationType")
        or conversation.get("conversation_type"),
        "conversation_tenant_id": conversation.get("tenantId")
        or conversation.get("tenant_id"),
        "recipient_id": recipient.get("id"),
        "from_id": sender.get("id"),
        "tenant_id": tenant.get("id"),
        "team_id": team.get("id"),
        "team_name": team.get("name"),
        "channel_id": channel.get("id"),
        "channel_name": channel.get("name"),
        "event_type": channel_data.get("eventType")
        or channel_data.get("event_type"),
        "action": data.get("action"),
        "members_added": data.get("membersAdded") or data.get("members_added"),
        "members_removed": data.get("membersRemoved")
        or data.get("members_removed"),
    }


def log_activity(activity: Any, *, debug: bool = False) -> dict[str, Any]:
    """Log safe activity metadata and optionally its sanitized raw payload."""
    metadata = activity_metadata(activity)
    logger.info("teams_activity_received", extra=metadata)
    if debug:
        logger.debug("teams_activity_raw", extra={"activity": sanitize_activity(activity)})
    return metadata
