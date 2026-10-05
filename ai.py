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
        message = data["choices"][0]["message"]
        if message.get("refusal"):
            log.warning("ИИ отказался отвечать (%s): %s", model, str(message["refusal"])[:300])
            return None
        if not message.get("content"):
            log.warning("ИИ вернул пустой ответ (%s), finish_reason=%s", model, data["choices"][0].get("finish_reason"))
        return message.get("content")
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


async def _download_images(urls: list[str]) -> list[bytes]:
    """Скачивает фото по ссылкам (до 8 МБ каждое). Пропускает те, что не скачались."""
    out: list[bytes] = []
    timeout = aiohttp.ClientTimeout(total=25)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for url in urls:
                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            body = await resp.read()
                            if 0 < len(body) <= 8 * 1024 * 1024:
                                out.append(body)
                except Exception as e:
                    log.debug("Не скачал фото %s: %s", url, e)
    except Exception as e:
        log.warning("Не скачал фото для ИИ: %s", e)
    return out


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
    "Тебе дают фото вещи из объявления на китайской барахолке Goofish, бренд и, если есть, "
    "заголовок, описание продавца, цену, репутацию продавца и цены похожих вещей в России. Оцени, насколько "
    "вещь похожа на оригинал, по тому, что реально видно: логотип и шрифты, бирки и их "
    "шрифт/расположение, строчка и швы, фурнитура (молнии, кнопки, их маркировка), материал, "
    "крой, принт, патчи. Учитывай цену: подозрительно низкая цена для бренда — красный флаг. "
    "Учитывай продавца: много продаж, хороший рейтинг и кредит Zhima — плюс; новый аккаунт без "
    "продаж — минус. Слова 高仿, 复刻, A货, 1:1, 原单, 同款 в описании — почти наверняка копия. "
    "Что смотреть по типу вещи: сумки — штамп/тиснение и дата-код или серийник, ровность и шаг "
    "строчки, гравировка на фурнитуре, подкладка, края; одежда — шейная бирка, бирка состава/ухода, "
    "QR/NFC/Certilogo (Stone Island, Moncler, Canada Goose), молнии и кнопки с маркировкой, патч; "
    "обувь — стелька и её логотип, бирка с размером внутри, подошва, коробка и её этикетка; "
    "аксессуары/косметика — шрифт, упаковка, партия/код. "
    "Пиши только о том, что действительно видно на фото, и указывай номер фото: "
    "«фото 3: ровная строчка по краю ручки». Общие фразы вроде «логотипы выглядят корректно» "
    "без конкретики запрещены. Про цену говори, только если дана медиана Авито или цена явно "
    "смешная для бренда. Если ключевых деталей (бирки, коды, фурнитура) на фото нет — оценка не "
    "выше 70 и verdict \"unclear\". "
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
    notes: str | None = None,
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
    if notes:
        info.append(notes[:2500])
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
    if (not isinstance(data, dict) or "verdict" not in data) and image_urls and not images:
        # ИИ не смог скачать фото по ссылкам alicdn или ответил не по формату —
        # скачиваем фото сами и пробуем ещё раз, уже присылая картинки целиком
        log.warning("Легит-чек: ответ ИИ без вердикта (%s) — пробую ещё раз со скачанными фото",
                    (text or "пусто")[:200].replace("\n", " "))
        raw = await _download_images(image_urls[:6])
        if raw:
            return await legit_check(brand, images=raw, title=title, price_text=price_text, notes=notes)
        return None
    if not isinstance(data, dict) or "verdict" not in data:
        log.warning("Легит-чек: ответ ИИ без вердикта: %s", (text or "пусто")[:200].replace("\n", " "))
        return None
    try:
        data["score"] = max(0, min(100, int(data.get("score", 50))))
    except (TypeError, ValueError):
        data["score"] = 50
    for key in ("good", "bad", "ask"):
        value = data.get(key)
        data[key] = [str(x) for x in value][:4] if isinstance(value, list) else []
    return data


# ----------------------------------------------------------------------
# ИИ-помощник по переписке с продавцом (тариф ELITE)
# ----------------------------------------------------------------------

_ASSISTANT_PROMPT = (
    "Ты — ИИ-помощник байера из России, который покупает брендовые вещи на китайской барахолке "
    "Goofish (闲鱼) и переписывается с продавцами. Тебе присылают скриншоты переписки (Goofish, WeChat), "
    "фото вещи от продавца, китайский текст или вопрос по-русски. Помогаешь:\n"
    "1) перевести, что написал продавец, и объяснить, что он имел в виду (сленг, намёки, уловки);\n"
    "2) написать готовый ответ продавцу на естественном разговорном китайском, как пишут на Goofish, "
    "коротко и вежливо, и дать его перевод;\n"
    "3) подсказать, как торговаться: реалистичная цена, вежливые формулировки (诚心要, 能便宜点吗, 包邮吗), "
    "когда лучше не давить;\n"
    "4) сказать, каких фото не хватает для проверки подлинности (бирки, QR/NFC-код, швы, фурнитура, "
    "дата-код, коробка, чек) — конкретно для этой вещи;\n"
    "5) если пришли фото вещи — уточнить оценку риска подделки с учётом прошлой оценки: после каждого "
    "нового фото говори, стало лучше, хуже или без изменений, и почему, со ссылкой на номер фото.\n"
    "Правила: никогда не обещай «100% оригинал» — это только оценка риска. Пиши только о том, что реально "
    "видно. Красные флаги: просьба оплатить вне Goofish (перевод на WeChat/Alipay/карту, 加微信 для оплаты, "
    "私下交易), отказ от 担保交易 (гарантии платформы), слова 高仿, 复刻, A货, 1:1, 原单, 同款, отказ показать "
    "бирки, слишком низкая цена. Всегда советуй платить только внутри Goofish. Не помогай обманывать продавца. "
    "Ты не можешь писать продавцу сам — ответ отправляет человек. Пиши по-русски коротко и по делу, "
    "без воды; китайский — только в полях reply_cn и seller_said_cn.\n"
    "Ответ строго JSON:\n"
    "{\"kind\": \"chat\" | \"item_photos\" | \"question\" | \"other\", "
    "\"seller_said\": \"перевод того, что написал продавец (если на скрине/в тексте есть его слова), иначе пусто\", "
    "\"meaning\": \"что это значит и на что обратить внимание, 1-3 предложения\", "
    "\"reply_cn\": \"готовый ответ продавцу на китайском или пусто, если отвечать не нужно\", "
    "\"reply_ru\": \"перевод reply_cn на русский\", "
    "\"tips\": [\"совет\", ...до 3], "
    "\"ask_photos\": [\"какое фото попросить\", ...до 4], "
    "\"red_flags\": [\"тревожный признак\", ...до 3], "
    "\"legit\": null или {\"verdict\": \"likely_real\" | \"unclear\" | \"likely_fake\", "
    "\"score\": 0-100, \"change\": \"better\" | \"worse\" | \"same\" | \"first\", "
    "\"why\": \"1-2 предложения с номерами фото\"} — заполняй legit только если на фото видна сама вещь, "
    "её бирки или детали}"
)

_INTENTS = {
    "alt": "Дай ДРУГОЙ вариант ответа продавцу на тот же последний вопрос — другими словами и другим тоном.",
    "bargain": "Помоги поторговаться: предложи реалистичную цену со скидкой и напиши вежливый ответ на "
               "китайском с этим предложением. Объясни, почему именно такая цена.",
    "photos": "Скажи, какие конкретно фото попросить у продавца для проверки подлинности этой вещи, "
              "и напиши просьбу на китайском одним сообщением.",
    "verdict": "Подведи итог по риску подделки по всему, что уже известно (фото, переписка, продавец, цена). "
               "Заполни legit. Если данных мало — так и скажи и перечисли, чего не хватает.",
}


async def assistant(
    context: str,
    history: list[dict],
    text: str | None = None,
    images: list[bytes] | None = None,
    intent: str | None = None,
) -> dict | None:
    """
    Один шаг помощника. history — прошлые реплики [{"role": "user"|"assistant", "content": str}].
    Возвращает словарь (см. _ASSISTANT_PROMPT) или None, если ИИ недоступен.
    """
    if not enabled():
        return None
    messages: list[dict] = [{"role": "system", "content": _ASSISTANT_PROMPT}]
    if context:
        messages.append({"role": "system", "content": "Что известно о вещи и сделке:\n" + context[:3000]})
    for turn in history[-8:]:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": str(turn["content"])[:1500]})
    content: list[dict] = []
    parts = []
    if intent in _INTENTS:
        parts.append(_INTENTS[intent])
    if text:
        parts.append(f"Сообщение пользователя: {text[:2000]}")
    if images:
        parts.append(f"Прислано изображений: {len(images[:6])} (нумеруй их по порядку: фото 1, фото 2…).")
    content.append({"type": "text", "text": "\n".join(parts) or "Помоги с перепиской."})
    for raw in (images or [])[:6]:
        b64 = base64.b64encode(raw).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}})
    messages.append({"role": "user", "content": content})
    answer = await _chat(config.AI_ASSISTANT_MODEL, messages, max_tokens=1100)
    data = _parse_json(answer)
    if not isinstance(data, dict):
        return None
    for key in ("tips", "ask_photos", "red_flags"):
        value = data.get(key)
        data[key] = [str(x) for x in value if x][:4] if isinstance(value, list) else []
    for key in ("kind", "seller_said", "meaning", "reply_cn", "reply_ru"):
        data[key] = str(data.get(key) or "").strip()
    legit = data.get("legit")
    if isinstance(legit, dict) and legit.get("verdict") in ("likely_real", "unclear", "likely_fake"):
        try:
            legit["score"] = max(0, min(100, int(legit.get("score", 50))))
        except (TypeError, ValueError):
            legit["score"] = 50
        legit["why"] = str(legit.get("why") or "")
        legit["change"] = legit.get("change") if legit.get("change") in ("better", "worse", "same", "first") else "same"
        data["legit"] = legit
    else:
        data["legit"] = None
    return data
