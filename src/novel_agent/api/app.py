"""FastAPI application exposing only the frozen Stage 0 operational contract."""

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from novel_agent import __version__


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    status: str
    service: str
    version: str


def create_app() -> FastAPI:
    application = FastAPI(
        title="Novel Agent",
        version=__version__,
        description="Stage 0 operational API; no creative capabilities are exposed.",
    )

    @application.get(
        "/health",
        response_model=HealthResponse,
        operation_id="stage0_health",
        tags=["operations"],
    )
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", service="novel-agent", version=__version__)

    return application


app = create_app()
