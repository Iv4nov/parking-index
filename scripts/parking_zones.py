"""
Выгрузка платных парковок (датасет 623, data.mos.ru) и фильтрация по
пилотной зоне (Садовое кольцо).

ПОДТВЕРЖДЕНО НА РЕАЛЬНОМ ЗАПРОСЕ (2026-07-28): при обычном GET без
дополнительных параметров API отдаёт JSON, не XML. Структура:

{
  "features": [
    {
      "geometry": {"type": "MultiLineString", "coordinates": [[[lon,lat],[lon,lat],...]]},
      "properties": {
        "datasetId": 623, "rowId": null,
        "attributes": {
          "ID": ..., "ParkingName": "...", "ParkingZoneNumber": "...",
          "AdmArea": "...", "District": "...", "Address": "...",
          "CarCapacity": ..., "CarCapacityDisabled": ...,
          "Tariffs": [{"TariffPeriod": "будни", "VehicleTypeForThisTariff":
                       "Легковой автомобиль", "HourPrice": 40, ...}, ...],
          "global_id": ...
        }
      }
    }, ...
  ]
}

Использование:
    export MOS_DATA_API_KEY=...      (Windows cmd: set MOS_DATA_API_KEY=...)
    python parking_zones.py
Результат: parking_zones_sadovoe.geojson — собственный чистый GeoJSON
(Point-геометрия = центроид улицы), уже отфильтрованный по пилоту.
"""

import json
import math
import os
from dataclasses import dataclass

import requests

MOS_DATA_API_KEY = os.environ.get("MOS_DATA_API_KEY", "")
BASE_URL = "https://apidata.mos.ru/v1"
PARKING_DATASET_ID = 623  # подтверждено: data.mos.ru/opendata/623/map

MOSCOW_CENTER_LAT, MOSCOW_CENTER_LON = 55.7520, 37.6175  # Кремль

# Садовое кольцо — приближение эллипсом, не кругом (кольцо заметно вытянуто).
# Полуоси выведены из известной длины кольца (~15.6 км) через формулу
# периметра эллипса Рамануджана при предполагаемом соотношении осей 1.4:1.
# Это оценка, не трассировка реального полигона — если понадобится точная
# граница, её нужно either отследить вручную на карте и передать сюда явно,
# либо получить через Overpass API (не Nominatim — тот запрещает
# автоматический доступ по robots.txt).
PILOT_SEMI_AXIS_EW_M = 2880  # восток-запад
PILOT_SEMI_AXIS_NS_M = 2060  # север-юг
METERS_PER_DEG_LAT = 111_320
METERS_PER_DEG_LON = METERS_PER_DEG_LAT * math.cos(math.radians(MOSCOW_CENTER_LAT))

OUTPUT_FILE = "parking_zones_sadovoe.geojson"
PAGE_SIZE = 100  # уменьшено с 500 — похоже, у API есть лимит на размер страницы


@dataclass
class ParkingSegment:
    global_id: str
    name: str
    zone_number: str
    adm_area: str
    district: str
    address: str
    car_capacity: int
    car_capacity_disabled: int
    tariff_weekday_car_rub: float
    lat: float
    lon: float
    geometry: dict  # исходная геометрия (LineString/MultiLineString) — для отрисовки вдоль улицы


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


def in_pilot_ellipse(lat: float, lon: float) -> bool:
    """Проверка попадания в эллипс, приближающий Садовое кольцо (см. пояснение
    у констант PILOT_SEMI_AXIS_*). Проекция в локальные метры вокруг центра —
    простая и достаточно точная на таком масштабе (несколько км)."""
    dx = (lon - MOSCOW_CENTER_LON) * METERS_PER_DEG_LON
    dy = (lat - MOSCOW_CENTER_LAT) * METERS_PER_DEG_LAT
    return (dx / PILOT_SEMI_AXIS_EW_M) ** 2 + (dy / PILOT_SEMI_AXIS_NS_M) ** 2 <= 1.0


def extract_centroid(geometry: dict) -> tuple[float, float]:
    """Возвращает (lat, lon) — среднее по всем точкам MultiLineString/LineString/Point."""
    coords = geometry["coordinates"]
    gtype = geometry["type"]

    points = []
    if gtype == "Point":
        points = [coords]
    elif gtype == "LineString":
        points = coords
    elif gtype == "MultiLineString":
        for line in coords:
            points.extend(line)
    elif gtype == "Polygon":
        points = coords[0]
    else:
        raise ValueError(f"Неожиданный тип геометрии: {gtype}")

    if not points:
        raise ValueError("Пустые координаты")

    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return sum(lats) / len(lats), sum(lons) / len(lons)


def extract_weekday_car_tariff(attrs: dict) -> float:
    """Ищет среди Tariffs запись 'будни' + 'Легковой автомобиль', берёт HourPrice.
    0.0, если такой записи нет (по факту частая ситуация — многие места
    бесплатны по выходным/праздникам, это само по себе сигнал)."""
    for tariff in attrs.get("Tariffs") or []:
        if (
            tariff.get("TariffPeriod") == "будни"
            and tariff.get("VehicleTypeForThisTariff") == "Легковой автомобиль"
        ):
            try:
                return float(tariff.get("HourPrice") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def parse_feature(feature: dict) -> ParkingSegment | None:
    geometry = feature.get("geometry")
    properties = feature.get("properties")
    if not geometry or not properties:
        return None
    attrs = properties.get("attributes")
    if not attrs:
        return None

    try:
        lat, lon = extract_centroid(geometry)
    except (ValueError, KeyError, IndexError):
        return None

    return ParkingSegment(
        global_id=str(attrs.get("global_id") or attrs.get("ID") or ""),
        name=attrs.get("ParkingName") or "Без названия",
        zone_number=str(attrs.get("ParkingZoneNumber") or ""),
        adm_area=attrs.get("AdmArea") or "",
        district=attrs.get("District") or "",
        address=attrs.get("Address") or "",
        car_capacity=int(attrs.get("CarCapacity") or 0),
        car_capacity_disabled=int(attrs.get("CarCapacityDisabled") or 0),
        tariff_weekday_car_rub=extract_weekday_car_tariff(attrs),
        lat=lat,
        lon=lon,
        geometry=geometry,
    )


def fetch_page(dataset_id: int, api_key: str, skip: int, top: int) -> dict:
    url = f"{BASE_URL}/datasets/{dataset_id}/features"
    params = {"api_key": api_key, "$skip": skip, "$top": top}
    resp = requests.get(url, params=params, timeout=30)
    if not resp.ok:
        print(f"[HTTP {resp.status_code}] тело ответа: {resp.text[:500]}")
    resp.raise_for_status()
    return resp.json()


def fetch_all_segments(dataset_id: int, api_key: str) -> list[ParkingSegment]:
    all_segments = []
    skip = 1  # подтверждено реальной ошибкой API: "$skip не может принимать значение меньше 1"
    while True:
        data = fetch_page(dataset_id, api_key, skip, PAGE_SIZE)
        features = data.get("features") or []
        page_segments = [s for f in features if (s := parse_feature(f)) is not None]
        if not page_segments:
            break
        all_segments.extend(page_segments)
        print(f"  ...получено {len(all_segments)}")
        if len(features) < PAGE_SIZE:
            break
        skip += PAGE_SIZE
    return all_segments


def to_geojson(segments: list[ParkingSegment]) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": s.geometry,  # реальная линия вдоль улицы — как в Парковках России
                "properties": {
                    "id": s.global_id,
                    "name": s.name,
                    "zone_number": s.zone_number,
                    "adm_area": s.adm_area,
                    "district": s.district,
                    "address": s.address,
                    "car_capacity": s.car_capacity,
                    "car_capacity_disabled": s.car_capacity_disabled,
                    "tariff_weekday_car_rub": s.tariff_weekday_car_rub,
                    "centroid_lat": s.lat,  # для расчёта близости к центру и т.п. в бэкенде
                    "centroid_lon": s.lon,
                },
            }
            for s in segments
        ],
    }


def main():
    if not MOS_DATA_API_KEY:
        print("[WARN] MOS_DATA_API_KEY не задан.")
        return

    print(f"Выгружаю датасет {PARKING_DATASET_ID}...")
    segments = fetch_all_segments(PARKING_DATASET_ID, MOS_DATA_API_KEY)
    print(f"Получено объектов всего: {len(segments)}")

    pilot = [s for s in segments if in_pilot_ellipse(s.lat, s.lon)]
    print(f"Внутри пилотной зоны (эллипс Садового кольца): {len(pilot)}")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(to_geojson(pilot), f, ensure_ascii=False, indent=2)
    print(f"Сохранено: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
