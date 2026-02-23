"""Модуль energiya.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import os
import re
import asyncio
import json
import threading
import random
import time
import logging
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
logger = logging.getLogger(__name__)

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
_CITIES_CACHE_PATH = os.getenv("ENERGIYA_CITIES_CACHE_PATH", "/app/exports/energiya_cities.json")

# route-level timeout ban for current run
_NO_ROUTE_ROUTES: set[tuple[str, str]] = set()
_ROUTE_TIMEOUTS: dict[tuple[str, str], int] = {}
_NET_ERR_LOCK = threading.Lock()
_NET_ERR_STREAK = 0
_COOLDOWN_UNTIL_TS = 0.0
ENERGIYA_COOLDOWN_AFTER_ERRORS = int(os.getenv("ENERGIYA_COOLDOWN_AFTER_ERRORS", "3") or 3)
ENERGIYA_COOLDOWN_S = float(os.getenv("ENERGIYA_COOLDOWN_S", "90") or 90)


def _route_key(from_city: str, to_city: str) -> tuple[str, str]:
    return (from_city or "").strip().lower(), (to_city or "").strip().lower()


def _is_no_route(from_city: str, to_city: str) -> bool:
    return _route_key(from_city, to_city) in _NO_ROUTE_ROUTES


def _remember_no_route(from_city: str, to_city: str) -> None:
    _NO_ROUTE_ROUTES.add(_route_key(from_city, to_city))


def _bump_route_timeout(from_city: str, to_city: str) -> int:
    key = _route_key(from_city, to_city)
    cnt = _ROUTE_TIMEOUTS.get(key, 0) + 1
    _ROUTE_TIMEOUTS[key] = cnt
    return cnt


def _is_cooldown_active() -> bool:
    now = time.time()
    with _NET_ERR_LOCK:
        return _COOLDOWN_UNTIL_TS > now


def _activate_cooldown() -> None:
    global _COOLDOWN_UNTIL_TS
    with _NET_ERR_LOCK:
        _COOLDOWN_UNTIL_TS = time.time() + max(0.0, ENERGIYA_COOLDOWN_S)


def _mark_network_error() -> None:
    global _NET_ERR_STREAK
    with _NET_ERR_LOCK:
        _NET_ERR_STREAK += 1
        streak = _NET_ERR_STREAK
    if streak >= max(1, ENERGIYA_COOLDOWN_AFTER_ERRORS):
        _activate_cooldown()


def _mark_network_success() -> None:
    global _NET_ERR_STREAK, _COOLDOWN_UNTIL_TS
    with _NET_ERR_LOCK:
        _NET_ERR_STREAK = 0
        _COOLDOWN_UNTIL_TS = 0.0


def _load_cities_cache_from_disk() -> None:
    global _CITIES_CACHE_AT
    path = _CITIES_CACHE_PATH
    if not path:
        return
    try:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            mapping = data.get("mapping") if "mapping" in data else data
            ts = data.get("ts")
            if isinstance(mapping, dict) and mapping:
                _CITIES_CACHE.clear()
                _CITIES_CACHE.update({str(k): int(v) for k, v in mapping.items()})
                _CITIES_CACHE_AT = float(ts) if ts else time.time()
    except Exception:
        # ignore cache load errors
        return


def _save_cities_cache_to_disk(mapping: Dict[str, int]) -> None:
    path = _CITIES_CACHE_PATH
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "mapping": mapping}, f, ensure_ascii=False)
    except Exception:
        # ignore cache save errors
        return


_NO_PICK_RE = re.compile(r"\s+не\s+выбирать\b.*$", re.IGNORECASE)

def _norm_city(name: str) -> str:
    s = " ".join((name or "").strip().lower().split())
    s = _NO_PICK_RE.sub("", s)
    return s.strip(" ,.!?;:-")


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
        if not _CITIES_CACHE:
            _load_cities_cache_from_disk()

        if _CITIES_CACHE and (now - _CITIES_CACHE_AT) < ENERGIYA_CITIES_TTL_S:
            hit = _CITIES_CACHE.get(key)
            if hit is not None:
                return hit

    max_attempts = int(os.getenv("ENERGIYA_CITIES_MAX_RETRIES", "3") or 3)
    base_delay = float(os.getenv("ENERGIYA_CITIES_BACKOFF_BASE_S", "0.7") or 0.7)
    max_delay = float(os.getenv("ENERGIYA_CITIES_BACKOFF_MAX_S", "6.0") or 6.0)
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(
                url=f"{BASE}v3/cities",
                headers=HEADERS,
                timeout=(ENERGIYA_CONNECT_TIMEOUT_S, ENERGIYA_READ_TIMEOUT_S),
            )
            resp.raise_for_status()
            json_data = resp.json() or {}
            break
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            if attempt >= max_attempts:
                # fall back to cache if present
                with _CITIES_LOCK:
                    if _CITIES_CACHE:
                        return _CITIES_CACHE.get(key)
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            jitter = random.uniform(0, delay * 0.35)
            time.sleep(delay + jitter)
            continue
    else:
        raise last_exc or RuntimeError("energiya: cities request failed")

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
        _save_cities_cache_to_disk(_CITIES_CACHE)

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
    if _is_cooldown_active():
        raise TemporaryError("energiya: cooldown active [no-retry]")

    if _is_no_route(from_city, to_city):
        raise InvalidInputError(f"energiya: no terminal for route (cached): {from_city} -> {to_city}")

    try:
        id_city_from = find_city(from_city)
        id_city_to = find_city(to_city)
    except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
        _mark_network_error()
        logger.warning("energiya city lookup network error from=%s to=%s err=%s", from_city, to_city, e)
        raise TemporaryError(f"energiya: city lookup network error: {e} [no-retry]") from e

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
    max_attempts = int(os.getenv("ENERGIYA_MAX_RETRIES", "3") or 3)
    base_delay = float(os.getenv("ENERGIYA_BACKOFF_BASE_S", "0.7") or 0.7)
    max_delay = float(os.getenv("ENERGIYA_BACKOFF_MAX_S", "6.0") or 6.0)
    for attempt in range(1, max_attempts + 1):
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
            _mark_network_error()
            if attempt >= max_attempts:
                raise TemporaryError(f"energiya: network error: {e} [no-retry]") from e
            # exponential backoff + jitter
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            jitter = random.uniform(0, delay * 0.35)
            time.sleep(delay + jitter)
            continue
    else:
        # should never happen
        raise last_exc or RuntimeError("energiya: request failed")
    _mark_network_success()

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
