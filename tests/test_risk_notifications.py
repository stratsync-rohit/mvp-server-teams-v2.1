"""Tests for database-driven risk notification rendering."""

import json

import pytest

from src.exceptions import AppError
from src.renderers.teams_risk_card_renderer import TeamsRiskCardRenderer
from src.repositories.risk_repository import RiskRepository
from src.services.risk_service import RiskService


def _texts(value):
    """Return Adaptive Card text values in document order."""
    if isinstance(value, dict):
        result = []
        if isinstance(value.get("text"), str):
            result.append(value["text"])
        for fact in value.get("facts", []):
            if isinstance(fact, dict):
                for key in ("title", "value"):
                    if isinstance(fact.get(key), str):
                        result.append(fact[key])
        for child in value.values():
            result.extend(_texts(child))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_texts(child))
        return result
    return []


class RiskCollection:
    """Mongo collection double recording the active-risk query."""

    def __init__(self, document=None):
        self.document = document
        self.query = None

    async def find_one(self, query):
        self.query = query
        return self.document


@pytest.mark.asyncio
async def test_risk_repository_queries_active_risk_and_hides_mongo_id():
    collection = RiskCollection({
        "_id": "mongo-id",
        "risk_id": "RSK-1",
        "is_active": True,
        "title": "Stored title",
    })

    risk = await RiskRepository(collection=collection).get_by_risk_id("RSK-1")

    assert collection.query == {"risk_id": "RSK-1", "is_active": True}
    assert risk == {
        "risk_id": "RSK-1",
        "is_active": True,
        "title": "Stored title",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("document", [None, {"risk_id": "RSK-1", "is_active": False}])
async def test_risk_service_rejects_unknown_or_inactive_risk(document):
    with pytest.raises(AppError) as error:
        await RiskService(RiskRepository(collection=RiskCollection(document))).get(" RSK-1 ")

    assert error.value.code == "RISK_NOT_FOUND"
    assert error.value.status_code == 404


def test_renderer_handles_dynamic_blocks_and_preserves_order(caplog):
    renderer = TeamsRiskCardRenderer()
    risk = {
        "_id": "must-not-render",
        "risk_id": "RSK-1",
        "severity": "high",
        "title": "Stored title",
        "subtitle": "Stored subtitle",
        "summary": "Stored summary",
        "unknown_field": {"secret": "must-not-render"},
        "views": {"notification": {"blocks": [
            {"type": "key_value", "title": "Snapshot", "items": [
                {"label": "Warehouse", "value": "East"},
            ]},
            {"type": "metrics", "columns": [
                {"key": "label", "label": "Metric"},
                {"key": "value", "label": "Value"},
                {"key": "status", "label": "Status"},
            ], "items": [
                {"label": "Demand", "value": "500", "status": "high"},
                {"label": "Stock", "value": "100", "status": "low"},
                {"label": "Lead time", "value": "7 days", "status": "unknown"},
            ]},
            {"type": "text", "title": "Note", "text": "Review required", "bold": True},
            {"type": "table", "columns": [
                {"key": "warehouse", "label": "Location"},
                {"key": "stock", "label": "Available"},
            ], "rows": [
                {"warehouse": "East", "stock": "100"},
                {"warehouse": "West", "stock": "250"},
            ]},
            {"type": "action_list", "items": [
                {"order": 1, "title": "Review demand"},
                {"order": 2, "title": "Contact supplier"},
                {"order": 3, "title": "Update forecast"},
            ]},
            {"type": "chart", "data": {"secret": "ignored"}},
        ]}},
    }

    with caplog.at_level("WARNING"):
        card = renderer.render_notification(risk)

    serialized = json.dumps(card)
    texts = _texts(card)
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert card["content"]["version"] == "1.5"
    assert card["content"]["msteams"] == {"width": "Full"}
    assert all(value in texts for value in (
        "HIGH", "Stored title", "Stored subtitle", "Stored summary",
        "Snapshot", "Warehouse", "East", "Demand", "500", "Lead time",
        "Review required", "East", "100", "West", "250",
        "1. Review demand", "2. Contact supplier", "3. Update forecast",
    ))
    assert texts.index("Stored title") < texts.index("Snapshot") < texts.index("Review required")
    assert not {"high", "low", "unknown"}.intersection(texts)
    assert "must-not-render" not in serialized
    assert "unsupported_risk_card_block" in caplog.text


def test_renderer_supports_changed_table_keys_without_code_changes():
    renderer = TeamsRiskCardRenderer()
    card = renderer.render_notification({
        "risk_id": "RSK-2",
        "views": {"notification": {"blocks": [{
            "type": "table",
            "columns": [
                {"key": "customer_segment", "label": "Segment"},
                {"key": "exposure", "label": "Exposure"},
            ],
            "rows": [{"customer_segment": "Enterprise", "exposure": "$90k"}],
        }]}}
    })

    table = next(item for item in card["content"]["body"] if item["type"] == "Table")
    assert table["firstRowAsHeader"] is True
    assert table["showGridLines"] is True
    assert table["gridStyle"] == "default"
    assert table["rows"][0]["type"] == "TableRow"
    assert all(cell["type"] == "TableCell" for cell in table["rows"][0]["cells"])
    assert [cell["items"][0]["text"] for cell in table["rows"][0]["cells"]] == [
        "Segment", "Exposure"
    ]
    assert [cell["items"][0]["text"] for cell in table["rows"][1]["cells"]] == [
        "Enterprise", "$90k"
    ]


def test_renderer_degrades_gracefully_for_missing_optional_fields():
    card = TeamsRiskCardRenderer().render_notification({
        "risk_id": "RSK-3",
        "is_active": True,
        "extra_field": "ignored",
    })

    assert "Extra Field" in json.dumps(card)
    assert "ignored" in json.dumps(card)
    assert "None" not in _texts(card)


def test_renderer_renders_all_schema_v2_block_types_with_responsive_content():
    long_text = "A detailed cross-functional update. " * 80
    card = TeamsRiskCardRenderer().render_notification({
        "risk_id": "RSK-SCHEMA-2",
        "views": {"notification": {"blocks": [
            {"type": "text", "text": long_text},
            {"type": "callout", "title": "Escalation", "message": "Owner review needed", "status": "high"},
            {"type": "metrics", "title": "Signals", "items": [
                {"label": "Coverage", "value": "82%", "status": "good"},
                {"label": "Variance", "value": "12%", "status": "warning"},
            ]},
            {"type": "key_value", "items": [
                {"label": "Portfolio", "value": "Core services"},
            ]},
            {"type": "table", "title": "Wide dynamic table", "columns": [
                {"key": "region", "label": "Region"},
                {"key": "owner", "label": "Owner"},
                {"key": "impact", "label": "Impact"},
                {"key": "review_date", "label": "Review date"},
            ], "rows": [{
                "region": "APAC", "owner": "Operations", "impact": "High", "review_date": "2026-11-15",
            }]},
            {"type": "bullet_list", "title": "Watch", "items": ["New supplier", {"text": "Capacity"}]},
            {"type": "numbered_list", "title": "Sequence", "items": [
                {"order": 4, "text": "Confirm inputs"}, "Publish outcome",
            ]},
            {"type": "action_list", "title": "Actions", "items": [{"order": 1, "title": "Assign owner"}]},
            {"type": "divider"},
        ]}},
    })

    body = card["content"]["body"]
    texts = _texts(card)
    assert all(value in texts for value in (
        long_text.strip(), "Escalation", "Owner review needed", "COVERAGE", "82%", "Portfolio",
        "Core services", "Region", "APAC", "Owner", "Operations", "• New supplier", "• Capacity",
        "4. Confirm inputs", "2. Publish outcome", "1. Assign owner",
    ))
    assert any(item["type"] == "FactSet" for item in body)
    assert any(item["type"] == "ColumnSet" for item in body)
    table = next(item for item in body if item["type"] == "Table")
    assert table["showGridLines"] is True
    assert [cell["items"][0]["text"] for cell in table["rows"][0]["cells"]] == [
        "Region", "Owner", "Impact", "Review date"
    ]
    assert [cell["items"][0]["text"] for cell in table["rows"][1]["cells"]] == [
        "APAC", "Operations", "High", "2026-11-15"
    ]
    assert any(item["type"] == "Container" and item.get("style") == "attention" for item in body)
    assert {"type": "TextBlock", "text": " ", "separator": True, "spacing": "Medium"} in body
    assert next(item for item in body if item.get("text") == long_text.strip())["wrap"] is True


def test_renderer_omits_empty_view_actions_and_missing_block_values():
    card = TeamsRiskCardRenderer().render_notification({
        "risk_id": "RSK-EMPTY-VIEWS",
        "views": {
            "notification": {"blocks": [
                {"type": "key_value", "items": [{"label": "Missing", "value": None}]},
                {"type": "metrics", "items": [{"label": None, "value": None}]},
                {"type": "table", "columns": ["Name"], "rows": [{"Name": None}]},
                {"type": "text", "text": "Still renders safely"},
            ]},
            "details": {"title": "No useful detail", "blocks": [
                {"type": "divider"}, {"type": "text", "text": None},
            ]},
            "mitigation": {"blocks": [{"type": "bullet_list", "items": ["Review source data"]}]},
        },
    })

    assert [action["data"]["view"] for action in card["content"]["actions"]] == ["mitigation"]
    assert "Still renders safely" in _texts(card)
    assert "None" not in _texts(card)


def test_renderer_preserves_dynamic_metric_fields_but_not_status_values():
    card = TeamsRiskCardRenderer().render_notification({
        "risk_id": "RSK-DYNAMIC-METRICS",
        "views": {"notification": {"blocks": [{
            "type": "metrics",
            "title": "Delivery signals",
            "items": [
                {"actual": "80%", "target": "90%", "status": "warning"},
                {"actual": "95%", "variance": "5%", "status": "good"},
            ],
        }]}},
    })

    body = card["content"]["body"]
    metric_rows = [
        item for item in body
        if item["type"] == "ColumnSet" and len(item["columns"]) == 3
    ]
    assert [column["items"][0]["text"] for column in metric_rows[0]["columns"]] == [
        "Actual", "Target", "Variance"
    ]
    assert [column["items"][0]["text"] for column in metric_rows[1]["columns"]] == [
        "80%", "90%", "—"
    ]
    assert [column["items"][0]["text"] for column in metric_rows[2]["columns"]] == [
        "95%", "—", "5%"
    ]
    assert not {"warning", "good"}.intersection(_texts(card))
