"""
Парсер объявлений Дептранса Москвы о пересмотре парковочных тарифов.
Источник ground truth (канал Б) — см. concept_final.md, раздел 5.

Использует публичную веб-версию Telegram-канала (t.me/s/<channel>) —
это открытая HTML-страница, доступная без токена/API/регистрации,
Telegram отдаёт последние посты канала в виде статичного HTML.

Требует: pip install requests beautifulsoup4

Ограничения:
  - t.me/s/ отдаёт только последние ~20 постов без пагинации назад —
    для глубокой истории нужно либо чаще опрашивать (раз в неделю
    достаточно, публикации редкие), либо параллельно смотреть mos.ru/dt/
    и агрегаторы новостей (РИА, Ведомости) по тем же ключевым словам.
  - Извлечение "улица -> процент occupancy" сделано регулярками по
    характерным оборотам ("загруженность", "занятость", "%") — тексты
    объявлений не стандартизированы, регулярку придётся периодически
    подстраивать под реальную формулировку конкретного поста.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

CHANNEL = "deptrans"  # уточнить точный @-юзернейм канала Дептранса перед запуском
URL = f"https://t.me/s/{CHANNEL}"

OCCUPANCY_PATTERN = re.compile(
    r"(?P<street>[А-ЯЁ][\w\s\.\-]{3,60}?)"
    r"[^.]{0,40}?"
    r"(?:загруженност\w+|занятост\w+)[^.]{0,20}?"
    r"(?P<pct>\d{1,3})\s*%",
    re.IGNORECASE,
)


@dataclass
class Disclosure:
    street_name: str
    occupancy_pct: float
    post_url: str
    disclosed_at: datetime


def fetch_channel_html(url: str = URL) -> str:
    resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.text


def parse_disclosures(html: str) -> list[Disclosure]:
    soup = BeautifulSoup(html, "html.parser")
    disclosures = []

    for post in soup.select(".tgme_widget_message"):
        text_el = post.select_one(".tgme_widget_message_text")
        if not text_el:
            continue
        text = text_el.get_text(separator=" ")

        time_el = post.select_one("time")
        ts = None
        if time_el and time_el.get("datetime"):
            ts = datetime.fromisoformat(time_el["datetime"])
        else:
            ts = datetime.now(timezone.utc)

        link_el = post.select_one("a.tgme_widget_message_date")
        post_url = link_el["href"] if link_el else URL

        for match in OCCUPANCY_PATTERN.finditer(text):
            disclosures.append(Disclosure(
                street_name=match.group("street").strip(),
                occupancy_pct=float(match.group("pct")),
                post_url=post_url,
                disclosed_at=ts,
            ))

    return disclosures


def main():
    html = fetch_channel_html()
    found = parse_disclosures(html)
    print(f"Найдено потенциальных раскрытий occupancy: {len(found)}")
    for d in found:
        print(f"  {d.disclosed_at:%Y-%m-%d} — {d.street_name}: {d.occupancy_pct}%  ({d.post_url})")

    # TODO: писать найденное в таблицу deptrans_disclosures (models.py) и
    # сопоставлять street_name с zone_id через нечёткое сравнение названий
    # (fuzzywuzzy/rapidfuzz) с датасетом парковочных зон.


if __name__ == "__main__":
    main()
