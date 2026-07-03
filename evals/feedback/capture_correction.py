"""
Capture correction — record a human override of the agent's recommendation.

Every time a reviewer disagrees with the agent's primary_layer call on a real
ticket, that disagreement is a labelled example the eval dataset doesn't have
yet. This script turns it into a structured record appended to
`corrections.jsonl`, one JSON object per line, status "pending".

Design notes:
  * Corrections are captured as their own append-only log, not written
    straight into the eval dataset. Two reasons: (1) a correction is a raw
    signal, not yet a reviewed one — someone still has to confirm it's a
    genuine labelling error and not a one-off judgment call before it
    becomes ground truth; (2) an append-only log means capture can happen
    at the moment of review (fast, low-friction) without touching the
    dataset file that the eval gate depends on.
  * The record captures the same `inputs` shape as a dataset ticket
    (issue_type, components, labels, description, ...) so a promoted
    correction can be dropped straight into ground_truth.example.json
    without reshaping.
  * Every correction requires a `human_note` — a one-line reason. A
    correction with no stated reason is a data point that can't be audited
    later when someone asks "why did we decide PROJ-1042 was BACKEND_E2E
    and not INTEGRATION?".
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CORRECTIONS_PATH = HERE / "corrections.jsonl"

LAYERS = {"UNIT", "INTEGRATION", "BACKEND_E2E", "PORTAL_UI_E2E", "CYPRESS_COMPONENT", "MANUAL"}


def build_record(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "correction_id": str(uuid.uuid4()),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "issue_key": args.issue_key,
        "reviewer": args.reviewer,
        "inputs": {
            "summary": args.summary,
            "issue_type": args.issue_type,
            "components": args.components,
            "labels": args.labels,
            "description": args.description,
            "acceptance_criteria": args.acceptance_criteria,
            "implementation_notes": args.implementation_notes,
            "impact_areas": args.impact_areas,
        },
        "agent_primary": args.agent_primary,
        "agent_supporting": args.agent_supporting,
        "agent_rationale": args.agent_rationale,
        "human_primary": args.human_primary,
        "human_note": args.human_note,
        "failure_mode": args.failure_mode,
        "promoted_at": None,
        "promoted_to_issue_key": None,
    }


def validate(record: dict[str, Any]) -> list[str]:
    """Return a list of validation errors; empty list means the record is clean."""
    errors = []
    if not record["issue_key"]:
        errors.append("issue_key is required")
    if not record["inputs"]["summary"]:
        errors.append("summary is required")
    if not record["reviewer"]:
        errors.append("reviewer is required")
    if not record["human_note"]:
        errors.append("human_note is required — state why the agent's call was wrong")
    if record["agent_primary"] not in LAYERS:
        errors.append(f"agent_primary must be one of {sorted(LAYERS)}")
    if record["human_primary"] not in LAYERS:
        errors.append(f"human_primary must be one of {sorted(LAYERS)}")
    if record["agent_primary"] == record["human_primary"]:
        errors.append(
            "agent_primary and human_primary are identical — this isn't a correction, "
            "nothing to capture"
        )
    return errors


def append_record(record: dict[str, Any]) -> None:
    CORRECTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CORRECTIONS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record a human correction of the agent's test-layer recommendation."
    )
    parser.add_argument("--issue-key", required=True, help="Ticket identifier, e.g. PROJ-1042")
    parser.add_argument("--summary", required=True)
    parser.add_argument("--issue-type", default="None")
    parser.add_argument("--components", default="None")
    parser.add_argument("--labels", default="None")
    parser.add_argument("--description", default="None")
    parser.add_argument("--acceptance-criteria", default="None")
    parser.add_argument("--implementation-notes", default="None")
    parser.add_argument("--impact-areas", default="None")
    parser.add_argument("--agent-primary", required=True, choices=sorted(LAYERS))
    parser.add_argument("--agent-supporting", default=None, choices=sorted(LAYERS))
    parser.add_argument("--agent-rationale", default=None, help="The agent's stated rationale, for the audit trail")
    parser.add_argument("--human-primary", required=True, choices=sorted(LAYERS))
    parser.add_argument("--human-note", required=True, help="One-line reason the agent's call was wrong")
    parser.add_argument("--reviewer", required=True, help="Who made the correction")
    parser.add_argument(
        "--failure-mode",
        default=None,
        choices=["wrong-rank", "over-automation", "under-automation"],
        help="Optional classification of the failure — leave unset if unsure; "
             "promote_corrections.py will infer 'wrong-rank' automatically when "
             "human_primary matches agent_supporting.",
    )
    args = parser.parse_args()

    record = build_record(args)
    errors = validate(record)
    if errors:
        print("ERROR: correction record failed validation:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 2

    append_record(record)
    print(f"Recorded correction {record['correction_id']} for {record['issue_key']} (status: pending)")
    print(f"Run promote_corrections.py to review and merge pending corrections into the dataset.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
