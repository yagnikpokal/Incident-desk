from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from pydantic import BaseModel, Field

from incident_desk.models import PlanStep
from incident_desk.reasoning import (
    action_arguments,
    choose_window,
    implicated_dependency,
    leading,
    next_log_query,
    pick_error_metric,
    ready_to_propose,
    recent_deploys,
)
from incident_desk.tools import TOOL_SPECS

PLAN_TITLES = [
    "Load the paging service contract and metric catalog",
    "Read a health snapshot once the metrics store accepts the window",
    "Compare recent deploys for the service and its callers",
    "Search logs, and broaden the query if the alert title misses",
    "Check any dependency the errors actually name",
    "Read the runbook and stop for approval before changing anything",
]


class Decision(BaseModel):
    thought: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    recovery_note: str | None = None


def build_plan() -> list[PlanStep]:
    return [PlanStep(id=f"step-{index}", title=title) for index, title in enumerate(PLAN_TITLES)]


class HeuristicPolicy:
    """Choose the next tool from missing evidence, not from the incident id."""

    name = "heuristic"

    def draft_plan(self, incident: dict) -> tuple[str, list[PlanStep]]:
        service = incident["service"]
        alert = incident["alert"]
        thought = (
            f"Paging {service} for {alert['name']} ({alert['metric']} over {alert['window']}). "
            "I will load the catalog before trusting that metric name, then deploys, logs, "
            "and any dependency the errors name. I will not change production myself."
        )
        return thought, build_plan()

    def decide(self, incident: dict, memory: dict, hypotheses: list) -> Decision:
        facts = memory["facts"]
        service = incident["service"]
        alert = incident["alert"]
        override = memory.get("operator_override")

        if override and override["action"] not in memory.get("banned_actions", []):
            action = override["action"]
            return Decision(
                thought=(
                    f"The operator asked for {action.replace('_', ' ')}. "
                    "I will propose that and wait. A hint is not approval, and I will verify it."
                ),
                tool="propose_remediation",
                arguments={
                    "action": action,
                    "service": service,
                    "args": action_arguments(action, incident, facts),
                    "rationale": override.get("note") or f"Operator requested {action}.",
                },
            )

        if "profile" not in facts:
            return Decision(
                thought=(
                    f"The page names {alert['metric']}, which may not be a real series. "
                    f"Loading {service} so I can see the catalog, callers, and dependencies."
                ),
                tool="get_service",
                arguments={"service": service},
            )

        if service not in facts["snapshots"]:
            rejected = memory.get("rejected_windows") or []
            if rejected:
                window = choose_window(alert["window"], rejected)
                thought = (
                    f"The metrics store rejected window {rejected[-1]}. "
                    f"Allowed windows are 5m, 15m, and 1h. The alert covers about "
                    f"{alert['window']}, so {window} is the tightest fit. Retrying the snapshot."
                )
            else:
                window = alert["window"]
                catalog = ", ".join(facts["profile"]["metrics"])
                thought = (
                    f"The alert metric {alert['metric']} is not in the catalog ({catalog}). "
                    f"I will snapshot the real series instead of querying a name the service does not export. "
                    f"Using the alert window {window}."
                )
            return Decision(
                thought=thought,
                tool="get_health_snapshot",
                arguments={"service": service, "window": window},
            )

        if service not in facts["deploys"]:
            return Decision(
                thought="Snapshot is in. Next I want deploys inside the last three hours, before blaming a dependency.",
                tool="list_recent_deploys",
                arguments={"service": service},
            )

        for caller in facts["profile"].get("callers", []):
            if caller not in facts["deploys"]:
                return Decision(
                    thought=(
                        f"{caller} calls {service}. I want its recent changes before treating this as a local fault."
                    ),
                    tool="list_recent_deploys",
                    arguments={"service": caller},
                )
            if recent_deploys(facts["deploys"][caller]) and caller not in facts["profiles"]:
                return Decision(
                    thought=f"{caller} changed inside the window. Loading its catalog before I read metrics.",
                    tool="get_service",
                    arguments={"service": caller},
                )
            if recent_deploys(facts["deploys"][caller]) and caller not in facts["snapshots"]:
                metric = pick_error_metric(facts["profiles"][caller]["metrics"])
                return Decision(
                    thought=(
                        f"Checking {caller} {metric}. If it stayed at baseline, the recent deploy is a red herring."
                    ),
                    tool="query_metrics",
                    arguments={"service": caller, "metric": metric, "window": memory["window"]},
                )

        query = next_log_query(facts, incident)
        if query is not None:
            if not facts["log_queries"]:
                thought = (
                    f"Searching {service} logs for the alert title {query!r}. "
                    "If the title is not in the log text, I will switch to a word the snapshot supports."
                )
            else:
                thought = f"{facts['log_queries'][-1]['query']!r} matched nothing. Searching {query!r} instead."
            return Decision(
                thought=thought,
                tool="search_logs",
                arguments={"service": service, "query": query},
            )

        dependency = implicated_dependency(facts, service)
        if dependency and dependency not in facts["snapshots"]:
            return Decision(
                thought=(
                    f"Errors name {dependency}. I need its health before deciding the fault is in {service}."
                ),
                tool="get_health_snapshot",
                arguments={"service": dependency, "window": memory["window"]},
            )

        if "runbook" not in facts:
            return Decision(
                thought=f"Evidence is assembled. Reading the {service} runbook before I propose an action.",
                tool="get_runbook",
                arguments={"service": service},
            )

        best = leading(hypotheses)
        if ready_to_propose(facts, memory, incident, best):
            assert best is not None
            return Decision(
                thought=(
                    f"Confidence {best.confidence:.2f} on: {best.claim} "
                    "Proposing that action and stopping for an operator."
                ),
                tool="propose_remediation",
                arguments={
                    "action": best.suggested_action,
                    "service": best.action_args.get("service", service),
                    "args": {
                        key: value
                        for key, value in best.action_args.items()
                        if key != "service"
                    },
                    "rationale": best.claim,
                },
            )

        banned = ", ".join(memory.get("banned_actions", [])) or "none"
        claim = best.claim if best else "no supported hypothesis"
        return Decision(
            thought=f"I will not invent a change. Banned actions: {banned}. Best remaining read: {claim}",
            tool="request_guidance",
            arguments={
                "question": (
                    "Which action should I propose? "
                    f"Banned so far: {banned}. Current read: {claim}"
                )
            },
        )


SYSTEM_PROMPT = """You are the investigator on an on-call desk. You are not a chatbot.
Use tools to gather evidence. Do not conclude from the alert alone.
The alert metric name and window are often wrong. Read tool errors and adapt.
Never repeat a call that already failed with the same arguments.
Do not call execute_remediation. You cannot change production.
Call propose_remediation only when logs, the runbook, and any named dependency agree.
Call request_guidance when the evidence does not support an allowed action.
Respond with JSON only: {"thought": "...", "tool": "...", "arguments": {...}}
"""


class LLMPolicy:
    name = "llm"

    def __init__(self, heuristic: HeuristicPolicy | None = None):
        self.heuristic = heuristic or HeuristicPolicy()
        self.model = os.environ.get("AGENT_MODEL", "gpt-4o-mini")
        self.base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.api_key = os.environ.get("OPENAI_API_KEY", "")

    def draft_plan(self, incident: dict) -> tuple[str, list[PlanStep]]:
        return self.heuristic.draft_plan(incident)

    def decide(self, incident: dict, memory: dict, hypotheses: list) -> Decision:
        if not self.api_key:
            fallback = self.heuristic.decide(incident, memory, hypotheses)
            fallback.recovery_note = "OPENAI_API_KEY is missing. Used the evidence-gap policy for this step."
            return fallback
        payload = {
            "incident": incident,
            "plan": memory.get("_plan_view", []),
            "hypotheses": [
                {
                    "id": item.id,
                    "claim": item.claim,
                    "confidence": item.confidence,
                    "status": item.status,
                    "suggested_action": item.suggested_action,
                }
                for item in hypotheses
            ],
            "facts": _compact_facts(memory["facts"]),
            "failed_calls": memory.get("failed_calls", []),
            "banned_actions": memory.get("banned_actions", []),
            "operator_hints": memory.get("operator_hints", []),
            "window": memory.get("window"),
            "tools": [
                spec
                for spec in TOOL_SPECS
                if spec["name"] != "execute_remediation"
            ],
        }
        try:
            content = _chat(self.base_url, self.api_key, self.model, payload)
            decision = parse_decision(content)
        except (OSError, urllib.error.URLError, KeyError, ValueError, json.JSONDecodeError) as exc:
            fallback = self.heuristic.decide(incident, memory, hypotheses)
            fallback.recovery_note = f"Model call failed ({exc.__class__.__name__}). Used the evidence-gap policy for this step."
            return fallback
        return decision


def parse_decision(text: str) -> Decision:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("model response did not contain JSON")
    data = json.loads(raw[start : end + 1])
    arguments = data.get("arguments") or data.get("args") or {}
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    tool = data.get("tool")
    thought = data.get("thought")
    if not isinstance(tool, str) or not isinstance(thought, str):
        raise ValueError("thought and tool are required")
    return Decision(thought=thought, tool=tool, arguments=arguments)


def resolve_policy_name(requested: str | None) -> str:
    choice = requested or os.environ.get("AGENT_POLICY", "auto")
    if choice == "auto":
        return "llm" if os.environ.get("OPENAI_API_KEY") else "heuristic"
    if choice not in {"llm", "heuristic"}:
        raise ValueError("policy must be auto, heuristic, or llm")
    return choice


def _compact_facts(facts: dict) -> dict:
    snapshots = {
        service: {name: reading["assessment"] for name, reading in metrics.items()}
        for service, metrics in facts.get("snapshots", {}).items()
    }
    deploys = {
        service: [
            {
                "version": deploy["version"],
                "previous_version": deploy["previous_version"],
                "age_minutes": deploy["age_minutes"],
                "note": deploy["note"],
                "risky": deploy["risky"],
            }
            for deploy in rows[:3]
        ]
        for service, rows in facts.get("deploys", {}).items()
    }
    return {
        "service": (facts.get("profile") or {}).get("name"),
        "metrics_catalog": (facts.get("profile") or {}).get("metrics"),
        "callers": (facts.get("profile") or {}).get("callers"),
        "dependencies": (facts.get("profile") or {}).get("dependencies"),
        "snapshots": snapshots,
        "deploys": deploys,
        "log_queries": facts.get("log_queries", []),
        "log_lines": [line["message"] for line in facts.get("logs", [])[:6]],
        "runbook": facts.get("runbook"),
    }


def _chat(base_url: str, api_key: str, model: str, payload: dict) -> str:
    body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
        }
    ).encode()
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        parsed = json.loads(response.read().decode())
    return parsed["choices"][0]["message"]["content"]
