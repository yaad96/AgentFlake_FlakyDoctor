# FlakyDoctor (AgentFlake Version: with NIO and TD)

FlakyDoctor repairs ID, OD, NIO and TD flaky
Java tests with a neuro-symbolic loop. This version adds a Claude (Anthropic)
backend and a containerized runner that reproduces a flake inside Docker, repairs
it with the original FlakyDoctor pipeline, and archives the full run under
`FlakyDoctor/data/<test>/run_<NN>/`.

## Requirements

- Docker installed and running (all builds and tests happen inside the container).
- An Anthropic API key.
- Linux and macOS are supported. The host needs `bash`, `python3`, and `docker`;
  the JDK/Maven toolchain lives in the image.

## Setup

From the repo root, create a file `.anthropic_api_key` and store your API key
there. The key is read from that file during a run. The file is git-ignored, so
it is safe.

## Basic Run

The runner auto-detects the test type from `test_config.csv`, so the same command
handles all the mentioned flakiness types. Pass the test name from the `result_container` column:

```bash
cd FlakyDoctor
python3 runner/run_claude.py <test> --runs 1 --models claude
```

## Model Aliases

Aliases are defined in `runner/config.py`.

| Alias | Model |
|---|---|
| `claude`, `sonnet` | `claude-sonnet-4-6` |
| `opus` | `claude-opus-4-7` |
| `haiku` | `claude-haiku-4-5-20251001` |

## Examples

### ID

```bash
cd FlakyDoctor
python3 runner/run_claude.py \
  apollojavaapolloopenapi5344bc4testFindItemsByNamespace \
  --runs 1 --models claude
```

Run data for this test is in
`FlakyDoctor_Data.zip/ID/apollojavaapolloopenapi5344bc4testFindItemsByNamespace`.

### OD

```bash
cd FlakyDoctor
python3 runner/run_claude.py \
  ormlitecore59309e5 \
  --runs 1 --models claude
```

Run data for this test is in `FlakyDoctor_Data.zip/OD/ormlitecore59309e5`.

### NIO

```bash
cd FlakyDoctor
python3 runner/run_claude.py \
  quickcheckc1c1 \
  --runs 1 --models claude
```

Run data for this test is in `FlakyDoctor_Data.zip/NIO/quickcheckc1c1`.

### TD

```bash
cd FlakyDoctor
python3 runner/run_claude.py \
  BOOKKEEPER-846 \
  --runs 1 --models claude
```

Run data for this test is in `FlakyDoctor_Data.zip/TD/BOOKKEEPER-846`.



## Options

| Option / env var | Purpose |
|---|---|
| `--runs N` | Independent runs for pass@k, which counts a test as repaired if at least one of the N independently sampled runs yields a verified fix. |
| `--models claude,opus,haiku` | One or more Claude models. |
| `--reproduce-only` | Reproduce the flake without repairing it. No API key needed. |

## Output

Each run is archived under:

```text
FlakyDoctor/data/<test>/run_<NN>/
  flakydoctor_output/     # FlakyDoctor results.csv / results.json / patches
    semantic_diff.diff    # the LLM's change per round (passing + failing), clean diff
  meta.json               # verdict, model, timing
  pipeline.log            # full container stdout
  .run_complete
```

The verdict in `meta.json` is `PASSED` or `FAILED`.

Summaries are written to:

```text
FlakyDoctor/data/<test>/summary.csv
FlakyDoctor/data/Complete_Containers_Summary.csv
```

All run data is available in `FlakyDoctor_Data.zip`, covering 41 OD tests and 41
ID tests.
