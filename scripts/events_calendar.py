"""
Календарь городских мероприятий (data.mos.ru) — событие рядом повышает
спрос на парковку в этой зоне. Реальный ID датасета афиш пока НЕ подтверждён —
в отличие от парковок (ID 623, подтверждён на реальном запросе), здесь
я специально не стал вписывать угаданный номер: прошлый раз с 2361 vs
623 показал, что гадать дороже, чем спросить.

Использование — сначала находим датасет:
    export MOS_DATA_API_KEY=...
    python events_calendar.py --find

Это выведет список датасетов, в названии которых есть "афиша"/"мероприят" —
нужно найти подходящий и его Id. Дальше вписать найденный ID в EVENTS_DATASET_ID
ниже и запускать:
    python events_calendar.py
"""

import argparse
import json
import os

import requests

MOS_DATA_API_KEY = os.environ.get("MOS_DATA_API_KEY", "")
BASE_URL = "https://apidata.mos.ru/v1"

EVENTS_DATASET_ID = None  # <- вписать сюда после --find, сейчас не подтверждён

SEARCH_KEYWORDS = ["афиш", "мероприят", "событ"]


def find_candidate_datasets(api_key: str) -> list[dict]:
    """Ищет датасеты по ключевым словам в названии — тот же приём, которым
    мы в итоге нашли реальный ID парковочного датасета (623)."""
    url = f"{BASE_URL}/datasets"
    resp = requests.get(url, params={"api_key": api_key, "$top": 2000}, timeout=30)
    resp.raise_for_status()
    all_datasets = resp.json()

    matches = []
    for ds in all_datasets:
        caption = (ds.get("Caption") or "").lower()
        if any(kw in caption for kw in SEARCH_KEYWORDS):
            matches.append({"Id": ds.get("Id"), "Caption": ds.get("Caption")})
    return matches


def fetch_events_near(lat: float, lon: float, radius_m: int = 1000) -> list[dict]:
    """Заглушка на будущее — как только EVENTS_DATASET_ID подтверждён,
    здесь будет реальный запрос с фильтром по bbox вокруг зоны."""
    if EVENTS_DATASET_ID is None:
        raise RuntimeError("EVENTS_DATASET_ID не подтверждён — сначала запусти --find")
    url = f"{BASE_URL}/datasets/{EVENTS_DATASET_ID}/features"
    resp = requests.get(url, params={"api_key": MOS_DATA_API_KEY, "$top": 100}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("features", [])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--find", action="store_true", help="Найти кандидатов датасета афиш")
    args = parser.parse_args()

    if not MOS_DATA_API_KEY:
        print("[WARN] MOS_DATA_API_KEY не задан.")
        return

    if args.find:
        candidates = find_candidate_datasets(MOS_DATA_API_KEY)
        print(f"Найдено кандидатов: {len(candidates)}")
        for c in candidates:
            print(f"  Id={c['Id']}: {c['Caption']}")
        print("\nВпиши подходящий Id в EVENTS_DATASET_ID в этом файле и запусти без --find.")
    else:
        print("Пока не запускалось с --find — сначала найди реальный ID датасета.")


if __name__ == "__main__":
    main()
