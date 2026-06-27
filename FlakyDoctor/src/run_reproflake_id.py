#!/usr/bin/env python3
"""
run_reproflake_id.py — run a ReproFlake ID container end-to-end through FlakyDoctor + Claude.

The ID analog of run_reproflake.py. It reuses that module's staging / build / maven
helpers verbatim, but swaps the OD-specific parts for ID:

  1. select the `id` row from test_config.csv (by result_container name)
  2. stage the Zenodo zip into projects/<zipbase>/<project>/  (reuses run_reproflake)
  3. git-init a baseline commit                               (reuses run_reproflake)
  4. build with the container's pre-staged .m2                (reuses run_reproflake)
  5. DETERMINISTICALLY reproduce the ID flake with the CSV's nondexSeed
     (mvn nondex -DnondexSeed=<seed>) — gated on >=1 NonDex iteration failing.
     Zero API cost.
  6. run flakydoctor.py --flakiness-type ID --model Claude   (unless --skip-repair)
  7. summarize.

NOTE ON DETERMINISM: only THIS driver's reproduce gate (step 5) is seed-pinned.
FlakyDoctor's own per-round NonDex check (src/cmds/run_nondex.sh) is left STOCK
(probabilistic, nondexRuns only) on purpose — we do not modify FlakyDoctor.

Usage (from the FlakyDoctor root):
  python3 src/run_reproflake_id.py \
      --test-config ../ReproFlake-C9E6/test_config.csv \
      --container apollojavaapolloopenapi5344bc4testFindItemsByNamespace \
      --api-key "$(cat ~/.anthropic_api_key)"

  python3 src/run_reproflake_id.py --test-config ... --list
  python3 src/run_reproflake_id.py --test-config ... --container X --skip-repair   # reproduce only
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time

import run_reproflake as rf  # reuse stage_container/build_project/maven_env/ensure_git_baseline/...

# Mirror run_nondex.sh's plugin + skip flags so the gate behaves like FlakyDoctor's
# NonDex run, with one addition: -DnondexSeed for deterministic reproduction.
NONDEX_PLUGIN = "edu.illinois:nondex-maven-plugin:2.1.7:nondex"
NONDEX_FLAGS = (
    "-Dbasepom.check.skip-prettier -Dgpg.skip -Dfindbugs.skip=true -Drat.skip "
    "-Dcheckstyle.skip -Denforcer.skip=true -Dspotbugs.skip -Dmaven.test.failure.ignore=true "
    "-Djacoco.skip -Danimal.sniffer.skip -Dmaven.antrun.skip -Dfmt.skip -Dskip.npm "
    "-Dlicense.skipCheckLicense -Dlicense.skipAddThirdParty=true -Dlicense.skip -Dskip.yarn "
    "-Dskip.bower -Dskip.grunt -Dskip.gulp -Dskip.jspm -Dskip.karma -Dskip.webpack "
    "-DskipDockerBuild -DskipDockerTag -DskipDockerPush -DskipDocker -Dstyle.color=never "
    "-Ddependency-check.skip -Dspotless.check.skip -Dskip.web.build=true"
).split()


# ---------------------------------------------------------------- row parsing

def load_id_rows(test_config):
    """All `id` rows. Columns (0-indexed): test=line[5], module=line[3],
    java=line[8], nondexSeed=line[9], url=line[10]."""
    rows = []
    with open(test_config, newline="") as f:
        for line in csv.reader(f):
            if len(line) < 11 or line[0].strip().lower() != "id":
                continue
            rows.append({
                "container": line[1].strip(),
                "zip_id": line[2].strip(),
                "module": line[3].strip() or ".",
                "test": line[5].strip(),        # Class#method (the single flaky test)
                "iterations": line[6].strip() or "10",
                "java": line[8].strip() or "8",
                "seed": line[9].strip(),         # NonDex seed for deterministic repro
                "url": line[10].strip(),
            })
    return rows


def nondex_runs(iterations, cap=10):
    """ReproFlake's iteration count, capped (a few runs is enough to trip a seeded flake)."""
    try:
        n = int(iterations)
    except (TypeError, ValueError):
        n = 10
    return str(min(max(n, 1), cap))


# --------------------------------------------------------- reproduce (seeded)

def _surefire_totals(output):
    """Sum (failures+errors) and tests across every NonDex iteration's Surefire line."""
    import re
    tests = fails = iters_failed = 0
    for m in re.finditer(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+)", output):
        t, f, e = int(m.group(1)), int(m.group(2)), int(m.group(3))
        tests += t
        if f + e >= 1:
            fails += f + e
            iters_failed += 1
    return tests, fails, iters_failed


def reproduce_with_nondex_seed(container_dir, project_dir, module, test, seed, runs, jdk):
    """Deterministic, seed-pinned NonDex reproduction. Gate: at least one NonDex
    iteration must FAIL (the ID flake reproduces). Zero API cost."""
    if not seed:
        rf.die("ID row has no nondexSeed — cannot reproduce deterministically")
    rf.log(f"reproducing with NonDex seed={seed}, runs={runs} (deterministic)")
    env = rf.maven_env(container_dir, jdk)
    cmd = (["mvn", NONDEX_PLUGIN, "-pl", module,
            f"-Dtest={test}", f"-DnondexSeed={seed}", f"-DnondexRuns={runs}"]
           + NONDEX_FLAGS)
    try:
        res = subprocess.run(cmd, cwd=project_dir, env=env,
                             capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        rf.die("NonDex run timed out after 30 min")
    out = res.stdout + ("\n" + res.stderr if res.stderr else "")
    if "BUILD FAILURE" in out and "Tests run:" not in out:
        rf.die("NonDex could not build/run the test (last lines):\n"
               + "\n".join(out.splitlines()[-15:]))
    tests, fails, iters_failed = _surefire_totals(out)
    rf.log(f"  NonDex totals: tests={tests}, failing-iterations={iters_failed}")
    if tests < 1:
        rf.die("NonDex executed 0 tests — check the build/test name (last lines):\n"
               + "\n".join(out.splitlines()[-15:]))
    if iters_failed < 1:
        rf.die("NonDex produced 0 failures under the recorded seed — flake did not "
               "reproduce here; not spending API calls.")
    rf.log(f"ID FLAKE REPRODUCED — {iters_failed} NonDex iteration(s) failed under seed {seed}")


# ------------------------------------------------------------------- repair

def run_flakydoctor_id(container_dir, row, github_url, project_name, projects_dir,
                       api_key, model, runs, jdk):
    zip_base = os.path.basename(container_dir)
    _, test_dotted = rf.split_test(row["test"])
    url = github_url or f"https://github.com/reproflake/{project_name}"
    if url.rstrip("/").split("/")[-1] != project_name:
        rf.die(f"staged dir name '{project_name}' must equal the URL's last segment ({url})")

    out_dir = os.path.join("outputs", f"reproflake_id_{zip_base}_{time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(out_dir, exist_ok=True)
    input_csv = os.path.join(out_dir, "input.csv")
    # repair_ID input format: project,sha,module,test,test_type,status,pr,notes
    with open(input_csv, "w") as f:
        f.write(f"{url},{zip_base},{row['module']},{test_dotted},ID,,,\n")

    env = rf.maven_env(container_dir, jdk)
    cmd = ["python3", "-u", "src/flakydoctor.py",
           "--input-tests-csv", input_csv,
           "--flakiness-type", "ID",
           "--projects", projects_dir,
           "--api-key", api_key,
           "--model", model,
           "--nondex-times", runs,
           "--output-dir", out_dir,
           "--output-result-csv", os.path.join(out_dir, "results.csv"),
           "--output-result-json", os.path.join(out_dir, "results.json"),
           "--output-details-json", os.path.join(out_dir, "details.json")]
    rf.log(f"running FlakyDoctor ({model}, ID); live output follows, artifacts in {out_dir}/")
    rf.run_flakydoctor_cmd(cmd, env, out_dir)
    return out_dir


def summarize_id(out_dir, container_dir):
    results = os.path.join(out_dir, "results.json")
    if not os.path.exists(results):
        rf.log("no results.json produced — check the run output above")
        return
    raw = open(results).read().strip()
    if not raw:
        rf.log("results.json is empty — FlakyDoctor produced no verdict (check output above)")
        return
    dec, idx = json.JSONDecoder(), 0
    while idx < len(raw):
        obj, end = dec.raw_decode(raw, idx)
        idx = end
        while idx < len(raw) and raw[idx] in " \n\t,":
            idx += 1
        tr = obj.get("test_results", {})
        fixed = any(v == "test_pass" for v in tr.values())
        print()
        rf.log(f"test    : {obj.get('victim') or obj.get('test')}")
        rf.log(f"rounds  : {tr}")
        rf.log(f"fixed   : {'YES' if fixed else 'NO'}")
        rf.log(f"patch   : {obj.get('patch_file')}")
        if obj.get("Exceptions"):
            rf.log(f"exceptions: {obj['Exceptions']}")
    dev_fix = os.path.join(container_dir, "Fixed.patch")
    if os.path.exists(dev_fix):
        rf.log(f"developer's reference fix for comparison: {dev_fix}")


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test-config", required=True, help="path to ReproFlake's test_config.csv")
    ap.add_argument("--container", help="result_container name (col 2) of the id row to run")
    ap.add_argument("--list", action="store_true", help="list id rows")
    ap.add_argument("--api-key", help="Anthropic API key (required unless --skip-repair)")
    ap.add_argument("--model", default="Claude", help="FlakyDoctor model (default: Claude)")
    ap.add_argument("--projects", default="projects", help="FlakyDoctor projects dir")
    ap.add_argument("--nondex-times", help="override NonDex runs (default: CSV iterations, capped at 10)")
    ap.add_argument("--skip-repair", action="store_true", help="stop after reproducing (zero API cost)")
    ap.add_argument("--keep-zip", action="store_true", help="keep the downloaded zip in /tmp")
    ap.add_argument("--fresh", action="store_true",
                    help="remove any existing staged container and re-download + rebuild "
                         "from pristine source (use when re-running an already-repaired container)")
    args = ap.parse_args()

    if not os.path.exists("src/flakydoctor.py"):
        rf.die("run this from the FlakyDoctor root (src/flakydoctor.py not found)")

    rows = load_id_rows(args.test_config)
    if not rows:
        rf.die(f"no id rows found in {args.test_config}")

    if args.list:
        print(f"{'container':55} java  test")
        for r in rows:
            print(f"{r['container']:55} {r['java']:5} {r['test']}")
        return

    if not args.container:
        rf.die("--container is required (use --list to see options)")
    matches = [r for r in rows if r["container"] == args.container]
    if not matches:
        rf.die(f"no id row with result_container == {args.container}")
    row = matches[0]

    if not args.skip_repair and not args.api_key:
        rf.die("--api-key is required (or pass --skip-repair to stop after reproduction)")

    runs = args.nondex_times or nondex_runs(row["iterations"])
    container_dir, project_dir, project_name, github_url = \
        rf.stage_container(row, args.projects, keep_zip=args.keep_zip, fresh=args.fresh)
    rf.ensure_git_baseline(project_dir)
    jdk = rf.build_project(container_dir, project_dir, row["module"], row["java"])
    reproduce_with_nondex_seed(container_dir, project_dir, row["module"],
                               row["test"], row["seed"], runs, jdk)

    if args.skip_repair:
        rf.log(f"--skip-repair: stopping after successful reproduction (seed={row['seed']}).")
        return

    out_dir = run_flakydoctor_id(container_dir, row, github_url, project_name,
                                 args.projects, args.api_key, args.model, runs, jdk)
    summarize_id(out_dir, container_dir)


if __name__ == "__main__":
    main()
