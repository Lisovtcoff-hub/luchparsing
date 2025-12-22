
"""Модуль contracts.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional


ClientType = Literal["fl", "yl"]
ServiceType = Literal["warehouse_warehouse", "warehouse_pvz", "warehouse_door"]
PayType = Literal["cash_on_order", "cash_on_delivery", "cashless_on_order", "cashless_on_delivery"]


@dataclass(frozen=True)
class Dimensions:
    """Класс Dimensions.

    Инкапсулирует связанную функциональность модуля.
    """
    length_cm: float
    width_cm: float
    height_cm: float


@dataclass(frozen=True)
class CalcParams:
    """Класс CalcParams.

    Инкапсулирует связанную функциональность модуля.
    """
    from_city: str
    to_city: str
    places: int
    weight_kg: float
    volume_m3: float
    dims: Dimensions
    client_type: ClientType = "yl"
    service_type: ServiceType = "warehouse_warehouse"
    pay_type: PayType = "cashless_on_order"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CalcResult:
    """Класс CalcResult.

    Инкапсулирует связанную функциональность модуля.
    """
    price: Optional[float]
    currency: str = "RUB"
    days: Optional[str] = None
    source: Optional[str] = None

    name_tarif: Optional[str] = None
    allowances: dict[str, float] = field(default_factory=dict)


class CarrierError(Exception):
    """Базовая ошибка адаптера."""


class TemporaryError(CarrierError):
    """Временная ошибка (сеть, таймаут, блокировка/капча, перегрузка сайта)."""


class InvalidInputError(CarrierError):
    """Неподдерживаемые входные параметры для конкретного перевозчика."""


class CarrierAdapter:
    """Класс CarrierAdapter.

    Инкапсулирует связанную функциональность модуля.
    """
    code: str

    async def calc(self, client, params: CalcParams) -> CalcResult:
        """Возвращает CalcResult или выбрасывает нормализованную ошибку."""
        raise NotImplementedError
