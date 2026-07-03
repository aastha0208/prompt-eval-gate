"""
Validate corrections — CI check for evals/feedback/corrections.jsonl.

Runs on every PR that touches the feedback log or the dataset. Catches the
failure modes that would otherwise surface much later, as a confusing
mismatch between what the corrections log claims and what the dataset
actually contains:

  * Malformed JSONL or a missing required field.
  * Duplicate correction_id.
  * A correction marked "promoted" whose promoted_to_issue_key does not
    actually exist in the dataset (promotion script ran, save failed
    partway, or someone hand-edited one of the two files).
  * A dataset ticket with source "human_correction" that has no
    corresponding promoted correction record (dataset was hand-edited,
    bypassing the loop).

Exits 0 on PASS, 1 on FAIL.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORRECTIONS_PATH = HERE / "corrections.jsonl"
DATASET_PATH = HERE.parent / "datasets" / "ground_truth.example.json"

REQUIRED_FIELDS = [
    "correction_id", "captured_at", "status", "issue_key", "reviewer",
    "inputs", "agent_primary", "human_primary", "human_note",
]
VALID_STATUSES = {"pending", "promoted", "rejected"}


def main() -> int:
    errors: list[str] = []

    if not CORRECTIONS_PATH.exists():
        print(f"No corrections log at {CORRECTIONS_PATH} — nothing to validate.")
        return 0

    records = []
    seen_ids: set[str] = set()
    for lineno, line in enumerate(CORRECTIONS_PATH.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {lineno}: invalid JSON ({exc})")
            continue

        for field_name in REQUIRED_FIELDS:
            if field_name not in record or record[field_name] in (None, ""):
                errors.append(f"line {lineno} ({record.get('issue_key', '?')}): missing '{field_name}'")

        if record.get("status") not in VALID_STATUSES:
            errors.append(f"line {lineno} ({record.get('issue_key', '?')}): status must be one of {sorted(VALID_STATUSES)}")

        cid = record.get("correction_id")
        if cid in seen_ids:
            errors.append(f"line {lineno}: duplicate correction_id {cid}")
        seen_ids.add(cid)

        records.append(record)

    if not DATASET_PATH.exists():
        errors.append(f"dataset not found at {DATASET_PATH}")
        dataset_keys: set[str] = set()
        human_correction_keys: set[str] = set()
    else:
        dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
        dataset_keys = {t["issue_key"] for t in dataset["tickets"]}
        human_correction_keys = {
            t["issue_key"] for t in dataset["tickets"]
            if t.get("ground_truth", {}).get("source") == "human_correction"
        }

    promoted_target_keys: set[str] = set()
    for record in records:
        if record.get("status") == "promoted":
            target = record.get("promoted_to_issue_key")
            if not target:
                errors.append(f"{record.get('issue_key')}: status is 'promoted' but promoted_to_issue_key is unset")
                continue
            promoted_target_keys.add(target)
            if target not in dataset_keys:
                errors.append(
                    f"{record.get('issue_key')}: promoted_to_issue_key '{target}' not found in dataset"
                )

    orphaned = human_correction_keys - promoted_target_keys
    for issue_key in orphaned:
        errors.append(
            f"dataset ticket {issue_key} has source=human_correction but no promoted "
            f"correction record points to it — was the dataset hand-edited?"
        )

    print(f"Checked {len(records)} correction record(s) against {len(dataset_keys)} dataset ticket(s).")

    if errors:
        print(f"\n[FAIL] {len(errors)} issue(s):")
        for e in errors:
            print(f"  - {e}")
        return 1

    print("[OK] corrections.jsonl is consistent with the dataset.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
