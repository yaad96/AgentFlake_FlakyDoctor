# FlakyDoctor — AgentFlake Version

FlakyDoctor repairs Implementation-Dependent (ID), Order-Dependent (OD), and
Non-Idempotent-Outcome (NIO) flaky Java tests with a neuro-symbolic loop. This
version adds a **Claude** (Anthropic) backend and a containerized runner that
reproduces a flake inside Docker and repairs it with the original FlakyDoctor
pipeline.


## Requirements

- Docker installed and running (all builds and tests happen inside the container).
- An Anthropic API key — only for repair; `--reproduce-only` needs none.
- Works on **Linux and macOS**. The host only needs `bash`, `python3`, and
  `docker`; the JDK/Maven toolchain lives in the image.

## setup

From the repo root, create a file ".anthropic_api_key" and store your api key there. During the run, the key needed will be accessed from there. It is git-ignored, so its safe. 

## Run a container (ID, OD, or NIO)

The runner auto-detects the test type from `test_config.csv`, so the same command
handles ID, OD, and NIO — just pass the `result_container` name:

```bash
cd FlakyDoctor
python3 runner/run_claude.py <container> --runs 1 --models claude
```

List the runnable containers:

```bash
python3 src/run_af_fd.py --list        # OD rows
python3 src/run_af_fd_id.py --list     # ID rows
python3 src/run_af_fd_nio.py --list    # NIO rows
```

### Examples

```bash
cd FlakyDoctor
# OD
python3 runner/run_claude.py ormlitecore59309e5 --runs 1 --models claude

# ID
cd FlakyDoctor
python3 runner/run_claude.py apollojavaapolloopenapi5344bc4testFindItemsByNamespace --runs 1 --models claude

# NIO
python3 runner/run_claude.py quickcheckc1c1 --runs 1 --models claude



## Options

| Option / env | Purpose |
|---|---|
| `--runs N` | Independent repair runs for pass@k. |
| `--models claude,opus,haiku` | One or more Claude models (aliases in `runner/config.py`). |


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
  flakydoctor_output/     # FlakyDoctor results.csv / results.json / patches
    semantic_diff.diff    # the LLM's change per round (passing + failing), clean diff
  .run_complete
```

Verdict is `PASSED` (a round reached `test_pass`), `FAILED`, or `INCOMPLETE`. Summaries are written to
`FlakyDoctor/data/<container>/summary.csv` and
`FlakyDoctor/data/Complete_Containers_Summary.csv`.
