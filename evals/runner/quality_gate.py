"""
Quality gate — compare a candidate result file against the baseline.

Exits 0 on PASS, 1 on FAIL. Intended to be called from CI on every prompt PR.

Pass criteria:
  1. Candidate's exact-match rate must be >= baseline's exact-match rate.
  2. No ticket that was a `match` in baseline may regress to `mismatch` in
     the candidate. (Partial is acceptable — that's the prompt being right
     but wrong-ranked, which is still a useful signal.)

Both criteria must hold. Failing either is a FAIL.

Usage:
    python quality_gate.py <baseline.json> <candidate.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def load_majority(path: Path) -> dict:
    """Load a majority-result file and return its parsed contents."""
    if not path.exists():
        print(f"::error::File not found: {path}", file=sys.stderr)
        sys.exit(2)
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(f"::error::Could not parse {path}: {exc}", file=sys.stderr)
        sys.exit(2)


def status_for_ticket(majority: dict, issue_key: str) -> str | None:
    """Return the majority status for a given ticket, or None if missing."""
    ticket = majority.get("tickets", {}).get(issue_key)
    if ticket is None:
        return None
    return ticket.get("majority_status")


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: quality_gate.py <baseline.json> <candidate.json>", file=sys.stderr)
        return 2

    baseline_path = Path(sys.argv[1])
    candidate_path = Path(sys.argv[2])

    baseline = load_majority(baseline_path)
    candidate = load_majority(candidate_path)

    baseline_rate = baseline.get("exact_match_rate", 0.0)
    candidate_rate = candidate.get("exact_match_rate", 0.0)

    print(f"Baseline:  {baseline_path.name}  exact-match rate {baseline_rate*100:.1f}%")
    print(f"Candidate: {candidate_path.name}  exact-match rate {candidate_rate*100:.1f}%")
    print()

    # Criterion 1: candidate's overall exact-match rate must be >= baseline's.
    rate_ok = candidate_rate >= baseline_rate
    if rate_ok:
        delta = (candidate_rate - baseline_rate) * 100
        print(f"[OK] Exact-match rate: candidate beats baseline by {delta:+.1f} pts")
    else:
        delta = (candidate_rate - baseline_rate) * 100
        print(f"[FAIL] Exact-match rate regressed by {delta:+.1f} pts")

    # Criterion 2: no individual match → mismatch regression.
    regressions: list[str] = []
    baseline_tickets = baseline.get("tickets", {})
    for issue_key in baseline_tickets:
        baseline_status = status_for_ticket(baseline, issue_key)
        candidate_status = status_for_ticket(candidate, issue_key)
        if candidate_status is None:
            # Candidate doesn't have this ticket (dataset shrank?) — not a regression
            # but worth flagging.
            print(f"[WARN] Ticket {issue_key} missing from candidate")
            continue
        if baseline_status == "match" and candidate_status == "mismatch":
            regressions.append(issue_key)

    if regressions:
        print(f"\n[FAIL] {len(regressions)} ticket(s) regressed from match → mismatch:")
        for issue_key in regressions:
            print(f"   - {issue_key}")
    else:
        print("[OK] No match → mismatch regressions on individual tickets")

    # Also report recovered tickets (the candidate fixing things baseline missed)
    # as informational signal, even though it doesn't affect pass/fail.
    recovered: list[str] = []
    for issue_key in baseline_tickets:
        baseline_status = status_for_ticket(baseline, issue_key)
        candidate_status = status_for_ticket(candidate, issue_key)
        if baseline_status == "mismatch" and candidate_status == "match":
            recovered.append(issue_key)
    if recovered:
        print(f"\n[INFO] {len(recovered)} ticket(s) recovered from mismatch → match:")
        for issue_key in recovered:
            print(f"   - {issue_key}")

    overall_pass = rate_ok and not regressions
    print()
    if overall_pass:
        print("=" * 60)
        print("PASS — candidate may be promoted.")
        print("=" * 60)
        return 0
    print("=" * 60)
    print("FAIL — candidate must not be promoted as-is.")
    print("=" * 60)
    return 1


if __name__ == "__main__":
    sys.exit(main())
