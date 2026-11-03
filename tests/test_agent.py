from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from incident_desk.fixtures import INCIDENTS
from incident_desk.loop import _guard
from incident_desk.main import create_app
from incident_desk.policy import Decision, parse_decision
from incident_desk.service import Desk
from incident_desk.store import Store
from incident_desk.world import Platform, ToolError


def drive(desk: Desk, run_id: str, limit: int = 20):
    run = desk.get_run(run_id)
    for _ in range(limit):
        if run.status.value in {"awaiting_approval", "awaiting_input", "resolved", "failed", "paused"}:
            return run
        result = desk.advance(run_id)
        run = result.run
        if not result.applied:
            return run
    return run


def test_alert_window_is_not_stored():
    platform = Platform(INCIDENTS["inc-1042"], {"shifts": {}, "actions": []})
    with pytest.raises(ToolError) as caught:
        platform.get_health_snapshot("payments-api", "10m")
    assert caught.value.code == "unsupported_window"
    snapshot = platform.get_health_snapshot("payments-api", "15m")
    assert snapshot["metrics"][0]["name"] == "http_server_errors_total"


def test_deploy_age_is_measured_from_the_incident_clock():
    platform = Platform(INCIDENTS["inc-1042"], {"shifts": {}, "actions": []})
    latest = platform.list_recent_deploys("payments-api")["deploys"][0]
    assert latest["version"] == "2.14.3"
    assert latest["age_minutes"] == 18
    assert latest["previous_version"] == "2.14.2"


def test_charge_incident_recovers_and_waits_for_rollback(tmp_path: Path):
    desk = Desk(Store(tmp_path / "charge.sqlite"))
    run = drive(desk, desk.create_run("inc-1042", policy="heuristic").id)

    assert run.status.value == "awaiting_approval"
    assert run.proposal is not None
    assert run.proposal.action == "rollback"
    assert run.proposal.service == "payments-api"
    assert run.proposal.args["to_version"] == "2.14.2"
    assert run.overlay["actions"] == []
    assert any(event.kind == "tool_error" and event.data.get("code") == "unsupported_window" for event in run.events)
    assert any(event.kind == "recovery" and "15m" in event.summary for event in run.events)
    assert any(
        event.kind == "tool_result" and event.data.get("tool") == "search_logs" and event.data.get("count") == 0
        for event in run.events
    )
    assert any(event.kind == "replan" and "red herring" in event.summary for event in run.events)
    assert any(
        event.kind == "tool_call"
        and event.data["tool"] == "query_metrics"
        and event.data["arguments"]["service"] == "edge-gateway"
        for event in run.events
    )
    assert any(
        event.kind == "tool_call"
        and event.data["tool"] == "get_health_snapshot"
        and event.data["arguments"]["service"] == "ledger-worker"
        for event in run.events
    )
    edge = next(item for item in run.hypotheses if item.id == "caller-edge-gateway")
    assert edge.status == "rejected"
    winner = next(item for item in run.hypotheses if item.id == "timeout-regression")
    assert winner.status == "supported"
    assert winner.confidence >= 0.75


def test_disk_incident_proposes_expand_and_drops_the_deploy(tmp_path: Path):
    desk = Desk(Store(tmp_path / "disk.sqlite"))
    run = drive(desk, desk.create_run("inc-1077", policy="heuristic").id)

    assert run.status.value == "awaiting_approval"
    assert run.proposal is not None
    assert run.proposal.action == "expand_volume"
    assert run.proposal.service == "ledger-worker"
    deploy = next(item for item in run.hypotheses if item.id == "recent-deploy")
    assert deploy.status == "rejected"
    assert not any(
        event.kind == "tool_call" and event.data.get("tool") == "query_metrics" for event in run.events
    )
    assert any(
        event.kind == "tool_call"
        and event.data["tool"] == "list_recent_deploys"
        and event.data["arguments"]["service"] == "payments-api"
        for event in run.events
    )


def test_approval_is_verified_before_the_run_resolves(tmp_path: Path):
    desk = Desk(Store(tmp_path / "approve.sqlite"))
    run = drive(desk, desk.create_run("inc-1042", policy="heuristic").id)
    approved = desk.intervene(run.id, "approve", "Rollback looks right.")
    assert approved.status.value == "running"
    assert approved.memory["pending_verification"] is True
    assert approved.overlay["actions"][0]["action"] == "rollback"

    checked = desk.advance(run.id).run
    assert checked.status.value == "resolved"
    assert "2.14.2" in checked.summary
    snapshot = checked.memory["facts"]["snapshots"]["payments-api"]
    assert snapshot["http_server_errors_total"]["assessment"] == "normal"
    assert snapshot["ledger_client_timeouts_total"]["assessment"] == "normal"


def test_rejected_rollback_then_bad_hint_fails_verification(tmp_path: Path):
    desk = Desk(Store(tmp_path / "bad.sqlite"))
    run = drive(desk, desk.create_run("inc-1042", policy="heuristic").id)
    desk.intervene(run.id, "reject", "I do not want a rollback yet.")
    asked = desk.advance(run.id).run
    assert asked.status.value == "awaiting_input"
    assert "rollback" in asked.memory["banned_actions"]

    hinted = desk.intervene(run.id, "hint", "expand the volume instead")
    assert hinted.status.value == "running"
    proposed = desk.advance(run.id).run
    assert proposed.status.value == "awaiting_approval"
    assert proposed.proposal is not None
    assert proposed.proposal.action == "expand_volume"

    desk.intervene(run.id, "approve")
    failed = desk.advance(run.id).run
    assert failed.status.value == "running"
    assert any(event.kind == "verification" and event.data.get("ok") is False for event in failed.events)
    assert "expand_volume" in failed.memory["banned_actions"]
    assert failed.proposal is None

    guidance = desk.advance(run.id).run
    assert guidance.status.value == "awaiting_input"


def test_bad_rollback_target_does_not_leave_the_gate(tmp_path: Path):
    desk = Desk(Store(tmp_path / "version.sqlite"))
    run = drive(desk, desk.create_run("inc-1042", policy="heuristic").id)
    run.proposal.args["to_version"] = "9.9.9"
    desk.store.save(run)
    stayed = desk.intervene(run.id, "approve")
    assert stayed.status.value == "awaiting_approval"
    assert stayed.overlay["actions"] == []
    assert any(event.kind == "tool_error" and event.data.get("code") == "bad_version" for event in stayed.events)


def test_pause_and_a_new_store_resume_the_same_run(tmp_path: Path):
    path = tmp_path / "resume.sqlite"
    desk = Desk(Store(path))
    run = desk.create_run("inc-1077", policy="heuristic")
    first = desk.advance(run.id).run
    paused = desk.pause(run.id)
    assert paused.status.value == "paused"
    held = desk.advance(run.id)
    assert held.applied is False
    assert held.reason == "paused"
    assert held.run.step_count == first.step_count

    reopened = Desk(Store(path))
    restored = reopened.get_run(run.id)
    assert len(restored.events) == len(paused.events)
    reopened.resume(run.id)
    nxt = reopened.advance(run.id)
    assert nxt.applied is True
    assert nxt.run.step_count == first.step_count + 1


def test_guard_blocks_a_mutating_tool():
    desk = Desk(Store(":memory:"))
    run = desk.create_run("inc-1042", policy="heuristic")
    guarded = _guard(
        run,
        Decision(
            thought="I am going to roll it back myself.",
            tool="execute_remediation",
            arguments={"action": "rollback", "service": "payments-api", "args": {"to_version": "2.14.2"}},
        ),
        INCIDENTS["inc-1042"],
    )
    assert guarded.tool == "get_service"
    assert guarded.recovery_note is not None
    assert "execute_remediation" in guarded.recovery_note


def test_parse_decision_accepts_a_fenced_object():
    decision = parse_decision(
        """```json
        {"thought": "load the catalog", "tool": "get_service", "arguments": {"service": "payments-api"}}
        ```"""
    )
    assert decision.tool == "get_service"
    assert decision.arguments["service"] == "payments-api"


def test_http_lists_tools_and_steps_a_run(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "http.sqlite"))
    tools = client.get("/api/tools")
    assert tools.status_code == 200
    names = {item["name"] for item in tools.json()}
    execute = next(item for item in tools.json() if item["name"] == "execute_remediation")
    assert execute["requires_approval"] is True
    assert {"get_health_snapshot", "search_logs", "propose_remediation", "execute_remediation"} <= names

    created = client.post("/api/runs", json={"incident_id": "inc-1077", "policy": "heuristic"})
    assert created.status_code == 200
    run_id = created.json()["id"]
    stepped = client.post(f"/api/runs/{run_id}/advance")
    assert stepped.status_code == 200
    body = stepped.json()
    assert body["applied"] is True
    assert body["run"]["plan"]
    assert body["run"]["events"][0]["kind"] == "thought"

    missing = client.post("/api/runs/run-missing/advance")
    assert missing.status_code == 404
