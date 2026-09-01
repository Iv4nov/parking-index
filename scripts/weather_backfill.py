"""
Разовый backfill исторической почасовой погоды для Москвы (центр) через
Open-Meteo Historical Weather API — бесплатно, без API-ключа, данные с 1940 года.

Использование:
  python weather_backfill.py --start 2019-01-01 --end 2026-07-28

Результат: пишет почасовые строки в ту же SQLite-базу, что и collector.py
(таблица weather_hourly_history), плюс дублирует в CSV для удобства анализа.
"""

import argparse
import sqlite3

import requests

MOSCOW_CENTER = (55.7520, 37.6175)  # Кремль — прокси для "погоды в центре"
DB_PATH = "parking_history.sqlite3"
CSV_PATH = "weather_history_backfill.csv"

HOURLY_VARS = [
    "temperature_2m",
    "precipitation",
    "snowfall",
    "wind_speed_10m",
    "weather_code",
    "relative_humidity_2m",
]


def fetch_historical(lat: float, lon: float, start: str, end: str) -> dict:
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start,
        "end_date": end,
        "hourly": ",".join(HOURLY_VARS),
        "timezone": "Europe/Moscow",
    }
    resp = requests.get(url, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()["hourly"]


def init_table(conn: sqlite3.Connection):
    cols = ", ".join(f"{v} REAL" for v in HOURLY_VARS)
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS weather_hourly_history (ts TEXT PRIMARY KEY, {cols})"
    )
    conn.commit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()

    print(f"Запрашиваю историю погоды {args.start} .. {args.end} у Open-Meteo...")
    hourly = fetch_historical(*MOSCOW_CENTER, args.start, args.end)
    timestamps = hourly["time"]
    n = len(timestamps)
    print(f"Получено {n} почасовых записей.")

    conn = sqlite3.connect(DB_PATH)
    init_table(conn)

    rows = []
    for i in range(n):
        row = [timestamps[i]] + [hourly[v][i] for v in HOURLY_VARS]
        rows.append(row)

    placeholders = ", ".join(["?"] * (len(HOURLY_VARS) + 1))
    conn.executemany(
        f"INSERT OR REPLACE INTO weather_hourly_history VALUES ({placeholders})", rows
    )
    conn.commit()
    print(f"Записано в {DB_PATH}, таблица weather_hourly_history.")

    # Дублируем в CSV — удобно для быстрого EDA без похода в SQLite.
    import csv

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ts"] + HOURLY_VARS)
        writer.writerows(rows)
    print(f"Сохранено: {CSV_PATH}")


if __name__ == "__main__":
    main()
