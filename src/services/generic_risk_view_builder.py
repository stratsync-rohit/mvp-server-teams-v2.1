"""Adapt arbitrary risk JSON into renderer-neutral Adaptive Card view blocks."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)


class GenericRiskViewBuilder:
    """Build bounded generic notification/details views without domain field rules."""

    max_depth = 5
    max_items = 25
    max_fields = 30
    max_text_length = 1_000
    max_notification_blocks = 8

    _SYSTEM_FIELDS = {"risk_id", "views", "is_active", "_id"}

    def build(self, risk: Mapping[str, Any]) -> dict[str, Any]:
        """Return a risk copy with explicit views preserved and generic gaps filled."""
        result = dict(risk)
        explicit_views = self._mapping(risk.get("views"))
        views = dict(explicit_views)
        data = {key: value for key, value in risk.items() if key not in self._SYSTEM_FIELDS}

        details = self._valid_view(views.get("details"))
        if details is None:
            details = {
                "title": self._text(risk.get("title")) or "Risk Details",
                "blocks": self._blocks_for_mapping(data, title=None, depth=0, seen=set()),
            }
            views["details"] = details

        if self._valid_view(views.get("notification")) is None:
            detail_blocks = details.get("blocks") if isinstance(details, Mapping) else []
            views["notification"] = {
                "blocks": self._compact_notification_blocks(detail_blocks),
            }

        result["views"] = views
        return result

    def _blocks_for_mapping(
        self,
        value: Mapping[str, Any],
        *,
        title: str | None,
        depth: int,
        seen: set[int],
    ) -> list[dict[str, Any]]:
        if id(value) in seen:
            self._truncated("circular value", title)
            return [self._text_block(title, "Circular value omitted")]
        if depth > self.max_depth:
            self._truncated("maximum nesting depth", title)
            return [self._text_block(title, "Additional nested content omitted")]

        seen.add(id(value))
        try:
            entries = list(value.items())
            if len(entries) > self.max_fields:
                self._truncated("mapping fields", title)
                entries = entries[:self.max_fields]

            blocks: list[dict[str, Any]] = []
            scalar_items = []
            for key, item in entries:
                if self._is_meaningful_scalar(item):
                    scalar_items.append({"label": self.label(key), "value": self._scalar(item)})
            if scalar_items:
                block: dict[str, Any] = {"type": "key_value", "items": scalar_items}
                if title:
                    block["title"] = title
                blocks.append(block)

            for key, item in entries:
                if self._is_meaningful_scalar(item):
                    continue
                section_title = self.label(key)
                if isinstance(item, Mapping):
                    blocks.extend(self._blocks_for_mapping(
                        item, title=section_title, depth=depth + 1, seen=seen
                    ))
                elif isinstance(item, (list, tuple)):
                    block = self._array_block(item, section_title, depth + 1, seen)
                    if block is not None:
                        blocks.append(block)
            if len(value) > self.max_fields:
                blocks.append(self._text_block(None, "Additional fields omitted"))
            return blocks
        finally:
            seen.discard(id(value))

    def _array_block(
        self, values: list[Any] | tuple[Any, ...], title: str, depth: int, seen: set[int]
    ) -> dict[str, Any] | None:
        if not values:
            return None
        if id(values) in seen:
            self._truncated("circular value", title)
            return self._text_block(title, "Circular value omitted")
        items = list(values)
        truncated = len(items) > self.max_items
        if truncated:
            self._truncated("array items", title)
            items = items[:self.max_items]

        seen.add(id(values))
        try:
            if all(self._is_meaningful_scalar(item) for item in items):
                return {
                    "type": "action_list",
                    "title": title,
                    "items": [
                        {"order": index + 1, "title": self._scalar(item)}
                        for index, item in enumerate(items)
                    ],
                }
            if all(isinstance(item, Mapping) for item in items):
                return self._table_block(items, title)

            rendered = []
            for index, item in enumerate(items):
                if self._is_meaningful_scalar(item):
                    rendered.append({"order": index + 1, "title": self._scalar(item)})
                elif isinstance(item, Mapping) or isinstance(item, (list, tuple)):
                    rendered.append({
                        "order": index + 1,
                        "title": self._compact_value(item, depth, seen),
                    })
            if truncated:
                rendered.append({"title": "Additional items omitted"})
            return {"type": "action_list", "title": title, "items": rendered} if rendered else None
        finally:
            seen.discard(id(values))

    def _table_block(self, rows: list[Mapping[str, Any]], title: str) -> dict[str, Any]:
        keys: list[str] = []
        for row in rows:
            for key in row:
                if key not in keys and len(keys) < self.max_fields:
                    keys.append(str(key))
        return {
            "type": "table",
            "title": title,
            "columns": [{"key": key, "label": self.label(key)} for key in keys],
            "rows": [
                {key: self._compact_value(row.get(key), 0, set()) for key in keys}
                for row in rows
            ],
        }

    def _compact_notification_blocks(self, blocks: Any) -> list[dict[str, Any]]:
        if not isinstance(blocks, list):
            return []
        if len(blocks) <= self.max_notification_blocks:
            return list(blocks)
        self._truncated("notification blocks", None)
        return [*blocks[:self.max_notification_blocks], self._text_block(None, "View Details for all risk data")]

    def _compact_value(self, value: Any, depth: int, seen: set[int]) -> str:
        if self._is_meaningful_scalar(value):
            return self._scalar(value)
        if isinstance(value, Mapping):
            if id(value) in seen or depth >= self.max_depth:
                return "[nested value omitted]"
            seen.add(id(value))
            try:
                pairs = [
                    f"{self.label(key)}: {self._compact_value(item, depth + 1, seen)}"
                    for key, item in list(value.items())[:self.max_fields]
                    if item is not None
                ]
                return "; ".join(pairs) or ""
            finally:
                seen.discard(id(value))
        if isinstance(value, (list, tuple)):
            if id(value) in seen or depth >= self.max_depth:
                self._truncated("circular or deeply nested array", None)
                return "[nested value omitted]"
            seen.add(id(value))
            try:
                return ", ".join(
                    self._compact_value(item, depth + 1, seen)
                    for item in value[:self.max_items]
                )
            finally:
                seen.discard(id(value))
        return ""

    @classmethod
    def label(cls, key: Any) -> str:
        """Convert snake_case, kebab-case and camelCase keys into readable labels."""
        text = str(key).strip()
        text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
        text = re.sub(r"[_-]+", " ", text)
        return " ".join(part.capitalize() for part in text.split()) or "Value"

    def _scalar(self, value: Any) -> str:
        if isinstance(value, bool):
            text = "True" if value else "False"
        else:
            text = str(value).strip()
        if len(text) > self.max_text_length:
            self._truncated("text value", None)
            return f"{text[:self.max_text_length]}…"
        return text

    @staticmethod
    def _is_meaningful_scalar(value: Any) -> bool:
        return isinstance(value, (str, int, float, bool)) and not (
            isinstance(value, str) and not value.strip()
        )

    @staticmethod
    def _mapping(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _valid_view(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, Mapping) or not isinstance(value.get("blocks"), list):
            return None
        return dict(value)

    @staticmethod
    def _text(value: Any) -> str | None:
        return str(value).strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _text_block(title: str | None, text: str) -> dict[str, Any]:
        block: dict[str, Any] = {"type": "text", "text": text}
        if title:
            block["title"] = title
        return block

    @staticmethod
    def _truncated(reason: str, section: str | None) -> None:
        logger.info("generic_risk_content_truncated", extra={"reason": reason, "section": section})
