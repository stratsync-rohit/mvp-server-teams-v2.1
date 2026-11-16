"""Tests for generic arbitrary-risk normalization into Teams card views."""

import json

import pytest

from src.renderers.teams_risk_card_renderer import TeamsRiskCardRenderer
from src.services.generic_risk_view_builder import GenericRiskViewBuilder
from src.services.risk_view_service import RiskViewService


def _blocks_of_type(view, block_type):
    return [block for block in view["blocks"] if block["type"] == block_type]


def test_generic_builder_maps_scalars_lists_nested_values_and_object_arrays():
    risk = {
        "risk_id": "RSK-1",
        "title": "Vendor Exposure Risk",
        "severity": "High",
        "vendor": {"name": "CloudX", "region": "APAC"},
        "exposure": 2500000,
        "is_customer_visible": True,
        "owners": ["Infra", "Security"],
        "scores": [1, 2, 3],
        "affected_services": [
            {"service": "Payments", "impact": "High"},
            {"service": "Auth", "impact": "Medium"},
        ],
        "mitigation": ["Enable failover", "Increase redundancy"],
    }

    normalized = GenericRiskViewBuilder().build(risk)
    details = normalized["views"]["details"]
    key_values = _blocks_of_type(details, "key_value")
    tables = _blocks_of_type(details, "table")
    lists = _blocks_of_type(details, "action_list")

    assert any({item["label"] for item in block["items"]} >= {
        "Exposure", "Is Customer Visible", "Severity", "Title"
    } for block in key_values)
    assert any(block.get("title") == "Vendor" for block in key_values)
    assert next(block for block in tables if block["title"] == "Affected Services") == {
        "type": "table",
        "title": "Affected Services",
        "columns": [
            {"key": "service", "label": "Service"},
            {"key": "impact", "label": "Impact"},
        ],
        "rows": [
            {"service": "Payments", "impact": "High"},
            {"service": "Auth", "impact": "Medium"},
        ],
    }
    assert {block["title"] for block in lists} >= {"Owners", "Scores", "Mitigation"}


def test_generic_builder_uses_union_columns_for_inconsistent_object_arrays():
    view = GenericRiskViewBuilder().build({
        "risk_id": "RSK-1",
        "custom_rows": [{"alpha": 1}, {"beta": 2, "alpha": 3}],
    })["views"]["details"]

    table = _blocks_of_type(view, "table")[0]
    assert table["columns"] == [
        {"key": "alpha", "label": "Alpha"},
        {"key": "beta", "label": "Beta"},
    ]
    assert table["rows"] == [{"alpha": "1", "beta": ""}, {"alpha": "3", "beta": "2"}]


def test_generic_builder_skips_empty_values_and_bounds_deep_content():
    deeply_nested = value = {}
    for index in range(8):
        next_value = {f"level_{index}": {}}
        value.update(next_value)
        value = next_value[f"level_{index}"]
    normalized = GenericRiskViewBuilder().build({
        "risk_id": "RSK-1", "null_value": None, "empty": "", "nested": deeply_nested,
    })
    serialized = json.dumps(normalized["views"])

    assert "Null Value" not in serialized
    assert "Empty" not in serialized
    assert "Additional nested content omitted" in serialized


def test_generic_builder_safely_omits_circular_list_values():
    circular_values = []
    circular_values.append(circular_values)

    normalized = GenericRiskViewBuilder().build({
        "risk_id": "RSK-1", "related_values": circular_values,
    })

    assert "[nested value omitted]" in json.dumps(normalized["views"])


def test_explicit_views_remain_authoritative_and_are_not_replaced():
    risk = {
        "risk_id": "RSK-1",
        "custom": "included only in generated views",
        "views": {
            "notification": {"blocks": [{"type": "text", "text": "Explicit notification"}]},
            "details": {"title": "Explicit details", "blocks": [{"type": "text", "text": "Explicit details body"}]},
            "mitigation": {"blocks": [{"type": "action_list", "items": [{"title": "Act"}]}]},
        },
    }

    normalized = GenericRiskViewBuilder().build(risk)
    card = TeamsRiskCardRenderer().render_notification(risk)

    assert normalized["views"] == risk["views"]
    assert "Explicit notification" in json.dumps(card)
    assert "included only in generated views" not in json.dumps(card)
    assert [action["data"]["view"] for action in card["content"]["actions"]] == [
        "details", "mitigation"
    ]


def test_generic_notification_has_view_details_action_and_valid_adaptive_card_json():
    card = TeamsRiskCardRenderer().render_notification({
        "risk_id": "RSK-1", "arbitrary_custom_field": "works without renderer changes",
    })

    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert card["content"]["type"] == "AdaptiveCard"
    assert card["content"]["version"] == "1.5"
    assert card["content"]["msteams"] == {"width": "Full"}
    assert json.loads(json.dumps(card)) == card
    assert "Arbitrary Custom Field" in json.dumps(card)
    assert card["content"]["actions"] == [{
        "type": "Action.Execute",
        "title": "View Details",
        "verb": "show_risk_view",
        "data": {"action": "show_risk_view", "risk_id": "RSK-1", "view": "details"},
    }]


@pytest.mark.asyncio
async def test_generic_details_view_is_available_without_explicit_views():
    class RiskServiceDouble:
        async def get(self, risk_id):
            return {
                "risk_id": risk_id,
                "custom_nested": {"region": "APAC"},
                "numbers": [1, 2],
            }

    card = await RiskViewService(
        RiskServiceDouble(), TeamsRiskCardRenderer()
    ).render("RSK-1", "details")

    serialized = json.dumps(card)
    assert "Custom Nested" in serialized
    assert "Region" in serialized
    assert "APAC" in serialized
    assert "1. 1" in serialized
    assert card["content"]["version"] == "1.5"
    assert card["content"]["msteams"] == {"width": "Full"}
