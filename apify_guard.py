"""
Сторож лимита Apify.

Все площадки (Goofish, Авито, загрузка всех фото) работают через Apify.
Когда на аккаунте Apify заканчиваются деньги или месячный лимит, каждый
запуск актора падает с ошибкой вроде «Monthly usage hard limit exceeded».
Этот модуль:
  * узнаёт такую ошибку (is_quota_error);
  * запоминает её на 30 минут (blocked), чтобы бот не долбил Apify каждые 5 минут;
  * раз в 6 часов пишет админу, что нужно сделать (should_alert).
"""

import time

PAUSE_SEC = 30 * 60          # после ошибки лимита не трогаем Apify столько секунд
ALERT_EVERY_SEC = 6 * 3600   # напоминание админу не чаще

_MARKS = ("hard limit", "usage limit", "monthly usage", "exceeded your", "not enough credit",
          "insufficient credit", "platform usage", "payment required")

_blocked_until = 0.0
_last_alert = 0.0
last_error = ""

ALERT_TEXT = (
    "⚠️ <b>Apify: закончился месячный лимит</b>\n\n"
    "Поиск на Goofish, загрузка всех фото и цены Авито сейчас не работают — "
    "Apify отвечает «{error}».\n\n"
    "Что сделать: console.apify.com → <b>Billing</b> → подними «Monthly usage limit» "
    "или перейди на платный план. Как только лимит появится, бот сам продолжит "
    "(проверяю раз в 30 минут)."
)


def is_quota_error(error: BaseException | str) -> bool:
    text = str(error).lower()
    return any(mark in text for mark in _MARKS)


def note(error: BaseException | str) -> bool:
    """Запомнить ошибку. True — если это ошибка лимита Apify."""
    global _blocked_until, last_error
    if not is_quota_error(error):
        return False
    _blocked_until = time.time() + PAUSE_SEC
    last_error = str(error)[:200]
    return True


def blocked() -> bool:
    """Apify недавно отказал по лимиту — пока не запускаем акторы."""
    return time.time() < _blocked_until


def clear() -> None:
    global _blocked_until
    _blocked_until = 0.0


def should_alert() -> bool:
    """Пора напомнить админу (не чаще раза в 6 часов)."""
    global _last_alert
    if time.time() - _last_alert < ALERT_EVERY_SEC:
        return False
    _last_alert = time.time()
    return True


def alert_text() -> str:
    import html
    return ALERT_TEXT.format(error=html.escape(last_error or "лимит исчерпан"))
