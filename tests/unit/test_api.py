import asyncio

from httpx import ASGITransport, AsyncClient, Response

from novel_agent import __version__
from novel_agent.api import create_app


def test_stage0_api_exposes_only_versioned_health_contract() -> None:
    async def exercise_api() -> tuple[Response, Response]:
        transport = ASGITransport(app=create_app())
        async with AsyncClient(transport=transport, base_url="http://stage0.test") as client:
            return await client.get("/health"), await client.get("/openapi.json")

    response, openapi_response = asyncio.run(exercise_api())

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "novel-agent",
        "version": __version__,
    }
    schema = openapi_response.json()
    assert schema["info"]["version"] == __version__
    assert set(schema["paths"]) == {"/health"}
