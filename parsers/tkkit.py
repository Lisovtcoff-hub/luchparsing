"""Модуль tkkit.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import os
import asyncio
import json
import logging
import time
import threading
from typing import Any, Dict, Optional, Tuple

import httpx
import requests

from core.contracts import CarrierAdapter, CalcParams, CalcResult, TemporaryError, InvalidInputError


TOKEN = os.getenv("TKKIT_TOKEN") or ""
BASE = os.getenv("TKKIT_BASE_URL") or "https://capi.tk-kit.com"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

TKKIT_SOURCE = "https://tk-kit.ru/"

logger = logging.getLogger(__name__)

_CITY_CODE_CACHE: Dict[str, str] = {}
_NO_CITY_LOCK = threading.Lock()
_NO_CITY_ROUTES: set[tuple[str, str]] = set()
_BANNED = []


def _route_key(from_city: str, to_city: str) -> tuple[str, str]:
    return ((from_city or "").strip().lower(), (to_city or "").strip().lower())


def _remember_no_city(from_city: str, to_city: str) -> None:
    with _NO_CITY_LOCK:
        _NO_CITY_ROUTES.add(_route_key(from_city, to_city))


def _is_no_city(from_city: str, to_city: str) -> bool:
    with _NO_CITY_LOCK:
        return _route_key(from_city, to_city) in _NO_CITY_ROUTES


def get_city_code(name: str) -> str:
    """Функция get_city_code.

    Параметры:
        name: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    key = (name or "").strip().lower()
    if key in _CITY_CODE_CACHE:
        return _CITY_CODE_CACHE[key]

    url = f"{BASE}/1.0/tdd/search/by-name?token={TOKEN}"
    payload = json.dumps({"title": name}, ensure_ascii=False)
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                url,
                headers=HEADERS,
                data=payload,
                timeout=(5, 20),
            )
            if resp.status_code == 429:
                logger.warning("tkkit city lookup rate limited name=%s attempt=%s", name, attempt)
                time.sleep(0.5 * attempt)
                continue
            resp.raise_for_status()
            answer = resp.json()
            if not isinstance(answer, list) or not answer:
                raise ValueError(f"tkkit: город не найден: {name!r}")
            if name == 'Октябрьский':
                code = "020000400000"
            else:
                code = answer[0]["code"]
            _CITY_CODE_CACHE[key] = code
            return code
        except Exception as exc:
            last_exc = exc
            logger.warning("tkkit city lookup failed name=%s attempt=%s err=%s", name, attempt, exc)
            time.sleep(0.3 * attempt)

    if last_exc:
        raise last_exc
    raise ValueError(f"tkkit: город не найден: {name!r}")



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
    for i in _BANNED:
        if from_city in i[0] and to_city in i[1]:
            print(f"tkkit: no terminal for route {from_city!r} -> {to_city!r} (cached)")
            return None, None, {}, None

    if _is_no_city(from_city, to_city):
        raise InvalidInputError(f"tkkit: no terminal for route {from_city!r} -> {to_city!r} (cached)")

    url = f"{BASE}/2.0/order/calculate?token={TOKEN}"
    

    places_i = max(1, int(places))

    try:
        pickup_code = get_city_code(from_city)
        delivery_code = get_city_code(to_city)
    except ValueError as e:
        _remember_no_city(from_city, to_city)
        raise InvalidInputError(str(e)) from e

    payload = {
        "city_pickup_code": pickup_code,
        "city_delivery_code": delivery_code,
        "places": [
            {
                "weight": float(weight_kg),
                "volume": round(float(volume_m3), 3),
                "count_place": places_i,
            }
        ],
        "cargo_type_code": "03",
        "all_places_same": 0,
        "declared_price": "100",
    }

    logger.info(
        "tkkit request params "
        "from_city=%s to_city=%s places=%s weight_kg=%s volume_m3=%s",
        from_city,
        to_city,
        places,
        weight_kg,
        volume_m3,
    )
    resp = requests.post(
        url,
        headers=HEADERS,
        data=json.dumps(payload, ensure_ascii=False),
        timeout=(5, 20),
    )
    logger.info("tkkit response status=%s", resp.status_code)
    resp.raise_for_status()
    json_data = resp.json()
    try:
        if isinstance(json_data, list):
            logger.info(
                "tkkit response list size=%s keys=%s",
                len(json_data),
                list(json_data[0].keys()) if json_data else [],
            )
        elif isinstance(json_data, dict):
            logger.info("tkkit response dict keys=%s", list(json_data.keys()))
        else:
            logger.info("tkkit response type=%s", type(json_data).__name__)
    except Exception:
        logger.exception("tkkit response log failed")


    price_raw = None
    days_raw = None
    insurance_raw = None

    try:
        t = json_data[0]["01"]["detail"][3]
        _BANNED.append((from_city, to_city))
        return None, None, {}, None
    except Exception:
        pass
    
    try:
        if json_data[0]["01"]["detail"][2]['name'] == "Льготная доставка":
            _BANNED.append((from_city, to_city))
            return None, None, {}, None
    except Exception:
        pass

    try:
        price_raw = json_data[0]["01"]["detail"][0]["price"]
    except Exception:
        logger.exception("tkkit price parse failed")
        price_raw = None

    try:
        days_raw = json_data[0]["01"]["time"] if int(json_data[0]["01"]["time"]) != 0 else 1
    except Exception:
        logger.exception("tkkit days parse failed")
        days_raw = None



    try:
        insurance_raw = json_data[0]["01"]["detail"][1]["price"]
    except Exception:
        logger.exception("tkkit insurance parse failed")
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
        # allowances["Страхование"] = insurance_raw
        allowances["Страхование"] = "Временно недоступно"


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
        except InvalidInputError:
            raise
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
