"""Модуль tkkit.

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


TOKEN = os.getenv("TKKIT_TOKEN") or ""
BASE = os.getenv("TKKIT_BASE_URL") or "https://capi.tk-kit.com"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

TKKIT_SOURCE = "https://tk-kit.ru/"



def get_city_code(name: str) -> str:
    """Функция get_city_code.

    Параметры:
        name: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    resp = requests.post(
        f"{BASE}/1.0/tdd/search/by-name?token={TOKEN}",
        headers=HEADERS,
        data=json.dumps({"title": name}, ensure_ascii=False),
        timeout=(5, 20),
    )
    resp.raise_for_status()
    answer = resp.json()
    if not isinstance(answer, list) or not answer:
        raise ValueError(f"tkkit: город не найден: {name!r}")
    return answer[0]["code"]


def tkkit(
    from_city: str,
    to_city: str,
    places: int,
    weight_kg: float,
    volume_m3: float,
    dims_cm_json: Dict[str, float],
) -> tuple[Optional[float], Optional[str], Dict[str, Any], Optional[str]]:
    """Функция tkkit.

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
    url = f"{BASE}/2.0/order/calculate?token={TOKEN}"

    places_i = max(1, int(places))

    payload = {
        "city_pickup_code": get_city_code(from_city),
        "city_delivery_code": get_city_code(to_city),
        "places": [
            {
                "height": dims_cm_json["h"],
                "width": dims_cm_json["w"],
                "length": dims_cm_json["l"],
                "weight": float(weight_kg) / places_i,
                "volume": float(volume_m3) / places_i,
                "count_place": places_i,
            }
        ],
        "delivery": 1,
        "pick_up": 0,
        "insurance": 0,
        "have_doc": 0,
        "cargo_type_code": "03",
        "all_places_same": 1,
        "currency_code": ["RUB"],
        "declared_price": "100",
        "confirmation_price": 0,
    }

    resp = requests.post(
        url,
        headers=HEADERS,
        data=json.dumps(payload, ensure_ascii=False),
        timeout=(5, 20),
    )
    resp.raise_for_status()
    json_data = resp.json()


    price_raw = None
    days_raw = None
    insurance_raw = None

    try:
        price_raw = json_data[0]["01"]["detail"][0]["price"]
    except Exception:
        price_raw = None

    try:
        days_raw = json_data[0]["01"]["time"]
    except Exception:
        days_raw = None



    try:
        insurance_raw = json_data[0]["01"]["detail"][2]["price"]
    except Exception:
        insurance_raw = None

    price: Optional[float]
    try:
        price = float(price_raw) if price_raw is not None else None
    except Exception:
        price = None

    days: Optional[str]
    if days_raw is None:
        days = None
    else:
        days = str(days_raw).strip() or None

    allowances: Dict[str, Any] = {}
    if insurance_raw is not None:
        allowances["Страхование"] = insurance_raw


    name_tarif_json: Optional[str] = None

    return price, days, allowances, name_tarif_json


class TkkitAdapter(CarrierAdapter):
    """Класс TkkitAdapter.

    Инкапсулирует связанную функциональность модуля.
    """
    code = "tkkit"

    async def calc(self, client: httpx.AsyncClient, p: CalcParams) -> CalcResult:
        """Функция calc.

        Параметры:
            client: Описание параметра.
            p: Описание параметра.

        Возвращает:
            Результат выполнения функции.
        """
        if not TOKEN:
            raise TemporaryError("tkkit: не задана переменная окружения TKKIT_TOKEN")
        try:
            dims_cm = {
                "l": float(p.dims.length_cm),
                "w": float(p.dims.width_cm),
                "h": float(p.dims.height_cm),
            }

            price, days, allowances, name_tarif_json = await asyncio.to_thread(
                tkkit,
                p.from_city,
                p.to_city,
                int(p.places),
                float(p.weight_kg),
                float(p.volume_m3),
                dims_cm,
            )
        except Exception as e:
            raise TemporaryError(f"tkkit: {type(e).__name__}: {e}") from e

        return CalcResult(
            price=price,
            days=days,
            currency="RUB",
            source=TKKIT_SOURCE,
            name_tarif=name_tarif_json,
            allowances=allowances,
        )


__all__ = ["tkkit", "get_city_code", "TkkitAdapter"]
