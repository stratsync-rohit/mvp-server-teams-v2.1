"""Tests for the public read-only risk APIs."""

import re

from fastapi.testclient import TestClient

from src.api.dependencies import Container
from src.application import create_app
from src.repositories.risk_repository import RiskRepository
from src.services.risk_service import RiskService


class RiskCollection:
    """Small async Mongo collection double for API integration tests."""

    def __init__(self, documents):
        self.documents = documents

    def find(self, query):
        return [
            document for document in self.documents
            if all(document.get(field) == value for field, value in query.items())
        ]

    async def find_one(self, query):
        return next(
            (
                document for document in self.documents
                if all(document.get(field) == value for field, value in query.items())
            ),
            None,
        )

    async def insert_one(self, document):
        self.documents.append(dict(document))


def _client(risks):
    app = create_app()
    app.state.container = Container(
        adapter=object(),
        bot=object(),
        risk_service=RiskService(RiskRepository(collection=RiskCollection(risks))),
    )
    return TestClient(app)


def test_list_risks_returns_empty_list_when_database_is_empty():
    response = _client([]).get("/api/risks")

    assert response.status_code == 200
    assert response.json() == []


def test_list_risks_returns_multiple_database_records_without_mongo_id():
    risks = [
        {"_id": "mongo-1", "risk_id": "RSK-1", "is_active": True, "title": "First"},
        {"_id": "mongo-2", "risk_id": "RSK-2", "is_active": True, "title": "Second"},
    ]

    response = _client(risks).get("/api/risks")

    assert response.status_code == 200
    assert response.json() == [
        {"risk_id": "RSK-1", "is_active": True, "title": "First"},
        {"risk_id": "RSK-2", "is_active": True, "title": "Second"},
    ]


def test_get_risk_returns_record_by_logical_risk_id():
    response = _client([
        {"_id": "mongo-1", "risk_id": "RSK-EFRY-O0QT", "is_active": True, "title": "Stored risk"},
    ]).get("/api/risks/RSK-EFRY-O0QT")

    assert response.status_code == 200
    assert response.json() == {
        "risk_id": "RSK-EFRY-O0QT",
        "is_active": True,
        "title": "Stored risk",
    }


def test_get_missing_risk_returns_404():
    response = _client([]).get("/api/risks/RSK-MISSING")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RISK_NOT_FOUND"


def test_create_risk_generates_id_and_persists_a_fetchable_document():
    client = _client([])

    response = client.post("/api/risks", json={
        "title": "Late supplier delivery",
        "severity": "high",
    })

    assert response.status_code == 201
    created = response.json()
    assert re.fullmatch(r"RSK-[A-Z0-9]{4}-[A-Z0-9]{4}", created["risk_id"])
    assert created == {
        "risk_id": created["risk_id"],
        "title": "Late supplier delivery",
        "severity": "high",
        "is_active": True,
    }

    fetched = client.get(f"/api/risks/{created['risk_id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created


def test_create_risk_preserves_valid_supplied_risk_id():
    response = _client([]).post("/api/risks", json={
        "risk_id": "RSK-A1B2-C3D4",
        "title": "Known identifier",
    })

    assert response.status_code == 201
    assert response.json()["risk_id"] == "RSK-A1B2-C3D4"


def test_create_risk_rejects_invalid_or_missing_required_payload_fields():
    client = _client([])

    missing_title = client.post("/api/risks", json={"severity": "high"})
    invalid_title = client.post("/api/risks", json={"title": "   "})
    invalid_risk_id = client.post("/api/risks", json={
        "risk_id": "risk-1", "title": "Invalid identifier",
    })

    assert missing_title.status_code == 422
    assert invalid_title.status_code == 422
    assert invalid_risk_id.status_code == 422


def test_create_risk_rejects_duplicate_risk_id():
    response = _client([{
        "risk_id": "RSK-A1B2-C3D4", "title": "Existing", "is_active": True,
    }]).post("/api/risks", json={
        "risk_id": "RSK-A1B2-C3D4", "title": "Duplicate",
    })

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RISK_ALREADY_EXISTS"
