# FlakyDoctor — Claude container runner

FlakyDoctor repairs Implementation-Dependent (ID) and Order-Dependent (OD) flaky
Java tests with a neuro-symbolic loop. This fork adds a **Claude** (Anthropic)
backend and a containerized runner that reproduces a flake inside Docker and
repairs it with the original FlakyDoctor pipeline.

Upstream: <https://github.com/Intelligent-CAT-Lab/FlakyDoctor>

## Requirements

- Docker installed and running (all builds and tests happen inside the container).
- An Anthropic API key — only for repair; `--reproduce-only` needs none.
- Works on **Linux and macOS**. The host only needs `bash`, `python3`, and
  `docker`; the JDK/Maven toolchain lives in the image.

## One-command setup

From the repo root:

```bash
ANTHROPIC_API_KEY=sk-ant-... bash FlakyDoctor/setup.sh --build-images
```

This stores the key in `FlakyDoctor/.anthropic_api_key` (git-ignored), checks
Docker, and prebuilds the OD/ID images. Omit `--build-images` to build each image
on first use. The `ANTHROPIC_API_KEY` environment variable always overrides the
key file.

## Run a container (ID or OD)

The runner auto-detects the test type from `test_config.csv`, so the same command
handles both ID and OD — just pass the `result_container` name:

```bash
cd FlakyDoctor
python3 agentic/run_agentic.py <container> --runs 1 --models claude --max-iterations 5
```

List the runnable containers:

```bash
python3 src/run_af_fd.py --list        # OD rows
python3 src/run_af_fd_id.py --list     # ID rows
```

### Examples

```bash
# OD
python3 agentic/run_agentic.py ormlitecore59309e5 --runs 1 --models claude --max-iterations 5

# ID
python3 agentic/run_agentic.py apollojavaapolloopenapi5344bc4testFindItemsByNamespace --runs 1 --models claude --max-iterations 5

# Reproduce only (no key, no API cost)
python3 agentic/run_agentic.py ormlitecore59309e5 --reproduce-only
```

### Lower-level entry point

`run_agentic.py` calls `docker/run_in_container.sh`, which you can also run directly:

```bash
docker/run_in_container.sh <container>                 # reproduce + repair with Claude
docker/run_in_container.sh <container> --skip-repair   # reproduce only, no key needed
```

## Options

| Option / env | Purpose |
|---|---|
| `--runs N` | Independent repair runs for pass@k. |
| `--models claude,opus,haiku` | One or more Claude models (aliases in `agentic/agentic_config.py`). |
| `--max-iterations N` | Repair-round budget per run (FlakyDoctor default 5). |
| `--reproduce-only` | Reproduce the flake and stop — no key, no API cost. |
| `FD_CLAUDE_MODEL` | Claude model id used by the repair loop (default `claude-sonnet-4-6`). |
| `FD_MAX_ROUNDS` | Repair-round cap (default 5). |
| `TEST_CONFIG=/path` | Use a different `test_config.csv`. |
| `API_KEY_FILE=/path` | Use a different key file. |

Model aliases:

| Alias | Model |
|---|---|
| `claude`, `sonnet` | `claude-sonnet-4-6` |
| `opus` | `claude-opus-4-7` |
| `haiku` | `claude-haiku-4-5-20251001` |

## Output

Each run is archived under:

```text
FlakyDoctor/data/<container>/run_<NN>/
  pipeline.log            # full container stdout
  meta.json               # verdict, model, timing
  flakydoctor_output/     # FlakyDoctor results.csv / results.json / patches (repair runs)
  .run_complete
```

Verdict is `PASSED` (a round reached `test_pass`), `FAILED`, `REPRODUCED`
(`--reproduce-only`), or `INCOMPLETE`. Summaries are written to
`FlakyDoctor/data/<container>/summary.csv` and
`FlakyDoctor/data/Complete_Containers_Summary.csv`.

## How it works

1. `run_agentic.py` reads `test_config.csv` and dispatches by test type (ID or OD).
2. `docker/run_in_container.sh` builds the matching JDK image and runs it as your
   host user with FlakyDoctor bind-mounted.
3. Inside the container, `src/run_af_fd.py` (OD) / `src/run_af_fd_id.py` (ID)
   reproduces the flake — OD under the Illinois `testorder` Surefire, ID under NonDex.
4. On a confirmed flake it calls `src/flakydoctor.py --model Claude`, the original
   FlakyDoctor repair loop (prompt → Claude → patch → symbolic stitch → re-run).
5. The driver captures results and writes a verdict.

FlakyDoctor repairs **ID and OD only**. The upstream `GPT-4` / `MagiCoder` backends
still work via `src/flakydoctor.py`; see the upstream repository for that flow.
