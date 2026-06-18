"""
Мультипровайдерная генерация: OpenAI (GPT), Anthropic (Claude), Google (Gemini).
Единый интерфейс generate_scenario(model_key, system_prompt, user_prompt).
Ключи и модели берутся из config (env). Если ключ провайдера не задан —
провайдер считается недоступным.
"""

import logging

from openai import AsyncOpenAI

from config import (
    OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY,
    OPENAI_MODEL, ANTHROPIC_MODEL, GEMINI_MODEL,
)

logger = logging.getLogger(__name__)

# Метаданные моделей для UI. Порядок = порядок кнопок.
MODELS = {
    "gpt": {
        "name": "🤖 ChatGPT",
        "provider": "openai",
        "model": OPENAI_MODEL,
    },
    "claude": {
        "name": "🧠 Claude Opus 4.8",
        "provider": "anthropic",
        "model": ANTHROPIC_MODEL,
    },
    "gemini": {
        "name": "✨ Gemini 3 Pro",
        "provider": "google",
        "model": GEMINI_MODEL,
    },
}

DEFAULT_MODEL = "gpt"

# Ленивая инициализация клиентов
_openai_client: AsyncOpenAI | None = None
_anthropic_client = None
_gemini_client = None


def _get_openai() -> AsyncOpenAI | None:
    global _openai_client
    if not OPENAI_API_KEY:
        return None
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


def _get_anthropic():
    global _anthropic_client
    if not ANTHROPIC_API_KEY:
        return None
    if _anthropic_client is None:
        from anthropic import AsyncAnthropic
        _anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client


def _get_gemini():
    global _gemini_client
    if not GEMINI_API_KEY:
        return None
    if _gemini_client is None:
        from google import genai
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


def is_model_available(model_key: str) -> bool:
    """Проверяет, задан ли ключ для провайдера этой модели."""
    meta = MODELS.get(model_key)
    if not meta:
        return False
    provider = meta["provider"]
    if provider == "openai":
        return bool(OPENAI_API_KEY)
    if provider == "anthropic":
        return bool(ANTHROPIC_API_KEY)
    if provider == "google":
        return bool(GEMINI_API_KEY)
    return False


def available_models() -> dict:
    """Возвращает только те модели, для которых есть API-ключ."""
    return {k: v for k, v in MODELS.items() if is_model_available(k)}


def model_name(model_key: str) -> str:
    """Человеко-читаемое имя модели для UI."""
    return MODELS.get(model_key, MODELS[DEFAULT_MODEL])["name"]


def normalize_model(model_key: str | None) -> str:
    """Возвращает валидный доступный ключ модели или дефолтный/первый доступный."""
    if model_key and is_model_available(model_key):
        return model_key
    if is_model_available(DEFAULT_MODEL):
        return DEFAULT_MODEL
    avail = available_models()
    if avail:
        return next(iter(avail))
    return DEFAULT_MODEL


async def _generate_openai(system_prompt: str, user_prompt: str) -> str:
    client = _get_openai()
    if client is None:
        raise RuntimeError("OpenAI API key not configured")
    response = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_completion_tokens=4000,
        temperature=0.8,
    )
    return response.choices[0].message.content or ""


async def _generate_anthropic(system_prompt: str, user_prompt: str) -> str:
    client = _get_anthropic()
    if client is None:
        raise RuntimeError("Anthropic API key not configured")
    response = await client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4000,
        temperature=0.8,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    return "".join(parts)


async def _generate_gemini(system_prompt: str, user_prompt: str) -> str:
    client = _get_gemini()
    if client is None:
        raise RuntimeError("Gemini API key not configured")
    from google.genai import types
    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.8,
            max_output_tokens=4000,
        ),
    )
    return response.text or ""


async def generate_scenario(model_key: str, system_prompt: str, user_prompt: str) -> str:
    """Генерирует текст выбранной нейронкой. При ошибке/недоступности модели
    откатывается на первую доступную (обычно GPT)."""
    model_key = normalize_model(model_key)
    provider = MODELS[model_key]["provider"]

    try:
        if provider == "openai":
            return await _generate_openai(system_prompt, user_prompt)
        if provider == "anthropic":
            return await _generate_anthropic(system_prompt, user_prompt)
        if provider == "google":
            return await _generate_gemini(system_prompt, user_prompt)
    except Exception as e:
        logger.error(f"Generation failed with {model_key} ({provider}): {e}")
        # Фоллбэк на OpenAI, если это была не она и она доступна
        if provider != "openai" and OPENAI_API_KEY:
            logger.info("Falling back to OpenAI")
            try:
                return await _generate_openai(system_prompt, user_prompt)
            except Exception as e2:
                logger.error(f"OpenAI fallback also failed: {e2}")
        raise

    raise RuntimeError(f"Unknown provider for model {model_key}")
