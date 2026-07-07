"""Model aliases and defaults for FlakyDoctor's Claude runner (run_claude.py).

FlakyDoctor's Claude path calls the Anthropic API directly, so only Claude models
are supported here.
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
