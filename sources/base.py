"""
Общий «шаблон» для всех площадок.

Каждая площадка (Goofish, 95分 и любые будущие) — это класс-наследник Source,
который умеет одно: по ключевому слову вернуть список объявлений (Listing).
Благодаря этому монитору всё равно, откуда пришли данные, —
новую площадку можно добавить, не трогая остальной код.
"""

from dataclasses import dataclass, field


@dataclass
class Listing:
    source: str                  # код площадки: "goofish", "fen95"
    id: str                      # уникальный id объявления на площадке
    title: str                   # заголовок
    url: str                     # ссылка на объявление
    price: float | None = None   # цена (число)
    currency: str = "CNY"        # валюта цены
    image: str | None = None     # ссылка на фото
    city: str | None = None      # город продавца
    seller: str | None = None    # имя продавца
    posted_at: str | None = None # когда опубликовано (как отдала площадка)
    extra: dict = field(default_factory=dict)  # всё остальное, на будущее


class Source:
    """Базовый класс площадки."""

    name: str = "base"          # короткий код (хранится в базе)
    title: str = "Base"         # красивое название для сообщений
    enabled: bool = False       # участвует ли площадка в мониторинге

    async def search(
        self,
        keyword: str,
        max_items: int,
        price_min: float | None = None,
        price_max: float | None = None,
    ) -> list[Listing]:
        """
        Вернуть самые свежие объявления по запросу.
        price_min / price_max — фильтр цены в юанях. Если площадка умеет
        фильтровать сама — передай их ей (так дешевле). Если не умеет —
        можно игнорировать: монитор всё равно проверит цену сам.
        """
        raise NotImplementedError

    async def close(self) -> None:
        """Освободить ресурсы (если нужно)."""
        return None
