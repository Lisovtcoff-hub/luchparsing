"""Модуль vozovoz.

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


TOKEN = os.getenv("VOZOVOZ_TOKEN") or ""
BASE_URL = os.getenv("VOZOVOZ_BASE_URL") or "https://vozovoz.org/api/"
API_URL = f"{BASE_URL}?token={TOKEN}"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

VOZOVOZ_SOURCE = "https://vozovoz.org/"


def vozovoz(
    from_city: str,
    to_city: str,
    places: int,
    weight_kg: float,
    volume_m3: float,
    dims_cm_json: Optional[Dict[str, float]] = None,
) -> tuple[Optional[float], Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """Функция vozovoz.

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
    payload = {
        "object": "price",
        "action": "get",
        "params": {
            "cargo": {
                "dimension": {
                    "quantity": int(places),
                    "volume": float(volume_m3),
                    "weight": float(weight_kg),
                }
            },
            "gateway": {
                "dispatch": {"point": {"location": from_city, "terminal": "default"}},
                "destination": {"point": {"location": to_city, "terminal": "default"}},
            },
        },
    }

    resp = requests.post(
        API_URL,
        headers=HEADERS,
        data=json.dumps(payload, ensure_ascii=False),
        timeout=(5, 20),
    )
    resp.raise_for_status()

    data = resp.json()
    res = (data.get("response") or {})

    try:
        service = res.get("service") or []
        if not service or not isinstance(service, list):
            return None, None, None, None


        base_price_raw = (service[0] or {}).get("basePrice")

        price: Optional[float]
        try:
            price = float(base_price_raw) if base_price_raw is not None else None
        except Exception:
            price = None


        dt = res.get("deliveryTime") or {}
        d_from = dt.get("from")
        d_to = dt.get("to")
        days: Optional[str] = None
        if d_from is not None and d_to is not None:
            days = f"{d_from}-{d_to}"
        elif d_from is not None:
            days = str(d_from)
        elif d_to is not None:
            days = str(d_to)


        allowances: Dict[str, Any] = {}
        ins = 0.0
        wh = 0.0
        try:
            ins_raw = (service[1] or {}).get("basePrice") if len(service) > 1 else None
            if ins_raw is not None:
                ins = float(ins_raw)
                allowances["Страхование"] = ins
        except Exception:
            pass
        try:
            wh_raw = (service[2] or {}).get("basePrice") if len(service) > 2 else None
            if wh_raw is not None:
                wh = float(wh_raw)
                allowances["Складская обработка"] = wh
        except Exception:
            pass

        name_tarif_json: Optional[str] = None

        total = (float(price) + ins + wh) if price is not None else None
        return total, days, (allowances or None), name_tarif_json

    except Exception:
        return None, None, None, None


class VozovozAdapter(CarrierAdapter):
    """Класс VozovozAdapter.

    Инкапсулирует связанную функциональность модуля.
    """
    code = "vozovoz"

    async def calc(self, client: httpx.AsyncClient, p: CalcParams) -> CalcResult:
        """Функция calc.

        Параметры:
            client: Описание параметра.
            p: Описание параметра.

        Возвращает:
            Результат выполнения функции.
        """
        if not TOKEN:
            raise TemporaryError("vozovoz: не задана переменная окружения VOZOVOZ_TOKEN")
        try:
            price, days, allowances, name_tarif_json = await asyncio.to_thread(
                vozovoz,
                p.from_city,
                p.to_city,
                int(p.places),
                float(p.weight_kg),
                float(p.volume_m3),
                {
                    "l": float(p.dims.length_cm),
                    "w": float(p.dims.width_cm),
                    "h": float(p.dims.height_cm),
                },
            )
        except Exception as e:
            raise TemporaryError(f"vozovoz: {type(e).__name__}: {e}") from e

        return CalcResult(
            price=float(price) if price is not None else None,
            days=str(days) if days is not None else None,
            currency="RUB",
            source=VOZOVOZ_SOURCE,
            name_tarif=name_tarif_json,
            allowances=allowances,
        )


__all__ = ["vozovoz", "VozovozAdapter"]
