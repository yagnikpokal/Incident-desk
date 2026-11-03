from __future__ import annotations

import os
import uuid

from incident_desk.fixtures import INCIDENTS
from incident_desk.loop import advance_run, approve, hint, make_policy, new_event, reject
from incident_desk.models import AdvanceResult, Run, RunStatus, now_iso
from incident_desk.policy import resolve_policy_name
from incident_desk.store import Store
from incident_desk.tools import TOOL_SPECS


class DeskError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def blank_memory() -> dict:
    return {
        "facts": {
            "profiles": {},
            "deploys": {},
            "snapshots": {},
            "logs": [],
            "log_queries": [],
        },
        "failed_calls": [],
        "rejected_windows": [],
        "banned_actions": [],
        "operator_hints": [],
        "proposal_history": [],
        "pending_verification": False,
        "question": "",
    }


class Desk:
    def __init__(self, store: Store):
        self.store = store

    def list_incidents(self) -> list[dict]:
        return [
            {
                "id": incident["id"],
                "title": incident["title"],
                "service": incident["service"],
                "severity": incident["severity"],
                "symptom": incident["symptom"],
                "opened_at": incident["opened_at"],
            }
            for incident in INCIDENTS.values()
        ]

    def get_incident(self, incident_id: str) -> dict:
        try:
            return INCIDENTS[incident_id]
        except KeyError as exc:
            raise DeskError(f"Unknown incident '{incident_id}'.", 404) from exc

    def list_tools(self) -> list[dict]:
        return TOOL_SPECS

    def list_runs(self) -> list[dict]:
        summaries = []
        for row in self.store.list():
            run = self.store.get(row["id"])
            if run is None:
                continue
            summaries.append(
                {
                    "id": run.id,
                    "incident_id": run.incident_id,
                    "status": run.status.value,
                    "policy": run.policy,
                    "step_count": run.step_count,
                    "updated_at": run.updated_at,
                    "summary": run.summary,
                }
            )
        return summaries

    def create_run(self, incident_id: str, policy: str | None = None) -> Run:
        self.get_incident(incident_id)
        try:
            name = resolve_policy_name(policy)
        except ValueError as exc:
            raise DeskError(str(exc)) from exc
        if name == "llm" and not os.environ.get("OPENAI_API_KEY"):
            raise DeskError("AGENT_POLICY=llm requires OPENAI_API_KEY. Use heuristic, or set the key.")
        stamp = now_iso()
        run = Run(
            id="run-" + uuid.uuid4().hex[:8],
            incident_id=incident_id,
            policy=name,
            model=os.environ.get("AGENT_MODEL", "gpt-4o-mini") if name == "llm" else None,
            created_at=stamp,
            updated_at=stamp,
            memory=blank_memory(),
            overlay={"shifts": {}, "actions": []},
        )
        self.store.save(run)
        return run

    def get_run(self, run_id: str) -> Run:
        run = self.store.get(run_id)
        if run is None:
            raise DeskError(f"Unknown run '{run_id}'.", 404)
        return run

    def advance(self, run_id: str) -> AdvanceResult:
        run = self.get_run(run_id)
        policy = make_policy(run.policy)
        applied, reason = advance_run(run, policy)
        self.store.save(run)
        return AdvanceResult(applied=applied, reason=reason, run=run)

    def pause(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run.status in {RunStatus.resolved, RunStatus.failed, RunStatus.paused}:
            raise DeskError(f"Cannot pause a run that is {run.status.value}.", 409)
        run.memory["resume_status"] = run.status.value
        run.status = RunStatus.paused
        run.events.append(new_event("status", "Paused. State is saved; resume continues from this step.", {"status": "paused"}))
        run.updated_at = now_iso()
        self.store.save(run)
        return run

    def resume(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run.status != RunStatus.paused:
            raise DeskError("Only a paused run can be resumed.", 409)
        previous = run.memory.pop("resume_status", RunStatus.running.value)
        run.status = RunStatus(previous)
        run.events.append(new_event("status", f"Resumed ({run.status.value}).", {"status": run.status.value}))
        run.updated_at = now_iso()
        self.store.save(run)
        return run

    def intervene(self, run_id: str, kind: str, note: str = "") -> Run:
        run = self.get_run(run_id)
        try:
            if kind == "approve":
                approve(run, note)
            elif kind == "reject":
                reject(run, note)
            elif kind == "hint":
                hint(run, note)
            else:
                raise DeskError("Intervention must be approve, reject, or hint.")
        except PermissionError as exc:
            raise DeskError(str(exc), 409) from exc
        except ValueError as exc:
            raise DeskError(str(exc), 400) from exc
        self.store.save(run)
        return run
