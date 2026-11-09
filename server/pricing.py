"""Typical hosted-API pricing used for the 'what would this cost?' estimate.

Local inference itself is free. The estimate maps a local model to the closest
hosted equivalent so users can see what the same token volume would typically
cost on a paid API. Rates are approximate USD per 1M tokens.
"""
from __future__ import annotations

import re

# (pattern, label, input $/M, output $/M)
PRICING: list[tuple[str, str, float, float]] = [
    (r"deepseek.*v4|deepseek.*v3|deepseek.*r1", "DeepSeek V3/V4 class", 0.27, 1.10),
    (r"deepseek", "DeepSeek class", 0.27, 1.10),
    (r"kimi|moonshot", "Kimi K2 class", 0.60, 2.50),
    (r"glm|zhipu", "GLM class", 0.60, 2.20),
    (r"minimax", "MiniMax class", 0.30, 1.20),
    (r"qwen3.*coder|qwen.*coder", "Qwen Coder class", 0.30, 1.20),
    (r"qwen3\.8|qwen3-235|qwen3", "Qwen3 class", 0.35, 1.40),
    (r"qwen", "Qwen class", 0.25, 1.00),
    (r"claude|sonnet|opus", "Claude Sonnet class", 3.00, 15.00),
    (r"gpt-4|gpt-5|o3|o4", "OpenAI frontier class", 1.25, 10.00),
    (r"gpt-4o|gpt-4\.1|gpt-5-mini", "OpenAI mid class", 0.40, 1.60),
    (r"gemini", "Gemini class", 0.30, 2.50),
    (r"codestral|mistral", "Mistral class", 0.30, 0.90),
    (r"gemma", "Gemma class", 0.20, 0.60),
    (r"llama|dolphin|lexi|heretic", "Llama class", 0.20, 0.70),
    (r"phi", "Phi class", 0.13, 0.52),
    (r"tiny|smol|0\.5b|1\.5b|1b|2b|3b|4b", "Small-model class", 0.10, 0.30),
]

DEFAULT = ("Generic hosted model", 0.50, 1.50)


def price_for(model_name: str | None) -> dict:
    name = (model_name or "").lower()
    for pattern, label, inp, out in PRICING:
        if re.search(pattern, name):
            return {
                "label": label,
                "input_per_million": inp,
                "output_per_million": out,
                "source": "typical hosted API list price (estimate)",
            }
    label, inp, out = DEFAULT
    return {
        "label": label,
        "input_per_million": inp,
        "output_per_million": out,
        "source": "typical hosted API list price (estimate)",
    }


def estimate(prompt_tokens: int, completion_tokens: int, model_name: str | None) -> dict:
    price = price_for(model_name)
    p = max(0, int(prompt_tokens or 0))
    c = max(0, int(completion_tokens or 0))
    cost = (p / 1_000_000) * price["input_per_million"] + (
        c / 1_000_000
    ) * price["output_per_million"]
    return {
        **price,
        "prompt_tokens": p,
        "completion_tokens": c,
        "cost_usd": round(cost, 8),
        "local_cost_usd": 0.0,
    }


def format_usd(value: float | None) -> str:
    value = float(value or 0)
    if value == 0:
        return "$0.00"
    if value < 0.0001:
        return f"${value:.6f}"
    if value < 0.01:
        return f"${value:.4f}"
    if value < 1:
        return f"${value:.3f}"
    return f"${value:.2f}"
