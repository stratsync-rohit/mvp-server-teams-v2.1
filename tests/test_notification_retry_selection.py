"""Regression tests for null-aware hourly-card retry selection."""

from datetime import datetime, timedelta, timezone

import pytest

import src.repositories.notification_repository as notification_module
from src.repositories.notification_repository import NotificationRepository


class _Cursor(list):
    def sort(self, field, direction):
        def key(record):
            value = record.get(field)
            # Mongo orders null/missing values before actual dates.
            return (value is not None, value or datetime.min.replace(tzinfo=timezone.utc))

        return _Cursor(sorted(self, key=key, reverse=direction < 0))

    def limit(self, limit):
        return _Cursor(self[:limit])


class _RetryCollection:
    def __init__(self, records):
        self.records = records
        self.last_query = None

    def find(self, query):
        self.last_query = query
        return _Cursor([record for record in self.records if self._matches_retry_clause(record, query["$or"])])

    @staticmethod
    def _matches_retry_clause(record, clauses):
        value = record.get("retry_next_at")
        for clause in clauses:
            condition = clause["retry_next_at"]
            if condition is None and value is None:  # Mongo null equality also matches missing fields.
                return True
            if isinstance(condition, dict) and "$lte" in condition and isinstance(value, datetime) and value <= condition["$lte"]:
                return True
            if isinstance(condition, dict) and condition.get("$type") == "date" and isinstance(value, datetime):
                return True
        return False


def _record(notification_id, **values):
    return {
        "notification_id": notification_id,
        "status": "sent",
        "update_eligible": True,
        "update_state": "eligible",
        **values,
    }


@pytest.mark.asyncio
async def test_candidate_selection_accepts_missing_null_and_expired_but_not_future_retry_deadlines(monkeypatch):
    now = datetime(2026, 11, 14, 14, tzinfo=timezone.utc)
    monkeypatch.setattr(notification_module, "utcnow", lambda: now)
    collection = _RetryCollection([
        _record("missing"),
        _record("null", retry_next_at=None),
        _record("expired", retry_next_at=now - timedelta(seconds=1)),
        _record("future", retry_next_at=now + timedelta(seconds=1)),
    ])
    repository = NotificationRepository(collection=collection)

    candidates = await repository.list_eligible_updates(now, limit=10)

    assert {record["notification_id"] for record in candidates} == {"missing", "null", "expired"}
    assert collection.last_query["$or"] == [
        {"retry_next_at": None},
        {"retry_next_at": {"$lte": now}},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("record", "expected_offset"),
    [
        (_record("missing"), 0),
        (_record("null", retry_next_at=None), 0),
        (_record("expired", retry_next_at=datetime(2026, 11, 14, 13, 59, tzinfo=timezone.utc)), -60),
        (_record("future", retry_next_at=datetime(2026, 11, 14, 14, 2, tzinfo=timezone.utc)), 120),
    ],
)
async def test_retry_scheduler_deadline_treats_null_or_missing_as_immediately_due(monkeypatch, record, expected_offset):
    now = datetime(2026, 11, 14, 14, tzinfo=timezone.utc)
    monkeypatch.setattr(notification_module, "utcnow", lambda: now)
    collection = _RetryCollection([record])
    repository = NotificationRepository(collection=collection)

    deadline = await repository.next_retry_at(now)

    assert deadline == now + timedelta(seconds=expected_offset)
    assert collection.last_query["$or"][0] == {"retry_next_at": None}
