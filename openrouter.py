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

# Затримки між ретраями при помилках (секунди)
_RETRY_DELAYS = [15, 30, 60]

# Глобальний семафор: макс. одночасних запитів до OpenRouter
# Запобігає масовим 429 при паралельній роботі агентів
_semaphore = asyncio.Semaphore(3)


class OpenRouterError(Exception):
    """Помилка від OpenRouter API з деталями."""
    def __init__(self, message: str, model: str = "", status_code: int = 0, raw_body: str = ""):
        self.model = model
        self.status_code = status_code
        self.raw_body = raw_body
        super().__init__(message)


def _extract_content(data: dict, model: str) -> str:
    """Безпечно витягнути контент з відповіді OpenRouter."""
    # Перевіряємо наявність помилки в тілі відповіді
    if "error" in data:
        err = data["error"]
        if isinstance(err, dict):
            err_msg = err.get("message", str(err))
            err_code = err.get("code", "unknown")
        else:
            err_msg = str(err)
            err_code = "unknown"
        raise OpenRouterError(
            f"API error (code={err_code}): {err_msg}",
            model=model, raw_body=str(data)[:500],
        )

    # Перевіряємо наявність choices
    choices = data.get("choices")
    if not choices:
        raise OpenRouterError(
            f"Відповідь без 'choices'. Модель '{model}' можливо перевантажена або повернула порожню відповідь.",
            model=model, raw_body=str(data)[:500],
        )

    # Перевіряємо структуру першого choice
    first = choices[0]
    message = first.get("message")
    if not message:
        raise OpenRouterError(
            f"Відповідь без 'message' в choices[0]. Модель: {model}",
            model=model, raw_body=str(data)[:500],
        )

    content = message.get("content")
    if content is None:
        raise OpenRouterError(
            f"Відповідь без 'content' в message. Модель: {model}",
            model=model, raw_body=str(data)[:500],
        )

    return content


def _is_retryable(data: dict) -> bool:
    """Перевірити чи помилка в body потребує ретрай (rate limit, overloaded)."""
    err = data.get("error", {})
    if isinstance(err, dict):
        code = err.get("code", 0)
        msg = err.get("message", "").lower()
        # Retryable codes/messages
        if code in (429, 502, 503):
            return True
        if any(kw in msg for kw in ("rate limit", "overloaded", "temporarily", "capacity", "too many")):
            return True
    return False


async def call_openrouter(
    model: str,
    system_prompt: str,
    user_message: str,
    api_key: str | None = None,
) -> str:
    key = api_key or os.getenv("OPENROUTER_API_KEY", "")
    if not key:
        raise OpenRouterError(
            "OPENROUTER_API_KEY не встановлено. "
            "Скопіюй .env.example → .env і заповни ключ.",
            model=model,
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
                "Retry %s — чекаю %ds (спроба %d/%d)...",
                model, delay, attempt + 1, len(_RETRY_DELAYS) + 1,
            )
            await asyncio.sleep(delay)

        try:
            # Семафор обмежує кількість одночасних запитів
            async with _semaphore:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(
                        f"{OPENROUTER_BASE_URL}/chat/completions",
                        headers=headers,
                        json=payload,
                    )

            # HTTP 429 — rate limit на рівні HTTP
            if resp.status_code == 429:
                last_error = OpenRouterError(
                    f"429 Too Many Requests (модель {model})",
                    model=model, status_code=429,
                )
                continue

            # Парсимо body навіть при не-200 статусах
            try:
                data = resp.json()
            except Exception:
                resp.raise_for_status()
                raise OpenRouterError(
                    f"Невалідний JSON у відповіді. Status: {resp.status_code}, Body: {resp.text[:300]}",
                    model=model, status_code=resp.status_code,
                )

            # Перевіряємо наявність помилки в body (часто HTTP 200 + error в JSON)
            if "error" in data:
                if _is_retryable(data):
                    err_msg = data["error"].get("message", str(data["error"])) if isinstance(data["error"], dict) else str(data["error"])
                    logger.warning("Retryable error від %s: %s", model, err_msg[:200])
                    last_error = OpenRouterError(
                        f"Retryable API error: {err_msg}",
                        model=model, status_code=resp.status_code, raw_body=str(data)[:500],
                    )
                    continue

            # Якщо HTTP не 2xx і не було error в body — raise
            if resp.status_code >= 400:
                raise OpenRouterError(
                    f"HTTP {resp.status_code}: {resp.text[:300]}",
                    model=model, status_code=resp.status_code,
                )

            # Безпечно витягуємо контент
            return _extract_content(data, model)

        except OpenRouterError:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                last_error = OpenRouterError(
                    f"429 Too Many Requests (модель {model})",
                    model=model, status_code=429,
                )
                continue
            raise OpenRouterError(
                f"HTTP {e.response.status_code}: {e.response.text[:300]}",
                model=model, status_code=e.response.status_code,
            )
        except httpx.TimeoutException:
            last_error = OpenRouterError(
                f"Timeout (120s) при запиті до {model}",
                model=model,
            )
            continue
        except httpx.ConnectError as e:
            last_error = OpenRouterError(
                f"Connection error до OpenRouter: {e}",
                model=model,
            )
            continue

    raise last_error or OpenRouterError(
        f"Усі {len(_RETRY_DELAYS) + 1} спроб вичерпано для {model}",
        model=model,
    )
