#!/usr/bin/env python3
"""
simulate_run_pass_at_k.py — replay-mode pass@k harness.

Mirrors run_pass_at_k.py end-to-end except every LLM call is replayed from
a previously-archived run under data/FULL_RUNS_RV/ or data/FULL_RUNS_NO_RV/.
Used for reproducibility checks: the deterministic parts of the pipeline
(extract sources, mvn, traces, RV analysis, apply patch, recompile, verify)
run for real; only the LLM step is fed canned responses from disk.

Usage:
  ./simulate_run_pass_at_k.py <container> --rv-traces <yes|no>
                              [--models claude,openai] [--runs 2]

--rv-traces selects BOTH the LLM-context variant (same as run_pass_at_k.py)
AND the source archive to replay from. Output goes to:
    data/SIMULATED_RUNS_RV/<container> runs/<Model>/run <N>/   (if yes)
    data/SIMULATED_RUNS_NO_RV/<container> runs/<Model>/run <N>/ (if no)

--runs is clamped to 2 because that's what's archived. The simulator
pre-validates that every (model, run) source has at least
llm_response_turn1.json before starting any docker/maven work.

If the simulated pipeline diverges from the original (e.g. simulated verify
produces FAILED so feedback fires, but the archive's original turn-1 patch
passed verify and no turn-3 file exists), call_llm_simulate.py hard-errors;
the wrapper records that run as INCOMPLETE and continues to the next.
"""

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Reuse all the heavy lifting (preflight CSV lookup, archiving, parsing,
# summary writing) from the live wrapper. Only the bits that need different
# behaviour for simulation (path resolution, env injection, no API key
# check, separate cross-invocation log) are reimplemented here.
import run_pass_at_k as rpak
from run_pass_at_k import (
    archive_run,
    collect_all_rows_on_disk,
    load_csv_row,
    parse_run,
    write_summary,
    DATA_DIR,
    REPROFLAKE_DIR,
    MODEL_DIR_NAME,
    SENTINEL,
    TYPE_TO_SCRIPT,
    CSV_FILE,
)


# Cross-invocation log for simulated runs. Same column shape as the live
# log — just a separate file so simulated rows don't pollute the real
# metrics. Header reused from run_pass_at_k.
COMPLETE_SUMMARY_FILE = REPROFLAKE_DIR / "Simulated Complete Containers Summary.csv"
COMPLETE_SUMMARY_COLS = rpak.COMPLETE_SUMMARY_COLS


def preflight(container, models):
    """Same shape as rpak.preflight, but skips the API-key check since the
    simulator never calls a live backend. Also skips the docker daemon
    check is left in — we still need docker for the build/verify steps."""
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
        if m not in MODEL_DIR_NAME:
            sys.exit(f"ERROR: unknown model '{m}'")

    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        sys.exit("ERROR: Docker daemon not reachable")

    return row, test_type, script


_first_append_this_process = True


def append_complete_summary(rows, rv_traces):
    """Append per-(model, run) rows to the simulated cross-invocation log.

    Behavior mirrors rpak.append_complete_summary: first call in this
    process either appends with a blank-line separator (if header matches)
    or rewrites for schema migration (if header differs); subsequent calls
    plain-append. Same atomic-rewrite reasoning applies.
    """
    global _first_append_this_process
    if not rows:
        return
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    new_row_dicts = []
    for r in rows:
        new_row_dicts.append({
            "timestamp": timestamp,
            "container": r["container"],
            "test_type": r["test_type"],
            "model": r["model"],
            "run": f"run {r['run']}",
            "final verdict": r["verdict"],
            "turns_taken": r["turns_taken"],
            "rv_traces_used": rv_traces,
            "feedback_used": r.get("feedback_used", "no"),
            "verdict_pre_feedback": r.get("verdict_pre_feedback", r["verdict"]),
            "feedback_category": r.get("feedback_category", ""),
            "input_tokens": r["input_tokens_total"],
            "output_tokens": r["output_tokens_total"],
            "total_tokens": r["total_tokens"],
            "llm_seconds": round(r["elapsed_llm_seconds"], 1),
        })

    if _first_append_this_process:
        _first_append_this_process = False
        existing_header = None
        if COMPLETE_SUMMARY_FILE.is_file() and COMPLETE_SUMMARY_FILE.stat().st_size > 0:
            with open(COMPLETE_SUMMARY_FILE, encoding="utf-8", newline="") as f:
                try:
                    existing_header = next(csv.reader(f))
                except StopIteration:
                    existing_header = None

        if existing_header == COMPLETE_SUMMARY_COLS:
            with open(COMPLETE_SUMMARY_FILE, "a", encoding="utf-8", newline="") as f:
                f.write("\n")
                w = csv.DictWriter(
                    f, fieldnames=COMPLETE_SUMMARY_COLS,
                    quoting=csv.QUOTE_ALL, extrasaction="ignore",
                )
                for r in new_row_dicts:
                    w.writerow(r)
        else:
            existing_rows = []
            if COMPLETE_SUMMARY_FILE.is_file():
                with open(COMPLETE_SUMMARY_FILE, encoding="utf-8", newline="") as f:
                    existing_rows = list(csv.DictReader(f))
            for r in existing_rows:
                if "verdict" in r and "final verdict" not in r:
                    r["final verdict"] = r.pop("verdict")
            tmp_path = COMPLETE_SUMMARY_FILE.with_suffix(COMPLETE_SUMMARY_FILE.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(
                    f, fieldnames=COMPLETE_SUMMARY_COLS,
                    quoting=csv.QUOTE_ALL, extrasaction="ignore",
                )
                w.writeheader()
                for r in existing_rows:
                    w.writerow(r)
                if existing_rows:
                    f.write("\n")
                for r in new_row_dicts:
                    w.writerow(r)
            tmp_path.replace(COMPLETE_SUMMARY_FILE)
    else:
        with open(COMPLETE_SUMMARY_FILE, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(
                f, fieldnames=COMPLETE_SUMMARY_COLS,
                quoting=csv.QUOTE_ALL, extrasaction="ignore",
            )
            for r in new_row_dicts:
                w.writerow(r)

    print(f"[sim] appended {len(rows)} row(s) to {COMPLETE_SUMMARY_FILE.name}")


def main():
    ap = argparse.ArgumentParser(
        description="Simulated pass@k harness — replays archived LLM responses."
    )
    ap.add_argument("container")
    ap.add_argument("--rv-traces", choices=["yes", "no"], required=True,
                    help="ablation switch: 'yes' replays from data/FULL_RUNS_RV/ "
                         "and writes to data/SIMULATED_RUNS_RV/; 'no' replays from "
                         "data/FULL_RUNS_NO_RV/ and writes to "
                         "data/SIMULATED_RUNS_NO_RV/. Required.")
    ap.add_argument("--models", default="claude,openai")
    ap.add_argument("--runs", type=int, default=2,
                    help="number of runs per model (max 2; archive only has 2). "
                         "Values >2 are clamped with a warning.")
    ap.add_argument("--keep-workspace", action="store_true",
                    help="don't clean up data/<container>/ scratch workspace + "
                         "docker container after the batch (default: clean up)")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]

    if args.runs > 2:
        print(f"[sim] WARNING: --runs={args.runs} requested; archive only has 2. Clamping to 2.")
        args.runs = 2
    if args.runs < 1:
        sys.exit(f"ERROR: --runs must be >= 1, got {args.runs}")

    row, test_type, script = preflight(args.container, models)

    sim_dir_name = "SIMULATED_RUNS_RV" if args.rv_traces == "yes" else "SIMULATED_RUNS_NO_RV"
    runs_root = DATA_DIR / sim_dir_name / f"{args.container} runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    src_dir_name = "FULL_RUNS_RV" if args.rv_traces == "yes" else "FULL_RUNS_NO_RV"
    src_root = DATA_DIR / src_dir_name / f"{args.container} runs"

    print(f"[sim] container={args.container}  test_type={test_type}  "
          f"models={models}  runs={args.runs}  rv_traces={args.rv_traces}")
    print(f"[sim] reading canned responses from {src_root}")
    print(f"[sim] writing simulated output to    {runs_root}")

    # Pre-validate every (model, run) source archive exists before starting
    # any docker/maven work — fail fast rather than after a 10-minute build.
    missing = []
    for model in models:
        for run_n in range(1, args.runs + 1):
            src_steps = src_root / MODEL_DIR_NAME[model] / f"run {run_n}" / "Steps Output Files"
            t1 = src_steps / "llm_response_turn1.json"
            if not t1.is_file():
                missing.append(str(t1))
    if missing:
        print("ERROR: canned LLM archive(s) missing:", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        sys.exit(1)

    data_container_dir = DATA_DIR / args.container

    rows = []
    for model in models:
        for run_n in range(1, args.runs + 1):
            per_run_dir = runs_root / MODEL_DIR_NAME[model] / f"run {run_n}"
            sentinel = per_run_dir / SENTINEL

            if per_run_dir.exists():
                print(f"[sim] clearing {per_run_dir} for fresh run")
                shutil.rmtree(per_run_dir, ignore_errors=True)
            per_run_dir.mkdir(parents=True, exist_ok=True)

            # Same dynamic-output wipe as rpak — see comment there for why.
            for stale in ("Steps Output Files", "result",
                          "traces-fixed", "traces-flaky", "traces-flakycc",
                          "traces-pass", "traces-fail"):
                stale_path = data_container_dir / stale
                if stale_path.is_dir():
                    shutil.rmtree(stale_path, ignore_errors=True)

            print(f"[sim] === starting {model}/run {run_n} ===")
            t0 = time.time()
            pipeline_log = per_run_dir / "pipeline.log"
            env = os.environ.copy()
            env.pop("KEEP_SOURCE", None)
            env["KEEP_CONTAINER"] = "1"
            env["RV_TRACES"] = args.rv_traces

            # The simulation hook: tell call_llm to replay from this
            # specific (model, run) archive instead of calling a live API.
            src_steps = src_root / MODEL_DIR_NAME[model] / f"run {run_n}" / "Steps Output Files"
            env["SIMULATE_FROM"] = str(src_steps)

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
            print(f"[sim] === finished {model}/run {run_n} (exit={exit_code}, "
                  f"wall={elapsed:.0f}s) ===")

            archive_run(data_container_dir, per_run_dir)

            # Same defense-in-depth as rpak: force INCOMPLETE if exit_code
            # is non-zero or no verdict was written. Covers the divergence
            # case (call_llm_simulate.py exits 1 with a clear message when
            # a needed turn file is missing).
            v_file = per_run_dir / "Steps Output Files" / "verify_after_fix.verdict"
            if exit_code != 0:
                v_file.parent.mkdir(parents=True, exist_ok=True)
                v_file.write_text("INCOMPLETE\n")
            elif not v_file.is_file():
                v_file.parent.mkdir(parents=True, exist_ok=True)
                v_file.write_text("INCOMPLETE\n")

            sentinel.write_text(f"exit_code={exit_code}\nelapsed={elapsed:.1f}\n")

            row_data = parse_run(per_run_dir, args.container, test_type, model, run_n)
            row_data["elapsed_total_seconds"] = round(elapsed, 1)
            rows.append(row_data)

            all_rows = collect_all_rows_on_disk(runs_root, args.container, test_type)
            write_summary(all_rows, runs_root, args.container, row, args.runs, log_prefix="[sim]")

            append_complete_summary([row_data], args.rv_traces)

    all_rows = collect_all_rows_on_disk(runs_root, args.container, test_type)
    if all_rows:
        write_summary(all_rows, runs_root, args.container, row, args.runs, log_prefix="[sim]")

    if not args.keep_workspace:
        container_name = "tm_" + re.sub(r"[^a-zA-Z0-9]", "_", args.container)
        subprocess.run(["docker", "rm", "-f", container_name],
                       capture_output=True)
        if data_container_dir.is_dir():
            print(f"[sim] cleaning workspace {data_container_dir.name}/")
            shutil.rmtree(data_container_dir)

    print(f"[sim] DONE. {sum(1 for r in rows if r['verdict']=='PASSED')}/{len(rows)} runs PASSED.")


if __name__ == "__main__":
    main()
