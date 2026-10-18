"""Authenticated Microsoft Bot Framework activity endpoint."""

import logging
from collections.abc import Mapping

from botbuilder.schema import Activity
from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from src.exceptions import AppError
from src.bot.diagnostics.activity_logger import (
    activity_metadata,
    log_activity,
    log_teams_debug,
    log_teams_debug_raw,
    sanitized_snapshot,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["bot"])


def _invalid_activity_error() -> AppError:
    """Return the safe public error for an unusable activity payload."""
    return AppError(
        "INVALID_ACTIVITY",
        "Request body must be a valid Bot Framework activity.",
        status.HTTP_400_BAD_REQUEST,
    )


@router.post("/api/messages")
async def bot_messages(request: Request) -> Response:
    """Deserialize and authenticate one Bot Framework activity."""
    content_type = request.headers.get("content-type", "")
    log_teams_debug(
        "/api/messages request received",
        request_method=request.method,
        request_path=request.url.path,
        content_type=content_type,
        authorization_present=bool(request.headers.get("authorization")),
    )
    if "application/json" not in request.headers.get("content-type", "").lower():
        raise AppError("UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json.", 415)

    try:
        body = await request.json()
    except (UnicodeDecodeError, ValueError):
        logger.warning("bot_activity_invalid", extra={"reason": "malformed_json"})
        raise _invalid_activity_error() from None

    log_teams_debug_raw("incoming activity payload", body)

    if not isinstance(body, Mapping) or not isinstance(body.get("type"), str) or not body["type"].strip():
        logger.warning("bot_activity_invalid", extra={"reason": "invalid_shape"})
        raise _invalid_activity_error()

    try:
        activity = Activity().deserialize(body)
    except (AttributeError, KeyError, TypeError, ValueError):
        logger.warning("bot_activity_invalid", extra={"reason": "deserialization_failed"})
        raise _invalid_activity_error() from None

    metadata = activity_metadata(activity)
    log_teams_debug("activity parsed", **metadata)
    log_teams_debug_raw("deserialized activity", activity, **metadata)

    settings = getattr(request.app.state, "settings", None)
    log_activity(body, debug=bool(getattr(settings, "teams_debug_activity_logging", False)))
    auth_header = request.headers.get("authorization", "")
    container = request.app.state.container
    try:
        log_teams_debug("adapter processing start", **metadata)
        invoke_response = await container.adapter.process_activity(
            auth_header, activity, container.bot.on_turn
        )
    except PermissionError:
        log_teams_debug("adapter authentication failed", **metadata)
        logger.warning(
            "bot_framework_authentication_failed",
            extra={"activity_id": activity.id, "activity_type": activity.type},
        )
        raise AppError(
            "UNAUTHORIZED",
            "Bot Framework authentication failed.",
            status.HTTP_401_UNAUTHORIZED,
        ) from None
    except Exception:
        logger.exception(
            "bot_activity_failed",
            extra={"activity_id": activity.id, "activity_type": activity.type},
        )
        raise
    invoke_summary = {
        "result_is_none": invoke_response is None,
        "result_type": type(invoke_response).__name__ if invoke_response is not None else None,
        "invoke_response_status": getattr(invoke_response, "status", None),
        "invoke_response_body": getattr(invoke_response, "body", None),
    }
    log_teams_debug(
        "adapter processing success",
        **metadata,
        invoke_response_present=invoke_response is not None,
        invoke_response_status=invoke_summary["invoke_response_status"],
    )
    log_teams_debug_raw("adapter result", sanitized_snapshot(invoke_summary), **metadata)
    if invoke_response:
        return JSONResponse(
            status_code=invoke_response.status,
            content=invoke_response.body,
        )
    return Response(status_code=status.HTTP_201_CREATED)
