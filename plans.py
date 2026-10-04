"""
Тарифы HUNTR.

Здесь в одном месте лежит всё, чем тарифы отличаются друг от друга:
сколько брендов можно отслеживать, как часто бот проверяет площадку,
сколько легит-чеков и сравнений цен входит в месяц.

Оплата — только звёздами Telegram (⭐, валюта XTR): так Telegram требует
для цифровых товаров. Месяц — подписка с автопродлением (Telegram сам
списывает звёзды раз в 30 дней), 3 месяца — разовый платёж со скидкой.
Чтобы поменять цену или лимит, достаточно поправить цифру ниже и
перезапустить бота.

Почему лимиты именно такие — см. PRODUCT.md (раздел «Экономика»).
Коротко: каждая проверка бренда стоит денег на Apify, поэтому
чем чаще проверка и чем больше «своих» брендов — тем дороже тариф.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Plan:
    code: str                 # как хранится в базе
    title: str                # как видит пользователь
    price_rub: int            # ориентир в рублях (для расчётов и /stats)
    price_stars: int          # цена за месяц в звёздах Telegram ⭐ (0 = бесплатно)
    brands: int               # сколько брендов всего можно отслеживать
    own_brands: int           # из них «своих» — не из каталога HUNTR
    interval_min: int         # как часто проверять бренды из каталога, минут
    legit_checks: int         # легит-чеков ИИ в месяц
    price_checks: int         # сравнений цены с Авито в месяц
    tagline: str = ""         # одна строка «для кого»
    perks: list[str] = field(default_factory=list)
    seats: int = 0            # 0 = без ограничения мест
    own_interval_min: int = 30  # как часто проверять «свои» бренды (их никто не делит — каждый запрос платный)
    extra_own: int = 0          # сколько слотов «+1 свой бренд» докуплено (заполняется для конкретного человека)


# «Свои» бренды (которых нет в каталоге) проверяются раз в 30 минут на любом
# тарифе. Бренд из каталога делят все подписчики, а свой бренд обычно ищешь
# только ты — и каждый его запрос оплачивается целиком.
OWN_BRAND_INTERVAL_MIN = 30

# Докупка «+1 свой бренд» к любому платному тарифу — отдельная подписка звёздами.
# Себестоимость одного своего бренда при проверке раз в 30 минут ≈ $11.5 в месяц
# (1 поисковый запрос × 48 раз в день × 30 дней на Apify) ≈ 885 ⭐ к выводу.
# Если этот же бренд добавил кто-то ещё — запрос общий и выходит дешевле.
OWN_ADDON_STARS = 1290
OWN_ADDON_MAX = 10          # больше слотов одному человеку не продаём

TRIAL = Plan(
    code="trial",
    title="Пробный",
    price_rub=0,
    price_stars=0,
    brands=2,
    own_brands=0,
    interval_min=60,
    legit_checks=1,
    price_checks=2,
    tagline="2 дня, чтобы увидеть первые находки",
    perks=["2 бренда из каталога", "проверка раз в час", "1 легит-чек", "2 сравнения с Авито"],
)

START = Plan(
    code="start",
    title="START",
    price_rub=1740,
    price_stars=1290,
    brands=5,
    own_brands=0,
    interval_min=30,
    legit_checks=10,
    price_checks=20,
    tagline="Для себя и первых перепродаж",
    perks=[
        "5 брендов из каталога",
        "проверка каждые 30 мин",
        "фильтр подделок и мусора",
        "калькулятор себестоимости",
        "10 легит-чеков · 20 сравнений с Авито",
    ],
)

PRO = Plan(
    code="pro",
    title="PRO",
    price_rub=4040,
    price_stars=2990,
    brands=15,
    own_brands=2,
    interval_min=15,
    legit_checks=40,
    price_checks=60,
    tagline="Для тех, кто перепродаёт регулярно",
    perks=[
        "15 брендов, из них 2 — любые свои",
        "каталог — каждые 15 мин, вдвое быстрее START",
        "свои бренды — каждые 30 мин",
        "40 легит-чеков · 60 сравнений с Авито",
        "все фото вещи, лента и избранное в приложении",
    ],
)

ELITE = Plan(
    code="elite",
    title="ELITE",
    price_rub=9440,
    price_stars=6990,
    brands=30,
    own_brands=3,
    interval_min=10,
    legit_checks=150,
    price_checks=300,
    tagline="Максимальная скорость — первым видишь находки",
    perks=[
        "30 брендов, из них 3 — любые свои",
        "каталог — каждые 10 мин, свои — каждые 30 мин",
        "150 легит-чеков · 300 сравнений с Авито",
        "ранний доступ к новым площадкам (95分)",
        "личная поддержка",
    ],
)

# Админ — без ограничений (для тебя и тестов)
ADMIN = Plan(
    code="admin",
    title="ADMIN",
    price_rub=0,
    price_stars=0,
    brands=100,
    own_brands=100,
    interval_min=30,
    legit_checks=10_000,
    price_checks=10_000,
    tagline="Полный доступ",
)

# Порядок важен: так тарифы показываются в боте
PAID_PLANS: list[Plan] = [START, PRO, ELITE]
ALL_PLANS: dict[str, Plan] = {p.code: p for p in [TRIAL, START, PRO, ELITE, ADMIN]}


def can_buy_own(plan: Plan) -> bool:
    """Можно ли докупить «+1 свой бренд» к этому тарифу (только к платным)."""
    return plan.code in {p.code for p in PAID_PLANS} and plan.extra_own < OWN_ADDON_MAX


def get_plan(code: str | None) -> Plan:
    """Тариф по коду. Старые пользователи без тарифа считаются PRO."""
    if not code:
        return PRO
    return ALL_PLANS.get(code, PRO)


# Скидка за оплату сразу на 3 месяца
QUARTER_DISCOUNT = 0.15
# Telegram не даёт выставить подписку дороже 10 000 ⭐; разовые счета
# тоже держим в этих пределах, чтобы не упереться в лимит
MAX_INVOICE_STARS = 10_000
SUBSCRIPTION_PERIOD = 2_592_000  # 30 дней в секундах — единственный вариант у Telegram


def quarter_stars(plan: "Plan") -> int | None:
    """Цена за 3 месяца со скидкой, округлённая до 10 ⭐. None — если выходит за лимит."""
    value = int(round(plan.price_stars * 3 * (1 - QUARTER_DISCOUNT) / 10) * 10)
    return value if 0 < value <= MAX_INVOICE_STARS else None


def stars(amount: int) -> str:
    """2490 -> '2 490 ⭐'"""
    return f"{amount:,}".replace(",", " ") + " ⭐"


def rub(amount: float) -> str:
    """12345 -> '12 345 ₽'"""
    return f"{amount:,.0f}".replace(",", " ") + " ₽"
