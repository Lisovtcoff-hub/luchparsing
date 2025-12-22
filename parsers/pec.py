"""Модуль pec.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional, List, Tuple, Dict, Any

import httpx
import requests

from core.contracts import CarrierAdapter, CalcParams, CalcResult, TemporaryError


HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

PEC_SITE = "https://pecom.ru/"
TOWNS_URL = "https://pecom.ru/ru/calc/towns.php"
CALC_URL = "https://calc.pecom.ru/bitrix/components/pecom/calc/ajax.php"

_TOWNS_CACHE: Optional[Dict[str, Dict[str, str]]] = None


def _load_towns() -> Dict[str, Dict[str, str]]:
    """Функция _load_towns.

    Возвращает:
        Результат выполнения функции.
    """
    global _TOWNS_CACHE
    if _TOWNS_CACHE is not None:
        return _TOWNS_CACHE

    resp = requests.post(
        TOWNS_URL,
        headers=HEADERS,
        timeout=(5, 20),
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("pec: unexpected towns.php payload")
    _TOWNS_CACHE = data
    return data


def _norm(s: str) -> str:
    """Функция _norm.

    Параметры:
        s: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    s = (s or "").strip()
    s = s.replace("г. ", "").replace("г.", "").replace("город ", "")
    s = s.replace("ё", "е").replace("Ё", "Е")
    return s.lower()


def find_city(name: str) -> Optional[str]:
    """Возвращает ID города ПЭК. Мягкий матчинг по названию."""
    data = _load_towns()
    target = _norm(name)
    if not target:
        return None

    for _, id2name in data.items():
        for city_id, city_name in id2name.items():
            if _norm(city_name) == target:
                return city_id

    for _, id2name in data.items():
        for city_id, city_name in id2name.items():
            cn = _norm(city_name)
            if cn.startswith(target) or target.startswith(cn):
                return city_id
            if target in cn:
                return city_id

    return None


def pec(
    from_city: str,
    to_city: str,
    places: int,
    weight_kg: float,
    volume_m3: float,
    dims_cm_json: Dict[str, float],
) -> tuple[Optional[float], Optional[str], Dict[str, Any], Dict[str, Any], str]:
    """Функция pec.

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
    take_id = find_city(from_city)
    deliver_id = find_city(to_city)
    if take_id is None or deliver_id is None:
        raise ValueError(f"pec: не найден город: from_city={from_city!r}, to_city={to_city!r}")


    l_m = float(dims_cm_json["l"]) / 100.0
    w_m = float(dims_cm_json["w"]) / 100.0
    h_m = float(dims_cm_json["h"]) / 100.0

    q = max(1, int(places))
    per_w = float(weight_kg) / q
    per_v = float(volume_m3) / q

    params: List[Tuple[str, str | float | int]] = [
        ("take[town]", take_id),
        ("deliver[town]", deliver_id),
    ]
    for i in range(q):
        params.extend(
            [
                (f"places[{i}][]", w_m),
                (f"places[{i}][]", l_m),
                (f"places[{i}][]", h_m),
                (f"places[{i}][]", per_v),
                (f"places[{i}][]", per_w),
                (f"places[{i}][]", 0),
                (f"places[{i}][]", 0),
            ]
        )

    r = requests.get(
        CALC_URL,
        params=params,
        headers={"Accept": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise ValueError("pec: unexpected calc payload")


    price: Optional[float] = None
    auto = data.get("auto")
    if isinstance(auto, list) and len(auto) >= 3 and isinstance(auto[2], (int, float)):
        price = float(auto[2])
    elif isinstance(auto, dict):
        pr = auto.get("price") or auto.get("total") or auto.get("sum")
        try:
            price = float(pr) if pr is not None else None
        except Exception:
            price = None


    days = data.get("periods_days")
    norm_days: Optional[str] = None
    if isinstance(days, dict):
        dmin = days.get("min")
        dmax = days.get("max")
        try:
            if dmin is not None and dmax is not None:
                norm_days = f"{int(dmin)}-{int(dmax)}"
            elif dmax is not None:
                norm_days = str(int(dmax))
            elif dmin is not None:
                norm_days = str(int(dmin))
        except Exception:
            norm_days = None
    elif isinstance(days, (int, float)):
        norm_days = str(int(days))
    elif isinstance(days, str):
        norm_days = days.strip() or None


    name_tarif: Dict[str, Any] = {}
    avia = data.get("avia")
    if isinstance(avia, list) and len(avia) >= 3:
        avia_price = avia[2]
        if isinstance(avia_price, (int, float)) and float(avia_price) > 0:
            name_tarif["Авиаперевозка"] = float(avia_price)
        elif avia_price is not None:
            name_tarif["Авиаперевозка"] = avia_price


    allowances: Dict[str, Any] = {}
    add_3 = data.get("ADD_3")
    if isinstance(add_3, dict):
        v = add_3.get("3")
        if v is not None:
            allowances["Страхование и организация"] = v

    source = PEC_SITE
    return price, norm_days, name_tarif, allowances, source


class PecAdapter(CarrierAdapter):
    """Класс PecAdapter.

    Инкапсулирует связанную функциональность модуля.
    """
    code = "pec"

    async def calc(self, client: httpx.AsyncClient, p: CalcParams) -> CalcResult:
        """Функция calc.

        Параметры:
            client: Описание параметра.
            p: Описание параметра.

        Возвращает:
            Результат выполнения функции.
        """
        try:
            dims_cm = {
                "l": float(p.dims.length_cm),
                "w": float(p.dims.width_cm),
                "h": float(p.dims.height_cm),
            }

            price, days, name_tarif_map, allowances, source = await asyncio.to_thread(
                pec,
                p.from_city,
                p.to_city,
                int(p.places),
                float(p.weight_kg),
                float(p.volume_m3),
                dims_cm,
            )

            name_tarif_json = json.dumps(name_tarif_map, ensure_ascii=False) if name_tarif_map else None

        except TemporaryError:
            raise
        except Exception as e:
            raise TemporaryError(f"pec: {type(e).__name__}: {e}") from e

        return CalcResult(
            price=float(price) if price is not None else None,
            days=days if days is not None else None,
            currency="RUB",
            source=source,
            name_tarif=name_tarif_json,
            allowances=allowances,
        )


__all__ = ["pec", "find_city", "PecAdapter"]
