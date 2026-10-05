"""Persistence for resolved outbound destinations."""

from typing import Any

from src.repositories._base import MongoRepository, maybe_await, utcnow
from src.schemas.destination import Destination, build_destination_id


class DestinationRepository(MongoRepository):
    """Store routes only after a valid provider route has been resolved."""

    collection_name = "destinations"

    async def upsert(self, destination: Destination | dict[str, Any]) -> None:
        """Persist one active destination keyed by tenant/team/channel."""
        data = destination.model_dump() if isinstance(destination, Destination) else dict(destination)
        data.setdefault("destination_id", build_destination_id(data["tenant_id"], data["team_id"], data["channel_id"]))
        created_at = data.pop("created_at", None) or utcnow()
        data["updated_at"] = utcnow()
        await maybe_await(self.collection.update_one(
            {"destination_id": data["destination_id"]},
            {"$set": data, "$setOnInsert": {"created_at": created_at}},
            upsert=True,
        ))

    async def get(self, destination_id: str) -> dict[str, Any] | None:
        """Fetch a destination by deterministic ID."""
        return await maybe_await(self.collection.find_one({"destination_id": destination_id}))

    async def get_by_channel(
        self, tenant_id: str, team_id: str, channel_id: str
    ) -> dict[str, Any] | None:
        """Fetch an existing destination by tenant-isolated channel identity."""
        return await maybe_await(self.collection.find_one({
            "tenant_id": tenant_id, "team_id": team_id, "channel_id": channel_id,
        }))

    async def update_channel_name(
        self, tenant_id: str, team_id: str, channel_id: str, channel_name: str
    ) -> None:
        """Update only the trusted display name of an existing destination."""
        await maybe_await(self.collection.update_one(
            {"tenant_id": tenant_id, "team_id": team_id, "channel_id": channel_id},
            {"$set": {"channel_name": channel_name, "updated_at": utcnow()}},
            upsert=False,
        ))

    async def mark_inactive_for_channel(
        self, tenant_id: str, team_id: str, channel_id: str
    ) -> None:
        """Deactivate only an existing destination for one channel."""
        await maybe_await(self.collection.update_one(
            {"tenant_id": tenant_id, "team_id": team_id, "channel_id": channel_id},
            {"$set": {"is_active": False, "updated_at": utcnow()}},
            upsert=False,
        ))

    async def list_active_for_team(
        self, tenant_id: str, team_id: str
    ) -> list[dict[str, Any]]:
        """Fetch only active destinations belonging to one tenant/team."""
        cursor = self.collection.find({
            "tenant_id": tenant_id,
            "team_id": team_id,
            "is_active": True,
        })
        if hasattr(cursor, "to_list"):
            return await cursor.to_list(length=None)
        if hasattr(cursor, "__aiter__"):
            return [document async for document in cursor]
        result = await maybe_await(cursor)
        return list(result or [])

    async def mark_inactive_for_team(self, tenant_id: str, team_id: str) -> None:
        """Soft-deactivate every destination for one tenant-isolated team."""
        await maybe_await(self.collection.update_many(
            {"tenant_id": tenant_id, "team_id": team_id, "is_active": True},
            {"$set": {"is_active": False, "updated_at": utcnow()}},
        ))
