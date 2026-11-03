from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from incident_desk.service import Desk, DeskError
from incident_desk.store import Store

STATIC = Path(__file__).resolve().parent / "static"


class CreateRunBody(BaseModel):
    incident_id: str
    policy: str | None = None


class InterveneBody(BaseModel):
    kind: str
    note: str = Field(default="")


def create_app(db_path: str | Path | None = None) -> FastAPI:
    path = db_path or os.environ.get("INCIDENT_DESK_DB", "incident_desk.sqlite")
    app = FastAPI(
        title="Incident Desk",
        version="0.1.0",
        description="On-call investigator. The loop can read the platform. It cannot change it until an operator approves.",
    )
    app.state.desk = Desk(Store(path))

    @app.exception_handler(DeskError)
    def _desk_error(_, exc: DeskError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status_code)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/incidents")
    def incidents():
        return app.state.desk.list_incidents()

    @app.get("/api/incidents/{incident_id}")
    def incident(incident_id: str):
        return app.state.desk.get_incident(incident_id)

    @app.get("/api/tools")
    def tools():
        return app.state.desk.list_tools()

    @app.get("/api/runs")
    def runs():
        return app.state.desk.list_runs()

    @app.post("/api/runs")
    def create_run(body: CreateRunBody):
        return app.state.desk.create_run(body.incident_id, body.policy)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        return app.state.desk.get_run(run_id)

    @app.post("/api/runs/{run_id}/advance")
    def advance(run_id: str):
        return app.state.desk.advance(run_id)

    @app.post("/api/runs/{run_id}/pause")
    def pause(run_id: str):
        return app.state.desk.pause(run_id)

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str):
        return app.state.desk.resume(run_id)

    @app.post("/api/runs/{run_id}/intervene")
    def intervene(run_id: str, body: InterveneBody):
        return app.state.desk.intervene(run_id, body.kind, body.note)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")
    return app


app = create_app()
