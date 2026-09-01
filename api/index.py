"""
Бэкенд. Требует: pip install fastapi uvicorn sqlalchemy pydantic requests

Запуск:
    uvicorn main:app --reload

Эндпоинты:
    GET  /zones           -> GeoJSON зон-сегментов (демо-слой, реальные линии улиц)
    GET  /zone-clusters    -> GeoJSON агрегированных зон (полигоны по номеру
                              парковочной зоны города — основной слой)
    GET  /alternatives      -> список зон с высокой доступностью рядом с зоной,
                              где не нашли место, + время пешком
    POST /feedback          -> записать ответ "нашёл/не нашёл" (единая таблица
                              для обоих слоёв — см. FeedbackIn)
"""

import json
import math
import os
from datetime import datetime, timedelta, timezone

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from lib.index_calc import ZoneFeatures, compute_availability_index, compute_proximity_exp, compute_factor_breakdown
from lib.models import AppFeedback, Base, DeptransDisclosure, IndexSnapshot, UserFeedback, Zone, ZoneIssueReport

MOSCOW_TZ = timezone(timedelta(hours=3))  # МСК = UTC+3, без перехода на летнее время в РФ с 2014

# На Vercel переменную DATABASE_URL подставляет интеграция с Neon Postgres
# (serverless-функции не могут держать файл SQLite между запросами — диск
# не переживает завершение функции). Локально, если переменная не задана,
# по умолчанию используется файл SQLite, как раньше.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./parking.db")
if DATABASE_URL.startswith("postgres://"):
    # Некоторые провайдеры (в т.ч. иногда Neon) отдают устаревшую схему
    # "postgres://" — SQLAlchemy 2.x требует именно "postgresql://".
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
MOSCOW_CENTER_LAT, MOSCOW_CENTER_LON = 55.7520, 37.6175

# Порог "разнородности" агрегированной зоны, в процентных пунктах индекса.
CLUSTER_HETEROGENEITY_THRESHOLD = 35

# Размер ячейки сетки для агрегированного слоя, метров. ~250м даёт в среднем
# несколько парковок на ячейку при плотности пилота (1752 объекта на ~18.6 км²
# площади эллипса Садового кольца) — не квартал целиком и не одна парковка.
# Ячейки — фиксированные прямоугольники, а не выпуклые оболочки: это даёт
# ГАРАНТИЮ отсутствия наложений (соседние прямоугольники не могут
# пересекаться по построению), а не вероятностную оценку.
GRID_CELL_SIZE_M = 250
METERS_PER_DEG_LAT = 111_320
METERS_PER_DEG_LON = METERS_PER_DEG_LAT * math.cos(math.radians(MOSCOW_CENTER_LAT))

# Радиус поиска альтернативы и параметры оценки пешего времени.
ALTERNATIVE_SEARCH_RADIUS_M = 900        # ~10-12 минут пешком, дальше уже не альтернатива, а другой поход
ALTERNATIVE_MIN_INDEX = 66               # "высокая доступность" — та же граница, что в легенде карты
WALK_SPEED_M_PER_MIN = 75                # ~4.5 км/ч
WALK_DETOUR_FACTOR = 1.3                 # поправка на то, что реальные улицы не по прямой

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args)
Base.metadata.create_all(engine)

app = FastAPI(title="Индекс доступности парковки — Москва (пилот: Садовое кольцо)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class FeedbackIn(BaseModel):
    zone_id: str
    found_spot: bool
    device_id: str | None = None
    layer: str = "demo"  # "demo" (конкретная парковка) или "cluster" (агрегированная зона) —
                          # для аналитики, на сам расчёт ground truth не влияет: и то, и
                          # другое в итоге пишется против конкретного zone_id.


# ---------- Погода: Open-Meteo, бесплатно, без ключа ----------
_weather_cache = {"ts": None, "penalty": 0.0, "temp_c": None, "precip_mm": None}
WEATHER_CACHE_TTL = timedelta(minutes=15)


def compute_weather_penalty(precip_mm: float, temp_c: float) -> float:
    penalty = 0.0
    if precip_mm and precip_mm > 0:
        penalty += min(precip_mm / 5.0, 0.6)
    if temp_c is not None:
        if temp_c < -10:
            penalty += 0.3
        elif temp_c > 28:
            penalty += 0.2
    return min(penalty, 1.0)


def get_weather_penalty() -> float:
    now = datetime.now(timezone.utc)
    if _weather_cache["ts"] and now - _weather_cache["ts"] < WEATHER_CACHE_TTL:
        return _weather_cache["penalty"]
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": MOSCOW_CENTER_LAT, "longitude": MOSCOW_CENTER_LON,
                "current": "temperature_2m,precipitation", "timezone": "Europe/Moscow",
            },
            timeout=5,
        )
        resp.raise_for_status()
        current = resp.json()["current"]
        temp_c, precip_mm = current.get("temperature_2m"), current.get("precipitation")
        penalty = compute_weather_penalty(precip_mm, temp_c)
        _weather_cache["temp_c"] = temp_c
        _weather_cache["precip_mm"] = precip_mm
    except Exception as e:
        print(f"[WARN] Не удалось получить погоду с Open-Meteo: {e}")
        penalty = _weather_cache["penalty"]
    _weather_cache["ts"] = now
    _weather_cache["penalty"] = penalty
    return penalty


# ---------- Производственный календарь РФ: isdayoff.ru, бесплатно, без ключа ----------
_calendar_cache = {"date": None, "is_day_off": None}


def get_is_day_off() -> bool:
    today = datetime.now(MOSCOW_TZ).date()
    if _calendar_cache["date"] == today and _calendar_cache["is_day_off"] is not None:
        return _calendar_cache["is_day_off"]
    try:
        resp = requests.get(f"https://isdayoff.ru/{today:%Y%m%d}", timeout=5)
        resp.raise_for_status()
        is_day_off = resp.text.strip() == "1"
    except Exception as e:
        print(f"[WARN] Не удалось получить производственный календарь с isdayoff.ru: {e}")
        is_day_off = today.weekday() >= 5
    _calendar_cache["date"] = today
    _calendar_cache["is_day_off"] = is_day_off
    return is_day_off


def compute_solar_altitude_deg(lat_deg: float, lon_deg: float, dt_utc: datetime) -> float:
    n = dt_utc.timetuple().tm_yday
    gamma = 2 * math.pi / 365 * (n - 1 + (dt_utc.hour - 12) / 24)
    eqtime = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma)
    )
    time_offset = eqtime + 4 * lon_deg
    tst = dt_utc.hour * 60 + dt_utc.minute + dt_utc.second / 60 + time_offset
    ha_rad = math.radians((tst / 4) - 180)
    lat_rad = math.radians(lat_deg)
    alt_rad = math.asin(math.sin(lat_rad) * math.sin(decl) + math.cos(lat_rad) * math.cos(decl) * math.cos(ha_rad))
    return math.degrees(alt_rad)


def get_is_dark() -> bool:
    now_utc = datetime.now(timezone.utc)
    return compute_solar_altitude_deg(MOSCOW_CENTER_LAT, MOSCOW_CENTER_LON, now_utc) < -6


# ---------- Школьные каникулы Москвы (см. пояснение в исходном файле этапа 3) ----------
SCHOOL_HOLIDAYS_2025_2026 = [
    (datetime(2025, 10, 25).date(), datetime(2025, 11, 2).date()),
    (datetime(2025, 12, 31).date(), datetime(2026, 1, 11).date()),
    (datetime(2026, 3, 28).date(), datetime(2026, 4, 5).date()),
    (datetime(2026, 6, 1).date(), datetime(2026, 8, 31).date()),
]


def get_is_school_holiday() -> bool:
    today = datetime.now(MOSCOW_TZ).date()
    return any(start <= today <= end for start, end in SCHOOL_HOLIDAYS_2025_2026)


def compute_business_hours_intensity(hour_float: float) -> float:
    """
    Было: двугорбая кривая с пиками ~9:00/~19:00 и провалом в обед — это
    модель ЗАТОРОВ НА ДОРОГЕ, где трафик действительно проседает днём между
    часами пик. Но нас интересует ЗАНЯТОСТЬ ПАРКОВКИ, а это другое явление:
    человек приехал в офис к 9 и его машина стоит на месте весь рабочий
    день — занятость не проваливается в обед, а держится плато. Заменено на
    сглаженную трапецию: подъём к 8-10, плато весь рабочий день, спад к 18-20."""
    rise = 1 / (1 + math.exp(-(hour_float - 8.5) / 1.0))
    fall = 1 / (1 + math.exp((hour_float - 18.5) / 1.0))
    return max(0.0, min(1.0, 0.08 + 0.85 * rise * fall))


FACTOR_LABELS = {
    "tariff": ("Высокий тариф зоны", "Низкий тариф зоны"),
    "proximity": ("Близко к центру", "Далеко от центра"),
    "capacity_pressure": ("Маленькая зона (мало мест)", "Большая зона (много мест)"),
    "business_hours": ("Час пик", "Не час пик"),
    "weekend_or_holiday": ("Будний день", "Выходной/праздник"),
    "weather_penalty": ("Плохая погода", "Хорошая погода"),
    "is_dark": ("Тёмное время суток", "Светлое время суток"),
    "car_dependency": ("Далеко от метро", "Рядом метро"),
    "school_holiday": ("Учебный период", "Школьные каникулы"),
}


def explain_top_factors(f: ZoneFeatures, limit: int = 3) -> list[dict]:
    """Топ-N факторов, сильнее всего повлиявших на индекс этой зоны прямо
    сейчас — превращает голый процент в объяснение, не только число."""
    breakdown = compute_factor_breakdown(f)
    breakdown.sort(key=lambda x: -abs(x[1]))
    result = []
    for name, value in breakdown[:limit]:
        if abs(value) < 0.05:
            continue  # незначимый вклад — не загромождаем объяснение
        label_up, label_down = FACTOR_LABELS.get(name, (name, name))
        result.append({
            "label": label_up if value > 0 else label_down,
            "direction": "down" if value > 0 else "up",  # down = снижает доступность
        })
    return result


def get_live_features(zone: Zone, weather_penalty: float, is_day_off: bool, is_dark: bool, is_school_holiday: bool) -> ZoneFeatures:
    now_msk = datetime.now(MOSCOW_TZ)
    hour_float = now_msk.hour + now_msk.minute / 60
    is_business_hours = compute_business_hours_intensity(hour_float)
    return ZoneFeatures(
        zone_id=zone.id,
        zone_base_tariff_normalized=zone.tariff_normalized,
        proximity_to_center=compute_proximity_exp(zone.distance_from_center_m or 1500),
        capacity_pressure=zone.capacity_pressure or 0.5,
        is_business_hours=is_business_hours,
        is_weekend_or_holiday=1.0 if is_day_off else 0.0,
        weather_penalty=weather_penalty,
        is_dark=1.0 if is_dark else 0.0,
        car_dependency=zone.car_dependency if zone.car_dependency is not None else 0.5,
        is_school_holiday=1.0 if is_school_holiday else 0.0,
        event_nearby=0.0,  # TODO: подключить датасет афиш data.mos.ru, см. README (этап 3)
    )


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


def grid_cell_key(lat: float, lon: float) -> tuple[int, int]:
    dx = (lon - MOSCOW_CENTER_LON) * METERS_PER_DEG_LON
    dy = (lat - MOSCOW_CENTER_LAT) * METERS_PER_DEG_LAT
    return (math.floor(dx / GRID_CELL_SIZE_M), math.floor(dy / GRID_CELL_SIZE_M))


def grid_cell_bounds_latlon(cell_x: int, cell_y: int) -> list[tuple[float, float]]:
    """Возвращает 4 угла ячейки как (lon, lat) — прямоугольник в метрах,
    переведённый обратно в градусы. Соседние ячейки делят общую границу
    ровно, без зазоров и без наложений."""
    x0_m, x1_m = cell_x * GRID_CELL_SIZE_M, (cell_x + 1) * GRID_CELL_SIZE_M
    y0_m, y1_m = cell_y * GRID_CELL_SIZE_M, (cell_y + 1) * GRID_CELL_SIZE_M

    def to_latlon(x_m, y_m):
        return (
            MOSCOW_CENTER_LON + x_m / METERS_PER_DEG_LON,
            MOSCOW_CENTER_LAT + y_m / METERS_PER_DEG_LAT,
        )

    return [to_latlon(x0_m, y0_m), to_latlon(x1_m, y0_m), to_latlon(x1_m, y1_m), to_latlon(x0_m, y1_m)]


def grid_cell_label(cell_x: int, cell_y: int) -> str:
    """Буквенно-числовой ярлык вроде 'C7', как в таблице — понятнее человеку,
    чем сырые координаты ячейки. Сдвиг 20/30 — с запасом относительно
    реального диапазона ячеек в пилоте (±12 по широте, ±9 по долготе при
    250-метровой сетке), чтобб не уйти в отрицательные и не разрастись
    до двух букв без необходимости."""
    n = cell_x + 20
    letters = ""
    n_letter = n
    while True:
        n_letter, rem = divmod(n_letter, 26)
        letters = chr(65 + rem) + letters
        if n_letter == 0:
            break
        n_letter -= 1
    return f"{letters}{cell_y + 30}"


FEEDBACK_CREDIBILITY_K = 8  # сколько ответов нужно, чтобы фидбек и эвристика влияли примерно поровну
                            # (формула credibility = n / (n + K), стандартный байесовский shrinkage)


def get_feedback_stats(session: Session) -> dict[str, tuple[int, int]]:
    """zone_id -> (найдено_раз, не_найдено_раз), по всей истории фидбека."""
    rows = session.scalars(select(UserFeedback)).all()
    stats: dict[str, list[int]] = {}
    for row in rows:
        counts = stats.setdefault(row.zone_id, [0, 0])
        counts[0 if row.found_spot else 1] += 1
    return {zid: (c[0], c[1]) for zid, c in stats.items()}


def blend_with_feedback(heuristic_idx: int, zone_id: str, feedback_stats: dict[str, tuple[int, int]]) -> int:
    """
    Смешивает индекс формулы с реальными наблюдениями пользователей —
    первый шаг от чистой эвристики к откалиброванной модели (см.
    concept_final.md, раздел 6: там же описан порог 200-300 меток на зону
    для перехода на полноценную регрессию — это промежуточный шаг к этому,
    доступный уже сейчас на любом объёме фидбека, просто с растущим весом).
    """
    yes, no = feedback_stats.get(zone_id, (0, 0))
    n = yes + no
    if n == 0:
        return heuristic_idx
    empirical_idx = round(100 * yes / n)
    credibility = n / (n + FEEDBACK_CREDIBILITY_K)
    return round(heuristic_idx * (1 - credibility) + empirical_idx * credibility)


TREND_COMPARISON_MINUTES = 25  # с чем сравниваем текущий индекс, чтобы показать тренд
TREND_THRESHOLD = 5            # порог в процентных пунктах — меньше считаем "без изменений",
                                # чтобы стрелка не дёргалась от шума


def get_trend_baseline(session: Session) -> dict[str, int]:
    """zone_id -> значение индекса из ближайшего снимка старше TREND_COMPARISON_MINUTES.
    NB: при росте истории снимков стоит будет добавить индекс по (zone_id, ts)
    в БД — на масштабе пилота выборка ещё небольшая, не критично."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=TREND_COMPARISON_MINUTES)
    rows = session.scalars(
        select(IndexSnapshot).where(IndexSnapshot.ts <= cutoff).order_by(IndexSnapshot.ts.desc())
    ).all()
    baseline: dict[str, int] = {}
    for row in rows:
        if row.zone_id not in baseline:
            baseline[row.zone_id] = row.index_value
    return baseline


def compute_all_indices(session: Session) -> list[dict]:
    """Общее ядро для /zones и /zone-clusters — считает живой индекс по всем
    зонам один раз, дальше каждый эндпоинт по-своему группирует результат.
    Погода/календарь/темнота/каникулы считаются один раз на весь список, не
    по кругу — 1752 внешних запроса на один /zones было бы неоправданно."""
    weather_penalty = get_weather_penalty()
    is_day_off = get_is_day_off()
    is_dark = get_is_dark()
    is_school_holiday = get_is_school_holiday()
    feedback_stats = get_feedback_stats(session)
    trend_baseline = get_trend_baseline(session)

    zones = session.scalars(select(Zone)).all()
    results = []
    for z in zones:
        live_features = get_live_features(z, weather_penalty, is_day_off, is_dark, is_school_holiday)
        heuristic_idx = compute_availability_index(live_features)
        idx = blend_with_feedback(heuristic_idx, z.id, feedback_stats)

        trend = "flat"
        if z.id in trend_baseline:
            diff = idx - trend_baseline[z.id]
            trend = "up" if diff >= TREND_THRESHOLD else "down" if diff <= -TREND_THRESHOLD else "flat"

        yes, no = feedback_stats.get(z.id, (0, 0))

        session.add(IndexSnapshot(
            zone_id=z.id, index_value=idx,
            weather_penalty=live_features.weather_penalty,
            event_nearby=bool(live_features.event_nearby),
            model_version="sigmoid_v2_blended",
        ))
        results.append({
            "zone": z, "index": idx, "trend": trend, "feedback_count": yes + no,
            "explain": explain_top_factors(live_features),
        })
    session.commit()
    return results


@app.get("/api/zones")
def list_zones():
    """Демо-слой — реальные линии улиц, индекс на уровне конкретного сегмента."""
    with Session(engine) as session:
        results = compute_all_indices(session)
        features = [{
            "type": "Feature",
            "geometry": json.loads(r["zone"].geometry_json),
            "properties": {
                "id": r["zone"].id,
                "zone_number": r["zone"].zone_number,
                "name": r["zone"].name,
                "lat": r["zone"].lat,
                "lon": r["zone"].lon,
                "index_value": r["index"],
                "model_version": "sigmoid_v2",
                "tariff_weekday_car_rub": r["zone"].tariff_rub_per_hour,
                "car_capacity": r["zone"].car_capacity or 0,
                "car_capacity_disabled": r["zone"].car_capacity_disabled or 0,
                "potentially_free": round((r["zone"].car_capacity or 0) * r["index"] / 100),
                "trend": r["trend"],
                "feedback_count": r["feedback_count"],
                "explain": r["explain"],
            },
        } for r in results]
        return {"type": "FeatureCollection", "features": features}


@app.get("/api/zone-clusters")
def list_zone_clusters():
    """
    Основной слой — агрегация по географической сетке (~250м ячейка), не по
    официальному номеру зоны: тарифный номер города может относиться к
    физически разбросанным парковкам, что давало огромные накладывающиеся
    фигуры (см. историю правок). Ячейка сетки — фиксированный прямоугольник,
    поэтому наложения между соседними зонами исключены геометрически, а не
    просто маловероятны.

    Индекс отдаётся диапазоном (min-max по сегментам в ячейке), и если
    разброс больше CLUSTER_HETEROGENEITY_THRESHOLD — зона помечается
    is_heterogeneous=true.
    """
    with Session(engine) as session:
        results = compute_all_indices(session)

        groups: dict[tuple[int, int], list[dict]] = {}
        for r in results:
            key = grid_cell_key(r["zone"].lat, r["zone"].lon)
            groups.setdefault(key, []).append(r)

        features = []
        for (cell_x, cell_y), members in groups.items():
            ring_coords = grid_cell_bounds_latlon(cell_x, cell_y)
            ring = ring_coords + [ring_coords[0]]

            indices = [m["index"] for m in members]
            idx_min, idx_max = min(indices), max(indices)
            total_capacity = sum(m["zone"].car_capacity or 0 for m in members)
            free_min = round(total_capacity * idx_min / 100)
            free_max = round(total_capacity * idx_max / 100)
            # Геометрический центр ячейки (среднее 4 углов), не среднее точек
            # внутри неё — точки могут скучиваться у одного края, тогда подпись
            # съезжала бы от видимого центра прямоугольника на карте.
            centroid_lon = sum(c[0] for c in ring_coords) / 4
            centroid_lat = sum(c[1] for c in ring_coords) / 4
            # Представитель для фидбека/альтернатив — зона внутри ячейки,
            # ближайшая к среднему индексу (не первая попавшаяся).
            mean_idx = sum(indices) / len(indices)
            representative = min(members, key=lambda m: abs(m["index"] - mean_idx))
            total_feedback = sum(m["feedback_count"] for m in members)
            total_disabled = sum(m["zone"].car_capacity_disabled or 0 for m in members)
            # Тренд зоны — большинство голосов среди участников (up/down/flat),
            # ничья решается в пользу "flat", чтобы не мигать стрелкой попусту.
            trend_counts = {"up": 0, "down": 0, "flat": 0}
            for m in members:
                trend_counts[m["trend"]] += 1
            cluster_trend = max(trend_counts, key=lambda k: (trend_counts[k], k == "flat"))

            features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {
                    "zone_label": grid_cell_label(cell_x, cell_y),
                    "segment_count": len(members),
                    "index_min": idx_min,
                    "index_max": idx_max,
                    "index_mean": round(mean_idx),
                    "total_capacity": total_capacity,
                    "total_capacity_disabled": total_disabled,
                    "potentially_free_min": free_min,
                    "potentially_free_max": free_max,
                    "is_heterogeneous": (idx_max - idx_min) > CLUSTER_HETEROGENEITY_THRESHOLD,
                    "trend": cluster_trend,
                    "feedback_count": total_feedback,
                    "centroid_lat": centroid_lat,
                    "centroid_lon": centroid_lon,
                    "representative_zone_id": representative["zone"].id,
                    "explain": representative["explain"],
                },
            })

        return {"type": "FeatureCollection", "features": features}


@app.get("/api/alternatives")
def get_alternatives(zone_id: str):
    """
    "Не нашли место?" — вместо геолокации (которая в Москве часто врёт)
    используем саму зону, которую пользователь только что отметил как
    "не нашёл" — она уже известна точно, без GPS. Ищем зоны с высокой
    доступностью в разумном радиусе пешком и оцениваем время по прямой с
    поправкой на реальную уличную сеть (WALK_DETOUR_FACTOR).
    """
    with Session(engine) as session:
        origin = session.get(Zone, zone_id)
        if origin is None:
            raise HTTPException(404, "Zone not found")

        results = compute_all_indices(session)
        candidates = []
        for r in results:
            z = r["zone"]
            if z.id == zone_id or r["index"] < ALTERNATIVE_MIN_INDEX:
                continue
            dist_m = haversine_m(origin.lat, origin.lon, z.lat, z.lon)
            if dist_m > ALTERNATIVE_SEARCH_RADIUS_M:
                continue
            walk_min = round((dist_m * WALK_DETOUR_FACTOR) / WALK_SPEED_M_PER_MIN)
            candidates.append({
                "zone_id": z.id, "name": z.name, "index_value": r["index"],
                "distance_m": round(dist_m), "walk_minutes": max(walk_min, 1),
                "lat": z.lat, "lon": z.lon,
                "tariff_weekday_car_rub": z.tariff_rub_per_hour,
                "car_capacity": z.car_capacity or 0,
                "potentially_free": round((z.car_capacity or 0) * r["index"] / 100),
            })

        candidates.sort(key=lambda c: c["distance_m"])
        return {"origin_zone_id": zone_id, "alternatives": candidates[:3]}


FEEDBACK_COOLDOWN_MINUTES = 5  # защита от случайного/спам-фидбека — не больше одной
                               # отметки на зону с одного устройства за этот период


@app.post("/api/feedback")
def submit_feedback(payload: FeedbackIn):
    with Session(engine) as session:
        zone = session.get(Zone, payload.zone_id)
        if zone is None:
            raise HTTPException(404, "Zone not found")

        if payload.device_id:
            cooldown_start = datetime.now(timezone.utc) - timedelta(minutes=FEEDBACK_COOLDOWN_MINUTES)
            recent = session.scalars(
                select(UserFeedback)
                .where(UserFeedback.zone_id == zone.id, UserFeedback.device_id == payload.device_id, UserFeedback.ts >= cooldown_start)
            ).first()
            if recent:
                return {"status": "ignored_cooldown", "message": f"Отметка по этой зоне уже была недавно — подожди {FEEDBACK_COOLDOWN_MINUTES} мин."}

        last_snapshot = session.scalars(
            select(IndexSnapshot).where(IndexSnapshot.zone_id == zone.id).order_by(IndexSnapshot.ts.desc())
        ).first()

        fb = UserFeedback(
            zone_id=zone.id,
            found_spot=payload.found_spot,
            index_value_at_time=last_snapshot.index_value if last_snapshot else None,
            device_id=payload.device_id,
        )
        session.add(fb)
        session.commit()
        return {"status": "ok"}


class IssueReportIn(BaseModel):
    zone_id: str
    reason: str  # "closed", "wrong_location", "other"
    device_id: str | None = None


@app.post("/api/report-issue")
def report_issue(payload: IssueReportIn):
    """Сигнал о проблеме с данными зоны — не о наличии места, а о самих
    данных (например, парковку закрыли на ремонт). Отдельная лёгкая таблица,
    не смешивается с ground truth фидбеком по доступности."""
    with Session(engine) as session:
        zone = session.get(Zone, payload.zone_id)
        if zone is None:
            raise HTTPException(404, "Zone not found")
        session.add(ZoneIssueReport(zone_id=payload.zone_id, reason=payload.reason, device_id=payload.device_id))
        session.commit()
        return {"status": "ok"}



@app.get("/api/context")
def get_context():
    """Общий контекст дня — время/день недели видны всегда и всем полезны,
    в отличие от 'каникулы', которые актуальны не для каждого пользователя.
    is_day_off/is_school_holiday остаются для дебага и других нужд, но
    в статус-пилле больше не выводятся отдельным текстом."""
    is_day_off = get_is_day_off()
    is_school_holiday = get_is_school_holiday()
    now_msk = datetime.now(MOSCOW_TZ)
    weekday_names_short = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    return {
        "is_day_off": is_day_off,
        "is_school_holiday": is_school_holiday,
        "is_dark": get_is_dark(),
        "weekday_short": weekday_names_short[now_msk.weekday()],
        "datetime_msk": now_msk.replace(tzinfo=None).isoformat(),
    }


@app.get("/api/community-stats")
def community_stats():
    """Коллективная статистика — сколько всего меток оставило сообщество,
    без привязки к конкретному человеку. Это и есть 'социальная' часть
    продукта: видно вклад всех сразу, а не чей-то персональный профиль."""
    with Session(engine) as session:
        all_rows = session.scalars(select(UserFeedback)).all()
        today_start = datetime.now(MOSCOW_TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        today_count = sum(1 for r in all_rows if r.ts.replace(tzinfo=timezone.utc) >= today_start)
        return {"total_all_time": len(all_rows), "total_today": today_count}


@app.get("/api/top-verified-today")
def top_verified_today(limit: int = 5):
    """Зоны с наибольшим числом сегодняшних отметок — витрина того, где
    данные сейчас надёжнее всего. Полностью анонимно, ничего личного:
    просто агрегат по зонам, не по людям. Заменяет 'избранное, требующее
    входа' на нечто полезное без личного кабинета вообще."""
    with Session(engine) as session:
        today_start = datetime.now(MOSCOW_TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        rows = session.scalars(select(UserFeedback).where(UserFeedback.ts >= today_start)).all()
        counts: dict[str, int] = {}
        for r in rows:
            counts[r.zone_id] = counts.get(r.zone_id, 0) + 1
        top_ids = sorted(counts, key=lambda z: -counts[z])[:limit]
        result = []
        for zid in top_ids:
            zone = session.get(Zone, zid)
            if zone:
                result.append({"zone_id": zid, "name": zone.name, "feedback_count": counts[zid]})
        return {"zones": result}


class AppFeedbackIn(BaseModel):
    category: str  # "bug" | "idea" | "other"
    message: str
    device_id: str | None = None


@app.post("/api/app-feedback")
def submit_app_feedback(payload: AppFeedbackIn):
    if not payload.message.strip():
        raise HTTPException(400, "Пустое сообщение")
    with Session(engine) as session:
        session.add(AppFeedback(category=payload.category, message=payload.message.strip(), device_id=payload.device_id))
        session.commit()
        return {"status": "ok"}


@app.get("/api/weather-now")
def weather_now():
    """Текущая погода в явном виде — раньше использовалась только внутри
    формулы, теперь показывается и пользователю для прозрачности."""
    get_weather_penalty()  # обновит кэш при необходимости
    return {
        "temp_c": _weather_cache["temp_c"],
        "precip_mm": _weather_cache["precip_mm"],
        "penalty": _weather_cache["penalty"],
    }


@app.get("/api/metro-stations")
def get_metro_stations():
    """Отдаёт список станций метро для слоя на карте — помогает ориентироваться
    без чтения названий улиц. Источник: collectors/metro_stations.py (см. этап 3)."""
    path = os.path.join(os.path.dirname(__file__), "metro_stations.json")
    if not os.path.exists(path):
        return {"type": "FeatureCollection", "features": []}
    with open(path, encoding="utf-8") as f:
        stations = json.load(f)
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
                "properties": {"name": s["name"], "line": s.get("line", "")},
            }
            for s in stations
        ],
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}
