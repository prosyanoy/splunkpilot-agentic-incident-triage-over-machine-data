"""FastAPI app: Splunk HEC-compatible ingest plus an SPL-like search API."""
from __future__ import annotations

import hmac
import json
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from . import __version__
from .config import Settings
from .hec import HECError, parse_hec_body
from .spl import SPLError, execute
from .store import EventStore


class SearchRequest(BaseModel):
    query: str
    earliest: str | float | None = None
    latest: str | float | None = None
    index: str | None = None
    limit: int = 1000


def create_app(store: EventStore | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or EventStore(settings.db_path)
    app = FastAPI(title="SplunkPilot", version=__version__)
    app.state.store = store

    @app.exception_handler(HECError)
    async def _hec_error(_: Request, exc: HECError) -> JSONResponse:
        return JSONResponse({"text": exc.text, "code": exc.code}, status_code=exc.status)

    @app.exception_handler(SPLError)
    async def _spl_error(_: Request, exc: SPLError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=400)

    def check_token(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() not in ("splunk", "bearer") or not token:
            raise HECError(401, 2, "Token is required")
        if not hmac.compare_digest(token.strip().encode(), settings.hec_token.encode()):
            raise HECError(403, 4, "Invalid token")

    async def ingest(request: Request) -> dict[str, Any]:
        check_token(request)
        events = parse_hec_body(await request.body())
        store.insert_many(events)
        return {"text": "Success", "code": 0}

    app.post("/services/collector/event")(ingest)
    app.post("/services/collector")(ingest)

    @app.get("/services/collector/health")
    def hec_health() -> dict[str, Any]:
        return {"text": "HEC is healthy", "code": 17}

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, **store.stats()}

    @app.post("/api/search")
    def search(req: SearchRequest) -> dict[str, Any]:
        res = execute(store, req.query, req.earliest, req.latest, req.index, max(1, min(req.limit, 50_000)))
        return res.to_json()

    @app.post("/services/search/jobs/export")
    async def export(request: Request) -> Response:
        """Splunk-style one-shot export: form body, newline-delimited JSON results."""
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        q = form.get("search")
        if not q:
            raise HTTPException(400, "missing 'search' parameter")
        if not q.lstrip().lower().startswith("search"):
            q = "search " + q
        res = execute(store, q, form.get("earliest_time"), form.get("latest_time"), form.get("index"),
                      int(form.get("count") or 1000)).to_json()
        lines = [json.dumps({"preview": False, "result": r}) for r in res["results"]]
        return Response("\n".join(lines) + ("\n" if lines else ""), media_type="application/x-ndjson")

    @app.get("/api/events/{event_id}")
    def get_event(event_id: int) -> dict[str, Any]:
        row = store.get(event_id)
        if row is None:
            raise HTTPException(404, "event not found")
        from .spl import SearchResult

        return SearchResult(list(row), [row]).to_json()["results"][0]

    return app


def app_factory() -> FastAPI:  # uvicorn --factory splunkpilot.api:app_factory
    return create_app()
