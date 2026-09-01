"""
Схема БД (SQLAlchemy). Старт на SQLite, переезд на Postgres — без изменения
кода, только строка подключения.

Требует: pip install sqlalchemy
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Boolean
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def utcnow():
    return datetime.now(timezone.utc)


class Zone(Base):
    """Парковочная зона — синхронизируется из data.mos.ru (см. parking_zones.py)."""

    __tablename__ = "zones"

    id = Column(String, primary_key=True)  # ID зоны как в data.mos.ru
    name = Column(String, nullable=False)
    zone_number = Column(String, index=True)  # официальный номер зоны (ParkingZoneNumber) —
                                                # единица группировки для агрегированного слоя
    lat = Column(Float, nullable=False)   # центроид — для расчётов (близость к центру и т.п.)
    lon = Column(Float, nullable=False)
    geometry_json = Column(String, nullable=False)  # реальная линия улицы (GeoJSON geometry), для отрисовки
    tariff_rub_per_hour = Column(Float, nullable=False)
    tariff_normalized = Column(Float, nullable=False)  # tariff / max_tariff_in_city
    car_capacity = Column(Integer, default=0)
    car_capacity_disabled = Column(Integer, default=0)  # места для инвалидов — были в данных с
                                                          # data.mos.ru с самого начала, просто не использовались
    distance_from_center_m = Column(Float, default=1500)  # сырое расстояние — для экспоненциального
                                                            # затухания спроса в index_calc.py (v2)
    capacity_pressure = Column(Float, default=0.5)     # 0..1, 1 = самая маленькая по вместимости
    car_dependency = Column(Float, default=0.5)         # 0..1, 1 = далеко от метро -> вероятнее приехали на машине

    snapshots = relationship("IndexSnapshot", back_populates="zone")
    feedback = relationship("UserFeedback", back_populates="zone")


class IndexSnapshot(Base):
    """Значение индекса в конкретный момент времени — нужно для привязки
    фидбека к тому, что индекс показывал в этот момент (см. concept_final.md)."""

    __tablename__ = "index_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    zone_id = Column(String, ForeignKey("zones.id"), nullable=False)
    ts = Column(DateTime, default=utcnow, nullable=False)
    index_value = Column(Integer, nullable=False)  # 0..100
    traffic_score = Column(Float)
    weather_penalty = Column(Float)
    event_nearby = Column(Boolean)
    model_version = Column(String, default="heuristic_v1")

    zone = relationship("Zone", back_populates="snapshots")


class UserFeedback(Base):
    """Ground truth, канал А — тап пользователя 'нашёл/не нашёл место'."""

    __tablename__ = "user_feedback"

    id = Column(Integer, primary_key=True, autoincrement=True)
    zone_id = Column(String, ForeignKey("zones.id"), nullable=False)
    ts = Column(DateTime, default=utcnow, nullable=False)
    found_spot = Column(Boolean, nullable=False)
    index_value_at_time = Column(Integer)  # снимок индекса в момент ответа
    device_id = Column(String)  # анонимный ID устройства, не пользователя

    zone = relationship("Zone", back_populates="feedback")


class ZoneIssueReport(Base):
    """Сигнал о проблеме с данными зоны (закрыта, неверное расположение и т.п.)
    — отдельно от ground truth фидбека по доступности, это про качество
    самих данных, не про занятость места."""

    __tablename__ = "zone_issue_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    zone_id = Column(String, ForeignKey("zones.id"), nullable=False)
    ts = Column(DateTime, default=utcnow, nullable=False)
    reason = Column(String, nullable=False)
    device_id = Column(String)


class AppFeedback(Base):
    """Общая обратная связь по приложению (баги, идеи, жалобы) — не про
    конкретную зону и не про доступность парковки, отдельно от обоих других
    каналов. Ничего личного не собираем — только текст и анонимный device_id
    для антиспама."""

    __tablename__ = "app_feedback"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, default=utcnow, nullable=False)
    category = Column(String, nullable=False)  # "bug", "idea", "other"
    message = Column(String, nullable=False)
    device_id = Column(String)


class DeptransDisclosure(Base):
    """Ground truth, канал Б — официальные объявления Дептранса о пересмотре
    тарифов с указанием occupancy по конкретным улицам."""

    __tablename__ = "deptrans_disclosures"

    id = Column(Integer, primary_key=True, autoincrement=True)
    street_name = Column(String, nullable=False)
    disclosed_occupancy_pct = Column(Float, nullable=False)
    source_url = Column(String)
    disclosed_at = Column(DateTime, nullable=False)
    matched_zone_id = Column(String, ForeignKey("zones.id"), nullable=True)
