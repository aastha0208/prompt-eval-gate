"""
Promote corrections — review pending corrections and merge approved ones into
the eval dataset as new, human-verified ground truth.

This is the second half of the loop `capture_correction.py` starts. A
correction on its own is just one reviewer's disagreement with the agent;
promoting it is a second, deliberate act that says "yes, this is a genuine
labelling gap, not a one-off judgment call" and turns it into a permanent
eval fixture.

Design notes:
  * Promotion is interactive by default — each pending correction is shown
    in full and the operator approves, rejects, or skips it. `--auto` exists
    for scripted/demo use but is off by default: silently promoting
    corrections with no human review would defeat the point of a system
    whose entire premise is "a human looked at this and disagreed."
  * A promoted ticket's `verified` field is always `true`. Unlike an
    inferred label, a correction *is* an explicit human verdict by
    construction — that's the whole reason it's worth more than a synthetic
    example.
  * `match_status` and `failure_mode` on the new ticket are derived from
    comparing the agent's original call against the human's, using the same
    match/partial/mismatch scoring the eval runner itself uses. This keeps
    a promoted correction commensurable with the rest of the dataset instead
    of introducing a parallel labelling scheme.
  * issue_key collisions are hard failures. Silently overwriting an existing
    dataset ticket on promotion would make dataset history impossible to
    reconstruct.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EVAL_ROOT = HERE.parent
CORRECTIONS_PATH = HERE / "corrections.jsonl"
DATASET_PATH = EVAL_ROOT / "datasets" / "ground_truth.example.json"

sys.path.insert(0, str(EVAL_ROOT / "runner"))
from eval_runner import score, STATUS_MATCH, STATUS_PARTIAL, STATUS_MISMATCH  # noqa: E402


def load_corrections() -> list[dict[str, Any]]:
    if not CORRECTIONS_PATH.exists():
        return []
    records = []
    for line in CORRECTIONS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def save_corrections(records: list[dict[str, Any]]) -> None:
    with CORRECTIONS_PATH.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def load_dataset() -> dict[str, Any]:
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


def save_dataset(dataset: dict[str, Any]) -> None:
    DATASET_PATH.write_text(json.dumps(dataset, indent=2) + "\n", encoding="utf-8")


def infer_failure_mode(status: str, explicit: str | None) -> str | None:
    """Fall back to a derived failure_mode when the reviewer didn't set one."""
    if explicit:
        return explicit
    if status == STATUS_MATCH:
        return None
    if status == STATUS_PARTIAL:
        return "wrong-rank"
    return "unclassified-mismatch"


def to_dataset_ticket(record: dict[str, Any]) -> dict[str, Any]:
    status = score(record["agent_primary"], record.get("agent_supporting"), record["human_primary"])
    # Dataset tickets keep `summary` at the top level only — matches the
    # shape of the existing synthetic tickets in ground_truth.example.json.
    inputs = {k: v for k, v in record["inputs"].items() if k != "summary"}
    return {
        "issue_key": record["issue_key"],
        "summary": record["inputs"]["summary"],
        "inputs": inputs,
        "agent_recommendation": {
            "primary_layer": record["agent_primary"],
            "supporting": record.get("agent_supporting"),
        },
        "ground_truth": {
            "correct_primary_layer": record["human_primary"],
            "match_status": status,
            "verified": True,
            "failure_mode": infer_failure_mode(status, record.get("failure_mode")),
            "notes": record["human_note"],
            "source": "human_correction",
            "reviewer": record["reviewer"],
            "captured_at": record["captured_at"],
        },
    }


def print_record(record: dict[str, Any]) -> None:
    print("-" * 60)
    print(f"issue_key      : {record['issue_key']}")
    print(f"summary        : {record['inputs']['summary']}")
    print(f"agent said     : {record['agent_primary']}  (supporting: {record.get('agent_supporting')})")
    print(f"agent rationale: {record.get('agent_rationale')}")
    print(f"human says     : {record['human_primary']}")
    print(f"human note     : {record['human_note']}")
    print(f"reviewer       : {record['reviewer']}  at {record['captured_at']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Review pending corrections and promote approved ones into the eval dataset."
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="Promote every pending correction without prompting. For scripted/demo use only.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be promoted without writing to the dataset or corrections log.",
    )
    args = parser.parse_args()

    corrections = load_corrections()
    pending = [r for r in corrections if r["status"] == "pending"]
    if not pending:
        print("No pending corrections.")
        return 0

    dataset = load_dataset()
    existing_keys = {t["issue_key"] for t in dataset["tickets"]}

    promoted_count = 0
    for record in pending:
        print_record(record)

        if record["issue_key"] in existing_keys:
            print(f"SKIP: issue_key {record['issue_key']} already exists in the dataset — resolve manually.")
            continue

        if args.auto:
            decision = "y"
        else:
            decision = input("Promote this correction into the dataset? [y/N/skip]: ").strip().lower()

        if decision not in ("y", "yes"):
            print("Skipped (left pending).")
            continue

        ticket = to_dataset_ticket(record)
        if not args.dry_run:
            dataset["tickets"].append(ticket)
            existing_keys.add(record["issue_key"])
            record["status"] = "promoted"
            record["promoted_at"] = datetime.now(timezone.utc).isoformat()
            record["promoted_to_issue_key"] = record["issue_key"]
        promoted_count += 1
        print(f"{'[dry-run] would promote' if args.dry_run else 'Promoted'} -> "
              f"ground_truth.correct_primary_layer={ticket['ground_truth']['correct_primary_layer']}, "
              f"match_status={ticket['ground_truth']['match_status']}")

    if promoted_count and not args.dry_run:
        dataset["_meta"]["counts"] = {
            "match": sum(1 for t in dataset["tickets"] if t["ground_truth"]["match_status"] == STATUS_MATCH),
            "partial": sum(1 for t in dataset["tickets"] if t["ground_truth"]["match_status"] == STATUS_PARTIAL),
            "mismatch": sum(1 for t in dataset["tickets"] if t["ground_truth"]["match_status"] == STATUS_MISMATCH),
            "total_scored": len(dataset["tickets"]),
        }
        save_dataset(dataset)
        save_corrections(corrections)
        print(f"\nPromoted {promoted_count} correction(s). Dataset now has {len(dataset['tickets'])} tickets.")
        print("Re-run the eval and re-commit baseline/candidate majority files before relying on the gate.")
    elif promoted_count and args.dry_run:
        print(f"\n[dry-run] Would have promoted {promoted_count} correction(s). No files written.")
    else:
        print("\nNo corrections promoted.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
