"""Two platform snapshots and the service catalog.

The catalog is shared. Each incident points at a scenario so the charge outage
and the full disk do not contaminate each other. Ages are computed from the
incident timestamp, not from the wall clock.
"""

from __future__ import annotations

CATALOG: dict[str, dict] = {
    "edge-gateway": {
        "name": "edge-gateway",
        "tier": "edge",
        "description": "Terminates TLS and routes into the cluster.",
        "callers": [],
        "dependencies": ["payments-api"],
        "metrics": ["http_server_errors_total", "request_latency_p99"],
        "oncall": "edge-primary",
    },
    "payments-api": {
        "name": "payments-api",
        "tier": "critical",
        "description": "Authorizes card charges and submits the entry to the ledger.",
        "callers": ["edge-gateway"],
        "dependencies": ["ledger-worker"],
        "metrics": [
            "http_server_errors_total",
            "ledger_client_timeouts_total",
            "request_latency_p99",
        ],
        "oncall": "payments-primary",
    },
    "ledger-worker": {
        "name": "ledger-worker",
        "tier": "critical",
        "description": "Appends charge entries and commits consumer offsets.",
        "callers": ["payments-api"],
        "dependencies": [],
        "metrics": ["consumer_lag", "disk_used_ratio", "process_restarts"],
        "oncall": "ledger-primary",
    },
}

INCIDENTS: dict[str, dict] = {
    "inc-1042": {
        "id": "inc-1042",
        "scenario": "charge-timeouts",
        "title": "Charge error rate is 18% on payments-api",
        "service": "payments-api",
        "severity": "sev-2",
        "opened_at": "2026-10-28T02:14:00Z",
        "alert": {
            "name": "HighErrorRate",
            "metric": "http_error_ratio",
            "value": 0.18,
            "baseline": 0.004,
            "window": "10m",
        },
        "symptom": "POST /v1/charges is returning 500 and 504.",
    },
    "inc-1077": {
        "id": "inc-1077",
        "scenario": "ledger-disk",
        "title": "Ledger consumer lag is climbing",
        "service": "ledger-worker",
        "severity": "sev-2",
        "opened_at": "2026-10-28T04:40:00Z",
        "alert": {
            "name": "HighConsumerLag",
            "metric": "queue_lag",
            "value": 180000,
            "baseline": 20,
            "window": "10m",
        },
        "symptom": "The ledger consumer has stopped committing offsets.",
    },
}


def _reading(value: float, baseline: float, unit: str, assessment: str) -> dict:
    return {
        "value": value,
        "baseline": baseline,
        "unit": unit,
        "assessment": assessment,
    }


_HEALTHY_EDGE = {
    "http_server_errors_total": _reading(0.003, 0.004, "ratio", "normal"),
    "request_latency_p99": _reading(0.08, 0.07, "seconds", "normal"),
}
_HEALTHY_PAYMENTS = {
    "http_server_errors_total": _reading(0.005, 0.004, "ratio", "normal"),
    "ledger_client_timeouts_total": _reading(0.1, 0.2, "per_sec", "normal"),
    "request_latency_p99": _reading(0.27, 0.28, "seconds", "normal"),
}
_HEALTHY_LEDGER = {
    "consumer_lag": _reading(6, 12, "messages", "normal"),
    "disk_used_ratio": _reading(0.41, 0.55, "ratio", "normal"),
    "process_restarts": _reading(0, 0, "count", "normal"),
}

SCENARIOS: dict[str, dict] = {
    "charge-timeouts": {
        "metrics": {
            "edge-gateway": _HEALTHY_EDGE,
            "payments-api": {
                "http_server_errors_total": _reading(0.18, 0.004, "ratio", "elevated"),
                "ledger_client_timeouts_total": _reading(41.0, 0.2, "per_sec", "elevated"),
                "request_latency_p99": _reading(1.4, 0.28, "seconds", "elevated"),
            },
            "ledger-worker": _HEALTHY_LEDGER,
        },
        "deploys": [
            {
                "service": "payments-api",
                "version": "2.14.3",
                "previous_version": "2.14.2",
                "at": "2026-10-28T01:56:00Z",
                "note": "Tighten the ledger client timeout from 800ms to 50ms to cut tail latency.",
                "author": "deploy-bot",
            },
            {
                "service": "payments-api",
                "version": "2.14.2",
                "previous_version": "2.14.1",
                "at": "2026-10-27T16:00:00Z",
                "note": "No behavior change. Dependency bump only.",
                "author": "deploy-bot",
            },
            {
                "service": "edge-gateway",
                "version": "1.9.0",
                "previous_version": "1.8.4",
                "at": "2026-10-28T00:10:00Z",
                "note": "Reload the edge rate-limit config for the marketing campaign.",
                "author": "edge-oncall",
            },
            {
                "service": "ledger-worker",
                "version": "4.2.1",
                "previous_version": "4.2.0",
                "at": "2026-10-22T03:00:00Z",
                "note": "Bump the kafka client.",
                "author": "ledger-oncall",
            },
        ],
        "logs": [
            {
                "id": "p1",
                "service": "payments-api",
                "at": "2026-10-28T02:05:00Z",
                "level": "warn",
                "message": "redis reconnect succeeded after 1 attempt",
            },
            {
                "id": "p2",
                "service": "payments-api",
                "at": "2026-10-28T02:08:12Z",
                "level": "error",
                "message": "ledger client timeout after 50ms on PostEntry",
            },
            {
                "id": "p3",
                "service": "payments-api",
                "at": "2026-10-28T02:08:13Z",
                "level": "error",
                "message": "charge failed: upstream timeout on POST /v1/charges",
            },
            {
                "id": "p4",
                "service": "payments-api",
                "at": "2026-10-28T02:11:40Z",
                "level": "error",
                "message": "ledger client timeout after 50ms on PostEntry",
            },
        ],
        "runbooks": [
            {
                "id": "RB-PAY-7",
                "service": "payments-api",
                "title": "Elevated charge failures",
                "preferred_actions": ["rollback"],
                "guidance": (
                    "If ledger client timeouts spiked, the ledger itself is healthy, "
                    "and a deploy in the last 3 hours changed that client timeout, "
                    "roll the caller back to the previous version. Do not restart the ledger."
                ),
            },
            {
                "id": "RB-EDGE-1",
                "service": "edge-gateway",
                "title": "Edge error budget",
                "preferred_actions": ["rollback"],
                "guidance": "Only touch the edge tier when its own error rate moved.",
            },
            {
                "id": "RB-LED-1",
                "service": "ledger-worker",
                "title": "Ledger health",
                "preferred_actions": ["expand_volume"],
                "guidance": "Restarting the ledger does not fix a caller-side timeout.",
            },
        ],
        "fixes": {
            "rollback": {
                "payments-api": {
                    "http_server_errors_total": _reading(0.006, 0.004, "ratio", "normal"),
                    "ledger_client_timeouts_total": _reading(0.15, 0.2, "per_sec", "normal"),
                    "request_latency_p99": _reading(0.31, 0.28, "seconds", "normal"),
                }
            }
        },
    },
    "ledger-disk": {
        "metrics": {
            "edge-gateway": _HEALTHY_EDGE,
            "payments-api": _HEALTHY_PAYMENTS,
            "ledger-worker": {
                "consumer_lag": _reading(180000, 20, "messages", "elevated"),
                "disk_used_ratio": _reading(0.97, 0.55, "ratio", "elevated"),
                "process_restarts": _reading(4, 0, "count", "elevated"),
            },
        },
        "deploys": [
            {
                "service": "ledger-worker",
                "version": "4.2.2",
                "previous_version": "4.2.1",
                "at": "2026-10-28T03:10:00Z",
                "note": "Adjust log verbosity for the consumer loop.",
                "author": "ledger-oncall",
            },
            {
                "service": "ledger-worker",
                "version": "4.2.1",
                "previous_version": "4.2.0",
                "at": "2026-10-22T03:00:00Z",
                "note": "Bump the kafka client.",
                "author": "ledger-oncall",
            },
            {
                "service": "payments-api",
                "version": "2.14.2",
                "previous_version": "2.14.1",
                "at": "2026-10-25T16:00:00Z",
                "note": "No behavior change. Dependency bump only.",
                "author": "deploy-bot",
            },
            {
                "service": "edge-gateway",
                "version": "1.8.4",
                "previous_version": "1.8.3",
                "at": "2026-10-20T00:10:00Z",
                "note": "Rotate the edge certificate.",
                "author": "edge-oncall",
            },
        ],
        "logs": [
            {
                "id": "d1",
                "service": "ledger-worker",
                "at": "2026-10-28T04:10:00Z",
                "level": "info",
                "message": "kafka rebalance completed for group ledger",
            },
            {
                "id": "d2",
                "service": "ledger-worker",
                "at": "2026-10-28T04:28:00Z",
                "level": "error",
                "message": "segment flush failed: no space left on device; disk full",
            },
            {
                "id": "d3",
                "service": "ledger-worker",
                "at": "2026-10-28T04:31:00Z",
                "level": "error",
                "message": "consumer stalled and stopped committing offsets",
            },
        ],
        "runbooks": [
            {
                "id": "RB-LED-2",
                "service": "ledger-worker",
                "title": "Consumer lag with disk pressure",
                "preferred_actions": ["expand_volume"],
                "guidance": (
                    "If disk_used_ratio is above 0.9 and logs show the disk is full, "
                    "expand the volume. A rollback will not free disk."
                ),
            },
            {
                "id": "RB-PAY-2",
                "service": "payments-api",
                "title": "Elevated charge failures",
                "preferred_actions": ["rollback"],
                "guidance": "Use this only when payments-api itself is failing.",
            },
        ],
        "fixes": {
            "expand_volume": {
                "ledger-worker": {
                    "consumer_lag": _reading(18, 20, "messages", "normal"),
                    "disk_used_ratio": _reading(0.61, 0.55, "ratio", "normal"),
                    "process_restarts": _reading(0, 0, "count", "normal"),
                }
            }
        },
    },
}
