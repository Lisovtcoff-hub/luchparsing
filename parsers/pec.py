"""Модуль pec.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional, List, Tuple, Dict, Any
import threading

import httpx
import requests
import math
from dataclasses import dataclass

from core.contracts import CarrierAdapter, CalcParams, CalcResult, TemporaryError, InvalidInputError


@dataclass(frozen=True)
class BoxResult:
    L: int
    W: int
    H: int
    target_cm3: float
    actual_cm3: int
    abs_error_cm3: float
    rel_error: float


def best_dimensions_cm(
    total_m3: float,
    places: int,
    margin_cm: int = 80,
    min_side_cm: int = 5,
) -> BoxResult:
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
    c = target_cm3 ** (1 / 3)  # оценка стороны куба (см)

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
                    best = (
                        key,
                        BoxResult(
                            L=L2,
                            W=W2,
                            H=H2,
                            target_cm3=target_cm3,
                            actual_cm3=actual,
                            abs_error_cm3=abs_err,
                            rel_error=rel_err,
                        ),
                    )

    return best[1]


HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

PEC_SITE = "https://pecom.ru/"
TOWNS_URL = "https://pecom.ru/ru/calc/towns.php"
CALC_URL = "https://calc.pecom.ru/bitrix/components/pecom/calc/ajax.php"

_TOWNS_CACHE: Optional[Dict[str, Dict[str, str]]] = None
_NO_CITY_LOCK = threading.Lock()
_NO_CITY_ROUTES: set[tuple[str, str]] = set()
_BANNED = ["Благовещенск"]


def _route_key(from_city: str, to_city: str) -> tuple[str, str]:
    return (_norm(from_city).lower(), _norm(to_city).lower())


def _remember_no_city(from_city: str, to_city: str) -> None:
    with _NO_CITY_LOCK:
        _NO_CITY_ROUTES.add(_route_key(from_city, to_city))


def _is_no_city(from_city: str, to_city: str) -> bool:
    with _NO_CITY_LOCK:
        return _route_key(from_city, to_city) in _NO_CITY_ROUTES


def _load_towns() -> Dict[str, Dict[str, str]]:
    """Грузит справочник городов ПЭК (кэшируется в памяти)."""
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
    """Нормализация названий городов для сравнения."""
    s = (s or "").strip()
    s = s.replace("г. ", "").replace("г.", "").replace("город ", "")
    s = s.replace("ё", "е").replace("Ё", "Е")
    return s.lower()


def find_city(name: str) -> Optional[str]:
    """Возвращает ID города ПЭК. Мягкий матчинг по названию."""
    if name in _BANNED:
        return None
    data = _load_towns()
    target = _norm(name)
    if not target:
        return None

    # точное совпадение
    for _, id2name in data.items():
        for city_id, city_name in id2name.items():
            if _norm(city_name) == target:
                return city_id

    # мягкое
    for _, id2name in data.items():
        for city_id, city_name in id2name.items():
            cn = _norm(city_name)
            if cn.startswith(target) or target.startswith(cn):
                return city_id
            if target in cn:
                return city_id

    return None


# есть ли терминал в городе

def _safe_branch_name(region: Any) -> Optional[str]:
    if isinstance(region, dict):
        bn = region.get("BranchName")
        if isinstance(bn, str) and bn.strip():
            return bn.strip()
    return None


def _has_terminal(from_city: str, to_city: str, data: Dict[str, Any]) -> tuple[Optional[bool], Optional[bool]]:
    """
    Возвращает:
      has_from - True/False/None
      has_to   - True/False/None
    None = если ответ не содержит region_from/region_to
    """
    actual_from = _safe_branch_name(data.get("region_from"))
    actual_to = _safe_branch_name(data.get("region_to"))

    has_from: Optional[bool] = None
    has_to: Optional[bool] = None

    if actual_from is not None:
        has_from = (_norm(actual_from) == _norm(from_city))

    if actual_to is not None:
        has_to = (_norm(actual_to) == _norm(to_city))

    return has_from, has_to


def pec(
    from_city: str,
    to_city: str,
    places: int,
    weight_kg: float,
    volume_m3: float,
    dims_cm_json: Dict[str, float],
) -> tuple[Optional[float], Optional[str], Optional[Dict[str, Any]], str]:
    """
    Возвращает:
      total      - итоговая стоимость (авто + страхование), если есть
      norm_days  - срок как строка
      allowances - доп. сборы (например, страхование)
      source     - источник

    ВАЖНО:
      Если в from_city или to_city нет терминала ПЭК (подмена филиала),
      то возвращаем (None, None, None, PEC_SITE).
    """
    if _is_no_city(from_city, to_city):
        raise InvalidInputError(f"pec: no terminal for route {from_city!r} -> {to_city!r} (cached)")

    take_id = find_city(from_city)
    deliver_id = find_city(to_city)
    if take_id is None or deliver_id is None:
        _remember_no_city(from_city, to_city)
        raise InvalidInputError(f"pec: city not found: {from_city!r} -> {to_city!r}")

    q = max(1, int(places))
    per_w = float(weight_kg) / q
    per_v = float(volume_m3) / q

    params: List[Tuple[str, str | float | int]] = [
        ("take[town]", take_id),
        ("deliver[town]", deliver_id),
    ]
    br = best_dimensions_cm(volume_m3, places)

    for i in range(q):
        params.extend(
            [
                (f"places[{i}][]", float(br.W / 100)),
                (f"places[{i}][]", float(br.L / 100)),
                (f"places[{i}][]", float(br.H / 100)),
                (f"places[{i}][]", per_v),
                (f"places[{i}][]", per_w),
                (f"places[{i}][]", 0),
                (f"places[{i}][]", 0),
            ]
        )

    resp = requests.get(
        CALC_URL,
        params=params,
        headers={"Accept": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("pec: unexpected calc payload")

    # ---- терминалы ----
    has_from, has_to = _has_terminal(from_city, to_city, data)

    # проверкк н а тк нет в городе
    if has_from is False or has_to is False:
        _remember_no_city(from_city, to_city)
        raise InvalidInputError(f"pec: no terminal for route {from_city!r} -> {to_city!r}")

    # --- цена ---
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

    # --- дни ---
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

    # --- допы ---
    allowances: Dict[str, Any] = {}
    add_3 = data.get("ADD_3")
    add_key = "Страхование"
    if isinstance(add_3, dict):
        v = add_3.get("3")
        if v is not None:
            allowances[add_key] = v

    source = PEC_SITE
    add_val = float(allowances.get(add_key) or 0)
    total = round((price + add_val), 2) if price is not None else None

    return total, norm_days, allowances, source



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

            price, days, allowances, source = await asyncio.to_thread(
                pec,
                p.from_city,
                p.to_city,
                int(p.places),
                float(p.weight_kg),
                float(p.volume_m3),
                dims_cm,
            )


        except InvalidInputError:
            raise
        except TemporaryError:
            raise
        except Exception as e:
            raise TemporaryError(f"pec: {type(e).__name__}: {e}") from e

        return CalcResult(
            price=float(price) if price is not None else None,
            days=days if days is not None else None,
            currency="RUB",
            source=source,
            name_tarif=None,
            allowances=allowances,
        )


__all__ = ["pec", "find_city", "PecAdapter"]
