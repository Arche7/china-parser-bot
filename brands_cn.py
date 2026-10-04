"""
Китайские названия популярных брендов.

Зачем: Goofish — китайская площадка, и продавцы часто пишут бренд
иероглифами (始祖鸟 вместо Arc'teryx). Когда ты добавляешь бренд из этого
списка, бот предлагает добавить и китайское название — так находится больше
объявлений.

Названия собраны по памяти, без проверки по источникам. Если какое-то
название неверное или ты знаешь, как бренд чаще пишут на Goofish, просто
поправь строку ниже. Формат: "бренд латиницей": "китайское название".
Регистр, пробелы, апострофы и дефисы в ключе не важны.
"""

import re

BRANDS_CN: dict[str, str] = {
    # Аутдор и спорт
    "arcteryx": "始祖鸟",
    "the north face": "北面",
    "north face": "北面",
    "tnf": "北面",
    "canada goose": "加拿大鹅",
    "moncler": "盟可睐",
    "patagonia": "巴塔哥尼亚",
    "salomon": "萨洛蒙",
    "nike": "耐克",
    "adidas": "阿迪达斯",
    "new balance": "新百伦",
    "asics": "亚瑟士",
    "lululemon": "露露乐蒙",
    # Стритвир и дизайнеры
    "stone island": "石头岛",
    "chrome hearts": "克罗心",
    "maison margiela": "马吉拉",
    "margiela": "马吉拉",
    "comme des garcons": "川久保玲",
    "cdg": "川久保玲",
    "yohji yamamoto": "山本耀司",
    "issey miyake": "三宅一生",
    "bape": "猿人头",
    "a bathing ape": "猿人头",
    "stussy": "斯图西",
    "carhartt": "卡哈特",
    "vivienne westwood": "西太后",
    "thom browne": "汤姆布朗",
    "kenzo": "高田贤三",
    "alexander mcqueen": "麦昆",
    "ralph lauren": "拉夫劳伦",
    # Люкс
    "balenciaga": "巴黎世家",
    "gucci": "古驰",
    "prada": "普拉达",
    "miu miu": "缪缪",
    "louis vuitton": "路易威登",
    "lv": "路易威登",
    "chanel": "香奈儿",
    "hermes": "爱马仕",
    "dior": "迪奥",
    "bottega veneta": "葆蝶家",
    "saint laurent": "圣罗兰",
    "ysl": "圣罗兰",
    "celine": "思琳",
    "loewe": "罗意威",
    "burberry": "博柏利",
    "versace": "范思哲",
    "fendi": "芬迪",
    "givenchy": "纪梵希",
    "valentino": "华伦天奴",
    "balmain": "巴尔曼",
    "tom ford": "汤姆福特",
    "loro piana": "诺悠翩雅",
    "goyard": "戈雅",
    # Часы и украшения
    "rolex": "劳力士",
    "omega": "欧米茄",
    "cartier": "卡地亚",
    "tiffany": "蒂芙尼",
    "van cleef arpels": "梵克雅宝",
    "montblanc": "万宝龙",
}


def _normalize(text: str) -> str:
    """'Arc'teryx' -> 'arcteryx', 'The North Face' -> 'thenorthface'."""
    return re.sub(r"[^0-9a-z一-鿿]+", "", text.lower())


_INDEX = {_normalize(key): value for key, value in BRANDS_CN.items()}


def chinese_name(keyword: str) -> str | None:
    """Китайское название бренда или None, если бренда нет в списке."""
    return _INDEX.get(_normalize(keyword))
