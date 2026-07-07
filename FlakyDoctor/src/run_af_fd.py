#!/usr/bin/env python3
"""
run_af_fd.py — run an AgentFlake OD container end-to-end through FlakyDoctor.

Bridges the format gap between AgentFlake's test_config.csv (Zenodo zip snapshots,
Class#method test names) and FlakyDoctor's OD pipeline (projects/<sha>/<name> git
checkouts, dotted test names), then executes the proven pipeline:

  1. select the od row from test_config.csv (by result_container name)
  2. download the Zenodo zip and stage it under projects/<zipbase>/<project>/
     (idempotent: skipped when already staged)
  3. git-init a baseline commit (FlakyDoctor restores files via git between rounds)
  4. build with the container's own pre-staged .m2 (MAVEN_ARGS, Maven 3.9+)
  5. auto-detect the SUREFIRE_RUN_ORDER that puts the polluter class first
     (stock Surefire has no `testorder` — that needs the Illinois fork)
  6. reproduce the flake (polluter passes, victim fails) — zero API cost
  7. run flakydoctor.py --model Claude (unless --skip-repair)
  8. summarize rounds / winning patch, and point at the developer's Fixed.patch

Usage (from the FlakyDoctor root):
  python3 src/run_af_fd.py \
      --container ormlitecore59309e5 \
      --api-key "$(cat .anthropic_api_key)"

  python3 src/run_af_fd.py --list                       # list runnable od rows
  python3 src/run_af_fd.py --container X --skip-repair
"""

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

MVN_SKIP_FLAGS = (
    # -Dskip.web.build=true skips graylog2-server's node/yarn web-UI build (its
    # web-interface-build profile is active unless this property is set); harmless
    # (unused) for every other project.
    "-DskipTests -Dfindbugs.skip=true -Dgpg.skip -Drat.skip -Dcheckstyle.skip "
    "-Denforcer.skip=true -Dspotbugs.skip -Djacoco.skip -Danimal.sniffer.skip "
    "-Dmaven.antrun.skip -Dlicense.skip -Dmaven.javadoc.skip=true -Dskip.web.build=true "
    "-DskipDockerBuild -Ddependency-check.skip -Dspotless.check.skip"
).split()

# hbase-common (and similar) GENERATE required sources (e.g. Version.java) with an
# antrun task that -Dmaven.antrun.skip starves, breaking compilation. The build
# fallback uses this antrun-enabled variant (skip dropped) so that codegen runs.
MVN_SKIP_FLAGS_ANTRUN = [f for f in MVN_SKIP_FLAGS if f != "-Dmaven.antrun.skip"]


def log(msg):
    print(f"[run_af_fd] {msg}", flush=True)


def die(msg, code=1):
    print(f"[run_af_fd] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


# ---------------------------------------------------------------- row parsing

def load_od_rows(test_config):
    """Return all od rows as dicts keyed by the test_config.csv columns."""
    rows = []
    with open(test_config, newline="") as f:
        for line in csv.reader(f):
            if len(line) < 11 or line[0].strip().lower() != "od":
                continue
            rows.append({
                "container": line[1].strip(),
                "zip_id": line[2].strip(),
                "module": line[3].strip() or ".",
                "polluter": line[4].strip(),   # Class#method
                "victim": line[5].strip(),     # Class#method (the flaky test)
                "java": line[8].strip() or "8",
                "url": line[10].strip(),
            })
    return rows


def split_test(hash_name):
    """'pkg.Class#method' -> (fqcn, dotted 'pkg.Class.method')."""
    if "#" not in hash_name:
        die(f"expected Class#method, got: {hash_name}")
    fqcn, method = hash_name.split("#", 1)
    return fqcn, f"{fqcn}.{method}"


def row_runnable(row):
    """Cross-class pairs are orderable via stock Surefire run orders. Same-class
    pairs depend on JUnit's deterministic in-class method order — they get one
    empirical trial run instead of order detection (see detect_order_and_reproduce)."""
    p_class, _ = split_test(row["polluter"])
    v_class, _ = split_test(row["victim"])
    return p_class != v_class


# ------------------------------------------------------------------- staging

def stage_container(row, projects_dir, keep_zip=False, fresh=False):
    """Download + unzip the Zenodo snapshot into projects/<zipbase>/.

    fresh=True removes any existing staged container first, forcing a clean
    re-download + rebuild from pristine source (so a re-run of an already-repaired
    container doesn't reuse last run's patched source / cached build).

    Returns (container_dir, project_dir, project_name, github_url)."""
    zip_base = os.path.basename(row["url"])
    zip_base = zip_base[:-4] if zip_base.endswith(".zip") else zip_base
    container_dir = os.path.join(projects_dir, zip_base)

    if fresh and os.path.isdir(container_dir):
        log(f"--fresh: removing existing staged container {container_dir}")
        shutil.rmtree(container_dir, ignore_errors=True)

    project_dir, project_name, github_url = _find_staged_project(container_dir)
    if project_dir:
        if os.path.isdir(os.path.join(container_dir, "Flakym2")):
            log(f"already staged: {project_dir} (skipping download)")
            return container_dir, project_dir, project_name, github_url
        # Project staged but its offline .m2 (Flakym2) was removed by a previous
        # run's cleanup — re-stage from scratch so the build has its dependencies.
        log(f"staged project found but Flakym2 (offline .m2) missing — re-staging {container_dir}")
        shutil.rmtree(container_dir, ignore_errors=True)

    zip_path = os.path.join("/tmp", f"af_fd_{zip_base}.zip")
    if not os.path.exists(zip_path):
        log(f"downloading {row['url']} ...")
        try:
            # download to a temp name; only rename once complete, so an interrupted
            # download can never be mistaken for a finished one on the next run
            urllib.request.urlretrieve(row["url"], zip_path + ".part")
            os.rename(zip_path + ".part", zip_path)
        except BaseException:
            if os.path.exists(zip_path + ".part"):
                os.remove(zip_path + ".part")
            raise
    log(f"unzipping {zip_path} ...")
    if not zipfile.is_zipfile(zip_path):
        os.remove(zip_path)
        die(f"{zip_path} is not a valid zip (corrupt download removed — rerun to refetch)")
    extract_root = os.path.join("/tmp", f"af_fd_extract_{zip_base}")
    shutil.rmtree(extract_root, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_root)

    # locate the directory that holds Flaky/ (zip usually wraps one top-level dir)
    src_root = None
    for cand in [extract_root] + [os.path.join(extract_root, d) for d in os.listdir(extract_root)]:
        if os.path.isdir(os.path.join(cand, "Flaky")):
            src_root = cand
            break
    if not src_root:
        die(f"could not find a Flaky/ directory inside {zip_path}")

    github_url, _ = _parse_flaky_info(os.path.join(src_root, "flaky_info.txt"))
    project_name = github_url.rstrip("/").split("/")[-1] if github_url \
        else row["zip_id"].split("=")[0]

    os.makedirs(container_dir, exist_ok=True)
    shutil.move(os.path.join(src_root, "Flaky"), os.path.join(container_dir, project_name))
    for extra in ("Flakym2", "Fixed.patch", "flaky_info.txt"):
        src = os.path.join(src_root, extra)
        if os.path.exists(src) and not os.path.exists(os.path.join(container_dir, extra)):
            shutil.move(src, os.path.join(container_dir, extra))
    shutil.rmtree(extract_root, ignore_errors=True)
    if not keep_zip:
        os.remove(zip_path)

    project_dir = os.path.join(container_dir, project_name)
    log(f"staged: {project_dir}")
    return container_dir, project_dir, project_name, github_url


def _parse_flaky_info(path):
    """flaky_info.txt: 'Flaky directory created from <url> at commit <sha>'."""
    if not os.path.exists(path):
        return None, None
    m = re.search(r"from\s+(\S+)\s+at commit\s+(\w+)", open(path).read())
    return (m.group(1), m.group(2)) if m else (None, None)


def _find_staged_project(container_dir):
    """If already staged, return (project_dir, project_name, github_url)."""
    if not os.path.isdir(container_dir):
        return None, None, None
    github_url, _ = _parse_flaky_info(os.path.join(container_dir, "flaky_info.txt"))
    for d in os.listdir(container_dir):
        full = os.path.join(container_dir, d)
        if d not in ("Flakym2",) and os.path.isdir(full) \
                and os.path.exists(os.path.join(full, "pom.xml")):
            return full, d, github_url
    return None, None, None


def remove_flaky_m2(container_dir):
    """Reclaim disk after a completed run by deleting the container's bundled
    offline maven repo (projects/<container>/Flakym2). Skipped when
    KEEP_FLAKY_M2=1 (e.g. between pass@k runs). A later re-run re-stages it
    automatically from the Zenodo zip."""
    if os.environ.get("KEEP_FLAKY_M2") == "1":
        return
    m2_dir = os.path.join(container_dir, "Flakym2")
    if os.path.isdir(m2_dir):
        shutil.rmtree(m2_dir, ignore_errors=True)
        log(f"cleanup: removed offline maven repo {m2_dir} (KEEP_FLAKY_M2=1 to keep)")


def ensure_git_baseline(project_dir):
    # CRITICAL: only skip if project_dir is the ROOT of its OWN git repo. A bare
    # `rev-parse --git-dir` succeeds even when project_dir merely sits *inside* an
    # outer repo — and these staged projects live under the bind-mounted FlakyRV
    # checkout. Without a project-local .git, FlakyDoctor's git_stash/git_checkout
    # would run against the OUTER repo and silently wipe its uncommitted changes.
    top = subprocess.run(["git", "-C", project_dir, "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    if top.returncode == 0 and \
            os.path.realpath(top.stdout.strip()) == os.path.realpath(project_dir):
        return
    log("creating project-local git baseline (isolates FlakyDoctor's git ops from the outer repo)")
    subprocess.run(["git", "-C", project_dir, "init", "-q"], check=True)
    subprocess.run(["git", "-C", project_dir, "add", "-A"], check=True)
    # Inline identity so the commit works in a fresh container with no global
    # git config (the image has neither user.name nor user.email set).
    subprocess.run(["git", "-C", project_dir,
                    "-c", "user.name=FlakyDoctor", "-c", "user.email=flakydoctor@local",
                    "commit", "-qm", "AgentFlake snapshot baseline"], check=True)


# -------------------------------------------------------------------- maven

def resolve_java_home(jdk):
    """macOS: /usr/libexec/java_home; Linux: the original hardcoded paths; then
    fall back to an existing $JAVA_HOME — inside the maven:3.8.6-openjdk-N base
    images the JDK lives at /usr/local/openjdk-N, not /usr/lib/jvm/..., so the
    hardcoded Linux path does not exist there."""
    if os.path.exists("/usr/libexec/java_home"):
        want = "1.8" if jdk == "8" else jdk
        out = subprocess.run(["/usr/libexec/java_home", "-v", want],
                             capture_output=True, text=True)
        if out.returncode == 0:
            return out.stdout.strip()
    linux_path = f"/usr/lib/jvm/java-1.{jdk}.0-openjdk-amd64"
    if os.path.isdir(linux_path):
        return linux_path
    env_home = os.environ.get("JAVA_HOME")
    if env_home and os.path.isdir(env_home):
        return env_home  # e.g. /usr/local/openjdk-8 inside the testorder image
    return linux_path


def maven_env(container_dir, jdk):
    env = dict(os.environ)
    env["JAVA_HOME"] = resolve_java_home(jdk)
    env["PATH"] = env["JAVA_HOME"] + "/bin:" + env["PATH"]
    staged_m2 = os.path.join(os.path.abspath(container_dir), "Flakym2", ".m2", "repository")
    if os.path.isdir(staged_m2):
        # MAVEN_ARGS is honored only by Maven 3.9+; the testorder image ships
        # Maven 3.8.6, which ignores it — so also pin repo.local via MAVEN_OPTS
        # (a JVM system property every Maven version reads). Set MAVEN_OPTS fresh
        # so it overrides the image's default /root/.m2 repo.local.
        env["MAVEN_ARGS"] = f"-Dmaven.repo.local={staged_m2}"
        env["MAVEN_OPTS"] = f"-Dmaven.repo.local={staged_m2}"
    # -Dskip.web.build=true via MAVEN_OPTS so it also reaches FlakyDoctor's stock
    # run_nondex.sh (it inherits this env) — deactivates graylog's yarn web build
    # at the verification step too, without modifying FlakyDoctor.
    env["MAVEN_OPTS"] = (env.get("MAVEN_OPTS", "") + " -Dskip.web.build=true").strip()
    return env


def clean_m2_markers(container_dir):
    """Remove Maven resolution-failure markers from the staged offline .m2 so that
    locally-present artifacts resolve from the local repo instead of being re-queried
    against now-dead remote repos. AgentFlake's staged .m2 was populated on machines
    with the projects' (often http://) repositories configured; Maven 3.8+ blocks
    http:// and re-verifies artifacts cached from unknown remote IDs. We strip:
      _remote.repositories - origin-tracking; deleting makes cached artifacts count
                             as locally installed
      *.lastUpdated        - cached "remote download failed" markers; while present
                             Maven refuses to reattempt OR fall back to the local jar
                             even though it is right there (breaks inter-module
                             SNAPSHOTs, e.g. servicecomb's foundation-config:SNAPSHOT)
    """
    staged_m2 = os.path.join(container_dir, "Flakym2", ".m2", "repository")
    if os.path.isdir(staged_m2):
        subprocess.run(["find", staged_m2, "-name", "_remote.repositories", "-delete"])
        subprocess.run(["find", staged_m2, "-name", "*.lastUpdated", "-delete"])


def strip_snapshot_versions(project_dir):
    """Rewrite <version>X-SNAPSHOT</version> -> <version>X</version> in every pom.xml
    under project_dir, so the project and its inter-module dependencies resolve against
    the RELEASE artifacts cached in the staged .m2 (the matching -SNAPSHOT jars are
    absent and no longer served by any remote). Dataset author's workaround for projects
    like servicecomb, whose test module needs foundation-config:3.0.0-SNAPSHOT while only
    the released 3.0.0 is cached. Opt-in via --strip-snapshot. Call AFTER ensure_git_baseline
    and COMMIT the rewrite, so it survives FlakyDoctor's `git stash` during repair (the
    staged source is itself a git repo, so an uncommitted rewrite gets stashed away before
    the verify and the -SNAPSHOT poms come back)."""
    res = subprocess.run(["find", project_dir, "-name", "pom.xml"],
                         capture_output=True, text=True)
    poms = [p for p in res.stdout.splitlines() if p]
    for p in poms:
        subprocess.run(["sed", "-i", "s|-SNAPSHOT</version>|</version>|g", p])
    subprocess.run(["git", "-C", project_dir, "add", "-A"])
    subprocess.run(["git", "-C", project_dir,
                    "-c", "user.name=FlakyDoctor", "-c", "user.email=flakydoctor@local",
                    "commit", "-qm", "strip -SNAPSHOT versions (resolve against cached releases)"])
    log(f"stripped -SNAPSHOT from {len(poms)} pom.xml file(s) and committed (survives git stash)")


def _maven_error_lines(output):
    """Surface the informative Maven failure lines (the 'Failed to execute goal'
    reason, compilation errors, unresolved dependencies) instead of Maven's
    generic '[Help 1]' footer."""
    keys = ("Failed to execute goal", "COMPILATION ERROR", "cannot find symbol",
            "does not exist", "Could not resolve dependencies", "Could not find artifact",
            "Non-resolvable", "Caused by:", "BUILD FAILURE")
    hits, seen = [], set()
    for ln in output.splitlines():
        s = ln.strip()
        if s and s not in seen and any(k in ln for k in keys):
            seen.add(s)
            hits.append(s)
    if hits:
        return "\n".join(hits[:12])
    return "\n".join(output.splitlines()[-12:])


def build_project(container_dir, project_dir, module, jdk):
    clean_m2_markers(container_dir)
    marker = os.path.join(project_dir, ".af_fd_built")
    if os.path.exists(marker):
        built_jdk = open(marker).read().strip() or jdk
        log(f"already built with JDK {built_jdk} (marker exists) — skipping build")
        return built_jdk
    # Most projects `install` cleanly. A couple of build patterns need a fallback,
    # tried in order (first BUILD SUCCESS wins). FlakyDoctor only needs the module +
    # its tests COMPILED — the NonDex reproduce and every per-round verification run
    # at the test phase, before package — so test-compile suffices when install can't
    # finish. Fallbacks only run when earlier strategies fail, so any project that
    # installs normally is completely unaffected:
    #   install                  - normal full build
    #   test-compile             - packaging=bundle projects (e.g. avro) whose
    #                              maven-bundle-plugin `bundle` goal fails at package
    #   test-compile + antrun on - projects that GENERATE sources via antrun (e.g.
    #                              hbase-common's Version.java), which the default
    #                              -Dmaven.antrun.skip starves; drop the skip
    strategies = [
        ("install", MVN_SKIP_FLAGS),
        ("test-compile", MVN_SKIP_FLAGS),
        ("test-compile", MVN_SKIP_FLAGS_ANTRUN),
    ]
    last_out = ""
    for goal, flags in strategies:
        label = goal + ("" if "-Dmaven.antrun.skip" in flags else "+antrun")
        for try_jdk in [jdk, "11" if jdk == "8" else "8"]:
            log(f"building ({label}) with JDK {try_jdk} ...")
            cmd = ["mvn", goal, "-pl", module, "-am"] + flags
            try:
                res = subprocess.run(cmd, cwd=project_dir, env=maven_env(container_dir, try_jdk),
                                     capture_output=True, text=True, timeout=3600)
            except subprocess.TimeoutExpired:
                log(f"build timed out after 1h on jdk {try_jdk}")
                continue
            if "BUILD SUCCESS" in res.stdout:
                log(f"BUILD SUCCESS ({label}, jdk {try_jdk})")
                open(marker, "w").write(try_jdk)
                return try_jdk
            last_out = res.stdout + (("\n" + res.stderr) if res.stderr else "")
            log(f"BUILD FAILURE ({label}) on jdk {try_jdk}:\n" + _maven_error_lines(last_out))
    # The per-strategy summary above shows only the key error lines; save the full
    # log of the last attempt so the real cause is inspectable on the host.
    fail_log = os.path.join(container_dir, "build_failure.log")
    try:
        with open(fail_log, "w") as fh:
            fh.write(last_out)
        log(f"full build log of the last attempt saved to {fail_log}")
    except OSError:
        fail_log = "(could not write build_failure.log)"
    die("project did not build with JDK 8 or 11 (tried install, test-compile, antrun-on); "
        f"see {fail_log} for the full error")


# --------------------------------------------------------- reproduce / order

def run_surefire(container_dir, project_dir, module, polluter, victim, jdk, run_order):
    env = maven_env(container_dir, jdk)
    env["SUREFIRE_RUN_ORDER"] = run_order
    try:
        res = subprocess.run(
            ["bash", "src/cmds/run_surefire.sh", project_dir, module, polluter, victim, jdk],
            env=env, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        die("surefire run timed out after 30 min — the victim/polluter likely hangs "
            "(e.g. waits on a network resource); this pair is not repairable here")
    # Fold stderr in: a mvn that dies before producing a "Tests run" line (bad
    # JAVA_HOME, JVM crash) writes only to stderr, which would otherwise be lost
    # and misread as a bare build_failure with no diagnostics.
    return res.stdout + (("\n" + res.stderr) if res.stderr else "")


def first_running_class(output):
    m = re.search(r"Running ([\w.$]+)", output)
    return m.group(1) if m else None


def od_test_result(output):
    """Mirror repair_OD.analyze_surefire_test_result for the 2-test totals line."""
    if re.search(r"Tests run: 2, Failures: 0, Errors: 0, Skipped: 0", output):
        return "test_pass"
    if re.search(r"Tests run: 2, (Failures: [12], Errors: 0|Failures: 0, Errors: [12]), Skipped: 0", output):
        return "test_failure"
    if "COMPILATION ERROR" in output:
        return "compilation_error"
    return "build_failure"


def victim_failed(output, victim):
    """True if the victim's method name appears on a surefire FAILURE/ERROR line.
    Guards against gating on a failure of the POLLUTER (e.g. a platform issue)."""
    method = victim.split("#", 1)[1]
    for line in output.splitlines():
        if method in line and ("FAILURE" in line or "<<< ERROR" in line):
            return True
    return False


def detect_order_and_reproduce(container_dir, project_dir, module, row, jdk):
    """Find the stock run order that puts the polluter first AND fails the victim."""
    p_class, _ = split_test(row["polluter"])
    v_class, _ = split_test(row["victim"])

    if p_class == v_class:
        # Same-class pair: stock Surefire cannot order methods within a class, but
        # JUnit's in-class method order is deterministic (hash-based) — it may already
        # run the polluter first. One trial run, gated on the VICTIM failing; the
        # order is stable across reruns, so FlakyDoctor's verify loop sees it too.
        log("same-class pair — trying JUnit's deterministic in-class method order")
        out = run_surefire(container_dir, project_dir, module,
                           row["polluter"], row["victim"], jdk, "alphabetical")
        result = od_test_result(out)
        log(f"  result: {result}")
        if result == "test_failure" and victim_failed(out, row["victim"]):
            log("FLAKE REPRODUCED under JUnit's fixed in-class method order")
            return "alphabetical"  # value irrelevant within a class; must be a valid runOrder
        if result == "test_failure":
            die("a test failed, but not the victim — likely a platform/setup issue, "
                "not the OD flake; not spending API calls (last lines):\n"
                + "\n".join(out.splitlines()[-15:]))
        if result == "test_pass":
            die("both tests passed — JUnit's fixed method order runs the victim first; "
                "this pair needs the Illinois Surefire fork (testorder) to reproduce")
        die(f"unexpected surefire result '{result}' — check the build (last lines):\n"
            + "\n".join(out.splitlines()[-15:]))

    for order in ("alphabetical", "reversealphabetical"):
        log(f"trying SUREFIRE_RUN_ORDER={order} ...")
        out = run_surefire(container_dir, project_dir, module,
                           row["polluter"], row["victim"], jdk, order)
        first = first_running_class(out)
        result = od_test_result(out)
        log(f"  first class: {first}; result: {result}")
        if first != p_class:
            continue  # wrong order — try the other direction
        if result == "test_failure" and victim_failed(out, row["victim"]):
            log(f"FLAKE REPRODUCED with {order} (polluter first, victim fails)")
            return order
        if result == "test_failure":
            die("a test failed, but not the victim — likely a platform/setup issue, "
                "not the OD flake; not spending API calls (last lines):\n"
                + "\n".join(out.splitlines()[-15:]))
        if result == "test_pass":
            die("polluter ran first but the victim PASSED — flake does not reproduce "
                "in this environment; not spending API calls. (Try Linux/the original "
                "AgentFlake container, or another row.)")
        die(f"unexpected surefire result '{result}' — check the build (last lines):\n"
            + "\n".join(out.splitlines()[-15:]))
    die(f"neither run order put polluter class {p_class} first — same-package "
        "ordering edge case; this pair needs the Illinois Surefire fork")


def reproduce_with_testorder(container_dir, project_dir, module, row, jdk):
    """With the Illinois `testorder` Surefire installed (the testorder
    image), `-Dtest=polluter,victim -Dsurefire.runOrder=testorder` runs the two
    tests in exactly that order — *including methods within one class*. So a
    single run reproduces both cross-class and same-class OD flakes, with no
    alphabetical-ordering gamble. Gated on the VICTIM failing so we never spend
    API calls on a polluter/platform failure."""
    log("reproducing with SUREFIRE_RUN_ORDER=testorder (Illinois fork — exact polluter->victim order)")
    out = run_surefire(container_dir, project_dir, module,
                       row["polluter"], row["victim"], jdk, "testorder")
    result = od_test_result(out)
    log(f"  result: {result}")
    if result == "test_failure" and victim_failed(out, row["victim"]):
        log("FLAKE REPRODUCED under testorder (polluter first, victim fails)")
        return "testorder"
    if result == "test_failure":
        die("a test failed, but not the victim — likely a platform/setup issue, "
            "not the OD flake; not spending API calls (last lines):\n"
            + "\n".join(out.splitlines()[-15:]))
    if result == "test_pass":
        die("both tests passed under testorder — the polluter does not pollute the "
            "victim in this environment; not spending API calls (last lines):\n"
            + "\n".join(out.splitlines()[-15:]))
    die(f"unexpected surefire result '{result}' — check the build (last lines):\n"
        + "\n".join(out.splitlines()[-15:]))


# ------------------------------------------------------------------- repair

# claude-sonnet-4-6 list price, USD per 1M tokens (FlakyDoctor's model).
CLAUDE_PRICE_IN, CLAUDE_PRICE_OUT = 3.0, 15.0


def run_flakydoctor_cmd(cmd, env, out_dir):
    """Run flakydoctor.py streaming its output live AND capturing it, then write
    per-run metrics to <out_dir>/logs.log. FlakyDoctor prints the Anthropic
    Usage(...) for each round but never persists it; we parse the captured stdout
    so FlakyDoctor itself stays unmodified."""
    t0 = time.time()
    captured = []
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            errors="replace", bufsize=1)
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        captured.append(line)
    proc.wait()
    _write_run_metrics("".join(captured), time.time() - t0, out_dir)


FIXED_TOOL_FAIL = "Failed due to tool failure"


def _run_fixed(out_dir):
    """Classify a run from FlakyDoctor's results.json:
      'YES' - some round reached test_pass (flake repaired)
      'NO'  - the repair loop ran but never passed (genuine miss)
      'Failed due to tool failure' - FlakyDoctor hit an internal error (non-empty
            Exceptions, e.g. method_code_location_failure) so it produced no
            usable result, or results.json is missing/empty.
    """
    rj = os.path.join(out_dir, "results.json")
    try:
        raw = open(rj).read().strip()
        if not raw:
            return FIXED_TOOL_FAIL
        dec, idx, tool_failed = json.JSONDecoder(), 0, False
        while idx < len(raw):
            obj, end = dec.raw_decode(raw, idx)
            idx = end
            while idx < len(raw) and raw[idx] in " \n\t,":
                idx += 1
            if any(v == "test_pass" for v in (obj.get("test_results") or {}).values()):
                return "YES"
            if obj.get("Exceptions"):
                tool_failed = True
        return FIXED_TOOL_FAIL if tool_failed else "NO"
    except Exception:
        return FIXED_TOOL_FAIL


def _write_run_metrics(output, elapsed, out_dir):
    # Each Claude round prints one Usage(...). In the Anthropic Usage repr,
    # input_tokens is rendered immediately before output_tokens, so match the
    # PAIR together: this ignores cache_*_input_tokens / output_tokens_details
    # and any stray "output_tokens=" that could appear inside a response body.
    pairs = re.findall(r"(?<![_A-Za-z])input_tokens=(\d+), output_tokens=(\d+)", output)
    in_toks = sum(int(a) for a, _ in pairs)
    out_toks = sum(int(b) for _, b in pairs)
    turns = len(pairs)
    cost = in_toks / 1e6 * CLAUDE_PRICE_IN + out_toks / 1e6 * CLAUDE_PRICE_OUT
    fixed = _run_fixed(out_dir)
    path = os.path.join(out_dir, "logs.log")
    with open(path, "w") as f:
        f.write("input_tokens,output_tokens,total_tokens,time_s,cost_usd,turns_used,fixed\n")
        f.write(f"{in_toks},{out_toks},{in_toks + out_toks},{elapsed:.2f},{cost:.6f},{turns},{fixed}\n")
    log(f"metrics -> {path}  (in={in_toks} out={out_toks} total={in_toks + out_toks} "
        f"time={elapsed:.1f}s cost=${cost:.4f} turns={turns} fixed={fixed})")


def run_flakydoctor(container_dir, row, github_url, project_name, projects_dir,
                    api_key, model, run_order, jdk):
    zip_base = os.path.basename(container_dir)
    _, victim_dotted = split_test(row["victim"])
    _, polluter_dotted = split_test(row["polluter"])
    url = github_url or f"https://github.com/agentflake/{project_name}"
    if url.rstrip("/").split("/")[-1] != project_name:
        die(f"staged dir name '{project_name}' must equal the URL's last segment ({url})")

    out_dir = os.path.join("outputs", f"af_fd_{zip_base}_{time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(out_dir, exist_ok=True)
    input_csv = os.path.join(out_dir, "input.csv")
    with open(input_csv, "w") as f:
        f.write(f"{url},{zip_base},{row['module']},{victim_dotted},{polluter_dotted}\n")

    env = maven_env(container_dir, jdk)
    env["SUREFIRE_RUN_ORDER"] = run_order
    cmd = ["python3", "-u", "src/flakydoctor.py",
           "--input-tests-csv", input_csv,
           "--flakiness-type", "OD",
           "--projects", projects_dir,
           "--api-key", api_key,
           "--model", model,
           "--output-dir", out_dir,
           "--output-result-csv", os.path.join(out_dir, "results.csv"),
           "--output-result-json", os.path.join(out_dir, "results.json"),
           "--output-details-json", os.path.join(out_dir, "details.json")]
    log(f"running FlakyDoctor ({model}); live output follows, artifacts in {out_dir}/")
    clean_m2_markers(container_dir)  # clear cached remote-failure markers before verify
    run_flakydoctor_cmd(cmd, env, out_dir)
    return out_dir


def summarize(out_dir, container_dir):
    details = os.path.join(out_dir, "details.json")
    if not os.path.exists(details):
        log("no details.json produced — check the run output above")
        return
    raw = open(details).read()
    dec, idx = json.JSONDecoder(), 0
    while idx < len(raw):
        obj, end = dec.raw_decode(raw, idx)
        idx = end
        while idx < len(raw) and raw[idx] in " \n\t":
            idx += 1
        results = obj.get("test_results", {})
        fixed = any(v == "test_pass" for v in results.values())
        print()
        log(f"victim   : {obj.get('victim')}")
        log(f"rounds   : {results}")
        log(f"fixed    : {'YES' if fixed else 'NO'}")
        log(f"patch    : {obj.get('patch_file')}")
        if obj.get("Exceptions"):
            log(f"exceptions: {obj['Exceptions']}")
    log(f"conversation transcript: {details} (fields 'prompts'/'responses' per round)")
    dev_fix = os.path.join(container_dir, "Fixed.patch")
    if os.path.exists(dev_fix):
        log(f"developer's reference fix for comparison: {dev_fix}")


def generate_semantic_diff(out_dir):
    """Write out_dir/semantic_diff.diff (every LLM round, passing + failing) via
    runner/patch_to_diff.py. Best-effort: skipped when the run wrote no details.json
    (e.g. FlakyDoctor errored early), and a failure here never fails the run."""
    if not os.path.exists(os.path.join(out_dir, "details.json")):
        return  # nothing to diff
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "runner", "patch_to_diff.py")
    if not os.path.exists(script):
        return
    try:
        subprocess.run([sys.executable, script, out_dir], check=False)
    except Exception as e:
        log(f"semantic_diff.diff generation skipped: {e}")


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test-config", default="test_config.csv",
                    help="path to test_config.csv (default: test_config.csv in the FlakyDoctor root)")
    ap.add_argument("--container", help="result_container name (col 2) of the od row to run")
    ap.add_argument("--list", action="store_true",
                    help="list od rows and whether they are runnable on stock Surefire")
    ap.add_argument("--api-key", help="Anthropic API key (required unless --skip-repair)")
    ap.add_argument("--model", default="Claude", help="FlakyDoctor model (default: Claude)")
    ap.add_argument("--projects", default="projects", help="FlakyDoctor projects dir")
    ap.add_argument("--skip-repair", action="store_true",
                    help="stop after reproducing the flake (zero API cost)")
    ap.add_argument("--testorder", action="store_true",
                    help="use the Illinois `testorder` Surefire (requires the testorder "
                         "image / extension) to force exact polluter->victim order; makes "
                         "same-class pairs deterministic. See docker/run_in_container.sh.")
    ap.add_argument("--keep-zip", action="store_true", help="keep the downloaded zip in /tmp")
    ap.add_argument("--fresh", action="store_true",
                    help="remove any existing staged container and re-download + rebuild "
                         "from pristine source (use when re-running an already-repaired container)")
    args = ap.parse_args()

    if not os.path.exists("src/flakydoctor.py"):
        die("run this from the FlakyDoctor root (src/flakydoctor.py not found)")

    rows = load_od_rows(args.test_config)
    if not rows:
        die(f"no od rows found in {args.test_config}")

    if args.list:
        print(f"{'container':55} {'orderable':10} polluter -> victim")
        for r in rows:
            ok = "yes" if row_runnable(r) else "same-class"
            p_class, _ = split_test(r["polluter"])
            v_class, _ = split_test(r["victim"])
            print(f"{r['container']:55} {ok:9} {p_class.split('.')[-1]} -> {v_class.split('.')[-1]}")
        return

    if not args.container:
        die("--container is required (use --list to see options)")
    matches = [r for r in rows if r["container"] == args.container]
    if not matches:
        die(f"no od row with result_container == {args.container}")
    row = matches[0]

    if not row_runnable(row) and not args.testorder:
        log("same-class pair — will rely on JUnit's deterministic in-class method "
            "order (works only if it happens to run the polluter first). For a "
            "deterministic same-class run, use --testorder inside the testorder image.")
    if not args.skip_repair and not args.api_key:
        die("--api-key is required (or pass --skip-repair to stop after reproduction)")

    container_dir, project_dir, project_name, github_url = \
        stage_container(row, args.projects, keep_zip=args.keep_zip, fresh=args.fresh)
    ensure_git_baseline(project_dir)
    jdk = build_project(container_dir, project_dir, row["module"], row["java"])
    if args.testorder:
        run_order = reproduce_with_testorder(container_dir, project_dir, row["module"], row, jdk)
    else:
        run_order = detect_order_and_reproduce(container_dir, project_dir, row["module"], row, jdk)

    if args.skip_repair:
        log("--skip-repair: stopping after successful reproduction. To repair, rerun "
            f"without --skip-repair (detected SUREFIRE_RUN_ORDER={run_order}).")
        return

    out_dir = run_flakydoctor(container_dir, row, github_url, project_name,
                              args.projects, args.api_key, args.model, run_order, jdk)
    summarize(out_dir, container_dir)
    generate_semantic_diff(out_dir)
    remove_flaky_m2(container_dir)


if __name__ == "__main__":
    main()
