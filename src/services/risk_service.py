"""Application service for loading safe risk data."""

from __future__ import annotations

import secrets
import string
from typing import Any

from src.exceptions import AppError
from src.repositories.risk_repository import RiskRepository
from src.schemas.risk import RiskCreateRequest, RiskDocument


_RISK_ID_ALPHABET = string.ascii_uppercase + string.digits


class RiskService:
    """Load and create active risks without coupling callers to MongoDB."""

    def __init__(self, repository: RiskRepository) -> None:
        self.repository = repository

    async def list(self) -> list[dict[str, Any]]:
        """Normalize and return active risk documents."""
        documents = await self.repository.list_active()
        return [
            RiskDocument.model_validate(document).as_mapping()
            for document in documents
        ]

    async def get(self, risk_id: str) -> dict[str, Any]:
        """Normalize, validate, and return one active risk document."""
        normalized_risk_id = risk_id.strip() if isinstance(risk_id, str) else ""
        if not normalized_risk_id:
            raise AppError("RISK_ID_INVALID", "risk_id must be a non-empty string.", 422)

        document = await self.repository.get_by_risk_id(normalized_risk_id)
        if document is None or document.get("is_active") is not True:
            raise AppError("RISK_NOT_FOUND", "The active risk was not found.", 404)

        safe_document = dict(document)
        safe_document.pop("_id", None)
        return RiskDocument.model_validate(safe_document).as_mapping()

    async def create(self, payload: RiskCreateRequest) -> dict[str, Any]:
        """Create one active risk using a supplied or collision-free logical ID."""
        document = payload.as_document()
        requested_risk_id = document.pop("risk_id", None)
        if requested_risk_id is not None:
            existing = await self.repository.get_any_by_risk_id(requested_risk_id)
            if existing is not None:
                raise AppError("RISK_ALREADY_EXISTS", "A risk with this risk_id already exists.", 409)
            risk_id = requested_risk_id
        else:
            risk_id = await self._generate_risk_id()

        # Creation is an active lifecycle operation; inactive documents are
        # retained only through other lifecycle behavior.
        document["risk_id"] = risk_id
        document["is_active"] = True
        try:
            created = await self.repository.create(document)
        except Exception as exc:
            if exc.__class__.__name__ == "DuplicateKeyError":
                raise AppError(
                    "RISK_ALREADY_EXISTS", "A risk with this risk_id already exists.", 409
                ) from None
            raise
        return RiskDocument.model_validate(created).as_mapping()

    async def _generate_risk_id(self) -> str:
        """Generate a project-format risk ID that is not already persisted."""
        for _ in range(10):
            risk_id = "RSK-{}-{}".format(
                "".join(secrets.choice(_RISK_ID_ALPHABET) for _ in range(4)),
                "".join(secrets.choice(_RISK_ID_ALPHABET) for _ in range(4)),
            )
            if await self.repository.get_any_by_risk_id(risk_id) is None:
                return risk_id
        raise AppError("RISK_ID_GENERATION_FAILED", "Could not generate a unique risk_id.", 503)
