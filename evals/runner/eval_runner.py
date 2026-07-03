"""
Eval runner — scores a candidate LLM prompt against a labelled dataset.

The runner sends each row of the dataset through the prompt under test via the
`claude` CLI, captures the structured JSON output, scores each prediction
against ground truth as `match` / `partial` / `mismatch`, and writes a result
file. With `--runs N` it does this N times and writes an additional majority
file (`run_<prompt>_majority.json`) — that's the file the CI gate consumes.

Design notes:
  * Model calls go through the `claude` CLI (`claude -p`), not the API directly.
    The runner is OAuth-based — no API key needed. The trade-off: the user must
    have `claude` installed and authenticated locally. A stale ANTHROPIC_API_KEY
    in the environment will hijack the auth path and produce a confusing
    "Invalid API key" error, so the runner explicitly clears it.
  * Output parsing relies on JSON_OUTPUT_START / JSON_OUTPUT_END markers in the
    prompt. The prompt instructs the model to emit these around a single JSON
    object; the runner extracts and parses it. This is more robust than asking
    the model to "respond in JSON only" — the markers survive markdown
    wrappers, conversational preambles, and trailing prose.
  * Scoring distinguishes `match` (correct primary), `partial` (correct layer
    appears as the supporting recommendation), and `mismatch`. Collapsing
    partial into either bucket destroys real signal about whether the prompt
    is right-but-wrong-ranked.
  * The runner tracks `recovered_from_baseline` (rows baseline got wrong that
    the candidate now gets right) and `regressed_from_baseline` (the inverse)
    as first-class output, not just an aggregate percentage. Aggregate
    accuracy can stay flat while the prompt is silently swapping which rows
    it's right about.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Paths ──────────────────────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
EVAL_ROOT = HERE.parent
PROMPT_DIR = EVAL_ROOT / "prompts"
DATASET_PATH = EVAL_ROOT / "datasets" / "ground_truth.example.json"
RESULTS_DIR = HERE / "results"

PROMPT_FILES: dict[str, Path] = {
    "baseline": PROMPT_DIR / "baseline.txt",
    # Add your candidate prompts here, e.g.:
    # "v2_strict": PROMPT_DIR / "v2_strict.txt",
}

# ── Constants ──────────────────────────────────────────────────────────────
LAYERS = {"UNIT", "INTEGRATION", "BACKEND_E2E", "PORTAL_UI_E2E", "CYPRESS_COMPONENT", "MANUAL"}
STATUS_MATCH = "match"
STATUS_PARTIAL = "partial"
STATUS_MISMATCH = "mismatch"

JSON_OUTPUT_START = "JSON_OUTPUT_START"
JSON_OUTPUT_END = "JSON_OUTPUT_END"


# ── CLI discovery and auth ─────────────────────────────────────────────────
def find_claude() -> str:
    """Resolve the `claude` CLI executable across platforms."""
    candidate = shutil.which("claude")
    if candidate:
        return candidate
    # Windows fallbacks where shutil.which can miss npm-global installs.
    for guess in [
        Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd",
        Path.home() / ".npm-global" / "bin" / "claude",
    ]:
        if guess.exists():
            return str(guess)
    raise RuntimeError(
        "Could not find the `claude` CLI on PATH. Install with: "
        "`npm install -g @anthropic-ai/claude-code`, then authenticate via `claude -p 'OK'`."
    )


def clean_env() -> dict[str, str]:
    """Return an environment copy with ANTHROPIC_API_KEY removed.

    The Claude CLI prefers the API key path when the env var is set, even if
    it's stale — producing a confusing 'Invalid API key' error. Force the
    OAuth path by stripping the variable from the subprocess environment.
    """
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    return env


# ── Data model ─────────────────────────────────────────────────────────────
@dataclass
class TicketResult:
    issue_key: str
    predicted_primary: str | None
    predicted_supporting: str | None
    correct_primary: str
    match_status: str
    rationale: str | None = None
    raw_response: str | None = None
    error: str | None = None


@dataclass
class RunSummary:
    prompt_name: str
    run_index: int
    results: list[TicketResult] = field(default_factory=list)

    def counts(self) -> Counter:
        return Counter(r.match_status for r in self.results)

    def exact_match_rate(self) -> float:
        if not self.results:
            return 0.0
        return self.counts()[STATUS_MATCH] / len(self.results)

    def partial_credit_rate(self) -> float:
        if not self.results:
            return 0.0
        c = self.counts()
        return (c[STATUS_MATCH] + c[STATUS_PARTIAL]) / len(self.results)


# ── Prompt construction and CLI invocation ─────────────────────────────────
def build_prompt(template: str, ticket: dict[str, Any]) -> str:
    """Substitute ticket fields into the prompt template.

    The template uses simple {{field}} placeholders. Missing fields render as
    'None' so the model sees the absence as a signal rather than a blank.
    """
    inputs = ticket.get("inputs", {})

    def get(field_name: str) -> str:
        value = inputs.get(field_name)
        if value is None or value == "":
            return "None"
        return str(value)

    return (
        template
        .replace("{{issue_key}}", ticket["issue_key"])
        .replace("{{summary}}", ticket.get("summary", "None"))
        .replace("{{issue_type}}", get("issue_type"))
        .replace("{{components}}", get("components"))
        .replace("{{labels}}", get("labels"))
        .replace("{{description}}", get("description"))
        .replace("{{acceptance_criteria}}", get("acceptance_criteria"))
        .replace("{{implementation_notes}}", get("implementation_notes"))
        .replace("{{impact_areas}}", get("impact_areas"))
    )


def call_claude(claude_path: str, prompt: str, model: str = "sonnet") -> str:
    """Invoke the CLI and return raw stdout."""
    completed = subprocess.run(
        [claude_path, "-p", "--output-format", "json", "--model", model],
        input=prompt,
        capture_output=True,
        text=True,
        env=clean_env(),
        timeout=300,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"`claude` CLI exited {completed.returncode}: {completed.stderr.strip()}"
        )
    # The CLI wraps the model output in its own envelope when --output-format json
    # is used. Extract the inner content text.
    try:
        envelope = json.loads(completed.stdout)
        return envelope.get("result") or envelope.get("content") or completed.stdout
    except json.JSONDecodeError:
        return completed.stdout


# ── Output parsing ─────────────────────────────────────────────────────────
def extract_layer_json(raw: str) -> dict[str, Any] | None:
    """Find the JSON_OUTPUT_START / JSON_OUTPUT_END block and parse it."""
    pattern = re.compile(
        rf"{JSON_OUTPUT_START}\s*\n(.*?)\n\s*{JSON_OUTPUT_END}",
        re.DOTALL,
    )
    match = pattern.search(raw)
    if not match:
        # Fallback: look for any JSON object containing primary_layer.
        obj_match = re.search(r"\{[^{}]*\"primary_layer\"[^{}]*\}", raw, re.DOTALL)
        if obj_match:
            try:
                return json.loads(obj_match.group(0))
            except json.JSONDecodeError:
                return None
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


# ── Scoring ────────────────────────────────────────────────────────────────
def score(predicted_primary: str | None, predicted_supporting: str | None,
          correct_primary: str) -> str:
    """Classify a single ticket as match / partial / mismatch."""
    if predicted_primary == correct_primary:
        return STATUS_MATCH
    if predicted_supporting == correct_primary:
        return STATUS_PARTIAL
    return STATUS_MISMATCH


# ── Per-run execution ──────────────────────────────────────────────────────
def run_one(prompt_name: str, run_index: int, template: str,
            dataset: list[dict[str, Any]], claude_path: str,
            dry_run: bool = False) -> RunSummary:
    """Score the prompt across every ticket in the dataset once."""
    summary = RunSummary(prompt_name=prompt_name, run_index=run_index)

    for ticket in dataset:
        issue_key = ticket["issue_key"]
        correct = ticket["ground_truth"]["correct_primary_layer"]
        prompt = build_prompt(template, ticket)

        if dry_run:
            summary.results.append(TicketResult(
                issue_key=issue_key,
                predicted_primary=None,
                predicted_supporting=None,
                correct_primary=correct,
                match_status="dry-run",
                rationale="(dry run — no model call)",
            ))
            continue

        try:
            raw = call_claude(claude_path, prompt)
            parsed = extract_layer_json(raw)
            if parsed is None:
                summary.results.append(TicketResult(
                    issue_key=issue_key,
                    predicted_primary=None,
                    predicted_supporting=None,
                    correct_primary=correct,
                    match_status=STATUS_MISMATCH,
                    raw_response=raw[:500],
                    error="Could not parse JSON_OUTPUT block from model response.",
                ))
                continue

            predicted_primary = parsed.get("primary_layer")
            predicted_supporting = parsed.get("supporting")
            status = score(predicted_primary, predicted_supporting, correct)

            summary.results.append(TicketResult(
                issue_key=issue_key,
                predicted_primary=predicted_primary,
                predicted_supporting=predicted_supporting,
                correct_primary=correct,
                match_status=status,
                rationale=parsed.get("primary_rationale"),
            ))
        except Exception as exc:  # noqa: BLE001 — surface any failure per-ticket
            summary.results.append(TicketResult(
                issue_key=issue_key,
                predicted_primary=None,
                predicted_supporting=None,
                correct_primary=correct,
                match_status=STATUS_MISMATCH,
                error=str(exc),
            ))

    return summary


# ── Majority aggregation across N runs ─────────────────────────────────────
def aggregate(runs: list[RunSummary], baseline: RunSummary | None = None) -> dict[str, Any]:
    """Take per-ticket majority across N runs; ties resolve to the worse status.

    A 'tie toward worse' rule prevents accidentally promoting a prompt whose
    accuracy is borderline: if two runs say match and one says mismatch, the
    majority is match. If the runs are 1/1/1 across all three statuses, the
    worst (mismatch) wins.
    """
    per_ticket: dict[str, dict[str, Any]] = {}
    unstable: list[str] = []

    issue_keys = [r.issue_key for r in runs[0].results]
    for issue_key in issue_keys:
        per_run_statuses = []
        primaries = []
        for run in runs:
            tr = next(r for r in run.results if r.issue_key == issue_key)
            per_run_statuses.append(tr.match_status)
            primaries.append(tr.predicted_primary)

        if len(set(per_run_statuses)) > 1:
            unstable.append(issue_key)

        # Majority status with tie-breaking toward worst.
        status_counts = Counter(per_run_statuses)
        if status_counts[STATUS_MISMATCH] >= len(runs) / 2:
            majority_status = STATUS_MISMATCH
        elif status_counts[STATUS_PARTIAL] >= len(runs) / 2 and status_counts[STATUS_MATCH] < status_counts[STATUS_PARTIAL]:
            majority_status = STATUS_PARTIAL
        else:
            majority_status = status_counts.most_common(1)[0][0]

        per_ticket[issue_key] = {
            "majority_status": majority_status,
            "per_run_statuses": per_run_statuses,
            "predicted_primaries": primaries,
            "correct_primary": next(r for r in runs[0].results if r.issue_key == issue_key).correct_primary,
        }

    # Recovered / regressed vs baseline.
    recovered: list[str] = []
    regressed: list[str] = []
    if baseline is not None:
        for issue_key, info in per_ticket.items():
            bt = next((r for r in baseline.results if r.issue_key == issue_key), None)
            if bt is None:
                continue
            if bt.match_status != STATUS_MATCH and info["majority_status"] == STATUS_MATCH:
                recovered.append(issue_key)
            if bt.match_status == STATUS_MATCH and info["majority_status"] == STATUS_MISMATCH:
                regressed.append(issue_key)

    total = len(per_ticket)
    match_count = sum(1 for v in per_ticket.values() if v["majority_status"] == STATUS_MATCH)
    partial_count = sum(1 for v in per_ticket.values() if v["majority_status"] == STATUS_PARTIAL)
    mismatch_count = sum(1 for v in per_ticket.values() if v["majority_status"] == STATUS_MISMATCH)

    return {
        "prompt_name": runs[0].prompt_name,
        "n_runs": len(runs),
        "tickets": per_ticket,
        "counts": {
            "match": match_count,
            "partial": partial_count,
            "mismatch": mismatch_count,
            "total": total,
        },
        "exact_match_rate": match_count / total if total else 0.0,
        "with_partial_credit_rate": (match_count + partial_count) / total if total else 0.0,
        "unstable_tickets": unstable,
        "recovered_from_baseline": recovered,
        "regressed_from_baseline": regressed,
    }


# ── I/O ────────────────────────────────────────────────────────────────────
def write_run(summary: RunSummary, out_path: Path) -> None:
    payload = {
        "prompt_name": summary.prompt_name,
        "run_index": summary.run_index,
        "counts": dict(summary.counts()),
        "exact_match_rate": summary.exact_match_rate(),
        "with_partial_credit_rate": summary.partial_credit_rate(),
        "results": [
            {
                "issue_key": r.issue_key,
                "predicted_primary": r.predicted_primary,
                "predicted_supporting": r.predicted_supporting,
                "correct_primary": r.correct_primary,
                "match_status": r.match_status,
                "rationale": r.rationale,
                "error": r.error,
            }
            for r in summary.results
        ],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))


def write_majority(majority: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(majority, indent=2))


def print_summary(majority: dict[str, Any]) -> None:
    counts = majority["counts"]
    print(f"\nMAJORITY across {majority['n_runs']} runs — prompt: {majority['prompt_name']}")
    print(f"  match {counts['match']}  partial {counts['partial']}  mismatch {counts['mismatch']}   (n={counts['total']})")
    print(f"  Exact-match rate    : {majority['exact_match_rate']*100:.1f}%")
    print(f"  With partial credit : {majority['with_partial_credit_rate']*100:.1f}%")
    if majority["recovered_from_baseline"]:
        recovered = ", ".join(majority["recovered_from_baseline"])
        print(f"  Recovered from baseline mismatch ({len(majority['recovered_from_baseline'])}): {recovered}")
    if majority["regressed_from_baseline"]:
        regressed = ", ".join(majority["regressed_from_baseline"])
        print(f"  ⚠ Regressed from baseline match  ({len(majority['regressed_from_baseline'])}): {regressed}")
    if majority["unstable_tickets"]:
        unstable = ", ".join(majority["unstable_tickets"])
        print(f"  ⚠ Unstable tickets: {unstable}")


# ── Entrypoint ─────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="Score an LLM prompt against the eval dataset.")
    parser.add_argument(
        "--prompt",
        choices=list(PROMPT_FILES.keys()),
        required=True,
        help="Which prompt to evaluate (must be registered in PROMPT_FILES).",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="How many times to run each ticket; majority vote is taken across runs. Use 3 for committed results.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate dataset and prompt without making any model calls.",
    )
    parser.add_argument(
        "--model",
        default="sonnet",
        help="Model name to pass to `claude -p --model`.",
    )
    args = parser.parse_args()

    if not DATASET_PATH.exists():
        print(f"ERROR: dataset not found at {DATASET_PATH}", file=sys.stderr)
        return 2

    prompt_path = PROMPT_FILES[args.prompt]
    if not prompt_path.exists():
        print(f"ERROR: prompt file not found at {prompt_path}", file=sys.stderr)
        return 2

    template = prompt_path.read_text()
    dataset = json.loads(DATASET_PATH.read_text())["tickets"]

    claude_path = "" if args.dry_run else find_claude()

    # Optionally load baseline for recovered/regressed analysis.
    baseline_path = RESULTS_DIR / "run_v1_majority.json"
    baseline_summary: RunSummary | None = None
    if baseline_path.exists() and args.prompt != "baseline":
        try:
            baseline_data = json.loads(baseline_path.read_text())
            baseline_summary = RunSummary(prompt_name="baseline", run_index=0)
            for issue_key, info in baseline_data.get("tickets", {}).items():
                baseline_summary.results.append(TicketResult(
                    issue_key=issue_key,
                    predicted_primary=info.get("predicted_primaries", [None])[0],
                    predicted_supporting=None,
                    correct_primary=info.get("correct_primary"),
                    match_status=info.get("majority_status"),
                ))
        except (json.JSONDecodeError, KeyError):
            baseline_summary = None  # baseline format mismatch — skip the comparison

    runs: list[RunSummary] = []
    for i in range(1, args.runs + 1):
        if args.runs > 1:
            print(f"--- run {i}/{args.runs} ---")
        summary = run_one(args.prompt, i, template, dataset, claude_path, dry_run=args.dry_run)
        out_path = RESULTS_DIR / f"run_{args.prompt}_{i}.json"
        write_run(summary, out_path)
        runs.append(summary)

    if args.dry_run:
        print(f"\nDry run complete. {len(dataset)} tickets validated against prompt {args.prompt!r}.")
        return 0

    if args.runs > 1:
        majority = aggregate(runs, baseline=baseline_summary)
        majority_path = RESULTS_DIR / f"run_{args.prompt}_majority.json"
        write_majority(majority, majority_path)
        print_summary(majority)
        print(f"\nWrote majority to {majority_path}")
    else:
        # Single run: print a brief summary without majority machinery.
        s = runs[0]
        c = s.counts()
        print(f"\nSingle run — prompt: {args.prompt}")
        print(f"  match {c[STATUS_MATCH]}  partial {c[STATUS_PARTIAL]}  mismatch {c[STATUS_MISMATCH]}")
        print(f"  Exact-match rate: {s.exact_match_rate()*100:.1f}%")

    return 0


if __name__ == "__main__":
    sys.exit(main())
