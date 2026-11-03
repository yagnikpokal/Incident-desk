"""Print one investigation so the timeline can be read without the browser."""

from __future__ import annotations

import tempfile
from pathlib import Path

from incident_desk.service import Desk
from incident_desk.store import Store


def narrate(incident_id: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        desk = Desk(Store(Path(directory) / "desk.sqlite"))
        run = desk.create_run(incident_id, policy="heuristic")
        print(f"\n=== {incident_id} {run.id} ===")
        for _ in range(run.max_steps):
            result = desk.advance(run.id)
            run = result.run
            if not result.applied:
                break
        for event in run.events:
            print(f"{event.kind:20} {event.summary}")
        print(f"status={run.status.value} steps={run.step_count}")
        if run.proposal:
            print(f"proposal={run.proposal.action} {run.proposal.args} conf={run.proposal.confidence}")
        for hypothesis in run.hypotheses:
            print(f"  [{hypothesis.status} {hypothesis.confidence:.2f}] {hypothesis.claim}")


def main() -> None:
    narrate("inc-1042")
    narrate("inc-1077")


if __name__ == "__main__":
    main()
