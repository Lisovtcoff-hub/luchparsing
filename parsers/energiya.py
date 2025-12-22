"""Модуль energiya.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import os
import asyncio
import json
from typing import Any, Dict, Optional, Tuple

import httpx
import requests

from core.contracts import CarrierAdapter, CalcParams, CalcResult, TemporaryError


DEV_TOKEN = os.getenv("ENERGIYA_DEV_TOKEN") or ""
BASE = os.getenv("ENERGIYA_BASE_URL") or "https://mainapi.nrg-tk.ru/"

TOKEN = os.getenv("ENERGIYA_TOKEN") or DEV_TOKEN
HEADERS = {
    "Accept": "application/json",
    "NrgApi-DevToken": DEV_TOKEN,
    "Content-Type": "application/json",
}

ENERGIYA_SOURCE = "https://nrg-tk.ru/"


def find_city(name: str) -> Optional[int]:
    """Функция find_city.

    Параметры:
        name: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    resp = requests.get(
        url=f"{BASE}v3/cities",
        headers=HEADERS,
        timeout=(5, 20),
    )
    resp.raise_for_status()
    json_data = resp.json()
    for city in (json_data.get("cityList") or []):
        if city.get("name") == name:
            return city.get("id")
    return None


def energiya(
    from_city: str,
    to_city: str,
    places: int,
    weight_kg: float,
    volume_m3: float,
    dims_cm_json: Dict[str, float],
) -> tuple[Optional[float], Optional[str], Dict[str, Any], Optional[Dict[str, Any]]]:
    """Функция energiya.

    Параметры:
        from_city: Описание параметра.
        to_city: Описание параметра.
        places: Описание параметра.
        weight_kg: Описание параметра.
        volume_m3: Описание параметра.
        dims_cm_json: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    id_city_from = find_city(from_city)
    id_city_to = find_city(to_city)

    if not id_city_from or not id_city_to:
        raise ValueError("energiya: город не найден")

    q = max(1, int(places))

    items = []
    for _ in range(q):
        items.append(
            {
                "weight": float(weight_kg) / q,
                "width": float(dims_cm_json["w"]) / 100.0,
                "height": float(dims_cm_json["h"]) / 100.0,
                "length": float(dims_cm_json["l"]) / 100.0,
                "isStandardSize": False,
            }
        )

    payload = {
        "idCityFrom": id_city_from,
        "idCityTo": id_city_to,
        "cover": 0,
        "idCurrency": 1,
        "items": items,
        "declaredCargoPrice": 0,
        "idClient": 0,
    }

    resp = requests.post(
        f"{BASE}v3/price",
        headers=HEADERS,
        data=json.dumps(payload, ensure_ascii=False),
        timeout=(5, 20),
    )
    resp.raise_for_status()
    json_data = resp.json()

    transfer = (json_data.get("transfer") or [])
    if not transfer or not isinstance(transfer, list):
        raise ValueError("energiya: нет секции transfer в ответе")

    first = transfer[0] if isinstance(transfer[0], dict) else {}
    price_raw = first.get("price")
    interval_raw = first.get("interval")
    express_raw = first.get("priceExpress")

    price: Optional[float]
    try:
        price = float(price_raw) if price_raw is not None else None
    except Exception:
        price = None

    days: Optional[str] = None
    if interval_raw is not None:
        s = str(interval_raw).strip()
        days = (s.split()[0] if s else None)

    name_tarif_map: Dict[str, Any] = {}
    if express_raw is not None:
        name_tarif_map["NRG-Экспресс"] = express_raw

    insurance_raw = json_data.get("priceInsurance")
    allowances: Optional[Dict[str, Any]] = None
    if insurance_raw:
        allowances = {"Страхование": insurance_raw}

    return price, days, name_tarif_map, allowances


class EnergiyaAdapter(CarrierAdapter):
    """Класс EnergiyaAdapter.

    Инкапсулирует связанную функциональность модуля.
    """
    code = "energiya"

    async def calc(self, client: httpx.AsyncClient, p: CalcParams) -> CalcResult:
        """Функция calc.

        Параметры:
            client: Описание параметра.
            p: Описание параметра.

        Возвращает:
            Результат выполнения функции.
        """
        if not DEV_TOKEN:
            raise TemporaryError("energiya: не задана переменная окружения ENERGIYA_DEV_TOKEN")
        try:
            dims_cm = {
                "l": float(p.dims.length_cm),
                "w": float(p.dims.width_cm),
                "h": float(p.dims.height_cm),
            }

            price, days, name_tarif_map, allowances = await asyncio.to_thread(
                energiya,
                p.from_city,
                p.to_city,
                int(p.places),
                float(p.weight_kg),
                float(p.volume_m3),
                dims_cm,
            )

            name_tarif_json = json.dumps(name_tarif_map, ensure_ascii=False) if name_tarif_map else None

        except Exception as e:
            raise TemporaryError(f"energiya: {type(e).__name__}: {e}") from e

        return CalcResult(
            price=float(price) if price is not None else None,
            days=days if days is not None else None,
            currency="RUB",
            source=ENERGIYA_SOURCE,
            name_tarif=name_tarif_json,
            allowances=allowances,
        )


__all__ = ["energiya", "find_city", "EnergiyaAdapter"]
