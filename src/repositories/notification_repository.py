"""Persistence for notification send state."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pymongo import ReturnDocument

from src.repositories._base import MongoRepository, maybe_await, utcnow


class NotificationRepository(MongoRepository):
    """Persist pending, sent, and failed notifications."""

    collection_name = "notifications"

    _READ_FIELDS = {
        "notification_id": 1,
        "destination_id": 1,
        "risk_id": 1,
        "tenant_id": 1,
        "team_id": 1,
        "channel_id": 1,
        "notification_type": 1,
        "view_type": 1,
        "status": 1,
        "teams_message_id": 1,
        "created_at": 1,
        "updated_at": 1,
        "_id": 0,
    }

    async def create(self, notification: dict[str, Any]) -> None:
        """Insert a pending notification record."""
        data = dict(notification)
        now = utcnow()
        data.setdefault("created_at", now)
        data["updated_at"] = now
        await maybe_await(self.collection.insert_one(data))

    async def mark_sent(self, notification_id: str, teams_message_id: str | None) -> None:
        """Record successful provider delivery."""
        await self._mark(notification_id, "sent", teams_message_id=teams_message_id, error=None)

    async def mark_sent_with_reference(
        self,
        notification_id: str,
        teams_message_id: str | None,
        message_reference: dict[str, Any] | None,
    ) -> None:
        """Finalize delivery and record whether the exact bot activity is editable."""
        eligible = message_reference is not None
        await self._mark(
            notification_id,
            "sent",
            teams_message_id=teams_message_id,
            message_reference=message_reference,
            update_eligible=eligible,
            update_state="eligible" if eligible else "missing_reference",
            failure_count=0,
            latest_update_error=None,
            error=None,
        )

    async def mark_failed(self, notification_id: str, error: str) -> None:
        """Record a failed delivery without hiding the retryable failure."""
        await self._mark(notification_id, "failed", error=error)

    async def _mark(self, notification_id: str, status: str, **values: Any) -> None:
        values.update({"status": status, "updated_at": utcnow()})
        await maybe_await(self.collection.update_one(
            {"notification_id": notification_id}, {"$set": values}
        ))

    async def get_by_message_id(self, teams_message_id: str, tenant_id: str | None = None) -> dict[str, Any] | None:
        """Resolve a notification from a tenant-scoped Teams message ID."""
        query: dict[str, Any] = {"teams_message_id": teams_message_id}
        if tenant_id:
            query["tenant_id"] = tenant_id
        return await maybe_await(self.collection.find_one(query))

    async def list(
        self,
        *,
        risk_id: str | None = None,
        destination_id: str | None = None,
        status: str | None = None,
        notification_type: str | None = None,
        view_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return safe notification fields newest first."""
        query: dict[str, Any] = {}
        for field, value in (
            ("risk_id", risk_id),
            ("destination_id", destination_id),
            ("status", status),
            ("notification_type", notification_type),
            ("view_type", view_type),
        ):
            if value is not None:
                query[field] = value

        try:
            cursor = self.collection.find(query, self._READ_FIELDS)
        except TypeError:
            # Keep small repository doubles useful when they only accept query.
            cursor = self.collection.find(query)
        cursor = await maybe_await(cursor)
        cursor_sortable = not isinstance(cursor, list) and hasattr(cursor, "sort")
        if cursor_sortable:
            cursor = cursor.sort("created_at", -1)
        if hasattr(cursor, "limit"):
            cursor = cursor.limit(limit)
        cursor = await maybe_await(cursor)
        documents = await self._collect(cursor)
        if not cursor_sortable:
            documents.sort(key=lambda item: self._sort_timestamp(item.get("created_at")), reverse=True)
        return documents[:limit]

    async def get_by_notification_id(self, notification_id: str) -> dict[str, Any] | None:
        """Return one notification using only frontend-safe fields."""
        query = {"notification_id": notification_id}
        try:
            document = self.collection.find_one(query, self._READ_FIELDS)
        except TypeError:
            document = self.collection.find_one(query)
        document = await maybe_await(document)
        if document is None:
            return None
        return {field: document[field] for field in self._READ_FIELDS if field != "_id" and field in document}

    async def list_eligible_updates(self, checkpoint: datetime, *, limit: int) -> list[dict[str, Any]]:
        """Load bounded sent notifications that have not succeeded this hour."""
        now = utcnow()
        query = {
            "status": "sent",
            "update_eligible": True,
            "update_state": {"$in": ["eligible", "retryable"]},
            "last_successful_checkpoint": {"$ne": checkpoint},
            "$or": [
                # MongoDB equality with null intentionally matches both a
                # missing field and an explicitly persisted null. Successful
                # updates write null, so $exists:false alone incorrectly
                # stranded otherwise eligible notifications.
                {"retry_next_at": None},
                {"retry_next_at": {"$lte": now}},
            ],
        }
        cursor = self.collection.find(query)
        if hasattr(cursor, "sort"):
            cursor = cursor.sort("updated_at", 1)
        if hasattr(cursor, "limit"):
            cursor = cursor.limit(limit)
        return (await self._collect(cursor))[:limit]

    async def claim_update(
        self, notification_id: str, checkpoint: datetime, owner: str, *, lease_seconds: int = 300
    ) -> dict[str, Any] | None:
        """Atomically claim one notification/checkpoint for a single worker."""
        now = utcnow()
        query = {
            "notification_id": notification_id,
            "status": "sent",
            "update_eligible": True,
            "last_successful_checkpoint": {"$ne": checkpoint},
            "$or": [
                {"update_claim": {"$exists": False}},
                {"update_claim.expires_at": {"$lte": now}},
                {"update_claim.owner": owner, "update_claim.checkpoint": checkpoint},
            ],
        }
        update = {"$set": {
            "update_claim": {
                "owner": owner,
                "checkpoint": checkpoint,
                "idempotency_key": f"{notification_id}:{checkpoint.isoformat()}",
                "expires_at": now + timedelta(seconds=lease_seconds),
            },
            "last_attempted_checkpoint": checkpoint,
            "updated_at": now,
        }}
        return await maybe_await(self.collection.find_one_and_update(
            query, update, return_document=ReturnDocument.AFTER
        ))

    async def mark_update_success(
        self, notification_id: str, checkpoint: datetime, card: dict[str, Any], *, owner: str | None = None
    ) -> None:
        """Record one successful in-place update for its exact hour checkpoint."""
        query: dict[str, Any] = {"notification_id": notification_id}
        if owner is not None:
            # A worker which lost an expired claim must not overwrite the
            # outcome recorded by its replacement after a slow provider call.
            query.update({"update_claim.owner": owner, "update_claim.checkpoint": checkpoint})
        await maybe_await(self.collection.update_one(
            query,
            {"$set": {
                "last_successful_checkpoint": checkpoint,
                "last_update_at": utcnow(),
                "last_attempted_checkpoint": checkpoint,
                "update_state": "eligible",
                "failure_count": 0,
                "latest_update_error": None,
                "retry_next_at": None,
                "card": card,
                "updated_at": utcnow(),
            }, "$unset": {"update_claim": ""}},
        ))

    async def mark_update_failure(
        self, notification_id: str, checkpoint: datetime, error: str, *, permanent: bool,
        retry_at: datetime | None, owner: str | None = None,
    ) -> None:
        """Record sanitized update state without changing sent delivery history."""
        query: dict[str, Any] = {"notification_id": notification_id}
        if owner is not None:
            query.update({"update_claim.owner": owner, "update_claim.checkpoint": checkpoint})
        fields = {
            "last_attempted_checkpoint": checkpoint,
            "last_update_at": utcnow(),
            "latest_update_error": error,
            "update_state": "inactive" if permanent else "retryable",
            "retry_next_at": retry_at,
            "updated_at": utcnow(),
        }
        if permanent:
            fields["update_eligible"] = False
        await maybe_await(self.collection.update_one(
            query,
            {"$set": fields, "$inc": {"failure_count": 1}, "$unset": {"update_claim": ""}},
        ))

    async def next_retry_at(self, checkpoint: datetime) -> datetime | None:
        """Return the first retry deadline for the active checkpoint.

        The worker uses this after a cycle instead of querying eligible cards
        every scheduler tick.  A restart can safely rediscover one deadline.
        """
        now = utcnow()
        query = {
            "status": "sent",
            "update_eligible": True,
            "update_state": {"$in": ["eligible", "retryable"]},
            "last_successful_checkpoint": {"$ne": checkpoint},
            "$or": [
                # Include null/missing here as an immediately due update. It
                # is important after a worker restart in the same checkpoint.
                {"retry_next_at": None},
                {"retry_next_at": {"$type": "date"}},
            ],
        }
        cursor = self.collection.find(query)
        if hasattr(cursor, "sort"):
            cursor = cursor.sort("retry_next_at", 1)
        if hasattr(cursor, "limit"):
            cursor = cursor.limit(1)
        records = await self._collect(cursor)
        if not records:
            return None
        value = records[0].get("retry_next_at")
        # A null-or-missing deadline does not mean "no retry"; it means the
        # notification is immediately eligible. Returning now lets the
        # scheduler trigger the current checkpoint without polling Mongo.
        return value if isinstance(value, datetime) else now

    @staticmethod
    async def _collect(cursor: Any) -> list[dict[str, Any]]:
        if hasattr(cursor, "to_list"):
            return list(await maybe_await(cursor.to_list(length=None)))
        if hasattr(cursor, "__aiter__"):
            return [document async for document in cursor]
        return list(cursor)

    @staticmethod
    def _sort_timestamp(value: Any) -> float:
        if isinstance(value, datetime):
            return value.timestamp()
        return float("-inf")
