"""
Готовые бренды: все написания + китайские названия.

Как это работает
----------------
У каждого бренда есть три списка:

  search  — по каким словам бот ИЩЕТ на Goofish. Каждое слово — это
            отдельный запуск актора Apify при каждой проверке, то есть
            отдельные деньги. Поэтому здесь только 1–2 самых ходовых
            названия: латиница + китайское.

  aliases — ВСЕ известные написания бренда (латиницей, сокращения,
            по-китайски, сленг). По ним бот бесплатно, у себя, проверяет
            заголовок найденного объявления: если ни одного написания
            в заголовке нет — значит, это мусор, а не наш бренд.
            Ещё по этому списку бот узнаёт бренд, если ты пишешь
            /add lv, /add 古驰 или /add ysl — всё это превратится
            в один и тот же бренд, а не в три разных.

  title   — красивое название для сообщений.

  ru      — как бренд пишут на Авито (по-русски и латиницей). Нужно для
            сравнения цен: так бот понимает, что объявление на Авито — про наш бренд.

Пробелы, точки и дефисы в написаниях не важны: "c.p. company",
"cp company" и "CPCOMPANY" — для бота одно и то же.

Чтобы добавить бренд в каталог — скопируй любой блок ниже и поменяй слова.
Он сразу появится в боте среди кнопок «➕ Добавить».
Чтобы урезать расходы — оставь в "search" одно слово.
"""

import re

# Цена «по умолчанию» для готового набора (/preset), в юанях ¥
PRESET_PRICE_MIN = 200
PRESET_PRICE_MAX = 1800

BRANDS: dict[str, dict] = {
    "gucci": {
        "title": "Gucci",
        "search": ["gucci", "古驰"],
        # «GG» специально НЕ добавлен: это слишком короткое сочетание,
        # оно встречается в куче посторонних заголовков.
        "aliases": ["gucci", "古驰", "古奇", "古琦"],
        "ru": ["гуччи", "gucci"],
    },
    "cp company": {
        "title": "C.P. Company",
        # Устоявшегося китайского названия нет — ищем только латиницей.
        "search": ["cp company"],
        # Голое «CP» НЕ добавлено: в китайском сленге CP = «парочка»
        # (情侣), и бот слал бы парные футболки и кружки.
        "aliases": ["cp company", "c.p. company", "cpcompany", "c.p.company"],
        "ru": ["cp company", "си пи компани", "c.p. company"],
    },
    "stone island": {
        "title": "Stone Island",
        "search": ["stone island", "石头岛"],
        "aliases": ["stone island", "stoneisland", "石头岛"],
        "ru": ["стон айленд", "стоник", "stone island"],
    },
    "louis vuitton": {
        "title": "Louis Vuitton",
        "search": ["lv", "路易威登"],
        # 驴牌 («ослиная марка») — разговорное прозвище LV в Китае
        "aliases": ["louis vuitton", "lv", "路易威登", "驴牌"],
        "ru": ["луи виттон", "louis vuitton"],
    },
    "prada": {
        "title": "Prada",
        "search": ["prada", "普拉达"],
        "aliases": ["prada", "普拉达"],
        "ru": ["прада", "prada"],
    },
    "saint laurent": {
        "title": "Saint Laurent",
        "search": ["ysl", "圣罗兰"],
        # 杨树林 («тополиная роща») — шуточное прозвище YSL у китайцев
        "aliases": ["saint laurent", "yves saint laurent", "ysl", "圣罗兰", "杨树林"],
        "ru": ["сен лоран", "saint laurent", "ysl"],
    },
    "burberry": {
        "title": "Burberry",
        "search": ["burberry", "巴宝莉"],
        # 博柏利 — официальное название, 巴宝莉/巴宝利 — народное,
        # Burberrys — старое написание на винтажных вещах
        "aliases": ["burberry", "burberrys", "bbr", "博柏利", "巴宝莉", "巴宝利"],
        "ru": ["барберри", "бербери", "burberry"],
    },
    # ----- Дальше — бренды каталога, которые НЕ входят в «Готовый набор».
    # Их можно выбрать кнопкой в боте. Пока бренд никто не отслеживает,
    # он ничего не стоит: запросы идут только по выбранным брендам.
    "arcteryx": {
        "title": "Arc'teryx",
        "search": ["始祖鸟"],
        "aliases": ["arcteryx", "arc'teryx", "arc teryx", "始祖鸟"],
        "ru": ["арктерикс", "arcteryx"],
    },
    "moncler": {
        "title": "Moncler",
        "search": ["moncler", "蒙口"],
        # 蒙口 — народное название, 盟可睐 — официальное
        "aliases": ["moncler", "蒙口", "盟可睐"],
        "ru": ["монклер", "moncler"],
    },
    "chrome hearts": {
        "title": "Chrome Hearts",
        "search": ["chrome hearts", "克罗心"],
        "aliases": ["chrome hearts", "chromehearts", "克罗心"],
        "ru": ["хром хартс", "chrome hearts"],
    },
    "balenciaga": {
        "title": "Balenciaga",
        "search": ["balenciaga", "巴黎世家"],
        "aliases": ["balenciaga", "巴黎世家"],
        "ru": ["баленсиага", "balenciaga"],
    },
    "maison margiela": {
        "title": "Maison Margiela",
        "search": ["margiela", "马吉拉"],
        "aliases": ["maison margiela", "margiela", "mm6", "马吉拉"],
        "ru": ["маржела", "марджела", "margiela"],
    },
    "dior": {
        "title": "Dior",
        "search": ["dior", "迪奥"],
        "aliases": ["dior", "christian dior", "迪奥"],
        "ru": ["диор", "dior"],
    },
    "miu miu": {
        "title": "Miu Miu",
        "search": ["miumiu", "缪缪"],
        "aliases": ["miu miu", "miumiu", "缪缪"],
        "ru": ["миу миу", "miu miu"],
    },
    "loewe": {
        "title": "Loewe",
        "search": ["loewe", "罗意威"],
        "aliases": ["loewe", "罗意威"],
        "ru": ["лоэве", "loewe"],
    },
    "canada goose": {
        "title": "Canada Goose",
        "search": ["加拿大鹅"],
        "aliases": ["canada goose", "canadagoose", "加拿大鹅"],
        "ru": ["канада гус", "canada goose"],
    },
    "bottega veneta": {
        "title": "Bottega Veneta",
        "search": ["bottega", "葆蝶家"],
        # «BV» не добавлен: слишком короткое сочетание
        "aliases": ["bottega veneta", "bottega", "葆蝶家"],
        "ru": ["боттега", "bottega"],
    },
}

# Бренды, которые добавляет «Готовый набор» (в таком порядке)
PRESET: list[str] = [
    "gucci", "cp company", "stone island", "louis vuitton", "prada", "saint laurent", "burberry",
]

# Варианты цены для кнопок при добавлении бренда: (подпись, от, до) в юанях
PRICE_PRESETS: list[tuple[str, float | None, float | None]] = [
    ("до ¥500", None, 500),
    ("¥200–1 800", 200, 1800),
    ("¥500–3 000", 500, 3000),
    ("¥1 000–5 000", 1000, 5000),
    ("от ¥3 000", 3000, None),
    ("любая", None, None),
]

# Если в заголовке есть одно из этих слов — объявление не присылаем.
# Это типичные пометки копий и подделок на китайских площадках.
EXCLUDE_WORDS: list[str] = [
    "高仿",   # «высококачественная подделка»
    "仿品",   # «подделка»
    "复刻",   # «реплика»
    "a货",    # «товар класса A» = копия
    "超a",    # «супер-A» = копия
    "1:1",    # «один в один»
    "1：1",   # то же, с китайским двоеточием
    "1比1",   # то же
    "莆田",   # Путянь — город, известный фабриками копий
    "原单",   # «с оригинального заказа» — обычно «фабричная» копия
    "同款",   # «такая же модель, как у …» = не этот бренд
]


# ----------------------------------------------------------------------
# Дальше — служебный код, менять не нужно
# ----------------------------------------------------------------------

def _normalize(text: str) -> str:
    """'C.P. Company' -> 'cpcompany', '古驰 Gucci' -> '古驰gucci'."""
    return re.sub(r"[^0-9a-z一-鿿]+", "", text.lower())


def _alias_regex(alias: str) -> re.Pattern:
    """
    Латинское написание ищем как отдельное слово, допуская пробелы, точки
    и дефисы внутри: 'lv' найдётся в 'LV包' и 'L.V', но НЕ в 'silver'.
    Китайское — просто как подстроку (в китайском нет пробелов).
    """
    alias = alias.lower().strip()
    if re.search(r"[a-z]", alias):
        compact = re.sub(r"[^a-z0-9]", "", alias)
        separator = r"[\s.\-_·'’&]*"
        body = separator.join(re.escape(ch) for ch in compact)
        return re.compile(rf"(?<![a-z]){body}(?![a-z])")
    return re.compile(re.escape(alias))


# Быстрый поиск бренда по любому его написанию
_INDEX: dict[str, str] = {}
_PATTERNS: dict[str, list[re.Pattern]] = {}
for _key, _brand in BRANDS.items():
    for _name in [_key, _brand["title"], *_brand["aliases"]]:
        _INDEX.setdefault(_normalize(_name), _key)
    _PATTERNS[_key] = [_alias_regex(a) for a in _brand["aliases"]]

_EXCLUDE = [w.lower() for w in EXCLUDE_WORDS]


def canonical(keyword: str) -> str:
    """
    Приводит любое написание к одному ключу бренда:
    'LV' / '路易威登' / 'Louis Vuitton' -> 'louis vuitton'.
    Незнакомые слова возвращаются как есть (в нижнем регистре).
    """
    keyword = keyword.strip().lower()
    return _INDEX.get(_normalize(keyword), keyword)


def get_brand(keyword: str) -> dict | None:
    """Описание бренда из списка выше или None, если бренд незнакомый."""
    return BRANDS.get(canonical(keyword))


def display_name(keyword: str) -> str:
    brand = get_brand(keyword)
    return brand["title"] if brand else keyword


def is_catalog(keyword: str) -> bool:
    """Бренд из каталога HUNTR (а не «свой»)?"""
    return get_brand(keyword) is not None


def russian_aliases(keyword: str) -> list[str]:
    """Как бренд пишут на Авито (для сравнения цен)."""
    brand = get_brand(keyword)
    if brand:
        return [a.lower() for a in brand.get("ru", [])] + [brand["title"].lower()]
    return [keyword.lower()]


def search_queries(keyword: str) -> list[str]:
    """По каким словам искать на площадке."""
    brand = get_brand(keyword)
    if brand:
        return list(brand["search"])
    return [keyword.strip().lower()]


def is_fake(title: str) -> bool:
    """Есть ли в заголовке пометка копии/подделки."""
    title = title.lower()
    return any(word in title for word in _EXCLUDE)


def mentions_brand(keyword: str, title: str) -> bool:
    """
    Упоминается ли бренд в заголовке.
    Для незнакомых брендов (которых нет в списке выше) всегда True —
    для них мы не знаем написаний и доверяем поиску площадки.
    """
    key = canonical(keyword)
    patterns = _PATTERNS.get(key)
    if not patterns:
        return True
    title = title.lower()
    return any(p.search(title) for p in patterns)


def listing_ok(keyword: str, title: str) -> bool:
    """Главная проверка: и бренд наш, и не подделка."""
    return mentions_brand(keyword, title) and not is_fake(title)
