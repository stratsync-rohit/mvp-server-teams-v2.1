"""Persistence for channels discovered through trusted Teams APIs."""

from typing import Any

from src.bot.diagnostics.activity_logger import log_teams_debug
from src.repositories._base import MongoRepository, maybe_await, utcnow
from src.schemas.teams import DiscoveredChannel


class DiscoveredChannelRepository(MongoRepository):
    """Upsert discovered channels using tenant/team/channel identity."""

    collection_name = "teams_discovered_channels"

    @staticmethod
    def _write_status(result: Any) -> dict[str, Any]:
        return {
            "matched_count": getattr(result, "matched_count", None),
            "modified_count": getattr(result, "modified_count", None),
            "upserted": getattr(result, "upserted_id", None) is not None,
        }

    async def upsert(self, channel: DiscoveredChannel | dict[str, Any]) -> None:
        """Persist channel metadata without using conversation ID as identity."""
        data = channel.model_dump() if isinstance(channel, DiscoveredChannel) else dict(channel)
        now = utcnow()
        discovered_at = data.pop("discovered_at", None) or now
        data = {key: value for key, value in data.items() if value is not None}
        data["updated_at"] = now
        fields = {
            "collection": self.collection_name,
            "operation": "upsert",
            "tenant_id": data["tenant_id"],
            "team_id": data["team_id"],
            "channel_id": data["channel_id"],
        }
        log_teams_debug("mongo discovered channel upsert", **fields)
        result = await maybe_await(self.collection.update_one(
            {key: data[key] for key in ("tenant_id", "team_id", "channel_id")},
            {"$set": data, "$setOnInsert": {"discovered_at": discovered_at}},
            upsert=True,
        ))
        log_teams_debug(
            "mongo discovered channel upsert complete", **fields, **self._write_status(result)
        )

    async def get(self, tenant_id: str, team_id: str, channel_id: str) -> dict[str, Any] | None:
        """Fetch a channel by its logical identity."""
        return await maybe_await(self.collection.find_one({
            "tenant_id": tenant_id, "team_id": team_id, "channel_id": channel_id,
        }))

    async def update_channel_name(
        self, tenant_id: str, team_id: str, channel_id: str, channel_name: str
    ) -> None:
        """Update only the trusted display name of an existing channel."""
        await maybe_await(self.collection.update_one(
            {"tenant_id": tenant_id, "team_id": team_id, "channel_id": channel_id},
            {"$set": {"channel_name": channel_name, "updated_at": utcnow()}},
            upsert=False,
        ))

    async def list_for_team(self, tenant_id: str, team_id: str) -> list[dict[str, Any]]:
        """Fetch only channels belonging to one tenant-isolated team."""
        cursor = self.collection.find({"tenant_id": tenant_id, "team_id": team_id})
        if hasattr(cursor, "to_list"):
            return await cursor.to_list(length=None)
        if hasattr(cursor, "__aiter__"):
            return [document async for document in cursor]
        result = await maybe_await(cursor)
        return list(result or [])
