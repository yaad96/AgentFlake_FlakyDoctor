# FlakyDoctor — AgentFlake Version

FlakyDoctor repairs Implementation-Dependent (ID), Order-Dependent (OD),
Non-Idempotent-Outcome (NIO), and Test/Timing-Dependent (TD) flaky Java tests with a
neuro-symbolic loop. This version adds a **Claude** (Anthropic) backend and a
containerized runner that reproduces a flake inside Docker and repairs it with
the original FlakyDoctor pipeline.


## Requirements

- Docker installed and running (all builds and tests happen inside the container).
- An Anthropic API key — only for repair; `--reproduce-only` needs none.
- Works on **Linux and macOS**. The host only needs `bash`, `python3`, and
  `docker`; the JDK/Maven toolchain lives in the image.

## Setup

One command, from the repo root:

```bash
ANTHROPIC_API_KEY=sk-ant-... bash FlakyDoctor/setup.sh --build-images
```

This checks Docker, stores the key in `FlakyDoctor/.anthropic_api_key` (git-ignored),
and prebuilds the OD/ID/NIO/TD images. Omit `--build-images` to build each image on
first use; add `--force-rebuild-images` to rebuild existing ones.

To place the key by hand instead, write it to **`FlakyDoctor/.anthropic_api_key`** —
not the repo root. The `ANTHROPIC_API_KEY` environment variable always overrides the
key file.

## Run a container (ID, OD, NIO, or TD)

The runner reads the `test_type` column of `test_config.csv` and dispatches to the
right repair pipeline, so the same command handles all four types — just pass the
`result_container` name. Run everything below from the `FlakyDoctor/` directory:

```bash
cd FlakyDoctor
python3 runner/run_claude.py <container> --runs 1 --models claude
```

List the runnable containers:

```bash
python3 src/run_af_fd.py --list        # OD rows  (125)
python3 src/run_af_fd_id.py --list     # ID rows  (819)
python3 src/run_af_fd_nio.py --list    # NIO rows (125)
python3 src/run_af_fd_td.py --list     # TD rows  (36)
```

### Examples

```bash
# OD
python3 runner/run_claude.py ormlitecore59309e5 --runs 1 --models claude

# ID
python3 runner/run_claude.py apollojavaapolloopenapi5344bc4testFindItemsByNamespace --runs 1 --models claude

# NIO
python3 runner/run_claude.py quickcheckc1c1 --runs 1 --models claude

# TD
python3 runner/run_claude.py BOOKKEEPER-846 --runs 1 --models claude
```

### How the types differ

All four share the same staging, build, and repair machinery; they differ only in how
the flake is deterministically reproduced before any API tokens are spent. Every driver
gates on that reproduction failing first.

| Type | `polluter/state setter` | Reproduction oracle |
|---|---|---|
| OD | always set (all 125 rows) | runs the polluter/state-setter before the victim, under a testorder fork |
| ID | empty | `mvn nondex -DnondexSeed=<seed>` from the CSV; gated on ≥1 NonDex iteration failing |
| NIO | empty | a generated JUnit wrapper invokes the victim **twice in one JVM**; the 2nd invocation must fail |
| TD | empty | the staged `FlakyCodeChange.patch` timing forcing is applied on top of the tree; the plain victim must then fail |

NIO and TD need no polluter and no NonDex seed. A NIO test passes the first time but
fails on re-invocation in the same JVM because it leaves shared state behind; a TD test
passes alone but breaks once an injected timing perturbation invalidates its
concurrency/timing assumption. In both cases the victim test method is the only repair
target.

## Options

| Option / env | Purpose |
|---|---|
| `--runs N` | Independent repair runs for pass@k. |
| `--models claude,opus,haiku` | One or more Claude models (aliases in `runner/config.py`). |
| `--reproduce-only` | Reproduce the flake and stop; no repair, so no API key needed. |

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
