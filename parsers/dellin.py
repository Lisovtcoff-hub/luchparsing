
"""Модуль dellin.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import os
import asyncio
import json
from datetime import date, datetime
from typing import Any, Dict, Optional

import httpx
import requests

from core.contracts import CalcParams, CalcResult, CarrierAdapter, TemporaryError


DELLIN_SITE = "https://www.dellin.ru/"
API_TERMINALS = "https://api.dellin.ru/v1/public/request_terminals.json"
API_CALC = "https://api.dellin.ru/v2/calculator.json"
APPKEY = os.getenv("DELLIN_APPKEY") or ""

API_TERMINALS = os.getenv("DELLIN_API_TERMINALS") or API_TERMINALS
API_CALC = os.getenv("DELLIN_API_CALC") or API_CALC
def _parse_dt(value: str | None) -> Optional[date]:
    """Функция _parse_dt.

    Параметры:
        value: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def calc_delivery_days(dates: Dict[str, Any], *, inclusive: bool = False, to_giveout: bool = False) -> Optional[int]:
    """Функция calc_delivery_days.

    Параметры:
        dates: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    start = _parse_dt(dates.get("derivalFromOspSender")) or _parse_dt(dates.get("arrivalToOspSender"))
    end_key = "giveoutFromOspReceiver" if to_giveout else "arrivalToOspReceiver"
    end = _parse_dt(dates.get(end_key)) or _parse_dt(dates.get("arrivalToOspReceiver"))
    if not start or not end:
        return None
    days = (end - start).days
    return days + 1 if inclusive else days


def _post(url: str, payload: dict) -> dict:
    """Функция _post.

    Параметры:
        url: Описание параметра.
        payload: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    r = requests.post(
        url,
        headers={"Content-Type": "application/json; charset=utf-8"},
        data=json.dumps(payload, ensure_ascii=False),
        timeout=30,
    )
    if r.status_code >= 400:
        try:
            err = r.json()
        except Exception:
            err = r.text
        raise requests.HTTPError(f"{r.status_code} {r.reason}: {err}", response=r)
    return r.json()


def _term(city: str, direction: str) -> str:
    """Функция _term.

    Параметры:
        city: Описание параметра.
        direction: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    payload = {"appkey": APPKEY, "direction": direction, "search": city}
    terminals = _post(API_TERMINALS, payload).get("terminals") or []
    if not terminals:
        raise TemporaryError(f"dellin: нет терминала для {city!r} ({direction})")
    for t in terminals:
        if t.get("default"):
            return str(t["id"])
    return str(terminals[0]["id"])


def dellin_calc(
    from_city: str,
    to_city: str,
    places: int,
    weight_kg: float,
    volume_m3: float,
    dims_cm: Dict[str, float],
) -> tuple[Optional[float], Optional[int], dict[str, float], Optional[str]]:
    """Функция dellin_calc.

    Параметры:
        from_city: Описание параметра.
        to_city: Описание параметра.
        places: Описание параметра.
        weight_kg: Описание параметра.
        volume_m3: Описание параметра.
        dims_cm: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    der_id = _term(from_city, "derival")
    arr_id = _term(to_city, "arrival")

    l_m = float(dims_cm["l"]) / 100.0
    w_m = float(dims_cm["w"]) / 100.0
    h_m = float(dims_cm["h"]) / 100.0

    per_place_weight = float(weight_kg) / max(1, int(places))

    payload = {
        "appkey": APPKEY,
        "delivery": {
            "deliveryType": {"type": "auto"},
            "derival": {
                "produceDate": date.today().isoformat(),
                "variant": "terminal",
                "terminalID": str(der_id),
            },
            "arrival": {"variant": "terminal", "terminalID": str(arr_id)},
        },
        "cargo": {
            "quantity": int(places),
            "length": l_m,
            "width": w_m,
            "height": h_m,
            "weight": per_place_weight,
            "totalWeight": float(weight_kg),
            "totalVolume": float(volume_m3),
        },
        "payment": {"type": "cash", "paymentCitySearch": {"search": from_city}},
    }

    resp = _post(API_CALC, payload)
    data = resp.get("data") or {}

    intercity = data.get("intercity") or {}
    price_raw = intercity.get("price") or data.get("price")

    price: Optional[float]
    try:
        price = float(price_raw) if price_raw is not None else None
    except Exception:
        price = None

    dates = data.get("orderDates") or data.get("dates") or {}
    days = calc_delivery_days(dates)

    insurance_raw = data.get("insurance")
    notify_raw = data.get("notify") or {}
    notify_price_raw = notify_raw.get("price")

    allowances: dict[str, float] = {}
    try:
        if insurance_raw is not None:
            allowances["Страхование"] = float(insurance_raw)
    except Exception:
        pass
    try:
        if notify_price_raw is not None:
            allowances["Уведомление"] = float(notify_price_raw)
    except Exception:
        pass




    name_tarif_json: Optional[str] = None

    return price, days, allowances, name_tarif_json


class DellinAdapter(CarrierAdapter):
    """Класс DellinAdapter.

    Инкапсулирует связанную функциональность модуля.
    """
    code = "dellin"

    async def calc(self, client: httpx.AsyncClient, p: CalcParams) -> CalcResult:
        """Функция calc.

        Параметры:
            client: Описание параметра.
            p: Описание параметра.

        Возвращает:
            Результат выполнения функции.
        """
        if not APPKEY:
            raise TemporaryError("dellin: не задана переменная окружения DELLIN_APPKEY")
        try:
            dims_cm = {
                "l": float(p.dims.length_cm),
                "w": float(p.dims.width_cm),
                "h": float(p.dims.height_cm),
            }

            price, days, allowances, name_tarif_json = await asyncio.to_thread(
                dellin_calc,
                p.from_city,
                p.to_city,
                int(p.places),
                float(p.weight_kg),
                float(p.volume_m3),
                dims_cm,
            )
        except TemporaryError:
            raise
        except Exception as e:
            raise TemporaryError(f"dellin: {type(e).__name__}: {e}") from e

        return CalcResult(
            price=price,
            days=days,
            currency="RUB",
            source=DELLIN_SITE,
            name_tarif=name_tarif_json,
            allowances=allowances,
        )
