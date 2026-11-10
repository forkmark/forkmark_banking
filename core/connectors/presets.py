"""Model connection presets for sovereign and commercial model families."""
from __future__ import annotations

from typing import Any, Dict

PRESETS: Dict[str, Dict[str, Any]] = {
    "jais": {
        "name": "Jais 2 8B Chat (Inception / Core42)",
        "model_family": "jais",
        "deployment_kind": "openai_compatible",
        "default_model": "inceptionai/Jais-2-8B-Chat",
        "recommended_max_tokens": 1024,
        "stop_tokens": ["<|endoftext|>", "###"],
        "default_temperature": 0.0,
        "smoke_prompt_en": "What are the primary AML requirements for retail banking transactions in the UAE under CBUAE rules?",
        "smoke_prompt_ar": "ما هي المتطلبات الأساسية لمكافحة غسل الأموال في المعاملات المصرفية للأفراد وفقاً لتعليمات مصرف الإمارات المركزي؟",
        "licence": "Gated / Inception AI Community License",
        "hosting": "bank_self_hosted",
        "vendor_name": "Inception AI (G42)",
    },
    "falcon": {
        "name": "Falcon-H1 Arabic 7B (TII)",
        "model_family": "falcon",
        "deployment_kind": "openai_compatible",
        "default_model": "tiiuae/Falcon-H1-Arabic-7B",
        "recommended_max_tokens": 1024,
        "stop_tokens": ["<|endoftext|>"],
        "default_temperature": 0.0,
        "smoke_prompt_en": "What documents are required to open a corporate bank account in the UAE?",
        "smoke_prompt_ar": "ما هي المستندات المطلوبة لفتح حساب مصرفي للشركات في دولة الإمارات؟",
        "licence": "Falcon LLM License",
        "hosting": "bank_self_hosted",
        "vendor_name": "Technology Innovation Institute (TII)",
    },
    "gpt": {
        "name": "OpenAI GPT-4o / GPT-4o-mini",
        "model_family": "gpt",
        "deployment_kind": "openai_compatible",
        "default_model": "gpt-4o-mini",
        "recommended_max_tokens": 1024,
        "stop_tokens": [],
        "default_temperature": 0.0,
        "smoke_prompt_en": "Summarize Central Bank of the UAE regulatory principles on consumer data privacy.",
        "smoke_prompt_ar": "لخص مبادئ مصرف الإمارات المركزي التنظيمية بشأن حماية خصوصية بيانات العملاء.",
        "licence": "Proprietary Commercial",
        "hosting": "vendor_hosted",
        "vendor_name": "OpenAI",
    },
    "azure_openai": {
        "name": "Azure OpenAI UAE North",
        "model_family": "gpt",
        "deployment_kind": "azure_openai",
        "default_model": "gpt-4o",
        "recommended_max_tokens": 1024,
        "stop_tokens": [],
        "default_temperature": 0.0,
        "smoke_prompt_en": "Summarize Central Bank of the UAE regulatory guidelines on model risk management.",
        "smoke_prompt_ar": "لخص إرشادات مصرف الإمارات المركزي التنظيمية لإدارة مخاطر النماذج المصرفية.",
        "licence": "Proprietary Commercial",
        "hosting": "vendor_hosted",
        "vendor_name": "Microsoft Azure",
    },
}


def get_preset(name: str) -> Dict[str, Any]:
    """Retrieve configuration preset by family/preset key."""
    key = name.lower().strip()
    if key in PRESETS:
        return dict(PRESETS[key])
    raise KeyError(f"Unknown connector preset: '{name}'. Available: {list(PRESETS.keys())}")


def list_presets() -> Dict[str, Dict[str, Any]]:
    """Return all available model presets."""
    return {k: dict(v) for k, v in PRESETS.items()}

