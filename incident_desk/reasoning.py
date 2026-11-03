"""Evidence scoring and the next-query helpers shared by the policies.

Hypotheses are derived here, from tool results, so a model cannot invent a
confidence number the operator then has to trust.
"""

from __future__ import annotations

import json
from typing import Any

from incident_desk.models import Hypothesis
from incident_desk.world import RECENT_DEPLOY_MINUTES

PROPOSE_AT = 0.75
KNOWN_ACTIONS = ("expand_volume", "rollback", "restart_service")


def call_signature(tool: str, arguments: dict) -> str:
    return tool + " " + json.dumps(arguments, sort_keys=True, default=str)


def parse_minutes(window: str) -> int:
    if window.endswith("h"):
        return int(window[:-1]) * 60
    if window.endswith("m"):
        return int(window[:-1])
    return 10


def choose_window(alert_window: str, rejected: list[str]) -> str:
    allowed = ["5m", "15m", "1h"]
    target = parse_minutes(alert_window)
    ranked = sorted(allowed, key=lambda item: (parse_minutes(item) < target, abs(parse_minutes(item) - target)))
    for window in ranked:
        if window not in rejected:
            return window
    return "1h"


def pick_error_metric(names: list[str]) -> str:
    for name in names:
        if "error" in name:
            return name
    return names[0]


def recent_deploys(deploys: list[dict]) -> list[dict]:
    return [deploy for deploy in deploys if deploy["age_minutes"] <= RECENT_DEPLOY_MINUTES]


def implicated_dependency(facts: dict, service: str) -> str | None:
    profile = facts.get("profile") or {}
    if profile.get("name") != service:
        return None
    blob = " ".join(facts.get("snapshots", {}).get(service, {})).lower()
    blob += " " + " ".join(line["message"].lower() for line in facts.get("logs", []))
    for dependency in profile.get("dependencies", []):
        token = dependency.split("-")[0]
        if token and token in blob:
            return dependency
    return None


def candidate_log_terms(facts: dict, incident: dict) -> list[str]:
    snapshot = facts.get("snapshots", {}).get(incident["service"], {})
    disk = snapshot.get("disk_used_ratio") or {}
    terms: list[str] = []
    if disk.get("value", 0) >= 0.9:
        terms.append("disk")
    if any("timeout" in name for name in snapshot):
        terms.append("timeout")
    if any("lag" in name for name in snapshot):
        terms.append("lag")
    terms.append("error")
    return terms


def next_log_query(facts: dict, incident: dict) -> str | None:
    queries = facts.get("log_queries", [])
    if queries and queries[-1]["count"] > 0:
        return None
    if len(queries) >= 3:
        return None
    tried = {query["query"] for query in queries}
    if not queries:
        return incident["alert"]["name"]
    for term in candidate_log_terms(facts, incident):
        if term not in tried:
            return term
    return None


def callers_cleared(facts: dict, service: str) -> bool:
    profile = facts.get("profile")
    if not profile or service not in facts.get("deploys", {}):
        return False
    for caller in profile.get("callers", []):
        deploys = facts.get("deploys", {}).get(caller)
        if deploys is None:
            return False
        if recent_deploys(deploys) and caller not in facts.get("snapshots", {}):
            return False
    return True


def _log_text(facts: dict) -> str:
    return " ".join(line["message"].lower() for line in facts.get("logs", []))


def _cap(value: float) -> float:
    return round(min(value, 0.95), 2)


def derive_hypotheses(facts: dict, incident: dict) -> list[Hypothesis]:
    service = incident["service"]
    metrics = facts.get("snapshots", {}).get(service, {})
    deploys = facts.get("deploys", {}).get(service, [])
    recent = recent_deploys(deploys) if deploys else []
    risky = [deploy for deploy in recent if deploy.get("risky")]
    logs = _log_text(facts)
    runbook = facts.get("runbook")
    hypotheses: list[Hypothesis] = []

    profile = facts.get("profile") or {}
    for caller in profile.get("callers", []):
        caller_deploys = facts.get("deploys", {}).get(caller)
        if not caller_deploys:
            continue
        caller_recent = recent_deploys(caller_deploys)
        if not caller_recent:
            continue
        latest = caller_recent[0]
        snapshot = facts.get("snapshots", {}).get(caller)
        base = (
            f"{caller} {latest['version']} went out {latest['age_minutes']}m ago: {latest['note']}"
        )
        if snapshot is None:
            hypotheses.append(
                Hypothesis(
                    id=f"caller-{caller}",
                    claim=base + " Not enough to blame it yet.",
                    confidence=0.28,
                    status="open",
                    evidence=[latest["note"]],
                )
            )
        else:
            error_name = pick_error_metric(list(snapshot))
            reading = snapshot[error_name]
            if reading["assessment"] == "normal":
                hypotheses.append(
                    Hypothesis(
                        id=f"caller-{caller}",
                        claim=base + " Its error rate stayed at baseline, so this is a red herring.",
                        confidence=0.12,
                        status="rejected",
                        evidence=[f"{error_name}={reading['value']} ({reading['assessment']})"],
                    )
                )
            else:
                hypotheses.append(
                    Hypothesis(
                        id=f"caller-{caller}",
                        claim=base + " Its own error rate moved with the deploy.",
                        confidence=0.62,
                        status="open",
                        suggested_action="restart_service",
                        action_args={"service": caller},
                        evidence=[f"{error_name}={reading['value']} ({reading['assessment']})"],
                    )
                )

    if recent and not risky:
        latest = recent[0]
        disk = metrics.get("disk_used_ratio") or {}
        disk_explains = disk.get("assessment") == "elevated" or "no space left" in logs or "disk full" in logs
        hypotheses.append(
            Hypothesis(
                id="recent-deploy",
                claim=(
                    f"{service} {latest['version']} ({latest['age_minutes']}m ago) only notes "
                    f"'{latest['note']}'. That does not explain the page."
                ),
                confidence=0.12 if disk_explains else 0.22,
                status="rejected" if disk_explains else "open",
                evidence=[latest["note"]],
            )
        )

    disk = metrics.get("disk_used_ratio")
    if disk and disk["assessment"] == "elevated" and disk["value"] >= 0.9:
        confidence = 0.4
        evidence = [f"disk_used_ratio={disk['value']} against baseline {disk['baseline']}"]
        if "no space left" in logs or "disk full" in logs:
            confidence += 0.22
            evidence.append("logs say the disk is full")
        lag = metrics.get("consumer_lag")
        if lag and lag["assessment"] == "elevated":
            evidence.append(f"consumer_lag={lag['value']} is the symptom, not a separate cause")
        if deploys and not risky:
            confidence += 0.1
            evidence.append("no risky deploy in the last 3 hours")
        if runbook and runbook.get("service") == service and "expand_volume" in runbook.get("preferred_actions", []):
            if confidence >= 0.6:
                confidence += 0.16
                evidence.append(f"runbook {runbook['id']} allows expand_volume")
        hypotheses.append(
            Hypothesis(
                id="disk-full",
                claim=f"{service} is failing because the disk is full.",
                confidence=_cap(confidence),
                status="supported" if confidence >= PROPOSE_AT else "open",
                suggested_action="expand_volume",
                action_args={"service": service, "amount_gb": 50},
                evidence=evidence,
            )
        )

    timeout_metric = next(
        ((name, reading) for name, reading in metrics.items() if "timeout" in name and reading["assessment"] == "elevated"),
        None,
    )
    timeout_deploy = next((deploy for deploy in risky if "timeout" in deploy["note"].lower()), None)
    dependency = implicated_dependency(facts, service)
    dependency_snapshot = facts.get("snapshots", {}).get(dependency, {}) if dependency else {}
    dependency_healthy = bool(dependency_snapshot) and all(
        reading["assessment"] == "normal" for reading in dependency_snapshot.values()
    )
    if timeout_deploy and (timeout_metric or "timeout" in logs):
        if dependency and dependency_snapshot and not dependency_healthy:
            hypotheses.append(
                Hypothesis(
                    id="timeout-regression",
                    claim=f"{dependency} is unhealthy, so rolling back {service} is the wrong first move.",
                    confidence=0.2,
                    status="rejected",
                    evidence=[f"{dependency} snapshot is not healthy"],
                )
            )
        else:
            confidence = 0.34
            evidence = [
                f"deploy {timeout_deploy['version']} {timeout_deploy['age_minutes']}m ago: {timeout_deploy['note']}"
            ]
            if timeout_metric:
                confidence += 0.18
                evidence.append(f"{timeout_metric[0]}={timeout_metric[1]['value']} ({timeout_metric[1]['assessment']})")
            if "timeout" in logs:
                confidence += 0.16
                evidence.append("logs record client timeouts")
            if dependency and not dependency_snapshot:
                confidence = min(confidence, 0.55)
                evidence.append(f"{dependency} has not been checked")
                claim = (
                    f"{service} {timeout_deploy['version']} tightened a client timeout and "
                    f"the error rate moved with it. {dependency} is not checked yet."
                )
            elif dependency_healthy:
                confidence += 0.18
                evidence.append(f"{dependency} is healthy, so the timeout is on the caller")
                claim = (
                    f"{service} {timeout_deploy['version']} tightened a client timeout and "
                    "started failing while its dependency stayed healthy."
                )
            else:
                claim = (
                    f"{service} {timeout_deploy['version']} tightened a client timeout and "
                    "the error rate moved with it."
                )
            if (
                runbook
                and "rollback" in runbook.get("preferred_actions", [])
                and confidence >= 0.6
            ):
                confidence += 0.12
                evidence.append(f"runbook {runbook['id']} allows rollback")
            hypotheses.append(
                Hypothesis(
                    id="timeout-regression",
                    claim=claim,
                    confidence=_cap(confidence),
                    status="supported" if confidence >= PROPOSE_AT else "open",
                    suggested_action="rollback",
                    action_args={
                        "service": service,
                        "to_version": timeout_deploy["previous_version"],
                    },
                    evidence=evidence,
                )
            )

    hypotheses.sort(key=lambda item: (item.status == "rejected", -item.confidence))
    return hypotheses


def leading(hypotheses: list[Hypothesis]) -> Hypothesis | None:
    actionable = [item for item in hypotheses if item.status != "rejected" and item.suggested_action]
    if not actionable:
        return None
    return max(actionable, key=lambda item: item.confidence)


def ready_to_propose(facts: dict, memory: dict, incident: dict, hypothesis: Hypothesis | None) -> bool:
    if hypothesis is None or hypothesis.status != "supported":
        return False
    if hypothesis.confidence < PROPOSE_AT:
        return False
    if hypothesis.suggested_action in memory.get("banned_actions", []):
        return False
    if not any(query["count"] > 0 for query in facts.get("log_queries", [])):
        return False
    runbook = facts.get("runbook")
    if not runbook or hypothesis.suggested_action not in runbook.get("preferred_actions", []):
        return False
    dependency = implicated_dependency(facts, incident["service"])
    if dependency and dependency not in facts.get("snapshots", {}):
        return False
    return True


def parse_operator_action(note: str) -> str | None:
    text = note.lower().replace("_", " ").replace("-", " ")
    if "expand" in text and "volume" in text:
        return "expand_volume"
    if "roll back" in text or "rollback" in text:
        return "rollback"
    if "restart" in text:
        return "restart_service"
    return None


def action_arguments(action: str, incident: dict, facts: dict) -> dict[str, Any]:
    service = incident["service"]
    if action == "rollback":
        deploys = facts.get("deploys", {}).get(service) or []
        previous = deploys[0]["previous_version"] if deploys else ""
        return {"to_version": previous}
    if action == "expand_volume":
        return {"amount_gb": 50}
    return {}
