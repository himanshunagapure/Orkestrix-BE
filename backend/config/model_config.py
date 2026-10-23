# Model and provider mapping configuration
# Maps task types to provider/model names

MODEL_MAPPINGS = {
    "ir_schema_generation": {
        # Not flash-lite: lite omitted required fields (dataSources.type) on live LinkedIn builds.
        "gemini": "gemini-3.5-flash",
        "claude": "claude-haiku-4",
        "openai": "gpt-4o-mini",
        "grok": "grok-2-mini"
    },
    "ir_datasource_enrichment": {
        "gemini": "gemini-3.1-pro-preview",  # dedicated pass: production-grade dataSource specs
        "claude": "claude-sonnet-4",
        "openai": "gpt-4o",
        "grok": "grok-2-latest"
    },
    "backend_generation": {
        "gemini": "gemini-3.6-flash",
        "claude": "claude-sonnet-4",
        "openai": "gpt-4o",
        "grok": "grok-2-latest"
    },
    # Multi-branch / LLM / external domain functions skip flash and start on pro.
    "backend_generation_complex": {
        "gemini": "gemini-3.1-pro-preview",
        "claude": "claude-sonnet-4",
        "openai": "gpt-4o",
        "grok": "grok-2-latest"
    },
    "angular_generation": {
        "gemini": "gemini-3-pro-preview",
        "claude": "claude-sonnet-4",
        "openai": "gpt-4o",
        "grok": "grok-2-latest"
    }
}

SUPPORTED_PROVIDERS = ["gemini", "claude", "openai", "grok"]

# Model-specific parameters (can be extended)
MODEL_PARAMS = {
    "gemini": {"temperature": 0.7, "max_tokens": 4096},
    "claude": {"temperature": 0.7, "max_tokens": 4096},
    "openai": {"temperature": 0.7, "max_tokens": 4096},
    "grok": {"temperature": 0.7, "max_tokens": 4096}
}
