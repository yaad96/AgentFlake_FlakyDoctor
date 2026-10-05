"""Model aliases and defaults for FlakyDoctor's LLM runner (run_claude.py).

Claude models use Anthropic. GPT/OpenAI models use OpenAI.
"""

# alias -> canonical OpenAI model id. The runner passes the resolved id to
# FlakyDoctor's repair loop via the FD_OPENAI_MODEL environment variable.
OPENAI_MODELS = {
    "openai": "gpt-5.4",
    "gpt": "gpt-5.4",
    "gpt-5.4": "gpt-5.4",
}

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
