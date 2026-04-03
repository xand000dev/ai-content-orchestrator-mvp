import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

FREE_MODELS = [
    "qwen/qwen3.6-plus:free",
    "stepfun/step-3.5-flash:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
    "mistralai/mistral-7b-instruct:free",
]

# Затримки між ретраями при 429 (секунди)
_RETRY_DELAYS = [15, 30, 60]


async def call_openrouter(
    model: str,
    system_prompt: str,
    user_message: str,
    api_key: str | None = None,
) -> str:
    key = api_key or os.getenv("OPENROUTER_API_KEY", "")
    if not key:
        raise ValueError(
            "OPENROUTER_API_KEY не встановлено. "
            "Скопіюй .env.example → .env і заповни ключ."
        )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_message},
        ],
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "HTTP-Referer": "http://localhost:8000",
        "X-Title": "AI Content Orchestrator",
        "Content-Type": "application/json",
    }

    last_error: Exception | None = None

    for attempt, delay in enumerate([0] + _RETRY_DELAYS):
        if delay:
            logger.warning(
                "429 rate limit на %s — чекаю %ds (спроба %d/4)...",
                model, delay, attempt + 1,
            )
            await asyncio.sleep(delay)

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{OPENROUTER_BASE_URL}/chat/completions",
                    headers=headers,
                    json=payload,
                )

            if resp.status_code == 429:
                last_error = Exception(f"429 Too Many Requests (модель {model})")
                continue  # → наступний ретрай

            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                last_error = e
                continue
            raise

    raise last_error or Exception(f"OpenRouter: усі ретраї вичерпано для {model}")
