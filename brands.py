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

  group   — группа в каталоге (вкладка в боте и в приложении), одна из
            GROUPS ниже. Необязательное: без него бренд попадёт в «Другое».

Пробелы, точки, дефисы и надстрочные знаки в написаниях не важны:
"c.p. company", "cp company" и "CPCOMPANY" — для бота одно и то же,
как и "Hermès" и "hermes".

Осторожно с короткими написаниями (2–3 буквы): латиницу бот ищет как
отдельное слово, но «LP», «OW», «RL» всё равно встречаются в чужих
заголовках. Лучше потерять пару объявлений, чем слать мусор.

Чтобы добавить бренд в каталог — скопируй любой блок ниже и поменяй слова.
Он сразу появится в боте среди кнопок «➕ Добавить».
Чтобы урезать расходы — оставь в "search" одно слово.
"""

import re
import unicodedata

# Цена «по умолчанию» для готового набора (/preset), в юанях ¥
PRESET_PRICE_MIN = 200
PRESET_PRICE_MAX = 1800

BRANDS: dict[str, dict] = {
    "gucci": {
        "title": "Gucci",
        "group": "Люкс-дома",
        "search": ["gucci", "古驰"],
        # «GG» специально НЕ добавлен: это слишком короткое сочетание,
        # оно встречается в куче посторонних заголовков.
        "aliases": ["gucci", "古驰", "古奇", "古琦"],
        "ru": ["гуччи", "gucci"],
    },
    "cp company": {
        "title": "C.P. Company",
        "group": "Аутдор и техно",
        # Устоявшегося китайского названия нет — ищем только латиницей.
        "search": ["cp company"],
        # Голое «CP» НЕ добавлено: в китайском сленге CP = «парочка»
        # (情侣), и бот слал бы парные футболки и кружки.
        "aliases": ["cp company", "c.p. company", "cpcompany", "c.p.company"],
        "ru": ["cp company", "си пи компани", "c.p. company"],
    },
    "stone island": {
        "title": "Stone Island",
        "group": "Аутдор и техно",
        "search": ["stone island", "石头岛"],
        "aliases": ["stone island", "stoneisland", "石头岛"],
        "ru": ["стон айленд", "стоник", "stone island"],
    },
    "louis vuitton": {
        "title": "Louis Vuitton",
        "group": "Люкс-дома",
        "search": ["lv", "路易威登"],
        # 驴牌 («ослиная марка») — разговорное прозвище LV в Китае
        "aliases": ["louis vuitton", "lv", "路易威登", "驴牌"],
        "ru": ["луи виттон", "louis vuitton"],
    },
    "prada": {
        "title": "Prada",
        "group": "Люкс-дома",
        "search": ["prada", "普拉达"],
        "aliases": ["prada", "普拉达"],
        "ru": ["прада", "prada"],
    },
    "saint laurent": {
        "title": "Saint Laurent",
        "group": "Люкс-дома",
        "search": ["ysl", "圣罗兰"],
        # 杨树林 («тополиная роща») — шуточное прозвище YSL у китайцев
        "aliases": ["saint laurent", "yves saint laurent", "ysl", "圣罗兰", "杨树林"],
        "ru": ["сен лоран", "saint laurent", "ysl"],
    },
    "burberry": {
        "title": "Burberry",
        "group": "Люкс-дома",
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
        "group": "Аутдор и техно",
        "search": ["始祖鸟"],
        "aliases": ["arcteryx", "arc'teryx", "arc teryx", "始祖鸟"],
        "ru": ["арктерикс", "arcteryx"],
    },
    "moncler": {
        "title": "Moncler",
        "group": "Аутдор и техно",
        "search": ["moncler", "蒙口"],
        # 蒙口 — народное название, 盟可睐 — официальное
        "aliases": ["moncler", "蒙口", "盟可睐"],
        "ru": ["монклер", "moncler"],
    },
    "chrome hearts": {
        "title": "Chrome Hearts",
        "group": "Стритвир",
        "search": ["chrome hearts", "克罗心"],
        "aliases": ["chrome hearts", "chromehearts", "克罗心"],
        "ru": ["хром хартс", "chrome hearts"],
    },
    "balenciaga": {
        "title": "Balenciaga",
        "group": "Люкс-дома",
        "search": ["balenciaga", "巴黎世家"],
        "aliases": ["balenciaga", "巴黎世家"],
        "ru": ["баленсиага", "balenciaga"],
    },
    "maison margiela": {
        "title": "Maison Margiela",
        "group": "Японцы и авангард",
        "search": ["margiela", "马吉拉"],
        "aliases": ["maison margiela", "margiela", "mm6", "马吉拉"],
        "ru": ["маржела", "марджела", "margiela"],
    },
    "dior": {
        "title": "Dior",
        "group": "Люкс-дома",
        "search": ["dior", "迪奥"],
        "aliases": ["dior", "christian dior", "迪奥"],
        "ru": ["диор", "dior"],
    },
    "miu miu": {
        "title": "Miu Miu",
        "group": "Люкс-дома",
        "search": ["miumiu", "缪缪"],
        "aliases": ["miu miu", "miumiu", "缪缪"],
        "ru": ["миу миу", "miu miu"],
    },
    "loewe": {
        "title": "Loewe",
        "group": "Люкс-дома",
        "search": ["loewe", "罗意威"],
        "aliases": ["loewe", "罗意威"],
        "ru": ["лоэве", "loewe"],
    },
    "canada goose": {
        "title": "Canada Goose",
        "group": "Аутдор и техно",
        "search": ["加拿大鹅"],
        "aliases": ["canada goose", "canadagoose", "加拿大鹅"],
        "ru": ["канада гус", "canada goose"],
    },
    "goyard": {
        "title": "Goyard",
        "group": "Люкс-дома",
        # 狗牙 («собачьи зубы», из-за узора) — народное прозвище Goyard в Китае,
        # 戈雅 — официальное. Ищем по обоим.
        "search": ["goyard", "狗牙"],
        "aliases": ["goyard", "狗牙", "戈雅"],
        "ru": ["гоярд", "goyard"],
    },
    "tom ford": {
        "title": "Tom Ford",
        "group": "Люкс-дома",
        # «TF» специально НЕ добавлен: слишком короткое сочетание
        "search": ["tom ford", "汤姆福特"],
        "aliases": ["tom ford", "tomford", "汤姆福特"],
        "ru": ["том форд", "tom ford"],
    },
    "bottega veneta": {
        "title": "Bottega Veneta",
        "group": "Люкс-дома",
        "search": ["bottega", "葆蝶家"],
        # «BV» не добавлен: слишком короткое сочетание
        "aliases": ["bottega veneta", "bottega", "葆蝶家"],
        "ru": ["боттега", "bottega"],
    },

    # ================= Люкс-дома =================
    "chanel": {
        "title": "Chanel",
        "group": "Люкс-дома",
        "search": ["chanel", "香奈儿"],
        # «小香» / «小香风» специально НЕ добавлены: «小香风» на Goofish — это
        # просто стиль «твидовый жакет как у Шанель», так подписывают тысячи
        # вещей других марок. «CC» и «CF» (Classic Flap) — слишком короткие.
        "aliases": ["chanel", "香奈儿"],
        "ru": ["шанель", "chanel"],
    },
    "hermes": {
        "title": "Hermès",
        "group": "Люкс-дома",
        "search": ["hermes", "爱马仕"],
        # «H家» не добавлен: в написании латинская буква + иероглиф, бот
        # искал бы просто отдельную «h». «铂金包»/«凯莉包» (Birkin/Kelly) —
        # названия моделей, ими подписывают и сумки-«похожки» других марок.
        "aliases": ["hermes", "hermès", "爱马仕"],
        "ru": ["гермес", "эрмес", "hermes"],
    },
    "celine": {
        "title": "Celine",
        "group": "Люкс-дома",
        "search": ["celine", "思琳"],
        # 思琳 — официальное название, 赛琳 — народное (часто у байеров).
        # «凯旋门» (Triomphe) не добавлен: это просто «Триумфальная арка».
        "aliases": ["celine", "céline", "思琳", "赛琳"],
        "ru": ["селин", "celine"],
    },
    "fendi": {
        "title": "Fendi",
        "group": "Люкс-дома",
        "search": ["fendi", "芬迪"],
        "aliases": ["fendi", "芬迪"],
        "ru": ["фенди", "fendi"],
    },
    "valentino": {
        "title": "Valentino",
        "group": "Люкс-дома",
        "search": ["valentino", "华伦天奴"],
        # Осторожно: «Mario Valentino» и «Valentino Orlandi» — другие, дешёвые
        # марки, и они тоже пройдут проверку по слову «valentino». Отсечь их
        # без риска потерять настоящие вещи нельзя — помогает бюджет.
        # VLTN — логотип самого Valentino, Garavani — фамилия основателя.
        "aliases": ["valentino", "valentino garavani", "garavani", "vltn", "华伦天奴",
                    "瓦伦蒂诺"],  # не уверен (瓦伦蒂诺 — встречается реже)
        "ru": ["валентино", "valentino"],
    },
    "givenchy": {
        "title": "Givenchy",
        "group": "Люкс-дома",
        "search": ["givenchy", "纪梵希"],
        "aliases": ["givenchy", "纪梵希"],
        "ru": ["живанши", "givenchy"],
    },

    # ================= Тихая роскошь =================
    "loro piana": {
        "title": "Loro Piana",
        "group": "Тихая роскошь",
        "search": ["loro piana", "诺悠翩雅"],
        # «LP» специально НЕ добавлен: на Goofish так подписывают виниловые
        # пластинки (LP唱片), и бот слал бы музыку.
        "aliases": ["loro piana", "loropiana", "诺悠翩雅"],
        "ru": ["лоро пиана", "loro piana"],
    },
    "brunello cucinelli": {
        "title": "Brunello Cucinelli",
        "group": "Тихая роскошь",
        # Одно слово «cucinelli»: оно есть и в «Brunello Cucinelli», а
        # китайское название продавцы пишут редко — экономим запуск Apify.
        "search": ["cucinelli"],
        # «BC» — слишком короткое. Голое «brunello» — это ещё и вино
        # Brunello di Montalcino, поэтому только вместе с фамилией.
        # 库奇内利 — из официального 布鲁内洛·库奇内利 (полное имя тоже найдётся).
        "aliases": ["brunello cucinelli", "cucinelli", "库奇内利"],
        "ru": ["брунелло кучинелли", "кучинелли", "cucinelli"],
    },
    "zegna": {
        "title": "Zegna",
        "group": "Тихая роскошь",
        "search": ["zegna", "杰尼亚"],
        # «zegna» находит и «Ermenegildo Zegna», и «Z Zegna»
        "aliases": ["zegna", "ermenegildo zegna", "杰尼亚"],
        "ru": ["зегна", "дзегна", "zegna"],
    },
    "max mara": {
        "title": "Max Mara",
        "group": "Тихая роскошь",
        "search": ["max mara", "麦丝玛拉"],
        # «maxmara» слитно найдётся само (пробелы не важны).
        # Голое «max» и «MM» — НЕ добавлены (MM6 — это Margiela).
        # Max&Co — отдельная, более дешёвая линия, её тоже не берём.
        "aliases": ["max mara", "麦丝玛拉"],
        "ru": ["макс мара", "max mara"],
    },
    "the row": {
        "title": "The Row",
        "group": "Тихая роскошь",
        "search": ["the row"],
        # Только целиком «the row» (или слитно «therow»): голое «row» —
        # обычное английское слово (row = ряд, гребля), а «TR» слишком короткое.
        # «the row» как отдельные слова в китайских заголовках почти не
        # встречается, а «throw», «rows» сюда не попадут — бот ищет слово целиком.
        # Устоявшегося китайского названия нет — продавцы пишут латиницей.
        "aliases": ["the row"],
        "ru": ["the row"],
    },
    "ralph lauren": {
        "title": "Ralph Lauren",
        "group": "Тихая роскошь",
        "search": ["ralph lauren", "拉夫劳伦"],
        # «Polo» НЕ добавлен: «polo衫» — это просто рубашка-поло любой марки.
        # «Polo Ralph Lauren» и так найдётся по «ralph lauren».
        # «RL» — слишком короткое; «RRL» (линия Double RL) — уникальное, берём.
        # «小马标» («значок с лошадкой») не добавлен: так же подписывают
        # и U.S. Polo Assn., и вещи без бренда. «劳伦» один — это ещё и «劳伦斯».
        "aliases": ["ralph lauren", "rrl", "拉夫劳伦", "拉尔夫劳伦"],
        "ru": ["ральф лорен", "ralph lauren"],
    },

    # ================= Японцы и авангард =================
    "comme des garcons": {
        "title": "Comme des Garçons",
        "group": "Японцы и авангард",
        # Продавцы почти всегда пишут «CDG» или «川久保玲»
        "search": ["cdg", "川久保玲"],
        # «PLAY» один НЕ добавлен — это обычное слово. «CDG PLAY» и
        # «Comme des Garçons PLAY» найдутся по «cdg» / полному названию.
        # «Homme Plus» — название линии CDG, другим маркам не встречается.
        "aliases": ["comme des garcons", "comme des garçons", "cdg", "homme plus", "川久保玲"],
        "ru": ["комм де гарсон", "комме де гарсон", "comme des garcons", "cdg"],
    },
    "yohji yamamoto": {
        "title": "Yohji Yamamoto",
        "group": "Японцы и авангард",
        "search": ["yohji", "山本耀司"],
        # «山本» / «耀司» / «山本风» НЕ добавлены: «山本风» = «в стиле Ямамото»,
        # так подписывают чёрные балахоны без бренда. «yamamoto» один —
        # частая японская фамилия. «Y's» — слишком короткое («ys»).
        # Y-3 НЕ добавлен: это совместная линия с adidas, её чаще продают
        # как кроссовки adidas («Y-3 adidas»), а не как Yohji, и цены другие.
        # Если в заголовке написано «Y-3 山本耀司» — вещь всё равно найдётся.
        # Ground Y — диффузная линия самого Yohji.
        "aliases": ["yohji yamamoto", "yohji", "ground y", "山本耀司"],
        "ru": ["йоджи ямамото", "ёджи ямамото", "yohji"],
    },
    "issey miyake": {
        "title": "Issey Miyake",
        "group": "Японцы и авангард",
        "search": ["三宅一生", "issey miyake"],
        # Линии Pleats Please, Homme Plissé и Bao Bao продавцы подписывают
        # своими названиями — считаем их тем же брендом.
        # «三宅» / «三宅风» НЕ добавлены: «三宅风» = «плиссе в стиле Мияке»,
        # так подписывают дешёвые вещи без бренда. «miyake» одна — фамилия.
        # «bao bao»: в теории это ещё и пиньинь «宝宝» (малыш), но в выдаче
        # по «三宅一生» латиницей так почти не пишут — риск маленький.
        "aliases": ["issey miyake", "issey", "pleats please", "homme plisse", "homme plissé",
                    "bao bao", "三宅一生"],
        "ru": ["иссей мияке", "issey miyake", "pleats please"],
    },
    "rick owens": {
        "title": "Rick Owens",
        "group": "Японцы и авангард",
        # Почти все продавцы пишут латиницей — одного запроса хватает
        "search": ["rick owens"],
        # «RO» — слишком короткое. «owens» / «rick» по отдельности — обычные
        # имя и фамилия. DRKSHDW — линия самого Rick Owens.
        "aliases": ["rick owens", "drkshdw", "瑞克欧文斯"],
        "ru": ["рик оуэнс", "рик овенс", "rick owens"],
    },
    "kapital": {
        "title": "Kapital",
        "group": "Японцы и авангард",
        "search": ["kapital"],
        # «kapital» — само название бренда, другого написания нет. Слово
        # проверяется целиком, так что «kapitalism» не пройдёт. Книга
        # «Das Kapital» на Goofish подписана как «资本论», её мы не увидим.
        "aliases": ["kapital",
                    "卡皮塔尔"],  # не уверен (продавцы чаще пишут латиницей)
        # «капитал» по-русски НЕ берём: на Авито это «капитальный ремонт» и т. п.
        "ru": ["kapital"],
    },
    "sacai": {
        "title": "Sacai",
        "group": "Японцы и авангард",
        "search": ["sacai"],
        # Коллаборации «Nike x sacai» тоже найдутся — это вещи Sacai
        "aliases": ["sacai",
                    "萨卡伊"],  # не уверен (обычно пишут латиницей)
        "ru": ["сакаи", "sacai"],
    },
    # ================= Стритвир =================
    "off white": {
        "title": "Off-White",
        "group": "Стритвир",
        "search": ["off white"],
        # «OW» — слишком короткое. «off white» — это ещё и цвет «молочный»,
        # но в китайских заголовках цвет пишут иероглифами (米白), так что риск
        # маленький. «Virgil Abloh» не добавлен: он же был дизайнером Louis Vuitton.
        "aliases": ["off white"],
        "ru": ["офф вайт", "оф вайт", "off white"],
    },
    "palm angels": {
        "title": "Palm Angels",
        "group": "Стритвир",
        "search": ["palm angels"],
        # «PA» — слишком короткое
        "aliases": ["palm angels",
                    "棕榈天使"],  # не уверен (дословный перевод, встречается редко)
        "ru": ["палм энджелс", "palm angels"],
    },
    "acne studios": {
        "title": "Acne Studios",
        "group": "Стритвир",
        "search": ["acne studios"],
        # «acne» одно добавлено: продавцы часто пишут просто «Acne 围巾».
        # Риск — косметика «от акне», но ищем мы по «acne studios», и такие
        # товары в выдачу почти не попадают. По-русски «акне» НЕ берём.
        "aliases": ["acne studios", "acne"],
        "ru": ["акне студиос", "acne studios"],
    },
    "thom browne": {
        "title": "Thom Browne",
        "group": "Стритвир",
        "search": ["thom browne", "汤姆布朗"],
        # «TB» — слишком короткое. «四条杠» (4 полоски) — просто узор.
        "aliases": ["thom browne", "汤姆布朗"],
        "ru": ["том браун", "thom browne"],
    },
    "alexander mcqueen": {
        "title": "Alexander McQueen",
        "group": "Стритвир",
        # По-китайски бренд почти всегда пишут коротко «麦昆» (麦昆小白鞋)
        "search": ["mcqueen", "麦昆"],
        # «闪电麦昆» (машинка из мультика «Тачки») отсекается в EXCLUDE_WORDS.
        # «McQ» и «AMQ» не добавлены — слишком короткие.
        "aliases": ["alexander mcqueen", "mcqueen", "麦昆"],
        "ru": ["александр маккуин", "маккуин", "mcqueen"],
    },
    "golden goose": {
        "title": "Golden Goose",
        "group": "Стритвир",
        "search": ["golden goose"],
        # GGDB = Golden Goose Deluxe Brand — так часто пишут продавцы.
        # «脏脏鞋» («грязные кеды») не добавлен: так зовут любые «состаренные»
        # кеды, в том числе копии и no-name.
        "aliases": ["golden goose", "ggdb",
                    "金鹅"],  # не уверен (дословный перевод)
        "ru": ["голден гус", "golden goose"],
    },
}

# Бренды, которые добавляет «Готовый набор» (в таком порядке)
PRESET: list[str] = [
    "gucci", "cp company", "stone island", "louis vuitton", "prada", "saint laurent", "burberry",
    "goyard", "tom ford", "dior",
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
    # «в стиле …» — так подписывают вещи без бренда «под» японских дизайнеров.
    # Настоящую вещь продавец так не назовёт. («…风» без «格» не берём:
    # «山本耀司风衣» — это настоящий тренч Yohji.)
    "三宅一生风格",
    "山本耀司风格",
    "川久保玲风格",
    "闪电麦昆",  # не подделка, а игрушка «Молния МакКуин» из «Тачек» — не Alexander McQueen
]

# Группы каталога — в таком порядке они идут вкладками в боте и в приложении.
# Название группы бренда — поле "group" в его блоке выше.
GROUPS: list[str] = ["Люкс-дома", "Тихая роскошь", "Японцы и авангард", "Стритвир", "Аутдор и техно"]


# ----------------------------------------------------------------------
# Дальше — служебный код, менять не нужно
# ----------------------------------------------------------------------

def _fold(text: str) -> str:
    """Нижний регистр и без надстрочных знаков: 'Hermès' -> 'hermes', 'Garçons' -> 'garcons'."""
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _normalize(text: str) -> str:
    """'C.P. Company' -> 'cpcompany', '古驰 Gucci' -> '古驰gucci', 'Hermès' -> 'hermes'."""
    return re.sub(r"[^0-9a-z一-鿿]+", "", _fold(text))


_SEPARATOR = r"[\s.\-_·'’&]*"


def _alias_regex(alias: str) -> re.Pattern:
    """
    Латинское написание ищем как отдельное слово, допуская пробелы, точки
    и дефисы внутри: 'lv' найдётся в 'LV包' и 'L.V', но НЕ в 'silver'.
    Китайское — как подстроку (в китайском нет пробелов), но тоже допускаем
    точку-разделитель внутри: '拉夫劳伦' найдётся и в '拉夫·劳伦'.
    """
    alias = _fold(alias).strip()
    if re.search(r"[a-z]", alias):
        compact = re.sub(r"[^a-z0-9]", "", alias)
        body = _SEPARATOR.join(re.escape(ch) for ch in compact)
        return re.compile(rf"(?<![a-z]){body}(?![a-z])")
    compact = re.sub(r"[\s.\-_·'’&]+", "", alias)
    return re.compile(_SEPARATOR.join(re.escape(ch) for ch in compact))


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
    Незнакомые слова приводятся к одному виду: нижний регистр, без лишних
    пробелов и знаков вокруг слов, «ё» -> «е».
    """
    keyword = keyword.strip().lower()
    found = _INDEX.get(_normalize(keyword))
    if found:
        return found
    # Свой бренд: одно и то же написание у всех, чтобы одинаковые бренды
    # разных людей проверялись одним общим запросом (и стоили дешевле):
    # 'RICK  Owens!' -> 'rick owens'. Знаки внутри названия (Y-3, A.P.C., D&G)
    # не трогаем — по ним ищет площадка.
    keyword = keyword.replace("ё", "е")
    keyword = re.sub(r"[!?,;:\"«»()\[\]{}*#@~^|/\\`´’]+", " ", keyword)
    keyword = re.sub(r"\s+", " ", keyword)
    return keyword.strip(" -_.&+'")


def get_brand(keyword: str) -> dict | None:
    """Описание бренда из списка выше или None, если бренд незнакомый."""
    return BRANDS.get(canonical(keyword))


def display_name(keyword: str) -> str:
    brand = get_brand(keyword)
    return brand["title"] if brand else keyword


def is_catalog(keyword: str) -> bool:
    """Бренд из каталога HUNTR (а не «свой»)?"""
    return get_brand(keyword) is not None


def catalog_groups() -> list[tuple[str, list[str]]]:
    """
    Каталог по группам для экрана выбора бренда:
    [('Люкс-дома', ['gucci', 'louis vuitton', ...]), ...].
    Группы идут в порядке GROUPS, бренды — в порядке BRANDS. Бренд без
    "group" (или с группой не из GROUPS) попадает в «Другое» в конце.
    """
    by_group: dict[str, list[str]] = {name: [] for name in GROUPS}
    for key, brand in BRANDS.items():
        name = brand.get("group")
        by_group.setdefault(name if name in by_group else "Другое", []).append(key)
    return [(name, keys) for name, keys in by_group.items() if keys]


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
    title = _fold(title)
    return any(p.search(title) for p in patterns)


def listing_ok(keyword: str, title: str) -> bool:
    """Главная проверка: и бренд наш, и не подделка."""
    return mentions_brand(keyword, title) and not is_fake(title)
