from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agentflow.log_viewer.analyzer import LogAnalyzer
from agentflow.log_viewer.codex_rollout import CodexRolloutAnalyzer


def _default_codex_sessions_dir() -> Path:
    codex_home = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    return codex_home / "sessions"


def create_log_viewer_app(
    *,
    runs_dir: str | Path | None = None,
    codex_sessions_dir: str | Path | None = None,
) -> FastAPI:
    analyzer = LogAnalyzer(runs_dir or os.getenv("AGENTFLOW_RUNS_DIR", ".agentflow/runs"))
    codex_analyzer = CodexRolloutAnalyzer(
        codex_sessions_dir
        or os.getenv("AGENTFLOW_CODEX_SESSIONS_DIR")
        or _default_codex_sessions_dir()
    )
    app = FastAPI(title="AgentFlow Log Explorer", version="0.1.0")
    app.state.analyzer = analyzer
    app.state.codex_analyzer = codex_analyzer

    @app.middleware("http")
    async def disable_ui_cache(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    web_dir = Path(__file__).with_name("web")
    templates = Jinja2Templates(directory=str(web_dir / "templates"))
    app.mount("/assets", StaticFiles(directory=str(web_dir / "static")), name="log-viewer-assets")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            name="index.html",
            request=request,
            context={
                "runs_dir": str(analyzer.runs_dir),
                "codex_sessions_dir": str(codex_analyzer.sessions_dir),
            },
        )

    @app.get("/api/runs")
    async def list_runs() -> JSONResponse:
        return JSONResponse(analyzer.list_runs())

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> JSONResponse:
        try:
            return JSONResponse(analyzer.run_detail(run_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.get("/api/runs/{run_id}/nodes/{node_id}")
    async def get_node(run_id: str, node_id: str) -> JSONResponse:
        try:
            detail = analyzer.node_detail(run_id, node_id)
            available_sessions = {session["id"] for session in codex_analyzer.list_sessions()}
            for rollout in detail.get("codex_rollouts", []):
                rollout["available"] = rollout["session_id"] in available_sessions
            return JSONResponse(detail)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run or node not found") from exc

    @app.get("/api/runs/{run_id}/nodes/{node_id}/events")
    async def get_node_events(
        run_id: str,
        node_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
        attempt: int | None = Query(default=None, ge=1),
        category: str | None = None,
        q: str | None = None,
        order: str = Query(default="asc", pattern="^(asc|desc)$"),
    ) -> JSONResponse:
        try:
            return JSONResponse(
                analyzer.node_events(
                    run_id,
                    node_id,
                    offset=offset,
                    limit=limit,
                    attempt=attempt,
                    category=category,
                    query=q,
                    order=order,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run or node not found") from exc

    @app.get("/api/runs/{run_id}/nodes/{node_id}/events/{line_number}")
    async def get_node_event(run_id: str, node_id: str, line_number: int) -> JSONResponse:
        try:
            return JSONResponse(analyzer.event_detail(run_id, node_id, line_number))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="trace event not found") from exc

    @app.get("/api/runs/{run_id}/nodes/{node_id}/artifacts/{name}")
    async def get_artifact(
        run_id: str,
        node_id: str,
        name: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=200_000, ge=1, le=1_000_000),
    ) -> JSONResponse:
        try:
            return JSONResponse(analyzer.artifact_chunk(run_id, node_id, name, offset=offset, limit=limit))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="artifact not found") from exc

    @app.get("/api/codex/sessions")
    async def list_codex_sessions() -> JSONResponse:
        return JSONResponse(codex_analyzer.list_sessions())

    @app.get("/api/codex/sessions/{session_id}")
    async def get_codex_session(session_id: str) -> JSONResponse:
        try:
            return JSONResponse(codex_analyzer.session_detail(session_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Codex session not found") from exc

    @app.get("/api/codex/sessions/{session_id}/events")
    async def get_codex_session_events(
        session_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
        turn_id: str | None = None,
        category: str | None = None,
        q: str | None = None,
        order: str = Query(default="asc", pattern="^(asc|desc)$"),
    ) -> JSONResponse:
        try:
            return JSONResponse(
                codex_analyzer.session_events(
                    session_id,
                    offset=offset,
                    limit=limit,
                    turn_id=turn_id,
                    category=category,
                    query=q,
                    order=order,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Codex session not found") from exc

    @app.get("/api/codex/sessions/{session_id}/events/{line_number}")
    async def get_codex_session_event(session_id: str, line_number: int) -> JSONResponse:
        try:
            return JSONResponse(codex_analyzer.event_detail(session_id, line_number))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Codex rollout event not found") from exc

    @app.get("/api/codex/sessions/{session_id}/raw")
    async def get_codex_session_raw(
        session_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=200_000, ge=1, le=1_000_000),
    ) -> JSONResponse:
        try:
            return JSONResponse(codex_analyzer.raw_chunk(session_id, offset=offset, limit=limit))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Codex session not found") from exc

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "runs_dir": str(analyzer.runs_dir),
                "runs": len(analyzer.list_runs()),
                "codex_sessions_dir": str(codex_analyzer.sessions_dir),
                "codex_sessions": len(codex_analyzer.list_sessions()),
            }
        )

    return app
