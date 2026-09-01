"""
!!! НЕ ИСПОЛЬЗОВАТЬ В ТЕКУЩЕМ ВИДЕ !!!

Проверил официальные условия использования API Яндекс.Карт (yandex.ru/dev/commercial/doc/ru/):
пункт условий бесплатного использования прямо запрещает "использовать API
для мониторинга и диспетчеризации" — а периодический опрос балла пробок
для накопления истории это и есть мониторинг. Плюс с апреля 2025 бесплатные
лимиты JS API пересмотрены по каждому продукту отдельно.

Этот файл оставлен как рабочий пример техники (headless-браузер + чтение
внутреннего состояния виджета), но как источник трафика для проекта —
не подходит ни по деньгам, ни по ToS. В index_calc.py вес traffic_score
сейчас обнулён. Возможные легальные замены на будущее: бесплатный тариф
TomTom Traffic API (нужно проверить реальное покрытие Москвы), либо
собственные GPS-данные пользователей приложения во время активной поездки
(это не сторонний API — данные, которые генерирует наш же продукт).

БЕСПЛАТНЫЙ вариант сбора истории пробок — без Yandex Router API (платный).

Идея: Яндекс.Карты JS API бесплатны при некоммерческом использовании и
нагрузке до ~25 000 загрузок карты в сутки (актуальный лимит уточнить в
текущих условиях API Яндекс.Карт — они периодически меняются). У JS API есть
публичный "балл пробок" (traffic.provider.Actual -> state.get('level')),
который обычно доступен только в браузере. Чтобы забирать его сервером —
поднимаем headless-браузер (Playwright), открываем локальную HTML-страницу
с картой, и читаем значение балла каждые 5 минут.

Требуется:
  pip install playwright
  playwright install chromium

  export YANDEX_JSAPI_KEY=...   # бесплатный ключ: https://developer.tech.yandex.ru/
                                 # (регистрация, без оплаты — но проверьте
                                 # актуальные условия использования JS API
                                 # для вашего масштаба нагрузки)

Ограничения, которые стоит держать в голове:
  - Балл пробок — это ОДНО число на весь видимый регион карты (0–10), а не
    почасовая скорость по конкретной улице. Чтобы получить разные сигналы
    для разных частей Садового кольца, придётся открывать несколько
    "карт"-инстансов с разным центром (в скрипте ниже — по одной точке на
    каждый условный сектор кольца).
  - Это неофициальный способ читать внутреннее состояние виджета — Яндекс
    не гарантирует стабильность API `state.get('level')` между версиями.
    Стоит на старте руками проверить в браузере (F12 → Console), что вызов
    отрабатывает, прежде чем автоматизировать.
"""

import asyncio
import os
import sqlite3
from datetime import datetime, timezone

from playwright.async_api import async_playwright

YANDEX_JSAPI_KEY = os.environ.get("YANDEX_JSAPI_KEY", "")
DB_PATH = "parking_history.sqlite3"
POLL_INTERVAL_SECONDS = 5 * 60

# Условные центры секторов Садового кольца — точки, для которых снимаем
# отдельный "балл пробок". Уточнить/расширить после выбора реальных
# парковочных зон из parking_zones.py.
SECTORS = [
    ("sever", 55.7658, 37.6096),
    ("zapad", 55.7595, 37.5930),
    ("vostok", 55.7616, 37.6389),
    ("centr", 55.7539, 37.6208),
]

HTML_TEMPLATE = """
<!DOCTYPE html>
<html><head>
<script src="https://api-maps.yandex.ru/2.1/?apikey={api_key}&lang=ru_RU"></script>
</head><body>
<div id="map" style="width:400px;height:400px;"></div>
<script>
var trafficLevel = null;
ymaps.ready(function () {{
    var map = new ymaps.Map("map", {{ center: [{lat}, {lon}], zoom: 12 }});
    var trafficControl = new ymaps.control.TrafficControl({{ state: {{ providerKey: 'traffic#actual' }} }});
    map.controls.add(trafficControl);
    trafficControl.getProvider('traffic#actual').then(function (provider) {{
        provider.state.events.add('change', function () {{
            trafficLevel = provider.state.get('level');
            window.__trafficLevel = trafficLevel;
        }});
    }});
}});
</script>
</body></html>
"""


def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS traffic_score_log (
            ts TEXT NOT NULL,
            sector TEXT NOT NULL,
            level INTEGER
        )
        """
    )
    conn.commit()
    return conn


async def read_traffic_level(page, lat: float, lon: float) -> int | None:
    html = HTML_TEMPLATE.format(api_key=YANDEX_JSAPI_KEY, lat=lat, lon=lon)
    await page.set_content(html)
    # Даём карте и провайдеру пробок время загрузиться и получить первое значение.
    await page.wait_for_timeout(6000)
    level = await page.evaluate("window.__trafficLevel")
    return level


async def poll_once(conn: sqlite3.Connection):
    ts = datetime.now(timezone.utc).isoformat()
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        for name, lat, lon in SECTORS:
            try:
                level = await read_traffic_level(page, lat, lon)
                conn.execute(
                    "INSERT INTO traffic_score_log VALUES (?, ?, ?)", (ts, name, level)
                )
            except Exception as e:
                print(f"[ERROR] {name}: {e}")
        await browser.close()
    conn.commit()


async def main():
    if not YANDEX_JSAPI_KEY:
        print("[WARN] YANDEX_JSAPI_KEY не задан — получите бесплатный ключ на developer.tech.yandex.ru")
        return
    conn = init_db()
    print(f"Старт бесплатного сбора баллов пробок. Интервал: {POLL_INTERVAL_SECONDS}с.")
    while True:
        await poll_once(conn)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
