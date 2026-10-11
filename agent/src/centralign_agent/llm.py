from __future__ import annotations

from .config import get_settings
from .model_adapter import MockModelAdapter, OpenAIModelAdapter


def get_model_adapter():
    settings = get_settings()
    provider = settings.llm_provider
    if provider == "mock":
        return MockModelAdapter()
    api_key = settings.llm_api_key
    if provider == "auto" and not api_key:
        return MockModelAdapter()
    if not api_key:
        raise ValueError("An API key is required for the selected LLM provider; set LLM_API_KEY locally.")

    base_url = settings.llm_base_url
    if provider == "groq" and not base_url:
        base_url = "https://api.groq.com/openai/v1"
    elif provider == "huggingface" and not base_url:
        base_url = "https://router.huggingface.co/v1"
    return OpenAIModelAdapter(settings.llm_model, api_key, base_url)
