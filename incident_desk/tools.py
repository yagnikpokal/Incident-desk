from __future__ import annotations

from incident_desk.world import Platform, ToolError

# Tools the investigator may call. execute_remediation is deliberately absent.
WORLD_TOOLS = {
    "get_service",
    "query_metrics",
    "get_health_snapshot",
    "list_recent_deploys",
    "search_logs",
    "get_runbook",
}

CONTROL_TOOLS = {"propose_remediation", "request_guidance"}

TOOL_SPECS = [
    {
        "name": "get_service",
        "mutates": False,
        "requires_approval": False,
        "description": "Service contract: tier, callers, dependencies, and the metric catalog.",
        "arguments": {"service": "string"},
    },
    {
        "name": "query_metrics",
        "mutates": False,
        "requires_approval": False,
        "description": "One series. Window must be 5m, 15m, or 1h. Unknown names return the catalog.",
        "arguments": {"service": "string", "metric": "string", "window": "5m|15m|1h"},
    },
    {
        "name": "get_health_snapshot",
        "mutates": False,
        "requires_approval": False,
        "description": "Every catalog series for a service, with baseline and assessment.",
        "arguments": {"service": "string", "window": "5m|15m|1h"},
    },
    {
        "name": "list_recent_deploys",
        "mutates": False,
        "requires_approval": False,
        "description": "Deploys for one service, newest first, with age against the incident clock.",
        "arguments": {"service": "string"},
    },
    {
        "name": "search_logs",
        "mutates": False,
        "requires_approval": False,
        "description": "Case-insensitive substring search. An empty result includes a hint, not an exception.",
        "arguments": {"service": "string", "query": "string"},
    },
    {
        "name": "get_runbook",
        "mutates": False,
        "requires_approval": False,
        "description": "The runbook filed for a service, including the actions it allows.",
        "arguments": {"service": "string"},
    },
    {
        "name": "propose_remediation",
        "mutates": False,
        "requires_approval": False,
        "description": "Stop the loop and ask an operator to approve one action. Does not change production.",
        "arguments": {
            "action": "rollback|expand_volume|restart_service",
            "service": "string",
            "args": "object",
            "rationale": "string",
        },
    },
    {
        "name": "request_guidance",
        "mutates": False,
        "requires_approval": False,
        "description": "Stop and ask the operator a question when evidence does not clear the bar.",
        "arguments": {"question": "string"},
    },
    {
        "name": "execute_remediation",
        "mutates": True,
        "requires_approval": True,
        "description": "Apply an approved action. The agent loop cannot call this. The approve endpoint does.",
        "arguments": {"action": "string", "service": "string", "args": "object"},
    },
]


class Registry:
    def __init__(self, platform: Platform):
        self.platform = platform

    def call(self, name: str, arguments: dict) -> dict:
        if name not in WORLD_TOOLS:
            raise ToolError("unknown_tool", f"'{name}' is not a platform tool.")
        fn = getattr(self.platform, name)
        try:
            return fn(**arguments)
        except TypeError as exc:
            raise ToolError("bad_arguments", str(exc)) from exc
