"""
LLM Cost Calculator
====================
Calculates cost for LLM API calls from token usage logs.

Pricing is defined per model in MODEL_PRICING. Each entry has:
  - input_per_1k  : cost in USD per 1,000 input (prompt) tokens
  - output_per_1k : cost in USD per 1,000 output (completion) tokens

For models with thinking/reasoning tokens (e.g. Gemini 2.5 series),
thoughts_tokens are billed as OUTPUT tokens by default. Set
`thoughts_as_input=True` in the pricing entry if a model bills them
as input tokens instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Model Pricing Registry  (USD per 1 000 tokens)
# ---------------------------------------------------------------------------

MODEL_PRICING: dict[str, dict] = {
    # ── Gemini 2.5 series (historical + still-used pro) ──────────────────
    "gemini-2.5-flash": {
        "input_per_1k": 0.00030,
        "output_per_1k": 0.00250,
    },
    "gemini-2.5-pro": {
        "input_per_1k": 0.00125,
        "output_per_1k": 0.01000,
    },
    "gemini-3.5-flash-lite": {
        "input_per_1k": 0.00030,
        "output_per_1k": 0.00250,
    },
    "gemini-3-flash-preview": {
        "input_per_1k": 0.00050,
        "output_per_1k": 0.00300,
    },
    "gemini-3.6-flash": {
        "input_per_1k": 0.00150,
        "output_per_1k": 0.00750,
    },
    # ── Gemini 3.x series (custom / preview models in your logs) ─────────
    "gemini-3.1-pro-preview": {
        "input_per_1k": 0.00200,
        "output_per_1k": 0.01200,
    },
    "gemini-3.5-flash": {
        "input_per_1k": 0.0015,
        "output_per_1k": 0.009,
    },
    # ── OpenAI GPT-4o family ─────────────────────────────────────────────
    "gpt-4o": {
        "input_per_1k": 0.00250,
        "output_per_1k": 0.01000,
    },
    "gpt-4o-mini": {
        "input_per_1k": 0.000150,
        "output_per_1k": 0.000600,
    },
    # ── OpenAI o-series (reasoning) ──────────────────────────────────────
    "o3": {
        "input_per_1k": 0.01000,
        "output_per_1k": 0.04000,
    },
    "o4-mini": {
        "input_per_1k": 0.00110,
        "output_per_1k": 0.00440,
    },
    # ── Anthropic Claude 3.x ─────────────────────────────────────────────
    "claude-3-5-sonnet": {
        "input_per_1k": 0.00300,
        "output_per_1k": 0.01500,
    },
    "claude-3-5-haiku": {
        "input_per_1k": 0.00080,
        "output_per_1k": 0.00400,
    },
    "claude-3-opus": {
        "input_per_1k": 0.01500,
        "output_per_1k": 0.07500,
    },
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    thoughts_tokens: int = 0        # reasoning / thinking tokens (if any)
    candidates_tokens: int = 0      # non-thinking portion of completion


@dataclass
class CostResult:
    model: str
    input_tokens: int
    output_tokens: int
    thoughts_tokens: int
    input_cost_usd: float
    output_cost_usd: float
    total_cost_usd: float
    pricing_source: str             # "registry" | "record_fallback" | "unknown"

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "thoughts_tokens": self.thoughts_tokens,
            "input_cost_usd": round(self.input_cost_usd, 6),
            "output_cost_usd": round(self.output_cost_usd, 6),
            "total_cost_usd": round(self.total_cost_usd, 6),
            "pricing_source": self.pricing_source,
        }


# ---------------------------------------------------------------------------
# Core calculation function
# ---------------------------------------------------------------------------

def calculate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    thoughts_tokens: int = 0,
    *,
    custom_pricing: Optional[dict] = None,
) -> CostResult:
    """
    Calculate the USD cost for a single LLM API call.

    Parameters
    ----------
    model : str
        Model identifier, e.g. ``"gemini-2.5-pro"``.
    prompt_tokens : int
        Number of input / prompt tokens.
    completion_tokens : int
        Total output tokens INCLUDING thoughts_tokens (as stored in the log).
    thoughts_tokens : int, optional
        Reasoning / thinking token count (subset of completion_tokens).
        Billed as output tokens by default.
    custom_pricing : dict, optional
        Override pricing for this call only::

            {"input_per_1k": 0.001, "output_per_1k": 0.005}

    Returns
    -------
    CostResult
    """
    # Resolve pricing  ── priority: custom > registry > fallback unknown
    pricing_source = "registry"
    pricing = custom_pricing

    if pricing is None:
        # Try exact match first, then prefix match (e.g. "gemini-2.5-pro-exp" → "gemini-2.5-pro")
        pricing = MODEL_PRICING.get(model)
        if pricing is None:
            for key in MODEL_PRICING:
                if model.startswith(key):
                    pricing = MODEL_PRICING[key]
                    break

    if pricing is None:
        pricing_source = "unknown"
        # Cannot compute cost; return zeros with flag
        return CostResult(
            model=model,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            thoughts_tokens=thoughts_tokens,
            input_cost_usd=0.0,
            output_cost_usd=0.0,
            total_cost_usd=0.0,
            pricing_source=pricing_source,
        )
    else:
        if custom_pricing:
            pricing_source = "custom"

    input_cost = (prompt_tokens / 1000) * pricing["input_per_1k"]
    output_cost = (completion_tokens / 1000) * pricing["output_per_1k"]
    total_cost = input_cost + output_cost

    return CostResult(
        model=model,
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        thoughts_tokens=thoughts_tokens,
        input_cost_usd=input_cost,
        output_cost_usd=output_cost,
        total_cost_usd=total_cost,
        pricing_source=pricing_source,
    )


# ---------------------------------------------------------------------------
# Batch processing helpers
# ---------------------------------------------------------------------------

def calculate_cost_from_record(record: dict) -> CostResult:
    """
    Calculate cost from a single token-log record dict.
    Falls back to the pricing rates stored IN the record if the model
    is not found in MODEL_PRICING (does NOT use the pre-computed costs).
    """
    model = record.get("model", "unknown")
    prompt_tokens = record.get("prompt_tokens", 0)
    completion_tokens = record.get("completion_tokens", 0)
    thoughts_tokens = record.get("thoughts_tokens", 0)

    # Check if model is known; if not, use rates from the record as fallback
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        for key in MODEL_PRICING:
            if model.startswith(key):
                pricing = MODEL_PRICING[key]
                break

    if pricing is None and "pricing" in record:
        stored = record["pricing"]
        rate_in = stored.get("rate_per_1k_input_tokens")
        rate_out = stored.get("rate_per_1k_output_tokens")
        if rate_in is not None and rate_out is not None:
            result = calculate_cost(
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                thoughts_tokens=thoughts_tokens,
                custom_pricing={"input_per_1k": rate_in, "output_per_1k": rate_out},
            )
            result.pricing_source = "record_fallback"
            return result

    return calculate_cost(
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        thoughts_tokens=thoughts_tokens,
    )


@dataclass
class BatchSummary:
    records_processed: int
    total_input_tokens: int
    total_output_tokens: int
    total_thoughts_tokens: int
    total_cost_usd: float
    cost_by_model: dict[str, float] = field(default_factory=dict)
    cost_by_operation: dict[str, float] = field(default_factory=dict)
    cost_by_sub_operation: dict[str, float] = field(default_factory=dict)
    unknown_models: list[str] = field(default_factory=list)
    record_details: list[dict] = field(default_factory=list)

    def print_summary(self) -> None:
        print("=" * 60)
        print("  LLM COST SUMMARY")
        print("=" * 60)
        print(f"  Records processed  : {self.records_processed}")
        print(f"  Total input tokens : {self.total_input_tokens:,}")
        print(f"  Total output tokens: {self.total_output_tokens:,}")
        print(f"  Total thought tok. : {self.total_thoughts_tokens:,}")
        print(f"  TOTAL COST (USD)   : ${self.total_cost_usd:.6f}")
        print()
        print("  Cost by model:")
        for model, cost in sorted(self.cost_by_model.items(), key=lambda x: -x[1]):
            print(f"    {model:<35} ${cost:.6f}")
        print()
        print("  Cost by operation:")
        for op, cost in sorted(self.cost_by_operation.items(), key=lambda x: -x[1]):
            print(f"    {op:<35} ${cost:.6f}")
        if self.unknown_models:
            print()
            print("  ⚠  Models with unknown pricing (cost = $0):")
            for m in set(self.unknown_models):
                print(f"    - {m}")
        print("=" * 60)


def calculate_cost_from_records(records: list[dict]) -> BatchSummary:
    """
    Process a list of token-log records and return a BatchSummary.
    """
    summary = BatchSummary(
        records_processed=len(records),
        total_input_tokens=0,
        total_output_tokens=0,
        total_thoughts_tokens=0,
        total_cost_usd=0.0,
    )

    for rec in records:
        result = calculate_cost_from_record(rec)

        summary.total_input_tokens += result.input_tokens
        summary.total_output_tokens += result.output_tokens
        summary.total_thoughts_tokens += result.thoughts_tokens
        summary.total_cost_usd += result.total_cost_usd

        # Breakdowns
        model_key = result.model
        summary.cost_by_model[model_key] = (
            summary.cost_by_model.get(model_key, 0.0) + result.total_cost_usd
        )

        operation = rec.get("operation", "unknown")
        summary.cost_by_operation[operation] = (
            summary.cost_by_operation.get(operation, 0.0) + result.total_cost_usd
        )

        sub_op = rec.get("sub_operation", "unknown")
        if sub_op:
            summary.cost_by_sub_operation[sub_op] = (
                summary.cost_by_sub_operation.get(sub_op, 0.0) + result.total_cost_usd
            )

        if result.pricing_source == "unknown":
            summary.unknown_models.append(result.model)

        summary.record_details.append({
            "record_id": rec.get("record_id"),
            "model": result.model,
            "operation": operation,
            "sub_operation": sub_op,
            **result.as_dict(),
        })

    return summary


def calculate_cost_from_file(path: str | Path) -> BatchSummary:
    """
    Load a JSON file (single record dict OR list of records) and return a BatchSummary.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = [data]
    return calculate_cost_from_records(data)


# ---------------------------------------------------------------------------
# CLI / quick-run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    # if len(sys.argv) < 2:
    #     print("Usage: python llm_cost_calculator.py <path_to_json>")
    #     sys.exit(1)
    # summary = calculate_cost_from_file(sys.argv[1])

    file_path = "/home/user/Projects/screen-builder/tokens_count_exampl1.json"
    summary = calculate_cost_from_file(file_path)
    summary.print_summary()
