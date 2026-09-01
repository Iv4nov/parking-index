"""
Загружает зоны из parking_zones_sadovoe.geojson (выход collectors/parking_zones.py)
в таблицу zones. Геометрия — реальная линия участка улицы (не точка), сохраняется
как есть для отрисовки на карте вдоль дороги (как в «Парковках России»).
Дополнительно считает и сохраняет: близость к центру пилота (сырое расстояние,
для экспоненциального затухания в index_calc.py), давление по вместимости
(устойчивое к выбросам, 90-й процентиль) и расстояние до ближайшей станции
метро (если рядом лежит metro_stations.json — см. collectors/metro_stations.py).

Использование:
    python seed_zones.py ../collectors/parking_zones_sadovoe.geojson
"""

import json
import math
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api", "lib"))
from models import Base, Zone

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./parking.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
MOSCOW_CENTER_LAT, MOSCOW_CENTER_LON = 55.7520, 37.6175
METRO_STATIONS_FILE = os.path.join(os.path.dirname(__file__), "..", "api", "metro_stations.json")


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


def percentile(values: list, pct: float) -> float:
    """Процентиль без numpy — устойчивее к выбросам, чем max(), для нормализации
    давления по вместимости (см. пояснение у capacity_cap ниже)."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def load_metro_stations() -> list[dict]:
    if not os.path.exists(METRO_STATIONS_FILE):
        print(f"[WARN] {METRO_STATIONS_FILE} не найден — расстояние до метро не будет посчитано. "
              f"Запусти collectors/metro_stations.py, чтобы его получить.")
        return []
    with open(METRO_STATIONS_FILE, encoding="utf-8") as f:
        return json.load(f)


def distance_to_nearest_metro_m(lat: float, lon: float, stations: list[dict]) -> float | None:
    if not stations:
        return None
    return min(haversine_m(lat, lon, s["lat"], s["lon"]) for s in stations)


def main(geojson_path: str):
    with open(geojson_path, encoding="utf-8") as f:
        data = json.load(f)

    features = data["features"]
    tariffs = [f["properties"]["tariff_weekday_car_rub"] for f in features]
    max_tariff = max(tariffs) if tariffs else 1.0
    max_tariff = max_tariff or 1.0  # избежать деления на 0 если весь пилот бесплатный

    metro_stations = load_metro_stations()

    distances = []
    capacities = []
    metro_distances = []
    for feat in features:
        props = feat["properties"]
        lat, lon = props["centroid_lat"], props["centroid_lon"]
        distances.append(haversine_m(lat, lon, MOSCOW_CENTER_LAT, MOSCOW_CENTER_LON))
        capacities.append(props["car_capacity"] or 0)
        metro_distances.append(distance_to_nearest_metro_m(lat, lon, metro_stations))

    max_capacity = percentile(capacities, 90) or 1.0
    # 90-й процентиль, не max(): если хотя бы одна гигантская парковка задаёт
    # максимум, у всех обычных зон давление искусственно схлопывается к 1.0 —
    # тот же класс ошибки, что уже ловили с тарифом на этапе 2. Значения выше
    # капа обрезаются до 0 давления ниже, а не уходят в минус.

    # Нормализация расстояния до метро — в центре Москвы метро густое, станции
    # часто в 400-800м друг от друга, поэтому кап на 1200м разумен (дальше —
    # уже "далеко от метро" в любом случае). Без файла станций — нейтральное 0.5.
    METRO_FAR_CAP_M = 1200.0

    engine = create_engine(DATABASE_URL)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        count = 0
        for feat, distance, capacity, metro_dist in zip(features, distances, capacities, metro_distances):
            props = feat["properties"]
            zone_id = str(props["id"])

            zone = session.get(Zone, zone_id) or Zone(id=zone_id)
            zone.name = props["name"]
            zone.zone_number = props.get("zone_number") or ""
            zone.lat = props["centroid_lat"]
            zone.lon = props["centroid_lon"]
            zone.geometry_json = json.dumps(feat["geometry"])
            zone.tariff_rub_per_hour = props["tariff_weekday_car_rub"]
            zone.tariff_normalized = props["tariff_weekday_car_rub"] / max_tariff
            zone.car_capacity = capacity
            zone.car_capacity_disabled = props.get("car_capacity_disabled", 0)
            zone.distance_from_center_m = distance
            zone.capacity_pressure = max(0.0, min(1.0, 1 - (capacity / max_capacity)))
            if metro_dist is not None:
                zone.car_dependency = max(0.0, min(1.0, metro_dist / METRO_FAR_CAP_M))
            else:
                zone.car_dependency = 0.5  # нейтраль, если станций не подгрузили
            session.add(zone)
            count += 1

        session.commit()
        print(f"Загружено/обновлено зон: {count}")
        if not metro_stations:
            print("[WARN] car_dependency у всех зон стоит на нейтральном 0.5 — метро не подключено в этом прогоне.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Использование: python seed_zones.py <путь_к_geojson>")
        sys.exit(1)
    main(sys.argv[1])
