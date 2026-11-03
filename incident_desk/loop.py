from __future__ import annotations

import uuid

from incident_desk.fixtures import INCIDENTS
from incident_desk.models import Event, Hypothesis, Proposal, Run, RunStatus, now_iso
from incident_desk.policy import Decision, HeuristicPolicy, LLMPolicy
from incident_desk.reasoning import (
    call_signature,
    callers_cleared,
    derive_hypotheses,
    implicated_dependency,
    leading,
    next_log_query,
    ready_to_propose,
)
from incident_desk.tools import CONTROL_TOOLS, WORLD_TOOLS, Registry
from incident_desk.world import Platform, ToolError


def new_event(kind: str, summary: str, data: dict | None = None) -> Event:
    return Event(id=uuid.uuid4().hex[:12], at=now_iso(), kind=kind, summary=summary, data=data or {})


def advance_run(run: Run, policy: HeuristicPolicy | LLMPolicy) -> tuple[bool, str]:
    if run.status == RunStatus.paused:
        return False, "paused"
    if run.status in {RunStatus.awaiting_approval, RunStatus.awaiting_input}:
        return False, run.status.value
    if run.status in {RunStatus.resolved, RunStatus.failed}:
        return False, run.status.value
    if run.step_count >= run.max_steps:
        _fail(run, f"Stopped after {run.step_count} steps without a verified fix.")
        return True, "budget"

    incident = INCIDENTS[run.incident_id]
    run.status = RunStatus.running

    if run.memory.get("pending_verification"):
        _verify(run, incident)
        _refresh_plan(run, incident)
        run.step_count += 1
        run.updated_at = now_iso()
        return True, "step"

    if not run.plan:
        thought, plan = policy.draft_plan(incident)
        run.plan = plan
        run.events.append(new_event("thought", thought))
        run.events.append(
            new_event("plan", "Drafted an investigation that stops before any production change.", {"steps": [step.title for step in plan]})
        )
        _refresh_plan(run, incident)
        run.step_count += 1
        run.updated_at = now_iso()
        return True, "step"

    hypotheses = [Hypothesis.model_validate(item) if isinstance(item, dict) else item for item in run.hypotheses]
    raw = policy.decide(incident, run.memory, hypotheses)
    decision = _guard(run, raw, incident)
    if decision.recovery_note:
        run.events.append(new_event("recovery", decision.recovery_note, {"blocked_tool": raw.tool}))
    run.events.append(new_event("thought", decision.thought, {"tool": decision.tool}))

    if decision.tool == "propose_remediation":
        _propose(run, incident, decision)
    elif decision.tool == "request_guidance":
        _ask(run, decision)
    else:
        _call_world(run, incident, decision)

    _refresh_plan(run, incident)
    run.step_count += 1
    run.updated_at = now_iso()
    return True, "step"


def approve(run: Run, note: str) -> None:
    if run.status != RunStatus.awaiting_approval or run.proposal is None:
        raise PermissionError("There is no proposal waiting for approval.")
    proposal = run.proposal
    incident = INCIDENTS[run.incident_id]
    platform = Platform(incident, run.overlay)
    run.events.append(
        new_event(
            "human",
            note or f"Approved {proposal.action} on {proposal.service}.",
            {"kind": "approve", "note": note},
        )
    )
    run.events.append(
        new_event(
            "tool_call",
            f"execute_remediation {proposal.action} {proposal.service}",
            {"tool": "execute_remediation", "arguments": {"action": proposal.action, "service": proposal.service, "args": proposal.args}},
        )
    )
    try:
        result = platform.execute(proposal.action, proposal.service, proposal.args)
    except ToolError as exc:
        run.events.append(
            new_event(
                "tool_error",
                exc.message,
                {"tool": "execute_remediation", "code": exc.code, "detail": exc.data},
            )
        )
        previous = exc.data.get("previous_version")
        hint = f" The previous version is {previous}." if previous else ""
        run.events.append(new_event("recovery", f"The change was not applied.{hint} The proposal is still waiting."))
        run.updated_at = now_iso()
        return

    proposal.outcome = "approved"
    snapshot = run.memory["facts"].get("snapshots", {}).get(proposal.service, {})
    run.memory["baseline_elevated"] = {
        name: reading for name, reading in snapshot.items() if reading["assessment"] == "elevated"
    }
    run.memory["execution"] = {
        "action": proposal.action,
        "service": proposal.service,
        "args": proposal.args,
    }
    run.memory["pending_verification"] = True
    run.status = RunStatus.running
    moved = ", ".join(result["metrics_moved"]) or "no series"
    run.events.append(
        new_event(
            "tool_result",
            f"Applied {proposal.action} on {proposal.service}. Series written: {moved}.",
            {"tool": "execute_remediation", "result": result},
        )
    )
    run.updated_at = now_iso()


def reject(run: Run, note: str) -> None:
    if run.status != RunStatus.awaiting_approval or run.proposal is None:
        raise PermissionError("There is no proposal to reject.")
    if not note.strip():
        raise ValueError("A rejection needs a note so the next step has something to read.")
    proposal = run.proposal
    proposal.outcome = "rejected"
    run.memory.setdefault("proposal_history", []).append(proposal.model_dump())
    banned = run.memory.setdefault("banned_actions", [])
    if proposal.action not in banned:
        banned.append(proposal.action)
    run.memory.pop("operator_override", None)
    run.proposal = None
    run.status = RunStatus.running
    run.events.append(new_event("human", f"Rejected {proposal.action}. {note}", {"kind": "reject", "note": note}))
    run.updated_at = now_iso()


def hint(run: Run, note: str) -> None:
    if run.status == RunStatus.awaiting_approval:
        raise PermissionError("Approve or reject the current proposal before adding a hint.")
    if not note.strip():
        raise ValueError("Hint is empty.")
    from incident_desk.reasoning import parse_operator_action

    run.memory.setdefault("operator_hints", []).append(note)
    action = parse_operator_action(note)
    if action and run.status == RunStatus.awaiting_input:
        run.memory["operator_override"] = {"action": action, "note": note}
        run.memory["banned_actions"] = [item for item in run.memory.get("banned_actions", []) if item != action]
    if run.status == RunStatus.awaiting_input:
        run.status = RunStatus.running
    run.events.append(new_event("human", note, {"kind": "hint", "action": action}))
    run.updated_at = now_iso()


def _guard(run: Run, decision: Decision, incident: dict) -> Decision:
    if decision.tool in WORLD_TOOLS or decision.tool in CONTROL_TOOLS:
        signature = call_signature(decision.tool, decision.arguments)
        duplicate = any(item["signature"] == signature for item in run.memory.get("failed_calls", []))
        if not duplicate:
            return decision
        reason = "Skipped a call that already failed with these arguments."
    elif decision.tool == "execute_remediation":
        reason = (
            f"Blocked execute_remediation ({decision.thought}). "
            "Mutations wait for the operator to approve a proposal."
        )
    else:
        reason = f"Unknown tool {decision.tool}. Using the evidence-gap policy instead."

    alternative = HeuristicPolicy().decide(incident, run.memory, run.hypotheses)
    if call_signature(alternative.tool, alternative.arguments) == call_signature(decision.tool, decision.arguments):
        alternative = Decision(
            thought="I cannot find a different safe call.",
            tool="request_guidance",
            arguments={"question": "The last tool call failed and I have no different next step. What should I do?"},
        )
    alternative.recovery_note = reason
    return alternative


def _call_world(run: Run, incident: dict, decision: Decision) -> None:
    run.events.append(
        new_event(
            "tool_call",
            _call_summary(decision.tool, decision.arguments),
            {"tool": decision.tool, "arguments": decision.arguments},
        )
    )
    platform = Platform(incident, run.overlay)
    try:
        result = Registry(platform).call(decision.tool, decision.arguments)
    except ToolError as exc:
        run.memory.setdefault("failed_calls", []).append(
            {
                "signature": call_signature(decision.tool, decision.arguments),
                "tool": decision.tool,
                "arguments": decision.arguments,
                "code": exc.code,
                "message": exc.message,
            }
        )
        if exc.code == "unsupported_window":
            window = decision.arguments.get("window")
            rejected = run.memory.setdefault("rejected_windows", [])
            if window and window not in rejected:
                rejected.append(window)
        run.events.append(
            new_event(
                "tool_error",
                exc.message,
                {"tool": decision.tool, "code": exc.code, "detail": exc.data, "arguments": decision.arguments},
            )
        )
        run.events.append(new_event("recovery", _recovery_line(exc, decision, incident, run)))
        return

    _ingest(run, incident, decision.tool, decision.arguments, result)
    summary, data = _result_event(decision.tool, result)
    run.events.append(new_event("tool_result", summary, data))
    if decision.tool == "search_logs" and result["count"] == 0:
        nxt = next_log_query(run.memory["facts"], incident)
        if nxt:
            run.events.append(
                new_event("recovery", f"No log lines matched {result['query']!r}. Next search will use {nxt!r}.")
            )
    _rescore(run, incident)


def _propose(run: Run, incident: dict, decision: Decision) -> None:
    arguments = decision.arguments
    action = arguments.get("action")
    service = arguments.get("service") or incident["service"]
    args = arguments.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    facts = run.memory["facts"]
    override = run.memory.get("operator_override")
    best = leading(run.hypotheses)
    allowed = bool(override and override["action"] == action and action not in run.memory.get("banned_actions", []))
    if not allowed:
        if not ready_to_propose(facts, run.memory, incident, best) or best is None or best.suggested_action != action:
            run.events.append(
                new_event(
                    "tool_error",
                    "Refusing to propose before the evidence and the runbook agree.",
                    {"tool": "propose_remediation", "code": "insufficient_evidence"},
                )
            )
            run.events.append(
                new_event("recovery", "The proposal was too early. Investigation continues.")
            )
            run.memory.setdefault("failed_calls", []).append(
                {
                    "signature": call_signature(decision.tool, decision.arguments),
                    "tool": decision.tool,
                    "arguments": decision.arguments,
                    "code": "insufficient_evidence",
                    "message": "too early",
                }
            )
            return
        service = best.action_args.get("service", service)
        args = {key: value for key, value in best.action_args.items() if key != "service"}
        rationale = best.claim
        evidence = list(best.evidence)
        confidence = best.confidence
    else:
        rationale = arguments.get("rationale") or decision.thought
        evidence = [override["note"]] if override else []
        confidence = best.confidence if best else 0.5

    if action in run.memory.get("banned_actions", []):
        run.events.append(
            new_event(
                "tool_error",
                f"{action} was already rejected or failed verification.",
                {"tool": "propose_remediation", "code": "action_banned"},
            )
        )
        return

    run.proposal = Proposal(
        action=action,
        service=service,
        args=args,
        rationale=rationale,
        evidence=evidence,
        confidence=confidence,
    )
    run.status = RunStatus.awaiting_approval
    run.events.append(
        new_event(
            "approval_requested",
            f"Propose {action} on {service}. Waiting for an operator.",
            {"action": action, "service": service, "args": args, "rationale": rationale, "evidence": evidence},
        )
    )


def _ask(run: Run, decision: Decision) -> None:
    question = decision.arguments.get("question") or decision.thought
    run.memory["question"] = question
    run.status = RunStatus.awaiting_input
    run.events.append(new_event("question", question, {"kind": "question"}))


def _verify(run: Run, incident: dict) -> None:
    execution = run.memory["execution"]
    service = execution["service"]
    window = run.memory.get("window") or "15m"
    platform = Platform(incident, run.overlay)
    result = platform.get_health_snapshot(service, window)
    _ingest(
        run,
        incident,
        "get_health_snapshot",
        {"service": service, "window": window},
        result,
    )
    current = {metric["name"]: metric for metric in result["metrics"]}
    still_bad = []
    for name, before in run.memory.get("baseline_elevated", {}).items():
        after = current.get(name)
        if after is None or after["assessment"] != "normal":
            still_bad.append(name)
    if not run.memory.get("baseline_elevated"):
        still_bad.append("(no elevated series to confirm)")

    run.events.append(
        new_event(
            "tool_call",
            f"get_health_snapshot {service} {window}",
            {"tool": "get_health_snapshot", "arguments": {"service": service, "window": window}, "purpose": "verify"},
        )
    )
    if still_bad:
        action = execution["action"]
        run.memory["pending_verification"] = False
        banned = run.memory.setdefault("banned_actions", [])
        if action not in banned:
            banned.append(action)
        run.memory.pop("operator_override", None)
        if run.proposal is not None:
            run.proposal.outcome = "failed"
            run.memory.setdefault("proposal_history", []).append(run.proposal.model_dump())
            run.proposal = None
        run.status = RunStatus.running
        joined = ", ".join(still_bad)
        run.events.append(
            new_event(
                "verification",
                f"Verification failed. Still elevated: {joined}. {action} did not repair {service}.",
                {"ok": False, "still_elevated": still_bad},
            )
        )
        run.events.append(
            new_event("recovery", f"Banning {action}. I will not report this incident as resolved.")
        )
        _rescore(run, incident)
        return

    run.memory["pending_verification"] = False
    if run.proposal is not None:
        run.proposal.outcome = "verified"
    run.status = RunStatus.resolved
    run.summary = _success_summary(execution)
    run.events.append(new_event("verification", run.summary, {"ok": True}))
    run.events.append(new_event("status", run.summary, {"status": "resolved"}))


def _success_summary(execution: dict) -> str:
    action = execution["action"]
    service = execution["service"]
    if action == "rollback":
        return f"Rolled {service} back to {execution['args'].get('to_version')}. Elevated series returned to baseline."
    if action == "expand_volume":
        return f"Expanded the volume on {service}. Disk pressure and lag returned to baseline."
    return f"{action} on {service} cleared the elevated series."


def _ingest(run: Run, incident: dict, tool: str, arguments: dict, result: dict) -> None:
    facts = run.memory["facts"]
    service = incident["service"]
    if tool == "get_service":
        facts.setdefault("profiles", {})[result["name"]] = result
        if result["name"] == service:
            facts["profile"] = result
    elif tool == "get_health_snapshot":
        facts.setdefault("snapshots", {})[result["service"]] = {
            metric["name"]: {key: value for key, value in metric.items() if key != "name"}
            for metric in result["metrics"]
        }
        run.memory["window"] = result["window"]
    elif tool == "query_metrics":
        facts.setdefault("snapshots", {}).setdefault(arguments["service"], {})[arguments["metric"]] = result["reading"]
        run.memory["window"] = result["window"]
    elif tool == "list_recent_deploys":
        facts.setdefault("deploys", {})[result["service"]] = result["deploys"]
    elif tool == "search_logs":
        facts.setdefault("log_queries", []).append(
            {"query": result["query"], "count": result["count"], "service": result["service"]}
        )
        known = {line["id"] for line in facts.setdefault("logs", [])}
        for line in result["lines"]:
            if line["id"] not in known:
                facts["logs"].append(line)
    elif tool == "get_runbook":
        facts["runbook"] = result


def _rescore(run: Run, incident: dict) -> None:
    previous = {item.id: item.status for item in run.hypotheses}
    previous_confidence = {item.id: item.confidence for item in run.hypotheses}
    updated = derive_hypotheses(run.memory["facts"], incident)
    changed = False
    closed: list[Hypothesis] = []
    for item in updated:
        if previous.get(item.id) != item.status or abs(previous_confidence.get(item.id, -1) - item.confidence) >= 0.05:
            changed = True
        if item.status == "rejected" and previous.get(item.id) != "rejected":
            closed.append(item)
    run.hypotheses = updated
    if changed:
        run.events.append(
            new_event(
                "hypothesis",
                _hypothesis_summary(updated),
                {"hypotheses": [item.model_dump() for item in updated]},
            )
        )
    for item in closed:
        run.events.append(new_event("replan", f"Closed this lead: {item.claim}", {"id": item.id}))


def _hypothesis_summary(hypotheses: list[Hypothesis]) -> str:
    if not hypotheses:
        return "No hypothesis yet."
    parts = [f"{item.status} {item.confidence:.2f} {item.claim}" for item in hypotheses[:3]]
    return " | ".join(parts)


def _refresh_plan(run: Run, incident: dict) -> None:
    if not run.plan:
        return
    facts = run.memory["facts"]
    service = incident["service"]
    flags = [
        "profile" in facts,
        service in facts.get("snapshots", {}) and bool(run.memory.get("window")),
        callers_cleared(facts, service),
        any(query["count"] > 0 for query in facts.get("log_queries", [])),
        _dependency_step_done(facts, service),
        run.proposal is not None or run.status in {RunStatus.awaiting_approval, RunStatus.resolved},
    ]
    first_open = next((index for index, done in enumerate(flags) if not done), None)
    for index, step in enumerate(run.plan):
        if flags[index]:
            step.status = "done"
        elif index == first_open:
            step.status = "active"
        else:
            step.status = "pending"
    if run.status == RunStatus.resolved:
        for step in run.plan:
            step.status = "done"


def _dependency_step_done(facts: dict, service: str) -> bool:
    if not any(query["count"] > 0 for query in facts.get("log_queries", [])):
        return False
    if service not in facts.get("snapshots", {}):
        return False
    dependency = implicated_dependency(facts, service)
    if dependency is None:
        return True
    return dependency in facts.get("snapshots", {})


def _recovery_line(exc: ToolError, decision: Decision, incident: dict, run: Run) -> str:
    if exc.code == "unsupported_window":
        from incident_desk.reasoning import choose_window

        window = choose_window(incident["alert"]["window"], run.memory.get("rejected_windows", []))
        allowed = ", ".join(exc.data.get("allowed", []))
        return f"Will retry the snapshot at {window}. The store only keeps {allowed}."
    hint = exc.data.get("hint")
    if hint:
        return str(hint)
    return "I will not repeat this call with the same arguments."


def _call_summary(tool: str, arguments: dict) -> str:
    if tool == "get_service":
        return f"get_service {arguments.get('service')}"
    if tool == "get_health_snapshot":
        return f"get_health_snapshot {arguments.get('service')} {arguments.get('window')}"
    if tool == "query_metrics":
        return f"query_metrics {arguments.get('service')} {arguments.get('metric')} {arguments.get('window')}"
    if tool == "list_recent_deploys":
        return f"list_recent_deploys {arguments.get('service')}"
    if tool == "search_logs":
        return f"search_logs {arguments.get('service')} {arguments.get('query')!r}"
    if tool == "get_runbook":
        return f"get_runbook {arguments.get('service')}"
    return tool


def _result_event(tool: str, result: dict) -> tuple[str, dict]:
    if tool == "get_service":
        summary = f"{result['name']} ({result['tier']}). Series: {', '.join(result['metrics'])}."
    elif tool == "get_health_snapshot":
        bits = [f"{metric['name']}={metric['value']} ({metric['assessment']})" for metric in result["metrics"]]
        summary = f"{result['service']} @ {result['window']}: " + "; ".join(bits)
    elif tool == "query_metrics":
        reading = result["reading"]
        summary = (
            f"{result['service']} {result['metric']}={reading['value']} "
            f"({reading['assessment']}, baseline {reading['baseline']})."
        )
    elif tool == "list_recent_deploys":
        deploys = result["deploys"]
        if not deploys:
            summary = f"{result['service']} has no deploys."
        else:
            latest = deploys[0]
            summary = f"Latest {result['service']} {latest['version']} {latest['age_minutes']}m ago. {latest['note']}"
    elif tool == "search_logs":
        if result["count"] == 0:
            summary = f"0 lines for {result['query']!r}."
        else:
            summary = f"{result['count']} lines for {result['query']!r}. First: {result['lines'][0]['message']}"
    elif tool == "get_runbook":
        summary = f"{result['id']} {result['title']}. Allows: {', '.join(result['preferred_actions']) or 'nothing'}."
    else:
        summary = tool
    return summary, {"tool": tool, "result": result, "count": result.get("count")}


def _fail(run: Run, summary: str) -> None:
    run.status = RunStatus.failed
    run.summary = summary
    run.events.append(new_event("status", summary, {"status": "failed"}))
    run.updated_at = now_iso()


def make_policy(name: str) -> HeuristicPolicy | LLMPolicy:
    if name == "llm":
        return LLMPolicy()
    return HeuristicPolicy()
