"""Public risk APIs."""

from fastapi import APIRouter, Request, status

from src.schemas.risk import RiskCreateRequest, RiskDocument

router = APIRouter(tags=["risks"])


@router.post("/api/risks", response_model=RiskDocument, status_code=status.HTTP_201_CREATED)
async def create_risk(payload: RiskCreateRequest, request: Request) -> RiskDocument:
    """Create and return one active risk document."""
    return await request.app.state.container.risk_service.create(payload)


@router.get("/api/risks", response_model=list[RiskDocument])
async def list_risks(request: Request) -> list[RiskDocument]:
    """Return active risks from the database."""
    return await request.app.state.container.risk_service.list()


@router.get("/api/risks/{risk_id}", response_model=RiskDocument)
async def get_risk(risk_id: str, request: Request) -> RiskDocument:
    """Return one active risk by its logical risk ID."""
    return await request.app.state.container.risk_service.get(risk_id)
