"""Render flexible risk documents as Microsoft Teams Adaptive Cards."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from src.services.generic_risk_view_builder import GenericRiskViewBuilder
from src.services.risk_timing import RiskTimingService

logger = logging.getLogger(__name__)


class TeamsRiskCardRenderer:
    """Render Schema V2 risk-view blocks without business-specific field rules."""

    _SEVERITY_COLORS = {
        "critical": "Attention",
        "high": "Attention",
        "medium": "Warning",
        "low": "Good",
        "warning": "Warning",
        "warn": "Warning",
        "pending": "Warning",
        "in progress": "Accent",
        "approved": "Good",
        "success": "Good",
        "completed": "Good",
        "healthy": "Good",
        "failed": "Attention",
        "error": "Attention",
        "danger": "Attention",
        "info": "Accent",
        "accent": "Accent",
    }

    _CALLOUT_STYLES = {
        "default": "default",
        "emphasis": "emphasis",
        "good": "good",
        "attention": "attention",
        "warning": "warning",
        "accent": "accent",
    }

    def __init__(
        self,
        view_builder: GenericRiskViewBuilder | None = None,
        timing_service: RiskTimingService | None = None,
    ) -> None:
        self.view_builder = view_builder or GenericRiskViewBuilder()
        self.timing_service = timing_service or RiskTimingService()

    def render_notification(self, risk: Mapping[str, Any]) -> dict[str, Any]:
        """Render a full-width risk notification using a shared visual language."""
        risk = self.view_builder.build(risk)
        content = self._new_content()
        body: list[dict[str, Any]] = content["body"]
        sender = self._mapping(risk.get("sender"))
        severity = self._display(risk.get("severity_label")) or self._display(risk.get("severity"))
        risk_id = self._display(risk.get("risk_id")) or ""

        body.append(self._sender_header(sender))
        body.append(self._title_row(self._display(risk.get("title")) or "Risk Alert", severity))
        body.append(self._timing_section(risk))
        self._append_text(body, risk.get("subtitle"), is_subtitle=True, spacing="None")
        self._append_text(body, risk.get("summary"), spacing="Medium")

        views = self._mapping(risk.get("views"))
        self._render_blocks(body, self._mapping(views.get("notification")), risk_id)
        self._render_connected_risks(body, risk)
        body.append(self._footer(risk_id, sender))
        self._append_view_actions(content, views, risk_id)
        return self._card(content)

    def render_view(self, risk: Mapping[str, Any], view_name: str) -> dict[str, Any]:
        """Keep server-side Action.Execute views and the same card styling."""
        risk = self.view_builder.build(risk)
        content = self._new_content()
        body: list[dict[str, Any]] = content["body"]
        views = self._mapping(risk.get("views"))
        view = self._mapping(views.get(view_name))
        self._append_text(body, view.get("title") or view_name.replace("_", " ").title(),
                          weight="Bolder", size="Medium")
        body.append(self._timing_section(risk))
        self._append_text(body, view.get("subtitle"), is_subtitle=True, spacing="None")
        self._render_blocks(body, view, self._display(risk.get("risk_id")) or "")
        return self._card(content)

    @staticmethod
    def _sender_header(sender: Mapping[str, Any]) -> dict[str, Any]:
        name = TeamsRiskCardRenderer._display(sender.get("source")) or TeamsRiskCardRenderer._display(sender.get("name")) or "StratSync RRM"
        time = TeamsRiskCardRenderer._display(sender.get("time")) or TeamsRiskCardRenderer._display(sender.get("timestamp"))
        cols = [{"type": "Column", "width": "stretch", "items": [
            {"type": "TextBlock", "text": name, "weight": "Bolder", "size": "Small",
             "color": "Accent", "wrap": True}]}]
        if time:
            cols.append({"type": "Column", "width": "auto", "items": [
                {"type": "TextBlock", "text": time, "size": "Small", "isSubtle": True,
                 "horizontalAlignment": "Right", "wrap": True}]})
        return {"type": "ColumnSet", "columns": cols}

    @classmethod
    def _title_row(cls, title: str, severity: str | None) -> dict[str, Any]:
        cols = [{"type": "Column", "width": "stretch", "items": [
            {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True}]}]
        if severity:
            cols.append({"type": "Column", "width": "auto", "items": [
                {"type": "TextBlock", "text": severity.upper(), "weight": "Bolder", "size": "Small",
                 "color": cls._severity_color(severity), "horizontalAlignment": "Right", "wrap": True}]})
        return {"type": "ColumnSet", "spacing": "Medium", "columns": cols}

    def _timing_section(self, risk: Mapping[str, Any]) -> dict[str, Any]:
        """Render the shared timing component from a fresh backend calculation."""
        timing = self.timing_service.calculate(
            risk.get("reported_date"), risk.get("reported_timezone")
        )
        return {
            "type": "ColumnSet",
            "spacing": "Small",
            "columns": [
                self._timing_column("REPORTED DATE", timing.reported_date),
                self._timing_column("ELAPSED TIME", timing.elapsed_time),
            ],
        }

    @staticmethod
    def _timing_column(label: str, value: str) -> dict[str, Any]:
        return {
            "type": "Column",
            "width": "stretch",
            "items": [
                {"type": "TextBlock", "text": label, "weight": "Bolder", "size": "Small",
                 "isSubtle": True, "wrap": True, "spacing": "None"},
                {"type": "TextBlock", "text": value, "wrap": True, "spacing": "None"},
            ],
        }

    @staticmethod
    def _footer(risk_id: str, sender: Mapping[str, Any]) -> dict[str, Any]:
        source = TeamsRiskCardRenderer._display(sender.get("source")) or TeamsRiskCardRenderer._display(sender.get("name")) or "StratSync RRM"
        caption = f"{source} · Risk ID {risk_id}" if risk_id else source
        return {"type": "Container", "separator": True, "spacing": "Medium", "items": [
            {"type": "TextBlock", "text": caption, "size": "Small", "isSubtle": True, "wrap": True}]}

    def _render_connected_risks(self, body: list[dict[str, Any]], risk: Mapping[str, Any]) -> None:
        """Show optional associated-risk data; don't fetch or invent extra records."""
        selected = None
        for key in ("connected_risk_overview", "associated_risk_overview", "associated_risks"):
            if key in risk:
                selected = risk.get(key)
                break
        rows: list[dict[str, str]] = []
        if isinstance(selected, list):
            for entry in selected:
                if not isinstance(entry, Mapping):
                    continue
                rows.append({
                    "risk_id": self._display(entry.get("risk_id")) or "—",
                    "risk": self._display(entry.get("risk")) or "—",
                    "severity": self._display(entry.get("severity")) or "—",
                    "primary_exposure": self._display(entry.get("primary_exposure")) or "—",
                })
        elif selected is None:
            own_id = self._display(risk.get("risk_id"))
            for risk_id in dict.fromkeys(x for x in self._as_list(risk.get("associated_risk_ids")) if isinstance(x, str) and x.strip() and x != own_id):
                rows.append({"risk_id": risk_id, "risk": "—", "severity": "—", "primary_exposure": "—"})
        if rows:
            self._render_table(body, {"type": "table", "title": "Connected Risk Overview",
                "columns": [{"key": "risk_id", "label": "Risk ID"}, {"key": "risk", "label": "Risk"},
                            {"key": "severity", "label": "Severity"},
                            {"key": "primary_exposure", "label": "Primary Exposure"}], "rows": rows})

    def _render_blocks(
        self,
        body: list[dict[str, Any]],
        view: Mapping[str, Any],
        risk_id: str,
    ) -> None:
        blocks = view.get("blocks")
        if not isinstance(blocks, list):
            return
        for index, block in enumerate(blocks):
            if not isinstance(block, Mapping):
                self._warn_unknown_block(risk_id, block, index)
                continue
            self._render_block(body, block, risk_id, index)

    @staticmethod
    def _new_content() -> dict[str, Any]:
        return {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            # Teams-specific full-width hint; Teams controls its actual chat canvas.
            "msteams": {"width": "Full"},
            "body": [],
        }

    @staticmethod
    def _card(content: dict[str, Any]) -> dict[str, Any]:
        return {
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": content,
        }

    def _append_view_actions(
        self,
        content: dict[str, Any],
        views: Mapping[str, Any],
        risk_id: str,
    ) -> None:
        if not risk_id:
            return
        actions = []
        for view_name, fallback_label in (
            ("details", "View Details"),
            ("mitigation", "Mitigation Plan"),
        ):
            view = views.get(view_name)
            if not isinstance(view, Mapping) or not self._has_renderable_content(view):
                continue
            label = self._display(view.get("action_label")) or fallback_label
            actions.append({
                "type": "Action.Execute",
                "title": label,
                "verb": "show_risk_view",
                "data": {
                    "action": "show_risk_view",
                    "risk_id": risk_id,
                    "view": view_name,
                },
            })
        if actions:
            content["actions"] = actions

    def _render_block(
        self,
        body: list[dict[str, Any]],
        block: Mapping[str, Any],
        risk_id: str,
        index: int,
    ) -> None:
        block_type = self._display(block.get("type"))
        block_type = (block_type or "").lower().replace("-", "_")
        if block_type == "key_value":
            self._render_key_value(body, block)
        elif block_type == "metrics":
            self._render_metrics(body, block)
        elif block_type == "text":
            self._render_text(body, block)
        elif block_type == "callout":
            self._render_callout(body, block)
        elif block_type == "table":
            self._render_table(body, block)
        elif block_type == "bullet_list":
            self._render_list(body, block, marker="bullet")
        elif block_type == "numbered_list":
            self._render_list(body, block, marker="number")
        elif block_type == "action_list":
            self._render_list(body, block, marker="action")
        elif block_type == "divider":
            self._render_divider(body)
        else:
            self._warn_unknown_block(risk_id, block_type, index)

    def _render_key_value(self, body: list[dict[str, Any]], block: Mapping[str, Any]) -> None:
        facts: list[dict[str, str]] = []
        items = block.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                label = self._display(item.get("label"))
                value = self._display(item.get("value"))
                if value is not None:
                    facts.append({"title": label or "", "value": value})
        if facts:
            self._append_text(body, block.get("title"), weight="Bolder", spacing="Medium")
            body.append({"type": "FactSet", "facts": facts, "spacing": "Small"})

    def _render_metrics(self, body: list[dict[str, Any]], block: Mapping[str, Any]) -> None:
        """Render label/value KPIs as responsive-ish rows, not equal-width label tables.

        Adaptive Cards cannot query the Teams viewport. Three per row is a
        reasonable default; caller can request 1–4 with ``columns``.
        """
        items = [item for item in self._as_list(block.get("items")) if isinstance(item, Mapping)]
        if not items:
            return
        # ``status`` controls the value's colour.  It is never a displayed
        # metric, even when an upstream generic payload includes it in columns.
        specs = [
            (key, label)
            for key, label in self._column_specs(block.get("columns"))
            if key.lower() != "status"
        ]
        if specs:
            self._append_text(body, block.get("title"), weight="Bolder", spacing="Medium")
            # Grid-shaped metric payloads use explicit column definitions.
            body.append(self._column_set([label for _, label in specs], weight="Bolder", spacing="Small"))
            for item in items:
                values = [self._display(item.get(key)) or "—" for key, _ in specs]
                body.append(self._column_set(values, color=self._status_color(item.get("status")), separator=True))
            return

        dynamic_specs = self._dynamic_metric_columns(items)
        if dynamic_specs:
            self._append_text(body, block.get("title"), weight="Bolder", spacing="Medium")
            body.append(self._column_set(
                [label for _, label in dynamic_specs], weight="Bolder", spacing="Small"
            ))
            for item in items:
                values = [self._display(item.get(key)) or "—" for key, _ in dynamic_specs]
                body.append(self._column_set(
                    values, color=self._status_color(item.get("status")), separator=True
                ))
            return

        requested = block.get("columns", 3)
        try:
            per_row = int(requested) if not isinstance(requested, bool) else 3
        except (TypeError, ValueError):
            per_row = 3
        if (isinstance(requested, float) and not requested.is_integer()) or per_row not in (1, 2, 3, 4):
            per_row = 3
        # Four across is possible in a wide Teams canvas, but long labels
        # benefit from three per row. Honor explicit column counts.
        rendered_rows = 0
        for start in range(0, len(items), per_row):
            chunk = items[start:start + per_row]
            columns: list[dict[str, Any]] = []
            for metric in chunk:
                label = self._display(metric.get("label") or metric.get("name") or metric.get("key"))
                value = self._display(metric.get("value"))
                if label is None and value is None:
                    continue
                cell_items: list[dict[str, Any]] = []
                if label:
                    cell_items.append({"type": "TextBlock", "text": label.upper(), "size": "Small",
                                       "isSubtle": True, "wrap": True, "spacing": "None"})
                if value is not None:
                    cell_items.append({"type": "TextBlock", "text": value, "size": "Medium",
                                       "weight": "Bolder", "wrap": True,
                                       "color": self._status_color(metric.get("status")), "spacing": "Small"})
                columns.append({"type": "Column", "width": "stretch", "items": [
                    {"type": "Container", "style": "emphasis", "items": cell_items}
                ]})
            if columns:
                if not rendered_rows:
                    self._append_text(body, block.get("title"), weight="Bolder", spacing="Medium")
                body.append({"type": "ColumnSet", "spacing": "Small", "columns": columns})
                rendered_rows += 1

    @classmethod
    def _dynamic_metric_columns(
        cls, items: list[Mapping[str, Any]],
    ) -> list[tuple[str, str]]:
        """Return arbitrary metric fields that need grid rendering.

        Label/value metrics retain their compact KPI layout.  For every other
        shape, preserve the union of supplied fields in first-seen order while
        treating ``status`` solely as styling metadata.
        """
        standard_keys = {"label", "name", "key", "value", "status"}
        fields: list[str] = []
        for item in items:
            for key in item:
                if not isinstance(key, str) or key.lower() == "status":
                    continue
                if key not in fields:
                    fields.append(key)
        if not fields or set(fields).issubset(standard_keys):
            return []
        return [(key, cls._metric_label(key)) for key in fields]

    @staticmethod
    def _metric_label(key: str) -> str:
        return key.replace("_", " ").strip().title() or key

    def _render_text(self, body: list[dict[str, Any]], block: Mapping[str, Any]) -> None:
        text = self._display(block.get("text")) or self._display(block.get("value")) or self._display(block.get("content"))
        if text is not None:
            self._append_text(body, block.get("title"), weight="Bolder", spacing="Medium")
        if text is not None:
            item: dict[str, Any] = {"type": "TextBlock", "text": text, "wrap": True}
            if block.get("bold") is True:
                item["weight"] = "Bolder"
            if self._display(block.get("status")):
                item["color"] = self._status_color(block.get("status"))
            body.append(item)

    def _render_callout(self, body: list[dict[str, Any]], block: Mapping[str, Any]) -> None:
        """Render a concise, host-safe highlighted message container."""
        title = self._display(block.get("title"))
        text = (self._display(block.get("text")) or self._display(block.get("message"))
                or self._display(block.get("value")) or self._display(block.get("content")))
        if title is None and text is None:
            return
        items: list[dict[str, Any]] = []
        if title is not None:
            items.append({"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True})
        if text is not None:
            message: dict[str, Any] = {"type": "TextBlock", "text": text, "wrap": True}
            color = self._status_color(block.get("status") or block.get("color"))
            if color != "Default":
                message["color"] = color
            items.append(message)
        body.append({
            "type": "Container",
            "style": self._callout_style(block),
            "spacing": "Medium",
            "items": items,
        })

    def _render_table(self, body: list[dict[str, Any]], block: Mapping[str, Any]) -> None:
        """Render every generic table as a real Adaptive Cards 1.5 Table.

        The declared columns determine their order. No number-of-columns or
        cell-length condition turns a table into a FactSet or a list.
        """
        columns = self._column_specs(block.get("columns"))
        raw_rows = self._as_list(block.get("rows"))
        if not columns or not raw_rows:
            return

        table_rows: list[dict[str, Any]] = [
            {
                "type": "TableRow",
                "cells": [self._table_cell(label, header=True) for _, label in columns],
            }
        ]
        for row in raw_rows:
            if isinstance(row, Mapping):
                values = [self._display(row.get(key)) or "—" for key, _ in columns]
            elif isinstance(row, (list, tuple)):
                values = [self._display(row[i]) or "—" if i < len(row) else "—"
                          for i in range(len(columns))]
            else:
                continue
            table_rows.append({
                "type": "TableRow",
                "cells": [self._table_cell(value) for value in values],
            })

        if len(table_rows) == 1:
            return
        self._append_text(body, block.get("title"), weight="Bolder", spacing="Medium")
        body.append({
            "type": "Table",
            "spacing": "Small",
            "firstRowAsHeader": True,
            "showGridLines": True,
            "gridStyle": "default",
            "columns": [
                {"width": self._table_column_weight(key, label, raw_rows)}
                for key, label in columns
            ],
            "rows": table_rows,
        })

    @staticmethod
    def _table_cell(value: str, *, header: bool = False) -> dict[str, Any]:
        text: dict[str, Any] = {
            "type": "TextBlock",
            "text": value,
            "wrap": True,
            "size": "Small",
            "spacing": "None",
        }
        if header:
            text["weight"] = "Bolder"
        cell: dict[str, Any] = {"type": "TableCell", "items": [text]}
        if header:
            cell["style"] = "emphasis"
        return cell

    @classmethod
    def _table_column_weight(
        cls, key: str, label: str, rows: list[Any]
    ) -> int:
        """Allocate relative width based on sampled content, not field names.

        Teams still owns the viewport. Long values wrap within their cells;
        their columns are never dropped or converted to a different layout.
        """
        lengths = [len(label)]
        for row in rows[:20]:
            if isinstance(row, Mapping):
                value = cls._display(row.get(key))
                if value is not None:
                    lengths.append(len(value))
        maximum = max(lengths)
        if maximum > 35:
            return 4
        if maximum > 18:
            return 3
        if maximum > 10:
            return 2
        return 1

    def _render_list(
        self, body: list[dict[str, Any]], block: Mapping[str, Any], *, marker: str
    ) -> None:
        entries = []
        for position, item in enumerate(self._as_list(block.get("items")), start=1):
            text, order = self._list_item(item)
            if text is None:
                continue
            if marker == "bullet":
                prefix = "• "
            elif marker == "number":
                prefix = f"{order or position}. "
            else:
                prefix = f"{order or position}. "
            status = item.get("status") if isinstance(item, Mapping) else None
            entry: dict[str, Any] = {
                "type": "TextBlock", "text": prefix + text, "wrap": True, "spacing": "Small"
            }
            if marker == "action":
                entry["weight"] = "Bolder"
            if status is not None:
                entry["color"] = self._status_color(status)
            entries.append(entry)
            if marker == "action" and isinstance(item, Mapping):
                context = []
                if self._display(item.get("owner")):
                    context.append(f"Owner: {self._display(item.get('owner'))}")
                if self._display(item.get("status")):
                    context.append(f"Status: {self._display(item.get('status'))}")
                if context:
                    entries.append({"type": "TextBlock", "text": "  ·  ".join(context),
                                    "size": "Small", "isSubtle": True, "wrap": True,
                                    "spacing": "None"})
        if entries:
            self._append_text(body, block.get("title"), weight="Bolder", spacing="Medium")
            body.extend(entries)

    @staticmethod
    def _render_divider(body: list[dict[str, Any]]) -> None:
        # Adaptive Cards have no standalone divider element. A blank
        # TextBlock with a separator is broadly supported by Teams hosts.
        body.append({"type": "TextBlock", "text": " ", "separator": True, "spacing": "Medium"})

    @staticmethod
    def _column_specs(columns: Any) -> list[tuple[str, str]]:
        if not isinstance(columns, list):
            return []
        result = []
        for column in columns:
            if isinstance(column, Mapping):
                key = TeamsRiskCardRenderer._display(column.get("key"))
                label = TeamsRiskCardRenderer._display(column.get("label"))
                if key:
                    result.append((key, label or key))
            elif isinstance(column, str) and column.strip():
                result.append((column.strip(), column.strip()))
        return result

    @staticmethod
    def _column_set(
        values: list[str],
        *,
        weight: str | None = None,
        weight_indexes: set[int] | None = None,
        color: str | None = None,
        spacing: str | None = None,
        separator: bool = False,
    ) -> dict[str, Any]:
        columns = []
        for index, value in enumerate(values):
            text_block: dict[str, Any] = {"type": "TextBlock", "text": value, "wrap": True}
            if weight or (weight_indexes is not None and index in weight_indexes):
                text_block["weight"] = weight
                if weight is None:
                    text_block["weight"] = "Bolder"
            if color and color != "Default":
                text_block["color"] = color
            columns.append({"type": "Column", "width": "stretch", "items": [text_block]})
        result: dict[str, Any] = {"type": "ColumnSet", "columns": columns}
        if spacing:
            result["spacing"] = spacing
        if separator:
            result["separator"] = True
            result["spacing"] = result.get("spacing", "Small")
        return result

    @staticmethod
    def _append_text(
        body: list[dict[str, Any]],
        value: Any,
        *,
        weight: str | None = None,
        size: str | None = None,
        wrap: bool = True,
        is_subtitle: bool = False,
        spacing: str | None = None,
    ) -> None:
        text = TeamsRiskCardRenderer._display(value)
        if text is None:
            return
        item: dict[str, Any] = {"type": "TextBlock", "text": text, "wrap": wrap}
        if weight:
            item["weight"] = weight
        if size:
            item["size"] = size
        if is_subtitle:
            item["isSubtle"] = True
        if spacing:
            item["spacing"] = spacing
        body.append(item)

    def _has_renderable_content(self, view: Mapping[str, Any]) -> bool:
        """Return whether a view contains information, rather than just structure."""
        blocks = view.get("blocks")
        return isinstance(blocks, list) and any(
            isinstance(block, Mapping) and self._block_has_content(block) for block in blocks
        )
    def _block_has_content(self, block: Mapping[str, Any]) -> bool:
        block_type = self._display(block.get("type"))
        block_type = (block_type or "").lower().replace("-", "_")
        if block_type == "text":
            return any(self._display(block.get(key)) is not None for key in ("text", "value", "content"))
        if block_type == "callout":
            return any(self._display(block.get(key)) is not None for key in ("title", "text", "message", "value", "content"))
        if block_type == "key_value":
            return any(
                isinstance(item, Mapping) and self._display(item.get("value")) is not None
                for item in self._as_list(block.get("items"))
            )
        if block_type == "metrics":
            return any(
                isinstance(item, Mapping)
                and any(
                    (not isinstance(key, str) or key.lower() != "status")
                    and self._display(value) is not None
                    for key, value in item.items()
                )
                for item in self._as_list(block.get("items"))
            )
        if block_type == "table":
            columns = self._column_specs(block.get("columns"))
            return bool(columns) and any(
                (isinstance(row, Mapping) and any(self._display(row.get(key)) is not None for key, _ in columns))
                or (isinstance(row, (list, tuple)) and any(self._display(v) is not None for v in row))
                for row in self._as_list(block.get("rows"))
            )
        if block_type in {"bullet_list", "numbered_list", "action_list"}:
            return any(self._list_item(item)[0] is not None for item in self._as_list(block.get("items")))
        # A divider is presentational only and must not enable an empty view.
        return False

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        return value if isinstance(value, list) else []

    def _callout_style(self, block: Mapping[str, Any]) -> str:
        explicit_style = self._display(block.get("style"))
        if explicit_style:
            style = self._CALLOUT_STYLES.get(explicit_style.lower())
            if style:
                return style
        status = self._status_color(block.get("status") or block.get("color")).lower()
        return self._CALLOUT_STYLES.get(status, "emphasis")

    @staticmethod
    def _list_item(item: Any) -> tuple[str | None, str | None]:
        if isinstance(item, Mapping):
            text = (TeamsRiskCardRenderer._display(item.get("text"))
                    or TeamsRiskCardRenderer._display(item.get("title"))
                    or TeamsRiskCardRenderer._display(item.get("description"))
                    or TeamsRiskCardRenderer._display(item.get("value"))
                    or TeamsRiskCardRenderer._display(item.get("action")))
            return text, TeamsRiskCardRenderer._display(item.get("order"))
        return TeamsRiskCardRenderer._display(item), None

    @classmethod
    def _severity_color(cls, value: str) -> str:
        return cls._SEVERITY_COLORS.get(value.strip().lower(), "Default")

    @classmethod
    def _status_color(cls, value: Any) -> str:
        return cls._SEVERITY_COLORS.get(str(value).strip().lower(), "Default")

    @staticmethod
    def _mapping(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _display(value: Any) -> str | None:
        if value is None or isinstance(value, (Mapping, list, tuple, set)):
            return None
        if isinstance(value, (str, int, float, bool)):
            text = str(value).strip()
            return text or None
        return None

    @staticmethod
    def _warn_unknown_block(risk_id: str, block_type: Any, block_index: int) -> None:
        logger.warning(
            "unsupported_risk_card_block",
            extra={
                "risk_id": risk_id,
                "block_type": TeamsRiskCardRenderer._display(block_type) or "unknown",
                "block_index": block_index,
            },
        )
