"""Persistence for Teams installation lifecycle state."""

from typing import Any

from src.bot.diagnostics.activity_logger import log_teams_debug
from src.repositories._base import MongoRepository, maybe_await, utcnow
from src.schemas.teams import TeamInstallation


class TeamInstallationRepository(MongoRepository):
    """Upsert and soft-delete Teams installations."""

    collection_name = "teams_installations"

    @staticmethod
    def _write_status(result: Any) -> dict[str, Any]:
        return {
            "matched_count": getattr(result, "matched_count", None),
            "modified_count": getattr(result, "modified_count", None),
            "upserted": getattr(result, "upserted_id", None) is not None,
        }

    async def upsert(self, installation: TeamInstallation | dict[str, Any]) -> None:
        """Create or update an installation without deleting history."""
        data = installation.model_dump() if isinstance(installation, TeamInstallation) else dict(installation)
        now = utcnow()
        installed_at = data.pop("installed_at", None) or now
        data["updated_at"] = now
        data["is_active"] = True
        data["uninstalled_at"] = None
        fields = {
            "collection": self.collection_name,
            "operation": "upsert",
            "tenant_id": data["tenant_id"],
            "team_id": data["team_id"],
        }
        log_teams_debug("mongo installation upsert", **fields)
        result = await maybe_await(self.collection.update_one(
            {"tenant_id": data["tenant_id"], "team_id": data["team_id"]},
            {"$set": data, "$setOnInsert": {"installed_at": installed_at}},
            upsert=True,
        ))
        log_teams_debug("mongo installation upsert complete", **fields, **self._write_status(result))

    async def mark_inactive(self, tenant_id: str, team_id: str) -> None:
        """Mark an installation inactive while retaining its record."""
        now = utcnow()
        await maybe_await(self.collection.update_one(
            {"tenant_id": tenant_id, "team_id": team_id},
            {"$set": {"is_active": False, "uninstalled_at": now, "updated_at": now}},
        ))

    async def get(self, tenant_id: str, team_id: str) -> dict[str, Any] | None:
        """Fetch one tenant-isolated installation."""
        return await maybe_await(self.collection.find_one({"tenant_id": tenant_id, "team_id": team_id}))

    async def list_all(self) -> list[dict[str, Any]]:
        """Fetch all installation documents without exposing persistence objects."""
        cursor = self.collection.find({})
        if hasattr(cursor, "to_list"):
            return await cursor.to_list(length=None)
        if hasattr(cursor, "__aiter__"):
            return [document async for document in cursor]
        result = await maybe_await(cursor)
        return list(result or [])

    async def list_active(self) -> list[dict[str, Any]]:
        """Fetch only currently active installations for public aggregation."""
        cursor = self.collection.find({"is_active": True})
        if hasattr(cursor, "to_list"):
            return await cursor.to_list(length=None)
        if hasattr(cursor, "__aiter__"):
            return [document async for document in cursor]
        result = await maybe_await(cursor)
        return list(result or [])
