
"""Модуль dellin.

Содержит прикладную логику и точки входа проекта.

ВАЖНО! ТУТ Я СДЕЛАЛ +5 ЧАСОВ ДЛЯ ВРЕМЕНИ РАССЧЕТА, ПОТОМУ ЧТО НА ЛОКАЛКЕ У МЕНЯ БЫЛА ПРОБЛЕМА С ВРЕМЕМ "В ПРОШЛОМ"
"""

from __future__ import annotations

import os
import asyncio
import json
import random
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

import httpx
import requests
from requests.adapters import HTTPAdapter

from core.contracts import CalcParams, CalcResult, CarrierAdapter, InvalidInputError, TemporaryError


DELLIN_SITE = "https://www.dellin.ru/"
API_TERMINALS = "https://api.dellin.ru/v1/public/request_terminals.json"
API_CALC = "https://api.dellin.ru/v2/calculator.json"
APPKEY = os.getenv("DELLIN_APPKEY") or ""

API_TERMINALS = os.getenv("DELLIN_API_TERMINALS") or API_TERMINALS
API_CALC = os.getenv("DELLIN_API_CALC") or API_CALC

# API Dellin может иногда разрывать TLS-соединения (UNEXPECTED_EOF_WHILE_READING).
# Две ключевые меры для смягчения проблемы:
# 1) повторное использование соединений через requests.Session для каждого потока + пул соединений
# 2) кэширование ID терминалов (одинаковые города повторяются в разных предустановках)

DELLIN_CONNECT_TIMEOUT = float(os.getenv("DELLIN_CONNECT_TIMEOUT", "10"))
DELLIN_READ_TIMEOUT = float(os.getenv("DELLIN_READ_TIMEOUT", "30"))
DELLIN_MAX_ATTEMPTS = int(os.getenv("DELLIN_MAX_ATTEMPTS", "5"))
DELLIN_BACKOFF_BASE = float(os.getenv("DELLIN_BACKOFF_BASE", "0.4"))
DELLIN_BACKOFF_MAX = float(os.getenv("DELLIN_BACKOFF_MAX", "6"))
_BANNED = ["Благовещенск"]


PRODUCE_DATE_MAX_SHIFT_DAYS = int(os.getenv('DELLIN_PRODUCE_DATE_MAX_SHIFT_DAYS', '7'))

def _next_workday(d: date) -> date:
    """Сдвигает дату на ближайший рабочий день (Пн–Пт)."""
    wd = d.weekday()  # Mon=0..Sun=6
    if wd == 5:  # Sat
        return d + timedelta(days=2)
    if wd == 6:  # Sun
        return d + timedelta(days=1)
    return d

def _initial_produce_date() -> date:
    """Базовая дата забора груза для Dellin.
    Dellin часто не принимает produceDate=сегодня на выходных, поэтому
    выбираем ближайший рабочий день.
    """
    # В проекте исторически +5 часов из-за локального времени в прошлом.
    d = (datetime.utcnow() + timedelta(hours=5)).date()
    return _next_workday(d)

_THREAD_LOCAL = threading.local()




class DellinDateUnavailable(Exception):
    """Dellin API: выбранная дата забора (produceDate) недоступна для терминала."""
    def __init__(self, payload: Any):
        super().__init__("produceDate unavailable")
        self.payload = payload
def _get_session() -> requests.Session:
    """One Session per thread (requests.Session is not designed for heavy cross-thread sharing)."""
    sess = getattr(_THREAD_LOCAL, "session", None)
    if sess is None:
        sess = requests.Session()
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16)
        sess.mount("https://", adapter)
        sess.headers.update(
            {
                "Content-Type": "application/json; charset=utf-8",
                # Некоторые специфичные сетевые конфигурации работают лучше с явным указанием User-Agent
                "User-Agent": "luchparsing/1.0 (+https://example.invalid)",
            }
        )
        _THREAD_LOCAL.session = sess
    return sess


_TERM_CACHE: dict[tuple[str, str], tuple[str, str]] = {}
_TERM_CACHE_LOCK = threading.Lock()

_TERM_INFLIGHT_LOCK = threading.Lock()
_TERM_INFLIGHT: dict[tuple[str, str], threading.Lock] = {}

_NO_CITY_LOCK = threading.Lock()
_NO_CITY_ROUTES: set[tuple[str, str]] = set()


def _route_key(from_city: str, to_city: str) -> tuple[str, str]:
    return (_clean_city(from_city).lower(), _clean_city(to_city).lower())


def _remember_no_city(from_city: str, to_city: str) -> None:
    with _NO_CITY_LOCK:
        _NO_CITY_ROUTES.add(_route_key(from_city, to_city))


def _is_no_city(from_city: str, to_city: str) -> bool:
    with _NO_CITY_LOCK:
        return _route_key(from_city, to_city) in _NO_CITY_ROUTES


def _clean_city(city: str) -> str:
    return " ".join((city or "").strip().split())


def _get_key_lock(key: tuple[str, str]) -> threading.Lock:
    # Одна блокировка на (направление, город) для исключения дублирующихся одновременных запросов терминалов
    with _TERM_INFLIGHT_LOCK:
        lock = _TERM_INFLIGHT.get(key)
        if lock is None:
            lock = threading.Lock()
            _TERM_INFLIGHT[key] = lock
        return lock


def _parse_dt(value: str | None) -> Optional[date]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            dt = dt + timedelta(hours=5)
            return dt.date()
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
    sess = _get_session()

    def _sleep_for_attempt(attempt: int, *, retry_after_s: float | None = None) -> None:
        # Экспоненциальная задержка с добавлением случайной составляющей. Ограничение, чтобы избежать слишком долгого ожидания
        if retry_after_s is not None and retry_after_s > 0:
            delay = min(float(retry_after_s), DELLIN_BACKOFF_MAX)
        else:
            delay = min(DELLIN_BACKOFF_MAX, DELLIN_BACKOFF_BASE * (2 ** (attempt - 1)))
            delay += random.random() * 0.25
        time.sleep(delay)

    last_exc: Exception | None = None
    for attempt in range(1, max(1, DELLIN_MAX_ATTEMPTS) + 1):
        try:
            r = sess.post(
                url,
                json=payload,
                timeout=(DELLIN_CONNECT_TIMEOUT, DELLIN_READ_TIMEOUT),
            )

            # Временные HTTP-статусы: повторный запрос
            if r.status_code in (429, 500, 502, 503, 504):
                retry_after = r.headers.get("Retry-After")
                retry_after_s: float | None = None
                if retry_after:
                    try:
                        retry_after_s = float(retry_after)
                    except Exception:
                        retry_after_s = None
                last_exc = requests.HTTPError(f"{r.status_code} {r.reason}", response=r)
                if attempt < DELLIN_MAX_ATTEMPTS:
                    _sleep_for_attempt(attempt, retry_after_s=retry_after_s)
                    continue
                raise last_exc

            if r.status_code >= 400:
                # Ошибки на стороне клиента: некоторые фактически "исправимы" путём корректировки produceDate (выходные/расписание терминала)
                try:
                    err = r.json()
                except Exception:
                    err = r.text

                # Dellin иногда возвращает код 180012 для delivery.derival.produceDate ("Выбранная дата недоступна")
                try:
                    errors = err.get("errors") if isinstance(err, dict) else None
                    if errors and isinstance(errors, list):
                        for it in errors:
                            if not isinstance(it, dict):
                                continue
                            if int(it.get("code") or 0) == 180012:
                                fields = it.get("fields") or []
                                if "delivery.derival.produceDate" in fields or not fields:
                                    raise DellinDateUnavailable(err)
                except DellinDateUnavailable:
                    raise
                except Exception:
                    # только парсинг по принципу best-effort
                    pass

                raise requests.HTTPError(f"{r.status_code} {r.reason}: {err}", response=r)

            return r.json()

        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            # типично для Dellin: SSLEOFError(UNEXPECTED_EOF_WHILE_READING).
            last_exc = e
            if attempt < DELLIN_MAX_ATTEMPTS:
                _sleep_for_attempt(attempt)
                continue
            raise

    # Не должно происходить, но чтобы mypy был доволен
    raise last_exc or RuntimeError("dellin: request failed")


def _term(city: str, direction: str) -> tuple[str, str]:
    """Функция _term.

    Параметры:
        city: Описание параметра.
        direction: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    city_search = _clean_city(city)
    key = (direction, city_search.lower())

    with _TERM_CACHE_LOCK:
        cached = _TERM_CACHE.get(key)
    if cached:
        return cached

    # Избегаем лавинного запроса: только один поток запрашивает терминалы для одной пары (направление, город)
    # в единицу времени. Остальные потоки будут ждать и затем использовать кэш.
    lock = _get_key_lock(key)
    with lock:
        with _TERM_CACHE_LOCK:
            cached = _TERM_CACHE.get(key)
        if cached:
            return cached

        payload = {"appkey": APPKEY, "direction": direction, "search": city_search}
        terminals = _post(API_TERMINALS, payload).get("terminals") or []
        if not terminals:
            # Это не временная ошибка: если терминала в городе нет, маршрут у перевозчика
            # в принципе не поддерживается. Не нужно ретраить и не нужно триггерить
            # circuit-breaker.
            raise InvalidInputError(f"dellin: нет терминала для {city_search!r} ({direction})")

        term_id: str | None = None
        term_name: str | None = None
        for t in terminals:
            if t.get("default"):
                term_id = str(t["id"])
                term_name = str(t['name'])
                break
        if term_id is None:
            term_id = str(terminals[0]["id"])
            term_name = str(terminals[0]['name'])

        with _TERM_CACHE_LOCK:
            _TERM_CACHE[key] = (term_id, term_name or "")
        return term_id, term_name or ""

def _sum_price_parts(base_price: Optional[float], *parts: Any) -> Optional[float]:
    """Safely sum optional price components."""
    if base_price is None:
        return None
    total = float(base_price)
    for part in parts:
        if part is None:
            continue
        try:
            total += float(part)
        except (TypeError, ValueError):
            continue
    return total


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
    if _is_no_city(from_city, to_city) or (to_city in _BANNED or from_city in _BANNED):
        raise InvalidInputError(f"dellin: нет терминала для {from_city} - {to_city} (cached)")

    try:
        der_id, der_name = _term(from_city, "derival")
        arr_id, arr_name = _term(to_city, "arrival")
    except InvalidInputError:
        _remember_no_city(from_city, to_city)
        raise
    if (from_city not in der_name) or (to_city not in arr_name):
        _remember_no_city(from_city, to_city)
        raise InvalidInputError(f"dellin: нет терминала для {from_city} - {to_city}")

    l_m = float(dims_cm["l"]) / 100.0
    w_m = float(dims_cm["w"]) / 100.0
    h_m = float(dims_cm["h"]) / 100.0
    # print(l_m, w_m, h_m)

    per_place_weight = float(weight_kg) / max(1, int(places))
    def dims_cube_cm(total_m3: float, places: int):
        v_per = total_m3          # м^3
        side_m = v_per ** (1/3)            # м
        return side_m, side_m, side_m

    l, w, h = dims_cube_cm(volume_m3, places) if places != 1 else (l_m, w_m, h_m)
    # print(l, w, h, places == 1)

    payload = {
        "appkey": APPKEY,
        "delivery": {
            "deliveryType": {"type": "auto"},
            "derival": {
                "produceDate": _initial_produce_date().isoformat(),
                "variant": "terminal",
                "terminalID": str(der_id),
            },
            "arrival": {"variant": "terminal", "terminalID": str(arr_id)},
        },
        "cargo": {
            "quantity": int(places),
            "length": l,
            "width": w,
            "height": h,
            "weight": per_place_weight,
            "totalWeight": float(weight_kg),
            "totalVolume": float(volume_m3),
        },
        "payment": {"type": "cash", "paymentCitySearch": {"search": from_city}},
    }

    # Dellin может вернуть 400 с кодом 180012 (produceDate недоступна для терминала).
    # В таком случае сдвигаем дату вперёд (до PRODUCE_DATE_MAX_SHIFT_DAYS) и пробуем снова.
    last_date_err: DellinDateUnavailable | None = None
    produce_date = _initial_produce_date()
    resp: dict[str, Any] | None = None
    for shift in range(0, max(0, PRODUCE_DATE_MAX_SHIFT_DAYS) + 1):
        cur_date = _next_workday(produce_date + timedelta(days=shift))
        payload["delivery"]["derival"]["produceDate"] = cur_date.isoformat()
        try:
            resp = _post(API_CALC, payload)
            last_date_err = None
            break
        except DellinDateUnavailable as e:
            last_date_err = e
            continue

    if resp is None:
        # Не удалось подобрать доступную дату — считаем временной ошибкой (можно повторить позже).
        raise TemporaryError(f"dellin: produceDate unavailable after {PRODUCE_DATE_MAX_SHIFT_DAYS}d: {getattr(last_date_err, 'payload', None)}")

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

    total_price = _sum_price_parts(price, insurance_raw, notify_price_raw)
    return total_price, days, allowances, name_tarif_json


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
        except InvalidInputError:
            raise
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
