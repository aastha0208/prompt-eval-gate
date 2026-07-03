"""
Dataset growth report — a health check on the eval dataset's composition.

The eval dataset only stays useful if it grows from real disagreements, not
just synthetic examples written once and never revisited. This script has no
pass/fail exit code — it's a report, not a gate — but it flags the two
things that quietly make a dataset stop being useful:

  * No human-corrected tickets at all. If every ticket is synthetic, the
    dataset only tests what its author already anticipated.
  * Heavy skew toward one layer. A dataset that's 80% UNIT tickets will
    happily pass a candidate prompt that's terrible at recommending
    BACKEND_E2E, because there aren't enough of those tickets to move the
    aggregate exact-match rate.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATASET_PATH = HERE.parent / "datasets" / "ground_truth.example.json"

SKEW_WARNING_THRESHOLD = 0.6  # one layer making up >60% of the dataset


def main() -> int:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    tickets = dataset["tickets"]
    total = len(tickets)

    if total == 0:
        print("Dataset is empty.")
        return 0

    source_counts = Counter(t["ground_truth"].get("source", "synthetic") for t in tickets)
    layer_counts = Counter(t["ground_truth"]["correct_primary_layer"] for t in tickets)
    failure_mode_counts = Counter(
        t["ground_truth"].get("failure_mode") for t in tickets if t["ground_truth"].get("failure_mode")
    )
    verified_count = sum(1 for t in tickets if t["ground_truth"].get("verified"))

    print(f"Dataset: {DATASET_PATH.name}")
    print(f"Total tickets       : {total}")
    print(f"Verified            : {verified_count}/{total} ({verified_count/total*100:.0f}%)")
    print()
    print("By source:")
    for source, count in source_counts.most_common():
        print(f"  {source:<18} {count:>3}  ({count/total*100:.0f}%)")
    print()
    print("By correct primary layer:")
    for layer, count in layer_counts.most_common():
        print(f"  {layer:<18} {count:>3}  ({count/total*100:.0f}%)")
    if failure_mode_counts:
        print()
        print("By failure mode (non-match tickets only):")
        for mode, count in failure_mode_counts.most_common():
            print(f"  {mode:<18} {count:>3}")

    print()
    warnings = []
    human_corrections = source_counts.get("human_correction", 0)
    if human_corrections == 0:
        warnings.append(
            "No human-corrected tickets in the dataset yet. The dataset is entirely synthetic — "
            "it only tests scenarios its author already thought of. Run capture_correction.py "
            "whenever a reviewer overrides the agent, then promote_corrections.py."
        )
    top_layer, top_count = layer_counts.most_common(1)[0]
    if top_count / total > SKEW_WARNING_THRESHOLD:
        warnings.append(
            f"{top_layer} makes up {top_count/total*100:.0f}% of the dataset (> "
            f"{SKEW_WARNING_THRESHOLD*100:.0f}% threshold). A prompt that's weak on other layers "
            "can still post a high aggregate exact-match rate. Prioritise corrections outside "
            f"{top_layer} to rebalance."
        )

    if warnings:
        print("Warnings:")
        for w in warnings:
            print(f"  ⚠ {w}")
    else:
        print("No warnings.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
