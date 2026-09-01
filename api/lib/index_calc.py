"""
Расчёт индекса доступности парковки — v2.1 (правка бага "всё красное").

Что было не так в v2 и как исправлено:

БАГ 1 — асимметричная формула. В v2 каждый признак был в диапазоне 0..1 и
умножался на ПОЛОЖИТЕЛЬНЫЙ коэффициент — то есть любое значение признака
только ДОБАВЛЯЛО к спросу, никогда не вычитало. У "средней" зоны в рабочее
время логит получался +4.3, что после сигмоиды давало 1.3% доступности —
проверено руками, не гипотеза. Формула была математически обречена красить
почти всё красным независимо от реальных условий.

ИСПРАВЛЕНО: каждый признак теперь центрируется вокруг "нейтральной" точки
(обычно 0.5) перед умножением на коэффициент — (значение - нейтраль) может
быть и отрицательным. Значение выше нейтрали толкает спрос вверх, ниже —
вниз. Это стандартный приём (effect coding) именно для того, чтобы бинарные
и непрерывные признаки могли работать в обе стороны, а не только в одну.

БАГ 2 — рабочие часы считались по UTC, не по московскому времени (разница
3 часа) — реальная ошибка в main.py, не в этом файле, но упоминаю здесь для
целостности картины.

БАГ 3 — давление по вместимости чувствительно к выбросам: если одна
гигантская парковка задаёт максимум в пилоте, у всех обычных зон давление
искусственно схлопывается к 1.0. Исправлено в seed_zones.py — см. там.
"""

import math
from dataclasses import dataclass

# Коэффициенты логит-модели (по-прежнему экспертная стартовая оценка, не
# откалиброванная модель — см. предупреждение в предыдущей версии файла,
# оно остаётся в силе). Центры показывают "нейтральное" значение признака,
# при котором его вклад в логит равен нулю.
COEF = {
    "tariff": 1.6,
    "proximity": 2.0,
    "capacity_pressure": 1.0,
    "business_hours": 1.3,
    "weekend_or_holiday": -0.9,
    "weather_penalty": 0.6,
    "event_nearby": 1.1,
    "is_dark": 0.4,  # в темноте чуть выше спрос — люди чаще выбирают знакомые,
                     # освещённые платные зоны вместо поиска бесплатных окраинных мест
    "car_dependency": 0.9,       # далеко от метро -> вероятнее приехали на машине -> выше спрос
    "school_holiday": -0.7,      # в каникулы меньше родителей возят детей -> ниже спрос
    "proximity_x_business_hours": 1.3,
    "weekend_x_proximity": -0.5,
}

CENTER = {
    "tariff": 0.5,
    "proximity": 0.3,           # среднее по площади пилота, не по центру самого кольца
    "capacity_pressure": 0.5,
    "business_hours": 0.5,      # бинарный признак — центр 0.5 даёт симметричный вклад ±0.5*coef
    "weekend_or_holiday": 0.3,  # ~2/7 дней недели + немного праздников
    "weather_penalty": 0.15,    # типичный фон — большую часть времени погода не штрафует
    "event_nearby": 0.0,        # события редки, нейтраль — их отсутствие
    "is_dark": 0.4,             # в Москве световой день заметно короче суток бо́льшую часть года
    "car_dependency": 0.5,      # нейтраль — половина зон ближе к метро, половина дальше
    "school_holiday": 0.15,     # каникулы занимают заметную, но не половину года долю
}

PROXIMITY_DECAY_M = 1200  # характерное расстояние затухания спроса, метров


def compute_proximity_exp(distance_from_center_m: float) -> float:
    """0..1, экспоненциальное затухание — 1 вплотную к центру, быстро падает дальше."""
    return math.exp(-distance_from_center_m / PROXIMITY_DECAY_M)


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


@dataclass
class ZoneFeatures:
    zone_id: str
    zone_base_tariff_normalized: float  # 0..1, тариф зоны / макс. тариф по пилоту
    proximity_to_center: float          # 0..1, экспоненциальная близость (compute_proximity_exp)
    capacity_pressure: float            # 0..1, 1 = самая маленькая по вместимости зона
    is_business_hours: float            # 0..1, непрерывная интенсивность (двугорбая кривая,
                                         # пики ~9:00 и ~19:00 МСК — см. main.py compute_business_hours_intensity)
    is_weekend_or_holiday: float        # 0 или 1 — из isdayoff.ru
    weather_penalty: float              # 0..1, чем хуже погода тем выше
    event_nearby: float                 # 0..1, 1 если рядом крупное событие сейчас
    is_dark: float = 0.0                # 0 или 1 — темнее гражданских сумерек (см. main.py get_is_dark)
    car_dependency: float = 0.5         # 0..1, дальше от метро -> ближе к 1 (см. seed_zones.py)
    is_school_holiday: float = 0.0      # 0 или 1 — школьные каникулы в Москве (см. main.py)


def compute_demand_logit(f: ZoneFeatures) -> float:
    return (
        COEF["tariff"] * (f.zone_base_tariff_normalized - CENTER["tariff"])
        + COEF["proximity"] * (f.proximity_to_center - CENTER["proximity"])
        + COEF["capacity_pressure"] * (f.capacity_pressure - CENTER["capacity_pressure"])
        + COEF["business_hours"] * (f.is_business_hours - CENTER["business_hours"])
        + COEF["weekend_or_holiday"] * (f.is_weekend_or_holiday - CENTER["weekend_or_holiday"])
        + COEF["weather_penalty"] * (f.weather_penalty - CENTER["weather_penalty"])
        + COEF["event_nearby"] * (f.event_nearby - CENTER["event_nearby"])
        + COEF["is_dark"] * (f.is_dark - CENTER["is_dark"])
        + COEF["car_dependency"] * (f.car_dependency - CENTER["car_dependency"])
        + COEF["school_holiday"] * (f.is_school_holiday - CENTER["school_holiday"])
        + COEF["proximity_x_business_hours"]
        * (f.proximity_to_center - CENTER["proximity"])
        * (f.is_business_hours - CENTER["business_hours"])
        + COEF["weekend_x_proximity"]
        * (f.is_weekend_or_holiday - CENTER["weekend_or_holiday"])
        * (f.proximity_to_center - CENTER["proximity"])
    )


def compute_demand_score(f: ZoneFeatures) -> float:
    """Возвращает 'спрос' 0..1 через сигмоиду логита."""
    return sigmoid(compute_demand_logit(f))


def compute_factor_breakdown(f: ZoneFeatures) -> list[tuple[str, float]]:
    """
    Раскладывает логит на вклад каждого фактора по отдельности — не для
    расчёта, а чтобы объяснить пользователю, ПОЧЕМУ индекс именно такой
    (это была прямая претензия — "процент ничего не говорит"). Каждое
    значение — сколько именно этот фактор добавил или убавил к логиту;
    main.py переводит топ-факторы в понятные фразы вроде "Дождь" или
    "Далеко от метро".
    """
    return [
        ("tariff", COEF["tariff"] * (f.zone_base_tariff_normalized - CENTER["tariff"])),
        ("proximity", COEF["proximity"] * (f.proximity_to_center - CENTER["proximity"])),
        ("capacity_pressure", COEF["capacity_pressure"] * (f.capacity_pressure - CENTER["capacity_pressure"])),
        ("business_hours", COEF["business_hours"] * (f.is_business_hours - CENTER["business_hours"])),
        ("weekend_or_holiday", COEF["weekend_or_holiday"] * (f.is_weekend_or_holiday - CENTER["weekend_or_holiday"])),
        ("weather_penalty", COEF["weather_penalty"] * (f.weather_penalty - CENTER["weather_penalty"])),
        ("is_dark", COEF["is_dark"] * (f.is_dark - CENTER["is_dark"])),
        ("car_dependency", COEF["car_dependency"] * (f.car_dependency - CENTER["car_dependency"])),
        ("school_holiday", COEF["school_holiday"] * (f.is_school_holiday - CENTER["school_holiday"])),
    ]


def compute_availability_index(f: ZoneFeatures) -> int:
    """Индекс доступности 0..100. 100 = высокая ожидаемая доступность."""
    demand = compute_demand_score(f)
    return round((1.0 - demand) * 100)


if __name__ == "__main__":
    scenarios = [
        ("Типичная зона, будни, рабочий час (контрольный сценарий бага)", ZoneFeatures(
            "z0", zone_base_tariff_normalized=0.9,
            proximity_to_center=compute_proximity_exp(1500), capacity_pressure=0.8,
            is_business_hours=1, is_weekend_or_holiday=0, weather_penalty=0.2, event_nearby=0,
        )),
        ("Будни, час пик, вплотную к центру (500м), дорого, маленькая", ZoneFeatures(
            "z1", zone_base_tariff_normalized=0.9,
            proximity_to_center=compute_proximity_exp(500), capacity_pressure=0.9,
            is_business_hours=1, is_weekend_or_holiday=0, weather_penalty=0.2, event_nearby=0,
        )),
        ("Выходной, дёшево, у края пилота (2800м), большая", ZoneFeatures(
            "z2", zone_base_tariff_normalized=0.2,
            proximity_to_center=compute_proximity_exp(2800), capacity_pressure=0.1,
            is_business_hours=0, is_weekend_or_holiday=1, weather_penalty=0.0, event_nearby=0,
        )),
        ("Ночь буднего дня, средняя зона (все признаки на нейтрали)", ZoneFeatures(
            "z3", zone_base_tariff_normalized=0.5,
            proximity_to_center=0.3, capacity_pressure=0.5,
            is_business_hours=0, is_weekend_or_holiday=0, weather_penalty=0.15, event_nearby=0,
        )),
    ]
    for name, feats in scenarios:
        idx = compute_availability_index(feats)
        print(f"{name}: индекс = {idx}")

