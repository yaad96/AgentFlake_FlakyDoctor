#!/usr/bin/env python3
"""
run_pass_at_k.py — pass@k harness for ReproFlake containers.

Runs run_<test_type>_tracemop.sh N times for each requested model, archives
per-run artifacts (excluding Flakym2/ and any target/), and emits summary
CSV + Markdown reports under '<container> runs/'.

This is a SMOKE-TEST version: minimal pre-flight, no lock file, no disk
check, no progress ETA. Verify the core loop works, then add hardening.

Usage:
  ./run_pass_at_k.py <container> [--models claude,openai] [--runs 3] [--force]
"""

import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPROFLAKE_DIR = SCRIPT_DIR.parent
DATA_DIR = REPROFLAKE_DIR / "data"
CSV_FILE = REPROFLAKE_DIR / "test_config.csv"

TYPE_TO_SCRIPT = {
    "od":  SCRIPT_DIR / "run_od_tracemop.sh",
    "td":  SCRIPT_DIR / "run_td_tracemop.sh",
    "id":  SCRIPT_DIR / "run_id_tracemop.sh",
    "nio": SCRIPT_DIR / "run_nio_tracemop.sh",
}
MODEL_API_KEY = {"claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}

# Per-run sentinel file written ONLY after the archive is fully complete.
# Skip-detection requires this AND a verdict file in {PASSED, FAILED}.
SENTINEL = ".run_complete"


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

def load_csv_row(container):
    with open(CSV_FILE, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("result_container", "").strip() == container:
                return row
    return None


def preflight(container, models):
    if not CSV_FILE.is_file():
        sys.exit(f"ERROR: CSV not found: {CSV_FILE}")
    row = load_csv_row(container)
    if not row:
        sys.exit(f"ERROR: container '{container}' not in CSV")
    test_type = row["test_type"].strip().lower()
    if test_type not in TYPE_TO_SCRIPT:
        sys.exit(f"ERROR: unsupported test_type '{test_type}'")
    script = TYPE_TO_SCRIPT[test_type]
    if not script.is_file():
        sys.exit(f"ERROR: script not found: {script}")

    for m in models:
        if m not in MODEL_API_KEY:
            sys.exit(f"ERROR: unknown model '{m}'")
    # NB: API-key check is DEFERRED to per-run execution time. A wrapper
    # invocation with already-completed runs (skip path) doesn't make any
    # LLM calls, so it shouldn't fail just because some other model's key
    # is missing. If a run actually needs to execute, we check that
    # specific model's key right before calling the per-type script.

    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        sys.exit("ERROR: Docker daemon not reachable")

    return row, test_type, script


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def archive_run(data_dir: Path, per_run_dir: Path):
    """Mirror data/<container>/ into per_run_dir/, excluding Flakym2/ and
    any target/ subdir. Defensive across all 4 test types — copytree silently
    skips missing entries so the same call works for OD/TD/ID/NIO."""
    skip_target = shutil.ignore_patterns("target")

    sources_with_target = [
        ("Fixed",            skip_target),
        ("Flaky",            skip_target),
        ("FlakyCodeChange",  skip_target),  # TD only
    ]
    sources_no_target = [
        ("result",           None),
        ("traces-fixed",     None),
        ("traces-flaky",     None),
        ("traces-flakycc",   None),  # TD
        ("traces-pass",      None),  # ID
        ("traces-fail",      None),  # ID
    ]
    for sub, ignore in sources_with_target + sources_no_target:
        src = data_dir / sub
        if src.is_dir():
            shutil.copytree(src, per_run_dir / sub, symlinks=True, ignore=ignore)

    steps = data_dir / "Steps Output Files"
    if steps.is_dir():
        shutil.copytree(steps, per_run_dir / "Steps Output Files", symlinks=True)

    for f in ["Fixed.patch", "FlakyCodeChange.patch", "FixedCodeChange.patch",
              "flaky_info.txt", "issue_description.txt"]:
        src = data_dir / f
        if src.is_file():
            shutil.copy2(src, per_run_dir / f)


# ---------------------------------------------------------------------------
# Per-run extraction (for the summary)
# ---------------------------------------------------------------------------

def safe_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def parse_run(per_run_dir: Path, container, test_type, model, run_n):
    """Extract a single CSV row's worth of data from a per-run folder."""
    steps = per_run_dir / "Steps Output Files"
    verdict_file = steps / "verify_after_fix.verdict"
    apply_file = steps / "apply_report.json"
    llm_resp = steps / "llm_response.json"
    llm_t1 = steps / "llm_response_turn1.json"
    llm_t2 = steps / "llm_response_turn2.json"
    artifacts_t2 = steps / "llm_artifacts_turn2.txt"
    verify_log = steps / "verify_after_fix.log"
    pipeline = per_run_dir / "pipeline.log"

    verdict = "INCOMPLETE"
    if verdict_file.is_file():
        v = verdict_file.read_text(encoding="utf-8").strip()
        if v in ("PASSED", "FAILED"):
            verdict = v

    apply = safe_json(apply_file) or {}
    resp = safe_json(llm_resp) or {}
    t1 = safe_json(llm_t1) or {}
    t2 = safe_json(llm_t2) or {}

    # Token shape varies (claude vs openai). Claude with prompt caching
    # splits input across `input_tokens` (uncached new), `cache_creation_input_tokens`
    # (newly cached), and `cache_read_input_tokens` (re-used from cache); the
    # full input charge is the SUM of these three. OpenAI uses `prompt_tokens`.
    def toks(d, *keys):
        u = d.get("usage") or {}
        return sum((u.get(k) or 0) for k in keys)

    in_tokens = (
        toks(t1, "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "prompt_tokens")
        + toks(t2, "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "prompt_tokens")
    )
    out_tokens = (
        toks(t1, "output_tokens", "completion_tokens")
        + toks(t2, "output_tokens", "completion_tokens")
    )
    total = in_tokens + out_tokens

    turns = resp.get("turns_taken", 1 if not llm_t2.is_file() else 2)
    finish = (t2 or t1).get("stop_reason") or (t2 or t1).get("finish_reason") or ""
    elapsed_llm = float((t1.get("elapsed_seconds") or 0) + (t2.get("elapsed_seconds") or 0))

    artifacts_req = len(t1.get("artifacts_requested") or [])
    artifacts_miss = 0
    if artifacts_t2.is_file():
        artifacts_miss = len(re.findall(r"^\s*-.*\bMISS\b", artifacts_t2.read_text(encoding="utf-8", errors="replace"), re.M))

    # apply_report fields
    layers = apply.get("layers_attempted") or []
    result = apply.get("result") or {}
    apply_layer = result.get("layer") or "none"
    path_rewritten = any(bool(la.get("path_rewritten")) for la in layers)
    imports_inferred = []
    for la in layers:
        for ap in (la.get("applied") or []):
            imports_inferred.extend(ap.get("imports_inferred") or [])
    recompile = apply.get("recompile") or {}
    recompile_ok = recompile.get("ok") if recompile and not recompile.get("skipped") else None
    compile_d = apply.get("compile") or {}
    host_compile_ok = compile_d.get("all_ok") if compile_d and not compile_d.get("skipped") else None

    # Verify log parse
    tests = failures = errors = markers = 0
    fail_snippet = ""
    if verify_log.is_file():
        log = verify_log.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)", log):
            tests, failures, errors = int(m.group(1)), int(m.group(2)), int(m.group(3))
        markers = len(re.findall(r"<<< (?:FAILURE|ERROR)!", log))
        if markers > 0:
            for line in log.splitlines():
                if "<<< FAILURE!" in line or "<<< ERROR!" in line:
                    fail_snippet = line.strip()[:200]
                    break

    # Recover elapsed_total_seconds from the sentinel for runs that
    # weren't executed in this invocation (skip path) — we wrote it to
    # the sentinel as `elapsed=<float>`. Without this, avg wall time in
    # the summary collapses to 0 for any skipped run.
    elapsed_total = 0.0
    sentinel_file = per_run_dir / SENTINEL
    if sentinel_file.is_file():
        for line in sentinel_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("elapsed="):
                try:
                    elapsed_total = float(line.split("=", 1)[1])
                except ValueError:
                    pass
                break

    # Categorise the failure
    cat = classify(verdict, apply, recompile_ok, failures, errors, markers, pipeline)

    if not fail_snippet and verdict != "PASSED":
        # Pull from apply_report's reason if no test-level snippet
        for la in layers:
            r = la.get("reason") or ""
            if r:
                fail_snippet = r.replace("\n", " | ")[:200]
                break
        if not fail_snippet:
            fail_snippet = result.get("reason", "")[:200]

    return {
        "container": container,
        "test_type": test_type,
        "model": model,
        "run": run_n,
        "verdict": verdict,
        "fail_category": cat,
        "turns_taken": turns,
        "artifacts_requested": artifacts_req,
        "artifacts_miss": artifacts_miss,
        "input_tokens_total": in_tokens,
        "output_tokens_total": out_tokens,
        "total_tokens": total,
        "llm_finish_reason": finish,
        "elapsed_llm_seconds": elapsed_llm,
        "apply_layer": apply_layer,
        "apply_path_rewritten": path_rewritten,
        "apply_imports_inferred": ";".join(imports_inferred),
        "recompile_ok": recompile_ok,
        "host_compile_ok": host_compile_ok,
        "step_13_tests": tests,
        "step_13_failures": failures,
        "step_13_errors": errors,
        "failure_markers": markers,
        "fail_snippet": fail_snippet,
        "elapsed_total_seconds": round(elapsed_total, 1),
    }


def classify(verdict, apply, recompile_ok, failures, errors, markers, pipeline):
    if verdict == "PASSED":
        return "passed"
    if verdict == "INCOMPLETE":
        return "incomplete"
    # verdict == FAILED — figure out why
    if pipeline.is_file():
        log = pipeline.read_text(encoding="utf-8", errors="replace")
        if any(s in log for s in [
            "ERROR: Flaky run had Failures=0",
            "ERROR: Flaky+wrapper passed unexpectedly",
            "ERROR: NonDex run produced 0 failures",
        ]):
            return "sanity_failed"
    result = (apply or {}).get("result") or {}
    if not result.get("ok") and result.get("layer") in (None, "none"):
        return "patch_apply_failed"
    if recompile_ok is False:
        return "compile_failed"
    if failures + errors > 0 or markers > 0:
        return "test_failed"
    return "unknown_failure"


# ---------------------------------------------------------------------------
# pass@k
# ---------------------------------------------------------------------------

def pass_at_k(n, c, k):
    if c == 0:
        return 0.0
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


# ---------------------------------------------------------------------------
# Summary writers
# ---------------------------------------------------------------------------

CSV_COLS = [
    "container", "test_type", "model", "run", "verdict", "fail_category",
    "turns_taken", "artifacts_requested", "artifacts_miss",
    "input_tokens_total", "output_tokens_total", "total_tokens",
    "llm_finish_reason", "elapsed_llm_seconds", "elapsed_total_seconds",
    "apply_layer", "apply_path_rewritten", "apply_imports_inferred",
    "recompile_ok", "host_compile_ok",
    "step_13_tests", "step_13_failures", "step_13_errors", "failure_markers",
    "fail_snippet",
]


def collect_all_rows_on_disk(runs_root: Path, container: str, test_type: str) -> list:
    """Walk runs_root for ALL existing per-run folders (across every model
    that's ever been run) and parse each one. This makes summary.csv/.md
    reflect everything currently on disk — not just the runs executed in
    THIS invocation. Without this, a single-model invocation (say, just
    --models openai) would truncate summary.csv to only openai rows, losing
    a prior claude batch's results.

    Folders are matched by the `<container> <model>` prefix, then runs by
    `run <N>` regex. Returns deterministic order: model alphabetical, run
    integer-sorted."""
    rows = []
    if not runs_root.is_dir():
        return rows
    prefix = f"{container} "
    for model_dir in sorted(runs_root.iterdir()):
        if not model_dir.is_dir() or not model_dir.name.startswith(prefix):
            continue
        model = model_dir.name[len(prefix):]
        run_dirs = []
        for d in model_dir.iterdir():
            if not d.is_dir():
                continue
            m = re.match(r"run (\d+)$", d.name)
            if m:
                run_dirs.append((int(m.group(1)), d))
        run_dirs.sort()
        for run_n, d in run_dirs:
            rows.append(parse_run(d, container, test_type, model, run_n))
    return rows


def write_summary(rows, runs_root: Path, container, row_meta, runs_per_model):
    csv_path = runs_root / "summary.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, quoting=csv.QUOTE_ALL,
                           extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    md = [f"# {container} — pass@k report\n"]
    md.append(f"**Test type**: {row_meta['test_type']}     "
              f"**Module**: {row_meta.get('module','')}     "
              f"**JDK**: {row_meta.get('java','')}\n")
    md.append(f"**Polluter**: {row_meta.get('polluter/state setter','') or 'n/a'}\n"
              f"**Victim**:   {row_meta.get('flaky_test','')}\n")
    md.append(f"\n## Aggregate (n={runs_per_model} per model)\n")
    md.append("### Plausibility (test-passing rate; pass@k Chen et al.)\n")
    md.append("| Model | runs passed | pass@1 (plausible) | pass@N (plausible) "
              "| avg tokens | avg wall time |")
    md.append("|---|---|---|---|---|---|")
    for m in sorted(set(r["model"] for r in rows)):
        m_rows = [r for r in rows if r["model"] == m]
        n = sum(1 for r in m_rows if r["verdict"] in ("PASSED", "FAILED"))
        c = sum(1 for r in m_rows if r["verdict"] == "PASSED")
        avg_tok = sum(r["total_tokens"] for r in m_rows) // max(1, len(m_rows))
        avg_wall = sum(r.get("elapsed_total_seconds", 0) for r in m_rows) / max(1, len(m_rows))
        p1 = pass_at_k(n, c, 1) if n else 0.0
        pN = pass_at_k(n, c, n) if n else 0.0
        md.append(f"| {m} | {c}/{n} | {p1:.0%} | {pN:.0%} | "
                  f"{avg_tok:,} | {avg_wall:.0f}s |")

    md.append("\n### Failure breakdown by category\n")
    cats = ["passed", "incomplete", "sanity_failed", "llm_failed",
            "patch_apply_failed", "compile_failed", "test_failed", "unknown_failure"]
    md.append("| Model | " + " | ".join(cats) + " |")
    md.append("|" + "---|" * (len(cats) + 1))
    for m in sorted(set(r["model"] for r in rows)):
        cells = []
        for c in cats:
            n = sum(1 for r in rows if r["model"] == m and r["fail_category"] == c)
            cells.append(str(n))
        md.append(f"| {m} | " + " | ".join(cells) + " |")

    md.append("\n## Per-run details\n")
    for r in rows:
        diag = ""
        d = safe_json(runs_root / f"{container} {r['model']}/run {r['run']}/Steps Output Files/llm_response.json") or {}
        resp = d.get("response") or {}
        if isinstance(resp, dict):
            # `.get("diagnosis", "")` returns None (not "") when the key exists
            # with a None value — common in the parsed-LLM-output shape. Use
            # `or ""` to coerce both missing-key and None-value to empty string.
            diag = ((resp.get("output_0") or {}).get("diagnosis") or "")[:250]
        md.append(f"### {r['model']} / run {r['run']} — {r['verdict']} [{r['fail_category']}]\n")
        md.append(f"- **LLM**: {r['turns_taken']} turn(s), "
                  f"{r['input_tokens_total']:,} in / {r['output_tokens_total']:,} out tokens; "
                  f"finish={r['llm_finish_reason']}")
        md.append(f"- **Apply**: layer=`{r['apply_layer']}` · "
                  f"path_rewritten={r['apply_path_rewritten']} · "
                  f"imports_inferred=`{r['apply_imports_inferred'] or '—'}`")
        md.append(f"- **Compile**: recompile_ok={r['recompile_ok']} · "
                  f"host_compile_ok={r['host_compile_ok']}")
        md.append(f"- **Verify**: tests={r['step_13_tests']} failures={r['step_13_failures']} "
                  f"errors={r['step_13_errors']} · markers={r['failure_markers']}")
        if diag:
            md.append(f"- **Diagnosis (first 250 chars)**:\n  > {diag.replace(chr(10), ' ')}\n")
        if r["fail_snippet"]:
            md.append(f"- **Fail snippet**: `{r['fail_snippet']}`")
        # Markdown link with URL-encoded spaces
        link_dir = f"{container}%20{r['model']}/run%20{r['run']}"
        md.append(f"- 📁 [pipeline.log]({link_dir}/pipeline.log) · "
                  f"[Steps Output Files/]({link_dir}/Steps%20Output%20Files/) · "
                  f"[Flaky/]({link_dir}/Flaky/)\n")

    md_path = runs_root / "summary.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    print(f"[wrapper] summary written: {csv_path.name}, {md_path.name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("container")
    ap.add_argument("--models", default="claude,openai")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--keep-workspace", action="store_true",
                    help="don't clean up data/<container>/ scratch workspace + "
                         "docker container after the batch (default: clean up; "
                         "the per-run folders already have their own snapshots)")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    row, test_type, script = preflight(args.container, models)

    # Place the runs folder INSIDE data/ so it inherits the project's existing
    # `data/` gitignore. Putting it at REPROFLAKE_DIR/ would make every
    # archived source tree show up as untracked (thousands of files) until
    # the user adds a separate gitignore entry.
    runs_root = DATA_DIR / f"{args.container} runs"
    runs_root.mkdir(exist_ok=True)
    print(f"[wrapper] container={args.container}  test_type={test_type}  "
          f"models={models}  runs={args.runs}")
    print(f"[wrapper] runs_root={runs_root}")

    data_container_dir = DATA_DIR / args.container

    rows = []
    for model in models:
        for run_n in range(1, args.runs + 1):
            per_run_dir = runs_root / f"{args.container} {model}" / f"run {run_n}"
            sentinel = per_run_dir / SENTINEL
            verdict_file = per_run_dir / "Steps Output Files" / "verify_after_fix.verdict"

            # Skip-detection: complete sentinel + verdict in {PASSED, FAILED}
            verdict_ok = verdict_file.is_file() and verdict_file.read_text(encoding="utf-8").strip() in ("PASSED", "FAILED")
            if not args.force and sentinel.is_file() and verdict_ok:
                print(f"[wrapper] skipping {model}/run {run_n} (already complete: "
                      f"{verdict_file.read_text(encoding='utf-8').strip()})")
                rows.append(parse_run(per_run_dir, args.container, test_type, model, run_n))
                continue

            # API-key check deferred to here so that already-skipped runs
            # don't need keys for unrelated models.
            if not os.environ.get(MODEL_API_KEY[model]):
                sys.exit(f"ERROR: {MODEL_API_KEY[model]} env var not set "
                         f"(required to execute {model}/run {run_n})")

            if args.force or per_run_dir.exists():
                print(f"[wrapper] clearing {per_run_dir} for fresh run")
                shutil.rmtree(per_run_dir, ignore_errors=True)
            per_run_dir.mkdir(parents=True, exist_ok=True)

            print(f"[wrapper] === starting {model}/run {run_n} ===")
            t0 = time.time()
            pipeline_log = per_run_dir / "pipeline.log"
            env = os.environ.copy()
            env.pop("KEEP_SOURCE", None)  # don't let user-set KEEP_SOURCE break run independence

            with open(pipeline_log, "w", encoding="utf-8") as logf:
                p = subprocess.Popen(
                    [str(script), args.container, model],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    env=env, text=True, bufsize=1,
                )
                for line in p.stdout:
                    sys.stdout.write(line)
                    logf.write(line)
                p.wait()
                exit_code = p.returncode

            elapsed = time.time() - t0
            print(f"[wrapper] === finished {model}/run {run_n} (exit={exit_code}, "
                  f"wall={elapsed:.0f}s) ===")

            # Archive (sources first, Steps Output Files last; sentinel last of all)
            archive_run(data_container_dir, per_run_dir)

            # If verdict missing after the run, write INCOMPLETE
            v_file = per_run_dir / "Steps Output Files" / "verify_after_fix.verdict"
            if not v_file.is_file():
                v_file.parent.mkdir(parents=True, exist_ok=True)
                v_file.write_text("INCOMPLETE\n")

            # Sentinel last
            sentinel.write_text(f"exit_code={exit_code}\nelapsed={elapsed:.1f}\n")

            row_data = parse_run(per_run_dir, args.container, test_type, model, run_n)
            row_data["elapsed_total_seconds"] = round(elapsed, 1)
            rows.append(row_data)

            # Incremental summary write — pull EVERY run on disk (not just
            # the ones from this invocation), so single-model invocations
            # don't truncate the summary and lose prior data.
            all_rows = collect_all_rows_on_disk(runs_root, args.container, test_type)
            write_summary(all_rows, runs_root, args.container, row, args.runs)

    # Always write summary at the end too, so a fully-skipped invocation
    # still refreshes the report (e.g., after extractor changes in this
    # script that the user wants applied to existing runs). Same disk-walk
    # rationale as the incremental write.
    all_rows = collect_all_rows_on_disk(runs_root, args.container, test_type)
    if all_rows:
        write_summary(all_rows, runs_root, args.container, row, args.runs)

    # Clean up the scratch workspace at data/<container>/ — it's just the
    # last run's state, redundant with the archived per-run snapshots.
    # Also stop the docker container the per-type script left running
    # (and that bind-mounts the workspace, so it must die before we wipe).
    if not args.keep_workspace:
        container_name = "tm_" + re.sub(r"[^a-zA-Z0-9]", "_", args.container)
        subprocess.run(["docker", "rm", "-f", container_name],
                       capture_output=True)
        if data_container_dir.is_dir():
            print(f"[wrapper] cleaning workspace {data_container_dir.name}/")
            shutil.rmtree(data_container_dir)

    print(f"[wrapper] DONE. {sum(1 for r in rows if r['verdict']=='PASSED')}/{len(rows)} runs PASSED.")


if __name__ == "__main__":
    main()
