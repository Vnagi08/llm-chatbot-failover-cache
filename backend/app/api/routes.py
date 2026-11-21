"""HTTP routes: /health, /chat, /sessions/{session_id}/messages."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, Path, Request
from fastapi.responses import JSONResponse

from app.llm.cache import cache_is_enabled
from app.llm.errors import ALL_PROVIDERS_UNAVAILABLE, AllProvidersUnavailableError, ProviderRequestRejectedError
from app.llm.simulation import InvalidSimulationHeaderError, parse_failure_header
from app.schemas import ChatRequest, ChatResponse, ErrorResponse, HealthResponse, HistoryResponse
from app.services.chat_service import ChatService, SessionNotFoundError

logger = logging.getLogger(__name__)
router = APIRouter()


def _service(request: Request) -> ChatService:
    return request.app.state.chat_service


@router.get("/health", response_model=HealthResponse, responses={503: {"model": ErrorResponse}})
async def health(request: Request):
    service = _service(request)
    db_ok = await service.db.ping()
    keys = service.llm.keys_configured
    cache_on = cache_is_enabled()
    if db_ok and keys >= 2 and cache_on:
        return HealthResponse(status="ok", keys_configured=keys, cache_enabled=cache_on)

    problems = []
    if not db_ok:
        problems.append("database unreachable")
    if keys < 2:
        problems.append("at least two API keys required")
    if not cache_on:
        problems.append("cache disabled")
    return JSONResponse(
        status_code=503,
        content={
            "detail": "; ".join(problems),
            "status": "degraded",
            "keys_configured": keys,
            "cache_enabled": cache_on,
        },
    )


@router.post(
    "/chat",
    response_model=ChatResponse,
    responses={400: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def chat(
    body: ChatRequest,
    request: Request,
    x_simulate_failure: str | None = Header(default=None, alias="X-Simulate-Failure"),
):
    failure_plan = None
    if x_simulate_failure is not None:
        if request.app.state.settings.enable_failure_simulation:
            try:
                failure_plan = parse_failure_header(x_simulate_failure)
            except InvalidSimulationHeaderError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
        else:
            logger.info("X-Simulate-Failure header ignored (ENABLE_FAILURE_SIMULATION=false)")

    try:
        return await _service(request).chat(
            session_id=body.session_id,
            message=body.message,
            system_prompt=body.system_prompt,
            failure_plan=failure_plan,
        )
    except AllProvidersUnavailableError:
        raise HTTPException(status_code=503, detail=ALL_PROVIDERS_UNAVAILABLE) from None
    except ProviderRequestRejectedError:
        raise HTTPException(status_code=502, detail="provider rejected the request") from None


@router.get(
    "/sessions/{session_id}/messages",
    response_model=HistoryResponse,
    response_model_exclude_none=True,
    responses={404: {"model": ErrorResponse}},
)
async def session_messages(request: Request, session_id: str = Path(..., min_length=1, max_length=128)):
    try:
        messages = await _service(request).history(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="session not found") from None
    return HistoryResponse(session_id=session_id, messages=messages)
