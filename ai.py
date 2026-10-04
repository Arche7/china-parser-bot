"""
ИИ-функции: перевод заголовков и легит-чек по фото.

Работает с любым сервисом, у которого OpenAI-совместимый API
(OpenAI, OpenRouter, DeepSeek, ProxyAPI и т. п.) — меняются только
AI_BASE_URL, AI_API_KEY и названия моделей в настройках.

Если AI_API_KEY не задан, функции просто возвращают None, а бот
переходит на бесплатный словарь из decoder.py.

Сколько это стоит (порядок цифр, точные цены смотри у своего сервиса):
  * перевод заголовка дешёвой моделью — доли цента за десяток объявлений;
  * легит-чек — одна «зрячая» модель на 1–6 фото, примерно 1–3 цента.
"""

import base64
import json
import logging
import re

import aiohttp

import config

log = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(config.AI_API_KEY)


async def _chat(model: str, messages: list[dict], max_tokens: int = 700, json_mode: bool = True) -> str | None:
    if not enabled():
        return None
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {config.AI_API_KEY}", "Content-Type": "application/json"}
    try:
        timeout = aiohttp.ClientTimeout(total=90)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{config.AI_BASE_URL}/chat/completions", json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    log.warning("ИИ ответил ошибкой %s: %s", resp.status, str(data)[:300])
                    return None
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning("Ошибка запроса к ИИ: %s", e)
        return None


def _parse_json(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except ValueError:
                return None
    return None


# ----------------------------------------------------------------------
# Перевод заголовков
# ----------------------------------------------------------------------

_TRANSLATE_PROMPT = (
    "Ты помогаешь байерам из России разбирать объявления с китайской барахолки Goofish. "
    "Переведи каждый заголовок на русский КОРОТКО (до 70 символов): что за вещь, модель/линейка, "
    "важные детали (размер, состояние, комплект). Убери хэштеги, эмодзи, рекламу, повторы и "
    "слова вроде «срочно», «недорого». Название бренда пиши латиницей. Не выдумывай того, "
    "чего нет в заголовке. Ответь JSON: {\"items\": [\"перевод 1\", \"перевод 2\", ...]} — "
    "в том же порядке и столько же, сколько заголовков."
)


async def translate_titles(titles: list[str]) -> list[str | None]:
    """Переводит пачку заголовков одним запросом. None — если не вышло."""
    if not enabled() or not titles:
        return [None] * len(titles)
    numbered = "\n".join(f"{i + 1}. {t[:300]}" for i, t in enumerate(titles))
    text = await _chat(
        config.AI_MODEL,
        [{"role": "system", "content": _TRANSLATE_PROMPT}, {"role": "user", "content": numbered}],
        max_tokens=60 * len(titles) + 100,
    )
    data = _parse_json(text) or {}
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != len(titles):
        return [None] * len(titles)
    return [str(x).strip()[:120] if x else None for x in items]


# ----------------------------------------------------------------------
# Легит-чек
# ----------------------------------------------------------------------

_LEGIT_PROMPT = (
    "Ты — опытный эксперт по проверке подлинности брендовых вещей (легит-чек) для перекупщиков. "
    "Тебе дают фото вещи, бренд и, если есть, заголовок и цену объявления. Оцени, насколько "
    "вещь похожа на оригинал, по тому, что реально видно: логотип и шрифты, бирки и их "
    "шрифт/расположение, строчка и швы, фурнитура (молнии, кнопки, их маркировка), материал, "
    "крой, принт, патчи. Учитывай цену: подозрительно низкая цена для бренда — красный флаг. "
    "Будь честным: если по фото нельзя понять — так и скажи и попроси конкретные ракурсы. "
    "Никогда не утверждай с полной уверенностью. Пиши по-русски, коротко, по делу, "
    "как живой эксперт, без воды.\n"
    "Ответ строго JSON:\n"
    "{\"verdict\": \"likely_real\" | \"unclear\" | \"likely_fake\", "
    "\"score\": число 0-100 (насколько похоже на оригинал), "
    "\"item\": \"что за вещь, 2-5 слов\", "
    "\"good\": [\"что выглядит как у оригинала\", ...до 4], "
    "\"bad\": [\"что смущает\", ...до 4], "
    "\"ask\": [\"какие фото запросить у продавца\", ...до 4]}"
)


async def legit_check(
    brand: str,
    images: list[bytes] | None = None,
    image_urls: list[str] | None = None,
    title: str | None = None,
    price_text: str | None = None,
) -> dict | None:
    """Возвращает словарь с вердиктом или None, если ИИ недоступен."""
    if not enabled():
        return None
    content: list[dict] = []
    info = [f"Бренд: {brand}"]
    if title:
        info.append(f"Заголовок объявления: {title[:300]}")
    if price_text:
        info.append(f"Цена: {price_text}")
    content.append({"type": "text", "text": "\n".join(info)})
    for url in (image_urls or [])[:6]:
        content.append({"type": "image_url", "image_url": {"url": url, "detail": "high"}})
    for raw in (images or [])[:6]:
        b64 = base64.b64encode(raw).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}})
    text = await _chat(
        config.AI_VISION_MODEL,
        [{"role": "system", "content": _LEGIT_PROMPT}, {"role": "user", "content": content}],
        max_tokens=900,
    )
    data = _parse_json(text)
    if not isinstance(data, dict) or "verdict" not in data:
        return None
    try:
        data["score"] = max(0, min(100, int(data.get("score", 50))))
    except (TypeError, ValueError):
        data["score"] = 50
    for key in ("good", "bad", "ask"):
        value = data.get(key)
        data[key] = [str(x) for x in value][:4] if isinstance(value, list) else []
    return data
