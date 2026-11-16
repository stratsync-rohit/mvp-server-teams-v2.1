"""Flexible domain contracts for evolving risk documents."""

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.services.risk_timing import RiskTimingService


_RISK_ID_PATTERN = re.compile(r"^RSK-[A-Z0-9]{4}-[A-Z0-9]{4}$")


class RiskDocument(BaseModel):
    """Require only identity while preserving unknown risk fields."""

    model_config = ConfigDict(extra="allow")

    risk_id: str

    def as_mapping(self) -> dict[str, Any]:
        """Return the safe, schema-evolution-friendly risk mapping."""
        return self.model_dump()


class RiskCreateRequest(BaseModel):
    """Input for a new active risk while preserving supported extra fields."""

    model_config = ConfigDict(extra="allow")

    title: str = Field(min_length=1)
    risk_id: str | None = None
    is_active: bool = True
    reported_date: datetime
    reported_timezone: str

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: object) -> str:
        """Require a usable risk title without coercing arbitrary values."""
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string")
        return value.strip()

    @field_validator("risk_id", mode="before")
    @classmethod
    def normalize_risk_id(cls, value: object) -> str | None:
        """Accept an optional existing-format logical risk identifier."""
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty RSK-XXXX-XXXX identifier")
        risk_id = value.strip()
        if not _RISK_ID_PATTERN.fullmatch(risk_id):
            raise ValueError("must match the RSK-XXXX-XXXX format")
        return risk_id

    @field_validator("reported_date")
    @classmethod
    def normalize_reported_date(cls, value: datetime) -> datetime:
        """Store a creator-provided, timezone-aware reported instant as UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a timezone offset")
        normalized = value.astimezone(timezone.utc)
        if normalized > datetime.now(timezone.utc):
            raise ValueError("must not be in the future")
        return normalized

    @field_validator("reported_timezone", mode="before")
    @classmethod
    def validate_reported_timezone(cls, value: object) -> str:
        """Accept only a creator-selected IANA timezone identifier."""
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty IANA timezone")
        timezone_name = value.strip()
        if RiskTimingService.resolve_timezone(timezone_name) is None:
            raise ValueError("must be a valid IANA timezone")
        return timezone_name

    def as_document(self) -> dict[str, Any]:
        """Return the flexible persistence document supplied by the client."""
        document = self.model_dump(exclude_none=True)
        # Elapsed time is always calculated when a card is rendered. Preserve
        # arbitrary generic fields, but never persist a client-supplied value
        # that could become stale or be treated as authoritative.
        document.pop("elapsed_time", None)
        return document
