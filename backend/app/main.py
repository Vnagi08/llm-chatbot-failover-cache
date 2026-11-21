"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes import router
from app.config import Settings
from app.db import Database
from app.llm.cache import configure_cache, prewarm_cache
from app.llm.client import AcompletionFn, FailoverLLMClient
from app.logging_config import configure_logging
from app.services.chat_service import ChatService

logger = logging.getLogger("app")


def create_app(settings: Settings | None = None, acompletion: AcompletionFn | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level, settings.api_keys)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.database_path)
        await db.init()
        cache_on = configure_cache(settings.cache_enabled, settings.cache_ttl_seconds, settings.cache_max_items)
        await prewarm_cache()
        llm = FailoverLLMClient(
            api_keys=settings.api_keys,
            models=settings.key_models,
            timeout_seconds=settings.llm_timeout_seconds,
            max_output_tokens=settings.llm_max_output_tokens,
            acompletion=acompletion,
            api_base=settings.llm_api_base,
        )
        app.state.settings = settings
        app.state.chat_service = ChatService(
            db=db,
            llm=llm,
            default_system_prompt=settings.default_system_prompt,
            max_history_messages=settings.max_history_messages,
        )
        if llm.keys_configured < 2:
            logger.warning("Only %d API key(s) configured; at least 2 are required", llm.keys_configured)
        logger.info(
            "Startup complete: db=%s keys=%d models=%s cache=%s failure_simulation=%s origins=%s",
            settings.database_path,
            llm.keys_configured,
            sorted(set(settings.key_models)),
            cache_on,
            settings.enable_failure_simulation,
            settings.origins,
        )
        yield
        logger.info("Shutting down")

    app = FastAPI(title="LLM Chatbot API", version="1.0.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Simulate-Failure"],
    )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled error: %s", type(exc).__name__, exc_info=False)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    app.include_router(router)
    return app


app = create_app()
