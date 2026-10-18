from types import SimpleNamespace

import pytest
from botbuilder.core.teams import TeamsActivityHandler
from fastapi.testclient import TestClient

import src.application as application_module
from src.api.dependencies import Container
from src.application import create_app
from src.bot.handlers.risk_bot import RiskBot


class RecordingAdapter:
    def __init__(self) -> None:
        self.calls = []

    async def process_activity(self, auth_header, activity, callback):
        self.calls.append((auth_header, activity, callback))
        return None


class StubBot:
    async def on_turn(self, turn_context):
        return None


class PermissionDeniedAdapter:
    async def process_activity(self, auth_header, activity, callback):
        raise PermissionError("Required Authorization token was not supplied")


class InvokeAdapter:
    async def process_activity(self, auth_header, activity, callback):
        return SimpleNamespace(status=200, body={"response": "ok"})


def _messages_app(adapter):
    app = create_app()
    app.state.container = Container(adapter=adapter, bot=StubBot())
    return app


def _valid_activity():
    return {
        "type": "message",
        "id": "activity-1",
        "channelId": "msteams",
        "serviceUrl": "https://smba.trafficmanager.net/emea/",
        "conversation": {"id": "conversation-1"},
        "from": {"id": "user-1"},
        "recipient": {"id": "bot-1"},
        "text": "hello",
    }


def test_health_and_readiness_are_dependency_free():
    client = TestClient(create_app())

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}


def test_application_route_inventory_is_minimal():
    app = create_app()
    application_routes = {
        (method, route.path)
        for route in app.routes
        for method in (route.methods or set())
        if route.path in {"/health", "/ready", "/api/messages"}
    }

    assert application_routes == {
        ("GET", "/health"),
        ("GET", "/ready"),
        ("POST", "/api/messages"),
    }


def test_messages_route_deserializes_and_delegates_with_authorization_header():
    adapter = RecordingAdapter()
    bot = StubBot()
    app = create_app()
    app.state.container = Container(adapter=adapter, bot=bot)
    client = TestClient(app)

    response = client.post(
        "/api/messages",
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
        json=_valid_activity(),
    )

    assert response.status_code == 201
    assert len(adapter.calls) == 1
    auth_header, activity, callback = adapter.calls[0]
    assert auth_header == "Bearer test-token"
    assert activity.type == "message"
    assert activity.id == "activity-1"
    assert callback.__self__ is bot


def test_messages_route_maps_missing_authorization_to_unauthorized():
    client = TestClient(_messages_app(PermissionDeniedAdapter()))

    response = client.post("/api/messages", json=_valid_activity())

    assert response.status_code == 401
    assert response.json() == {
        "success": False,
        "data": None,
        "error": {
            "code": "UNAUTHORIZED",
            "message": "Bot Framework authentication failed.",
        },
    }


def test_messages_route_maps_adapter_permission_error_to_unauthorized():
    client = TestClient(_messages_app(PermissionDeniedAdapter()))

    response = client.post(
        "/api/messages",
        headers={"Authorization": "Bearer invalid-token"},
        json=_valid_activity(),
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.parametrize(
    ("content", "json_body"),
    [
        ("{not-json", None),
        (None, {}),
        (None, None),
        (None, []),
        (None, {"id": "activity-without-type"}),
        (None, {"type": ""}),
    ],
)
def test_messages_route_rejects_malformed_or_invalid_activity_bodies(content, json_body):
    adapter = RecordingAdapter()
    client = TestClient(_messages_app(adapter))
    request = {
        "headers": {"Authorization": "Bearer test-token", "Content-Type": "application/json"},
    }
    if content is not None:
        request["content"] = content
    else:
        request["json"] = json_body

    response = client.post("/api/messages", **request)

    assert response.status_code == 400
    assert response.json() == {
        "success": False,
        "data": None,
        "error": {
            "code": "INVALID_ACTIVITY",
            "message": "Request body must be a valid Bot Framework activity.",
        },
    }
    assert adapter.calls == []


def test_messages_route_preserves_invoke_response():
    client = TestClient(_messages_app(InvokeAdapter()))

    response = client.post(
        "/api/messages",
        headers={"Authorization": "Bearer test-token"},
        json=_valid_activity(),
    )

    assert response.status_code == 200
    assert response.json() == {"response": "ok"}


@pytest.mark.asyncio
async def test_lifespan_wires_adapter_and_minimal_bot(monkeypatch):
    adapter = RecordingAdapter()
    monkeypatch.setattr(application_module, "create_adapter", lambda settings: adapter)
    app = create_app()

    async with app.router.lifespan_context(app):
        assert app.state.container.adapter is adapter
        assert isinstance(app.state.container.bot, RiskBot)


@pytest.mark.asyncio
async def test_risk_bot_delegates_to_sdk_dispatch(monkeypatch):
    calls = []

    async def sdk_on_turn(self, turn_context):
        calls.append(turn_context)

    monkeypatch.setattr(TeamsActivityHandler, "on_turn", sdk_on_turn)
    context = SimpleNamespace(
        activity=SimpleNamespace(type="conversationUpdate", id="activity-2")
    )

    await RiskBot().on_turn(context)

    assert calls == [context]
