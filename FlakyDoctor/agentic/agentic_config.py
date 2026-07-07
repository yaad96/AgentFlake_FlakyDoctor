"""Model aliases and defaults for FlakyDoctor's agentic-style runner (run_agentic.py).

FlakyDoctor's Claude path calls the Anthropic API directly (not the Claude Code CLI),
so only Claude models are supported here. The alias table mirrors AF_Claude_Agent's
so the same `--models` values work.
"""

# alias -> canonical Anthropic model id. The runner passes the resolved id to
# FlakyDoctor's repair loop via the FD_CLAUDE_MODEL environment variable.
CLAUDE_MODELS = {
    "claude": "claude-sonnet-4-6",
    "sonnet": "claude-sonnet-4-6",
    "claude-sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
    "claude-opus": "claude-opus-4-7",
    "haiku": "claude-haiku-4-5-20251001",
}

# Default repair-round cap (FlakyDoctor's native budget). Overridable via
# --max-iterations, which the runner forwards as FD_MAX_ROUNDS.
MAX_ITERATIONS = 5
