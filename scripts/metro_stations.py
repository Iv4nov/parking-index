"""
Список станций метро с координатами — источник: публичный API HeadHunter
(api.hh.ru/metro/1 — идентификатор 1 это Москва в их справочнике городов),
бесплатно, без ключа, без регистрации. Используется как единственный
надёжный бесплатный источник координат станций — вручную вбивать 200+
координат по памяти рискованно (гарантированные ошибки), лучше взять
готовый проверенный список.

Использование:
    python metro_stations.py
Результат: metro_stations.json — плоский список [{"name", "lat", "lon"}, ...]
"""

import json

import requests

OUTPUT_FILE = "metro_stations.json"


def fetch_moscow_metro() -> list[dict]:
    resp = requests.get("https://api.hh.ru/metro/1", timeout=15)
    resp.raise_for_status()
    data = resp.json()

    stations = []
    for line in data.get("lines", []):
        for station in line.get("stations", []):
            # Источники расходятся в точном имени полей координат у этого API
            # (lat/lng либо latitude/longitude) — проверяем оба варианта,
            # чтобы не упасть из-за неточной документации.
            lat = station.get("lat", station.get("latitude"))
            lon = station.get("lng", station.get("longitude"))
            if lat is None or lon is None:
                continue
            stations.append({
                "name": station.get("name"),
                "line": line.get("name"),
                "lat": lat,
                "lon": lon,
            })
    return stations


def main():
    stations = fetch_moscow_metro()
    print(f"Получено станций: {len(stations)}")

    if not stations:
        print("[WARN] 0 станций — похоже, поля координат называются иначе, чем ожидалось.")
        print("Смотрю сырую структуру первой станции для диагностики:")
        resp = requests.get("https://api.hh.ru/metro/1", timeout=15)
        raw = resp.json()
        if raw.get("lines") and raw["lines"][0].get("stations"):
            print(json.dumps(raw["lines"][0]["stations"][0], ensure_ascii=False, indent=2))
        return

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(stations, f, ensure_ascii=False, indent=2)
    print(f"Сохранено: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
