from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from incident_desk.fixtures import CATALOG, SCENARIOS

ALLOWED_WINDOWS = ("5m", "15m", "1h")
RECENT_DEPLOY_MINUTES = 180
RISKY_WORDS = ("timeout", "limit", "retry", "pool", "memory", "config", "flag")


class ToolError(Exception):
    def __init__(self, code: str, message: str, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def risky_note(note: str) -> bool:
    text = note.lower()
    return any(word in text for word in RISKY_WORDS)


class Platform:
    """Read-only platform, plus remediations recorded on a per-run overlay."""

    def __init__(self, incident: dict, overlay: dict):
        self.incident = incident
        self.scenario = SCENARIOS[incident["scenario"]]
        self.overlay = overlay

    def get_service(self, service: str) -> dict:
        if service not in CATALOG:
            raise ToolError(
                "unknown_service",
                f"Unknown service '{service}'.",
                {"available": sorted(CATALOG)},
            )
        return deepcopy(CATALOG[service])

    def query_metrics(self, service: str, metric: str, window: str) -> dict:
        self._check_window(window)
        catalog = self.get_service(service)["metrics"]
        if metric not in catalog:
            raise ToolError(
                "unknown_metric",
                f"'{metric}' is not exported by {service}.",
                {
                    "available": catalog,
                    "hint": "Query a name from the service catalog, or take a health snapshot.",
                },
            )
        return {
            "service": service,
            "metric": metric,
            "window": window,
            "reading": self._reading(service, metric),
        }

    def get_health_snapshot(self, service: str, window: str) -> dict:
        self._check_window(window)
        profile = self.get_service(service)
        metrics = [
            {"name": name, **self._reading(service, name)} for name in profile["metrics"]
        ]
        return {"service": service, "window": window, "metrics": metrics}

    def list_recent_deploys(self, service: str) -> dict:
        self.get_service(service)
        opened = parse_time(self.incident["opened_at"])
        rows = []
        for deploy in self.scenario["deploys"]:
            if deploy["service"] != service:
                continue
            age = int((opened - parse_time(deploy["at"])).total_seconds() // 60)
            rows.append({**deploy, "age_minutes": age, "risky": risky_note(deploy["note"])})
        rows.sort(key=lambda row: row["age_minutes"])
        return {"service": service, "deploys": rows}

    def search_logs(self, service: str, query: str) -> dict:
        self.get_service(service)
        needle = query.lower().strip()
        if not needle:
            raise ToolError("empty_query", "Log query must not be empty.")
        lines = [
            deepcopy(line)
            for line in self.scenario["logs"]
            if line["service"] == service and needle in line["message"].lower()
        ]
        hint = None
        if not lines:
            hint = (
                "No lines matched. Alert titles often miss the log text. "
                "Retry with a concrete word such as timeout, disk, or refused."
            )
        return {
            "service": service,
            "query": query,
            "count": len(lines),
            "lines": lines[:8],
            "hint": hint,
        }

    def get_runbook(self, service: str) -> dict:
        self.get_service(service)
        for book in self.scenario["runbooks"]:
            if book["service"] == service:
                return deepcopy(book)
        raise ToolError("no_runbook", f"No runbook is filed for {service}.")

    def execute(self, action: str, service: str, args: dict | None = None) -> dict:
        args = dict(args or {})
        self.get_service(service)
        if action == "rollback":
            deploys = self.list_recent_deploys(service)["deploys"]
            if not deploys:
                raise ToolError("no_deploy", f"{service} has no deploy to roll back.")
            target = args.get("to_version")
            previous = deploys[0]["previous_version"]
            if target != previous:
                raise ToolError(
                    "bad_version",
                    f"{target} is not the previous release of {service}.",
                    {"previous_version": previous},
                )
        shifts = deepcopy(self.scenario["fixes"].get(action, {}).get(service, {}))
        self.overlay.setdefault("shifts", {}).setdefault(service, {}).update(shifts)
        record = {"action": action, "service": service, "args": args}
        self.overlay.setdefault("actions", []).append(record)
        return {
            "ok": True,
            "action": record,
            "metrics_moved": sorted(shifts),
        }

    def _check_window(self, window: str) -> None:
        if window not in ALLOWED_WINDOWS:
            raise ToolError(
                "unsupported_window",
                f"Metrics are not stored at window {window}.",
                {
                    "allowed": list(ALLOWED_WINDOWS),
                    "hint": "Pick the shortest allowed window that still covers the alert.",
                },
            )

    def _reading(self, service: str, metric: str) -> dict:
        shifted = self.overlay.get("shifts", {}).get(service, {}).get(metric)
        if shifted:
            return deepcopy(shifted)
        try:
            return deepcopy(self.scenario["metrics"][service][metric])
        except KeyError as exc:
            raise ToolError(
                "unknown_metric",
                f"No reading for {service} {metric}.",
            ) from exc
