"""Модуль energiya.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import os
import asyncio
import json
import threading
import time
from typing import Any, Dict, Optional, Tuple

import httpx
import requests

from core.contracts import CarrierAdapter, CalcParams, CalcResult, InvalidInputError, TemporaryError

import math
from dataclasses import dataclass

@dataclass(frozen=True)
class BoxResult:
    L: int
    W: int
    H: int
    target_cm3: float
    actual_cm3: int
    abs_error_cm3: float
    rel_error: float

def best_dimensions_cm(total_m3: float, places: int,
                       margin_cm: int = 80,
                       min_side_cm: int = 5) -> BoxResult:
    """
    Подбирает целые L,W,H (см) для 1 места так, чтобы L*W*H был максимально близок
    к объёму на место, и при этом коробка была "нормальной", а не 83333x1x1.

    margin_cm  - запас к кубическому корню объёма, чтобы расширить поиск
    min_side_cm- минимальная сторона (см), чтобы исключить 1-2 см "иголки"
    """

    if total_m3 <= 0:
        raise ValueError("total_m3 должен быть > 0")
    if not (1 <= places <= 80):
        raise ValueError("places должен быть 1..80")

    target_cm3 = (total_m3 / places) * 1_000_000.0  # см^3 на 1 место
    c = target_cm3 ** (1/3)                          # оценка стороны куба (см)

    max_side = int(math.ceil(c + margin_cm))
    max_side = max(max_side, min_side_cm)

    best = None

    for L in range(min_side_cm, max_side + 1):
        for W in range(min_side_cm, L + 1):
            base = L * W
            h_real = target_cm3 / base
            H0 = int(round(h_real))

            # проверяем несколько ближайших H вокруг округления
            for H in (H0 - 2, H0 - 1, H0, H0 + 1, H0 + 2):
                if H < min_side_cm or H > max_side:
                    continue

                actual = base * H
                abs_err = abs(actual - target_cm3)
                rel_err = abs_err / target_cm3

                dims = sorted((L, W, H), reverse=True)
                L2, W2, H2 = dims

                # метрика "насколько это похоже на коробку", а не на иголку
                cube_score = (L2 - H2)  # меньше = более "квадратно"

                # 1) сначала точность по объёму
                # 2) потом "квадратность"
                # 3) потом меньшая максимальная сторона
                key = (rel_err, cube_score, L2, (L2 + W2 + H2))

                if best is None or key < best[0]:
                    best = (key, BoxResult(
                        L=L2, W=W2, H=H2,
                        target_cm3=target_cm3,
                        actual_cm3=actual,
                        abs_error_cm3=abs_err,
                        rel_error=rel_err
                    ))

    return best[1]



DEV_TOKEN = os.getenv("ENERGIYA_DEV_TOKEN") or ""
BASE = os.getenv("ENERGIYA_BASE_URL") or "https://mainapi.nrg-tk.ru/"

TOKEN = os.getenv("ENERGIYA_TOKEN") or DEV_TOKEN
HEADERS = {
    "Accept": "application/json",
    "NrgApi-DevToken": DEV_TOKEN,
    "Content-Type": "application/json",
}

ENERGIYA_SOURCE = "https://nrg-tk.ru/"

# --- Cities cache ---
# В исходной версии find_city ходил в /v3/cities на каждый пресет, из-за чего
# под высокой параллельностью легко ловится connect timeout. Кешируем cityList
# на длительное время (по умолчанию сутки).

ENERGIYA_CITIES_TTL_S = float(os.getenv("ENERGIYA_CITIES_TTL_S", "86400") or 86400)
ENERGIYA_CONNECT_TIMEOUT_S = float(os.getenv("ENERGIYA_CONNECT_TIMEOUT_S", "25") or 25)
ENERGIYA_READ_TIMEOUT_S = float(os.getenv("ENERGIYA_READ_TIMEOUT_S", "60") or 60)

_CITIES_LOCK = threading.Lock()
_CITIES_CACHE_AT: float = 0.0
_CITIES_CACHE: Dict[str, int] = {}


def _norm_city(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def find_city(name: str) -> Optional[int]:
    """Функция find_city.

    Параметры:
        name: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    global _CITIES_CACHE_AT
    key = _norm_city(name)
    now = time.time()

    with _CITIES_LOCK:
        if _CITIES_CACHE and (now - _CITIES_CACHE_AT) < ENERGIYA_CITIES_TTL_S:
            return _CITIES_CACHE.get(key)

    resp = requests.get(
        url=f"{BASE}v3/cities",
        headers=HEADERS,
        timeout=(ENERGIYA_CONNECT_TIMEOUT_S, ENERGIYA_READ_TIMEOUT_S),
    )
    resp.raise_for_status()
    json_data = resp.json() or {}

    mapping: Dict[str, int] = {}
    for city in (json_data.get("cityList") or []):
        try:
            cid = city.get("id")
            cname = city.get("name")
            if cid is None or not cname:
                continue
            mapping[_norm_city(str(cname))] = int(cid)
        except Exception:
            continue

    with _CITIES_LOCK:
        _CITIES_CACHE.clear()
        _CITIES_CACHE.update(mapping)
        _CITIES_CACHE_AT = time.time()

    return _CITIES_CACHE.get(key)


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

    if not id_city_from:
        raise InvalidInputError(f"energiya: не найден город отправления: {from_city}")
    if not id_city_to:
        raise InvalidInputError(f"energiya: не найден город назначения: {to_city}")

    q = max(1, int(places))
    
    r = best_dimensions_cm(volume_m3, q)
    items = []
    for _ in range(q):
        items.append(
            {
                "weight": float(weight_kg) / q,
                "width": r.W/100,
                "height": r.H/100,
                "length": r.L/100,
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

    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                f"{BASE}v3/price",
                headers=HEADERS,
                data=json.dumps(payload, ensure_ascii=False),
                timeout=(ENERGIYA_CONNECT_TIMEOUT_S, ENERGIYA_READ_TIMEOUT_S),
            )
            resp.raise_for_status()
            json_data = resp.json()
            break
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            if attempt >= 3:
                raise
            time.sleep(0.7 * attempt)
            continue
    else:
        # should never happen
        raise last_exc or RuntimeError("energiya: request failed")

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

        except InvalidInputError:
            raise
        except TemporaryError:
            raise
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
