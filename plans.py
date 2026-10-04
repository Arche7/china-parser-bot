"""
Тарифы HUNTR.

Здесь в одном месте лежит всё, чем тарифы отличаются друг от друга:
сколько брендов можно отслеживать, как часто бот проверяет площадку,
сколько легит-чеков и сравнений цен входит в месяц.

Цены пока только показываются в боте — оплату подключим позже.
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
    price_rub: int            # цена за месяц, ₽ (0 = бесплатно)
    brands: int               # сколько брендов всего можно отслеживать
    own_brands: int           # из них «своих» — не из каталога HUNTR
    interval_min: int         # как часто проверять бренды из каталога, минут
    legit_checks: int         # легит-чеков ИИ в месяц
    price_checks: int         # сравнений цены с Авито в месяц
    tagline: str = ""         # одна строка «для кого»
    perks: list[str] = field(default_factory=list)
    seats: int = 0            # 0 = без ограничения мест


# «Свои» бренды (которых нет в каталоге) проверяются не чаще, чем раз
# в столько минут на любом тарифе: такой бренд никто с тобой не делит,
# и каждый его запрос оплачивается целиком.
OWN_BRAND_INTERVAL_MIN = 30

TRIAL = Plan(
    code="trial",
    title="Пробный",
    price_rub=0,
    brands=3,
    own_brands=0,
    interval_min=30,
    legit_checks=2,
    price_checks=3,
    tagline="3 дня, чтобы понять, как это работает",
    perks=["3 бренда из каталога", "проверка каждые 30 мин", "2 легит-чека", "3 сравнения с Авито"],
)

START = Plan(
    code="start",
    title="START",
    price_rub=1490,
    brands=5,
    own_brands=0,
    interval_min=30,
    legit_checks=10,
    price_checks=15,
    tagline="Для себя и первых перепродаж",
    perks=[
        "5 брендов из каталога",
        "проверка каждые 30 мин",
        "фильтр подделок и мусора",
        "калькулятор себестоимости",
        "10 легит-чеков · 15 сравнений с Авито",
    ],
)

PRO = Plan(
    code="pro",
    title="PRO",
    price_rub=3490,
    brands=15,
    own_brands=2,
    interval_min=15,
    legit_checks=40,
    price_checks=60,
    tagline="Для тех, кто перепродаёт регулярно",
    perks=[
        "15 брендов, из них 2 — любые свои",
        "проверка каждые 15 мин — вдвое быстрее",
        "40 легит-чеков · 60 сравнений с Авито",
        "приложение HUNTR: лента, избранное, аналитика",
    ],
)

ELITE = Plan(
    code="elite",
    title="ELITE",
    price_rub=7990,
    brands=30,
    own_brands=5,
    interval_min=10,
    legit_checks=150,
    price_checks=300,
    tagline="Максимальная скорость. Ограниченное число мест",
    perks=[
        "30 брендов, из них 5 — любые свои",
        "проверка каждые 10 мин — первым видишь находки",
        "150 легит-чеков · 300 сравнений с Авито",
        "ранний доступ к новым площадкам (95分)",
        "личная поддержка",
    ],
    seats=50,
)

# Админ — без ограничений (для тебя и тестов)
ADMIN = Plan(
    code="admin",
    title="ADMIN",
    price_rub=0,
    brands=100,
    own_brands=100,
    interval_min=10,
    legit_checks=10_000,
    price_checks=10_000,
    tagline="Полный доступ",
)

# Порядок важен: так тарифы показываются в боте
PAID_PLANS: list[Plan] = [START, PRO, ELITE]
ALL_PLANS: dict[str, Plan] = {p.code: p for p in [TRIAL, START, PRO, ELITE, ADMIN]}


def get_plan(code: str | None) -> Plan:
    """Тариф по коду. Старые пользователи без тарифа считаются PRO."""
    if not code:
        return PRO
    return ALL_PLANS.get(code, PRO)


def rub(amount: float) -> str:
    """12345 -> '12 345 ₽'"""
    return f"{amount:,.0f}".replace(",", " ") + " ₽"
