# Eval system — how to test a prompt change

This folder contains the regression-testing harness for the LLM prompt used by the deployed agent. Use it before any prompt change ships, so a regression cannot reach production unnoticed.

The deployed agent is **not affected** by anything in this folder. You can run, break, or rewrite the evals freely — production stays untouched.

---

## What's here

| Path | What it does |
|------|--------------|
| `datasets/ground_truth.example.json` | 9 tickets (8 synthetic + 1 promoted from a real correction), each labelled with the correct primary layer, supporting layer, and human verdict on the original agent run. The source of truth. |
| `prompts/*.txt` | Every prompt version under test. `baseline.txt` is the original deployed prompt. |
| `runner/eval_runner.py` | Runs a chosen prompt against the dataset via the `claude` CLI and scores the predictions. Supports `--runs N` for majority voting across multiple runs to average out LLM noise. |
| `runner/quality_gate.py` | Compares a candidate result file against the baseline. Exits 0 (PASS) or 1 (FAIL). |
| `results/run_v1_majority.json` | Frozen baseline anchor. Every candidate is compared against this. Do not regenerate. |
| `results/run_*_majority.json` | Majority-voted results for each candidate prompt version. |
| `feedback/` | Capture and promotion pipeline for human corrections — how the dataset above grows from real disagreements instead of staying static. See `feedback/README.md`. |
| `../.github/workflows/eval-gate.yml` | CI workflow that runs the quality gate and feedback-loop checks on PRs. |

---

## Prerequisites

You will need:

1. **Python 3.11+** with the `anthropic` package: `pip install anthropic`
2. **The `claude` CLI authenticated.** The runner calls `claude -p` and uses OAuth — no API key needed. Verify with: `claude -p "say OK"`. If that returns "OK", you're set.
3. **An empty `ANTHROPIC_API_KEY` environment variable.** A stale key in the environment makes the CLI try the API-key path and fail with *"Invalid API key"*. The runner clears it automatically, but check your shell with `echo $ANTHROPIC_API_KEY` if you hit auth errors.

---

## How to test a prompt change — the full procedure

This is the workflow every prompt change must follow. There are no shortcuts; the gate verifies committed results, not your prompt directly.

### Step 1 — Create the new prompt file

Save your new prompt at `evals/prompts/v3_<name>.txt`. Use a descriptive suffix (`v3_stricter_unit`, `v3_no_e2e`, etc.) so the version is self-explanatory.

The prompt must produce output bracketed by `JSON_OUTPUT_START` and `JSON_OUTPUT_END` markers — the runner depends on that format for parsing. Copy the structure from `baseline.txt`.

### Step 2 — Register the new prompt in the runner

Open `evals/runner/eval_runner.py`. Make two small additions:

In the `PROMPT_FILES` dict, add your version:

```python
PROMPT_FILES = {
    "baseline": PROMPT_DIR / "baseline.txt",
    "v3": PROMPT_DIR / "v3_<name>.txt",   # new
}
```

In the argparse `choices` list, add `"v3"`:

```python
p.add_argument("--prompt", choices=["baseline", "v3"], required=True, ...)
```

### Step 3 — Run the eval locally with majority voting

```bash
cd <repo-root>
python evals/runner/eval_runner.py --prompt v3 --runs 3
```

**Always use `--runs 3`.** Single runs are noisy enough that borderline tickets can flip status purely from LLM variance. The runner's whole majority-voting machinery exists to average that noise out, and the CI gate only accepts files with the `_majority` suffix.

This produces four files in `evals/runner/results/`:

- `run_v3_1.json`, `run_v3_2.json`, `run_v3_3.json` — the three individual runs
- `run_v3_majority.json` — the per-ticket majority across the three (this is what the gate reads)

Expect each run to take a few minutes and cost a small amount of Claude credit depending on the model.

### Step 4 — Read the summary

The runner prints a summary at the end of each run:

```
MAJORITY across 3 runs — prompt: v3
  match 6  partial 1  mismatch 1   (n=8)
  Exact-match rate    : 75.0%
  With partial credit : 87.5%
  Recovered from baseline mismatch (1): PROJ-1003
  ⚠ Unstable tickets: PROJ-1007
```

The number that matters is **exact-match rate**. If your candidate is below the baseline, do not proceed — fix the prompt and re-run.

Unstable tickets are ones that flipped status between runs. They're not necessarily a problem, but they tell you which tickets are borderline.

### Step 5 — Commit results and prompt to a branch

```bash
git checkout -b prompt-v3-<name>
git add evals/prompts/v3_<name>.txt
git add evals/runner/results/run_v3_*.json
git add evals/runner/eval_runner.py   # the two lines you edited in step 2
git commit -m "Add v3 prompt: <short description>"
git push origin prompt-v3-<name>
```

### Step 6 — Open a pull request

Open a PR from your branch to `main`. Include in the PR description:

- The summary block from step 4 (paste it verbatim)
- A one-line rationale for what you changed and why
- Any unstable tickets and whether you investigated them

### Step 7 — Trigger the CI gate

The CI gate (`eval-gate.yml`) runs automatically on prompt PRs but defaults to checking the previous-best candidate. To gate your v3 change specifically, trigger it manually:

1. Open the **Actions** tab of the repo
2. Pick **Prompt Eval Quality Gate** from the left sidebar
3. Click **Run workflow**
4. In the candidate input, type `v3` (matching the suffix you used in `PROMPT_FILES`)
5. Click **Run workflow**

The gate reads `run_v3_majority.json` and compares it against the baseline `run_v1_majority.json`. It exits **PASS** if v3 beats baseline cleanly, **FAIL** otherwise.

### Step 8 — Merge if PASS

A PASS verdict means v3 is at least as good as v1 across the dataset. Merge the PR.

A FAIL verdict means v3 regressed somewhere. Read the gate's output to see which tickets, iterate on the prompt, and repeat from step 3.

### Step 9 — Promote to production

After merging, update the deployed agent to use the new prompt content. The eval system is independent of the deployment — merging here does not change what runs in production. That's a separate change.

---

## How the CI gate works

The gate is pure Python — it does **not** call the model in CI. It only reads JSON files you committed locally and compares their numbers.

What it checks:

- The baseline file (`run_v1_majority.json`) exists and is well-formed
- The candidate file (`run_<name>_majority.json`) exists and is well-formed
- The candidate's exact-match rate is at least as high as the baseline's
- No tickets that were matches in baseline regressed to mismatches in the candidate

If any of these fail, the gate exits 1 and the GitHub check goes red.

The gate also runs a `gate-selftest` job that iterates over every committed `run_*_majority.json` file and re-runs the comparison for each. This is informational — it confirms previously approved candidates still pass against the current baseline.

---

## Things to know

**The gate trusts your committed JSON.** It does not regenerate results in CI. If you edit a prompt file but forget to re-run the eval before committing, the gate will pass against the *old* results — silently, with no warning. **Re-run after every prompt edit, no exceptions.**

**The baseline is frozen at v1.** Even when v3 ships to production, the gate continues to compare new candidates against v1. This is intentional — it tracks long-term drift. If the cumulative drift becomes confusing later, you can promote a new baseline by manually replacing `run_v1_majority.json`. Don't do this casually.

---

## Quick reference

| Task | Command |
|------|---------|
| Run a single eval (noisy, exploratory) | `python evals/runner/eval_runner.py --prompt v3` |
| Run a 3-run majority (for committing) | `python evals/runner/eval_runner.py --prompt v3 --runs 3` |
| Validate dataset and prompt without API calls | `python evals/runner/eval_runner.py --prompt v3 --dry-run` |
| Run the gate locally (same as CI does) | `python evals/runner/quality_gate.py results/run_v1_majority.json results/run_v3_majority.json` |
| Capture a human override in production | `python evals/feedback/capture_correction.py --issue-key ... --agent-primary ... --human-primary ... --human-note ... --reviewer ...` |
| Review and promote pending corrections | `python evals/feedback/promote_corrections.py` |
| Check the dataset's composition and skew | `python evals/feedback/dataset_growth.py` |

---

## Troubleshooting

**`Invalid API key` from the runner.** Your shell has a stale `ANTHROPIC_API_KEY`. Unset it: `unset ANTHROPIC_API_KEY`, then re-run.

**`claude CLI not found`.** Install Claude Code and authenticate: `npm install -g @anthropic-ai/claude-code`, then `claude -p "OK"` to trigger OAuth.

**`Missing candidate majority file`.** You triggered the gate with a candidate name that has no corresponding `_majority.json` committed. Either you forgot `--runs 3` (which produces the majority file), or you typed the wrong candidate name. Re-run step 3 and verify the file exists in `evals/runner/results/`.

**Gate passes but production regressed.** The committed JSON is stale — it reflects an earlier version of the prompt. Re-run step 3 from a clean state and re-commit.

**Unstable tickets across runs.** Borderline tickets that flip status between runs. Not necessarily a bug — they tell you the prompt is on the edge for certain ticket types. Investigate the rationale in each run's individual file to understand why.
