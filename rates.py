"""
Курс юаня к рублю по ЦБ РФ.

Берём бесплатный JSON с cbr-xml-daily.ru раз в 6 часов. Если сайт не
ответил — используем последний известный курс, а если его нет —
CNY_RUB_RATE из настроек.
"""

import logging
import time

import aiohttp

import config

log = logging.getLogger(__name__)

_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
_TTL = 6 * 3600

_rate: float = config.CNY_RUB_RATE
_updated_at: float = 0.0
_is_live: bool = False


async def refresh() -> None:
    global _rate, _updated_at, _is_live
    if time.time() - _updated_at < _TTL:
        return
    _updated_at = time.time()  # даже при ошибке не дёргаем сайт каждую секунду
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_URL) as resp:
                data = await resp.json(content_type=None)
        cny = data["Valute"]["CNY"]
        value = float(cny["Value"]) / float(cny.get("Nominal", 1))
        if 3 < value < 50:  # защита от странных данных
            _rate, _is_live = value, True
            log.info("Курс ЦБ: 1 ¥ = %.2f ₽", value)
    except Exception as e:
        log.warning("Не удалось обновить курс ЦБ: %s (оставляю %.2f)", e, _rate)


def cny_rub() -> float:
    return _rate


def source_label() -> str:
    return "курс ЦБ" if _is_live else "примерный курс"
