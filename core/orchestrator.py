"""Модуль orchestrator.

Содержит планировщик расчётов (оркестратор) и устойчивую к сбоям модель исполнения.

Цели:
- Не терять задачи (каждый item -> строка в results).
- Держать параллелизм под контролем (общий + per-site).
- Правильно классифицировать ошибки: временные vs некорректные входные данные.
- Корректно работать с circuit-breaker (в т.ч. внутри одного job).
- Не «рубить» Selenium-адаптеры внешним таймаутом меньше их внутренних лимитов.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Tuple

import httpx

from core.contracts import CalcParams, CalcResult, CarrierAdapter, InvalidInputError, TemporaryError
from core.logging_setup import kv, manual_log
from database.db import Database

logger = logging.getLogger(__name__)

# --- HTTP defaults ---
HTTP_CONNECT_TIMEOUT = float(os.getenv("HTTP_CONNECT_TIMEOUT", "15").strip() or 15.0)
HTTP_READ_TIMEOUT = float(os.getenv("HTTP_READ_TIMEOUT", "30").strip() or 30.0)

# --- Orchestrator defaults ---
DEFAULT_SITE_LIMIT = 4

# Некоторые API начинают падать/таймаутиться при слишком большом параллелизме
# Это защитный кап поверх sites_config.parallel_limit..........................
CODE_PARALLEL_CAPS = {
    'energiya': int(os.getenv('CAP_PARALLEL_ENERGIYA', '2') or 2),
    'dellin': int(os.getenv('CAP_PARALLEL_DELLIN', '2') or 2),
    'jde': int(os.getenv('CAP_PARALLEL_JDE', '2') or 2),
}
DEFAULT_SITE_TIMEOUT = 30.0

# Ретраи: общие
MAX_RETRIES = 3
BACKOFF_BASE = 0.5
BACKOFF_MAX = 20.0

# Circuit breaker: защита от лавины ошибок по одному сайту
CIRCUIT_FAIL_THRESHOLD = int(os.getenv("CIRCUIT_FAIL_THRESHOLD", "12") or 12)
CIRCUIT_BLOCK_MINUTES = int(os.getenv("CIRCUIT_BLOCK_MINUTES", "2") or 2)

# Пакетная запись в БД
BATCH_SIZE = 200

# Очереди/воркеры
DEFAULT_WORKERS = 16
DEFAULT_QUEUE_MULTIPLIER = 4

# --- Tail retry ("догоняющий" прогон) ---
# Идея: если по (site, route) есть хотя бы один ok и есть пропуски из-за временных ошибок/блокировки,
# то в самом конце (после небольшой паузы) повторяем только эти пресеты.
# Дополнительно: если по (site, route) ВСЕ пресеты упали строго из-за сетевых ошибок,
# тоже делаем один догоняющий прогон (но не для "selenium wait timeout").

# Включается/выключается переменной окружения ORCH_TAIL_RETRY (1/0, true/false)
TAIL_RETRY_ENABLED = (os.getenv("ORCH_TAIL_RETRY", "1").strip().lower() not in {"0", "false", "no"})
# Пауза перед "догоняющим" прогоном (по умолчанию 30с)
TAIL_RETRY_DELAY_S = float(os.getenv("ORCH_TAIL_RETRY_DELAY_S", "30") or 30)
# Общий лимит параллельности во второй фазе (по умолчанию 6)
TAIL_RETRY_GLOBAL_LIMIT = int(os.getenv("ORCH_TAIL_RETRY_GLOBAL_LIMIT", "6") or 6)
# Доп. задержка между ретраями по одному сайту (по умолчанию 0.35s)
TAIL_RETRY_SITE_DELAY_S = float(os.getenv("ORCH_TAIL_RETRY_SITE_DELAY_S", "0.35") or 0.35)
# Защита от случайного взрыва количества ретраев (по умолчанию 800)
TAIL_RETRY_MAX_ITEMS = int(os.getenv("ORCH_TAIL_RETRY_MAX_ITEMS", "800") or 800)

# Сентинел для writer'а
_WORKER_DONE = object()


@asynccontextmanager
async def _noop_async():
    yield


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_dt(value: str) -> datetime:
    """Парсит ISO datetime, в т.ч. naive -> считаем UTC."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _params_key(p: CalcParams) -> Tuple[Any, ...]:
    """Ключ для дедупликации одновременных запросов на один и тот же расчёт."""
    d = p.dims
    return (
        p.from_city,
        p.to_city,
        p.places,
        round(p.weight_kg, 4),
        round(p.volume_m3, 4),
        round(d.length_cm, 1),
        round(d.width_cm, 1),
        round(d.height_cm, 1),
        p.client_type,
        p.service_type,
        p.pay_type,
    )


def _to_allowances_json(value: Any) -> str | None:
    if isinstance(value, dict) and value:
        return json.dumps(value, ensure_ascii=False)
    return None

def _context_json(p: CalcParams) -> str | None:
    try:
        payload = [p.from_city, p.to_city, p.places, p.weight_kg, p.volume_m3]
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        return None


def _truncate(s: str, limit: int = 500) -> str:
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def _is_networkish_error(msg: str | None) -> bool:
    """Грубая эвристика: похоже ли сообщение на сетевую/транспортную проблему.

    Нужна, чтобы во 2-й фазе НЕ перезапускать маршруты, где перевозчик просто
    не поддерживает города (и адаптер должен возвращать no_price/invalid_input).

    Важное исключение: 'selenium wait timeout' — часто означает "не нашли элемент"
    или "страница показала сообщение, а мы его не обработали". В таких случаях
    повторение "всего маршрута" бессмысленно.
    """
    if not msg:
        return False
    s = msg.lower()
    if "selenium" in s:
        return False
    needles = (
        "connecttimeout",
        "readtimeout",
        "timeout",
        "timed out",
        "too many requests",
        " 429",
        "ssl",
        "eof",
        "connection reset",
        "connection aborted",
        "connection refused",
        "remote disconnected",
        "name or service not known",
        "temporary failure",
        "service unavailable",
        " 502",
        " 503",
        " 504",
    )
    return any(n in s for n in needles)


@dataclass(frozen=True)
class WorkItem:
    site_id: int
    route_id: int
    preset_id: int
    params: CalcParams


@dataclass
class Outcome:
    site_id: int
    route_id: int
    preset_id: int

    price: float | None
    days: str | None
    name_tarif: str | None
    allowances_json: str | None
    context_json: str | None

    created_ts: float

    status: str  # ok | no_price | invalid_input | temporary_error | error | blocked | disabled | no_adapter
    error: str | None


async def _calc_with_retry(adapter: CarrierAdapter, client: httpx.AsyncClient, params: CalcParams) -> CalcResult:
    """Единый слой retry/backoff для адаптеров.

    Важно: InvalidInputError не ретраим.
    TemporaryError ретраим с экспоненциальной задержкой + jitter.
    """
    delay = BACKOFF_BASE
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return await adapter.calc(client, params)
        except InvalidInputError:
            raise
        except TemporaryError as e:
            last_exc = e

            msg = str(e) or type(e).__name__
            if "[no-retry]" in msg:
                raise
            # для 429/limit обычно нужно подольше подождать
            if "429" in msg or "rate" in msg.lower() or "limit" in msg.lower():
                delay = max(delay, 2.0)

            if attempt == MAX_RETRIES:
                raise
            # jitter
            await asyncio.sleep(min(BACKOFF_MAX, delay) + (0.1 * delay))
            delay = min(BACKOFF_MAX, delay * 2)
        except (httpx.TimeoutException, httpx.HTTPError) as e:
            last_exc = e
            if attempt == MAX_RETRIES:
                raise TemporaryError(str(e) or type(e).__name__) from e
            await asyncio.sleep(min(BACKOFF_MAX, delay) + (0.1 * delay))
            delay = min(BACKOFF_MAX, delay * 2)
        except Exception as e:
            # не нормализуем неожиданное — пусть решит внешний слой
            raise e

    if last_exc:
        raise last_exc
    raise RuntimeError("retry loop failure")


def _flush_batch(
    db: Database,
    job_id: int | None,
    batch: List[
        tuple[
            int,  # site_id
            int,  # route_id
            int,  # preset_id
            float | None,  # price
            str | None,  # days_str
            float,  # created_ts_unix
            str | None,  # name_tarif
            str | None,  # allowances_json
            str,  # status
            str | None,  # error
            str | None,  # context_json
        ]
    ],
) -> None:
    """Пишет батч результатов в БД.

    Формат batch:
      (site_id, route_id, preset_id, price, days_str, created_ts_unix, name_tarif, allowances_json, status, error, context_json)
    """
    if not batch:
        return

    if hasattr(db, "insert_results_batch"):
        try:
            db.insert_results_batch(batch, job_id=job_id)
            return
        except (TypeError, ValueError):
            # совместимость со старым форматом — попробуем обрезать до 8 полей
            pass

    # Fallback: построчно (дольше, но надёжно)
    if hasattr(db, "insert_result"):
        for (site_id, route_id, preset_id, price, days_str, _ts, name_tarif, allowances_json, status, error, context_json) in batch:
            db.insert_result(
                site_id=site_id,
                route_id=route_id,
                preset_id=preset_id,
                price=price,
                days=days_str,
                job_id=job_id,
                name_tarif=name_tarif,
                allowances=allowances_json,
                status=status,
                error=error,
                context_json=context_json,
            )
        return

    raise RuntimeError("Database has no suitable method to write results batch.")


async def run_orchestrator(
    *,
    db: Database,
    adapters: Dict[int, CarrierAdapter],
    items: Iterable[Tuple[int, int, int, CalcParams]],
    site_limits: Dict[int, int] | None = None,
    job_id: int | None = None,
    total_items: int | None = None,
) -> None:
    """Запускает расчёт по items и пишет результаты в БД."""

    # items часто приходят генератором (и его нельзя пройти дважды). Нам нужен
    # доступ к списку задач, чтобы:
    # 1) корректно считать total
    # 2) уметь сделать "tail retry" только по нужным пресетам
    items_list: List[Tuple[int, int, int, CalcParams]] = list(items)

    # Индексы для "tail retry": WorkItem по ключу и группировка по (site, route)
    items_by_key: Dict[Tuple[int, int, int], WorkItem] = {}
    keys_by_site_route: Dict[Tuple[int, int], List[Tuple[int, int, int]]] = defaultdict(list)
    for s, r, p, params in items_list:
        k = (int(s), int(r), int(p))
        wi = WorkItem(site_id=k[0], route_id=k[1], preset_id=k[2], params=params)
        items_by_key[k] = wi
        keys_by_site_route[(k[0], k[1])].append(k)

    site_limits = site_limits or {}
    cfg_rows = db.get_sites_config()
    cfg_by_site = {r["id_site"]: r for r in cfg_rows}

    # семафоры per-site
    sem_by_site: Dict[int, asyncio.Semaphore] = {}

    def _limit_for(site_id: int) -> int:
        if site_id in site_limits:
            base = max(1, int(site_limits[site_id]))
        else:
            row = cfg_by_site.get(site_id)
            if row and row.get("parallel_limit"):
                base = max(1, int(row["parallel_limit"]))
            else:
                base = DEFAULT_SITE_LIMIT

        # надежный кап сверху
        code = getattr(adapters.get(site_id), "code", None)
        if code and code in CODE_PARALLEL_CAPS:
            try:
                cap = int(CODE_PARALLEL_CAPS[code])
                if cap > 0:
                    base = min(base, cap)
            except Exception:
                pass
        return base

    for sid in adapters.keys():
        sem_by_site[sid] = asyncio.Semaphore(_limit_for(sid))

    global_limit_raw = os.getenv("ORCH_GLOBAL_LIMIT", "").strip()
    global_limit = int(global_limit_raw) if global_limit_raw.isdigit() else 0
    global_sem = asyncio.Semaphore(global_limit) if global_limit > 0 else None

    # ЛИМИТЫ ПАРАЛЛЕЛИЗМА
    workers_raw = os.getenv("ORCH_WORKERS", "").strip()
    workers = int(workers_raw) if workers_raw.isdigit() else 0
    if workers <= 0:
        workers = global_limit if global_limit > 0 else min(32, max(8, len(adapters) * 2, DEFAULT_WORKERS))

    q_mult_raw = os.getenv("ORCH_QUEUE_MULT", "").strip()
    q_mult = int(q_mult_raw) if q_mult_raw.isdigit() else DEFAULT_QUEUE_MULTIPLIER

    work_q: asyncio.Queue[WorkItem | None] = asyncio.Queue(maxsize=max(1, workers * q_mult))
    res_q: asyncio.Queue[Any] = asyncio.Queue(maxsize=max(1, workers * q_mult))

    # circuit breaker state (in-memory, чтобы блок работал внутри job)
    blocked_until: Dict[int, datetime] = {}
    fail_streak: Dict[int, int] = defaultdict(int)

    def _site_timeout(site_id: int) -> float:
        row = cfg_by_site.get(site_id)
        if row and row.get("timeout_s"):
            try:
                return max(1.0, float(row["timeout_s"]))
            except (TypeError, ValueError):
                pass
        return DEFAULT_SITE_TIMEOUT

    def _is_blocked(site_id: int) -> bool:
        now = _utcnow()
        local_until = blocked_until.get(site_id)
        if local_until and local_until > now:
            return True
        if local_until and local_until <= now:
            blocked_until.pop(site_id, None)

        row = cfg_by_site.get(site_id)
        if not row:
            return False
        val = row.get("disabled_until")
        if not val:
            return False
        try:
            until = _parse_iso_dt(str(val))
            return until > now
        except Exception:
            return False

    def _set_blocked(site_id: int, *, minutes: int, reason: str) -> None:
        until_dt = _utcnow() + timedelta(minutes=minutes)
        blocked_until[site_id] = until_dt
        until_str = until_dt.isoformat()

        # Пишем в БД и обновляем локальный снимок конфига, чтобы _is_blocked работал в этом же run
        db.set_site_disabled_until(site_id, until_str)
        row = cfg_by_site.get(site_id)
        if row is not None:
            row["disabled_until"] = until_str

        logger.warning(
            "circuit breaker opened " + kv(
                job_id=job_id,
                site_id=site_id,
                disabled_until=until_str,
                reason=reason,
            )
        )

    # total/progress (считаем по фактическому списку)
    total = len(items_list)

    # done_unique — количество обработанных уникальных (site,route,preset). Это
    # позволяет не "перешагивать" 100% прогресса, если в конце будет tail-retry.
    done_unique = 0
    seen_keys: set[Tuple[int, int, int]] = set()

    # Снимок исходных результатов (до tail retry), чтобы решить, какие пресеты
    # "догонять"
    phase1_outcomes: Dict[Tuple[int, int, int], Outcome] = {}
    last_progress_push = 0.0

    if job_id is not None:
        db.set_job_status(job_id, "running", progress=0.0)

    logger.info(
        "orchestrator start "
        + kv(
            job_id=job_id,
            items=total,
            adapters=len(adapters),
            workers=workers,
            global_limit=global_limit or None,
        )
    )

    async with AsyncExitStack() as stack:
        # per-site httpx clients (там где адаптеры реально используют client)
        clients_by_site: Dict[int, httpx.AsyncClient] = {}
        for sid in adapters.keys():
            read_timeout = max(HTTP_READ_TIMEOUT, _site_timeout(sid))
            clients_by_site[sid] = await stack.enter_async_context(
                httpx.AsyncClient(
                    timeout=httpx.Timeout(HTTP_CONNECT_TIMEOUT, read=read_timeout),
                    http2=True,
                )
            )

        inflight: Dict[Tuple[int, Tuple[Any, ...]], asyncio.Task] = {}
        inflight_lock = asyncio.Lock()

        async def _calc_normalized(
            site_id: int, params: CalcParams
        ) -> tuple[float | None, str | None, str | None, str | None, str, str | None]:
            """Считает одну цену и нормализует результат.

            Возвращает:
              price, days, name_tarif, allowances_json, status, error
            """
            cfg = cfg_by_site.get(site_id)
            if cfg and int(cfg.get("enabled", 1)) == 0:
                return None, None, None, None, "disabled", "site disabled"
            if _is_blocked(site_id):
                return None, None, None, None, "blocked", "site blocked by circuit breaker"

            adapter = adapters.get(site_id)
            if adapter is None:
                return None, None, None, None, "no_adapter", "adapter not configured"

            site_sem = sem_by_site[site_id]
            sem_ctx = global_sem if global_sem is not None else _noop_async()

            async with sem_ctx:
                async with site_sem:
                    try:
                        client = clients_by_site[site_id]
                        res: CalcResult = await _calc_with_retry(adapter, client, params)

                        fail_streak[site_id] = 0
                        db.set_site_last_error(site_id, None)

                        price = float(res.price) if res.price is not None else None
                        days = (str(res.days).strip() if res.days is not None else None)

                        name_tarif = None
                        if getattr(res, "name_tarif", None):
                            name_tarif = str(res.name_tarif).strip() or None

                        allowances_json = _to_allowances_json(getattr(res, "allowances", None))

                        if price is None:
                            return None, days, name_tarif, allowances_json, "no_price", "adapter returned price=None"
                        return price, days, name_tarif, allowances_json, "ok", None

                    except InvalidInputError as e:
                        # входные данные не поддерживаются конкретным перевозчиком — не трогаем circuit breaker
                        msg = _truncate(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
                        db.set_site_last_error(site_id, f"InvalidInput: {msg}")
                        return None, None, None, None, "invalid_input", msg

                    except TemporaryError as e:
                        fail_streak[site_id] += 1
                        msg = _truncate(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
                        db.set_site_last_error(site_id, f"TemporaryError: {msg}")

                        if fail_streak[site_id] >= CIRCUIT_FAIL_THRESHOLD:
                            _set_blocked(site_id, minutes=CIRCUIT_BLOCK_MINUTES, reason=msg)

                        return None, None, None, None, "temporary_error", msg

                    except Exception as e:
                        fail_streak[site_id] += 1
                        msg = _truncate(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
                        db.set_site_last_error(site_id, msg)

                        logger.exception(
                            "adapter failed "
                            + kv(
                                job_id=job_id,
                                site_id=site_id,
                                adapter=getattr(adapter, "code", adapter.__class__.__name__),
                            )
                        )

                        if fail_streak[site_id] >= CIRCUIT_FAIL_THRESHOLD:
                            _set_blocked(site_id, minutes=CIRCUIT_BLOCK_MINUTES, reason=msg)

                        return None, None, None, None, "error", msg

        async def run_one(item: WorkItem) -> Outcome:
            site_id, route_id, preset_id, params = item.site_id, item.route_id, item.preset_id, item.params
            created_ts = time.time()

            context_json = _context_json(params)

            # inflight-дедупликация на уровне site+params
            key = (site_id, _params_key(params))

            async with inflight_lock:
                task = inflight.get(key)
                if task is None:
                    task = asyncio.create_task(_calc_normalized(site_id, params))
                    inflight[key] = task

            try:
                price, days, name_tarif, allowances_json, status, err = await task
            finally:
                async with inflight_lock:
                    inflight.pop(key, None)

            # единый "result" лог (для анализа пропусков)
            manual_log(
                logger,
                logging.INFO,
                "result " + kv(
                    job_id=job_id,
                    site_id=site_id,
                    route_id=route_id,
                    preset_id=preset_id,
                    status=status,
                    price=price,
                    err=err,
                ),
            )

            return Outcome(
                site_id=site_id,
                route_id=route_id,
                preset_id=preset_id,
                price=price,
                days=days,
                name_tarif=name_tarif,
                allowances_json=allowances_json,
                created_ts=created_ts,
                status=status,
                error=err,
            )

        cancel_event = asyncio.Event()

        async def producer() -> None:
            try:
                for (s, r, p, params) in items_list:
                    if cancel_event.is_set():
                        break
                    await work_q.put(
                        WorkItem(site_id=int(s), route_id=int(r), preset_id=int(p), params=params)
                    )
            finally:
                for _ in range(workers):
                    await work_q.put(None)

        async def worker(worker_id: int) -> None:
            while True:
                item = await work_q.get()
                try:
                    if item is None:
                        await res_q.put(_WORKER_DONE)
                        return

                    if job_id is not None and db.is_job_cancelled(job_id):
                        cancel_event.set()
                        await res_q.put(_WORKER_DONE)
                        return

                    try:
                        out = await run_one(item)
                    except Exception as e:
                        # внутренняя ошибка оркестратора — всё равно пишем строку
                        msg = _truncate(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
                        logger.exception("orchestrator worker error " + kv(job_id=job_id, worker=worker_id))
                        out = Outcome(
                            site_id=item.site_id,
                            route_id=item.route_id,
                            preset_id=item.preset_id,
                            price=None,
                            days=None,
                            name_tarif=None,
                            allowances_json=None,
                            created_ts=time.time(),
                            status="error",
                            error=msg,
                        )
                    await res_q.put(out)
                finally:
                    work_q.task_done()

        async def writer() -> None:
            nonlocal done_unique, last_progress_push
            batch: List[tuple[int, int, int, float | None, str | None, float, str | None, str | None, str, str | None, str | None]] = []
            finished_workers = 0
            last_flush_ts = time.time()

            while finished_workers < workers:
                # cancel check
                if job_id is not None and db.is_job_cancelled(job_id):
                    cancel_event.set()

                msg = await res_q.get()
                if msg is _WORKER_DONE:
                    finished_workers += 1
                else:
                    out: Outcome = msg  # type: ignore[assignment]
                    batch.append(
                        (
                            out.site_id,
                            out.route_id,
                            out.preset_id,
                            out.price,
                            out.days,
                            float(out.created_ts),
                            out.name_tarif,
                            out.allowances_json,
                            out.status,
                            out.error,
                        out.context_json,
                        )
                    )
                    key = (out.site_id, out.route_id, out.preset_id)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        done_unique += 1
                        # фиксируем исходный результат (до tail retry)
                        phase1_outcomes[key] = out

                # flush by size or time (чтобы UI не "молчал" до конца)
                now = time.time()
                if batch and (len(batch) >= BATCH_SIZE or (now - last_flush_ts) >= 0.8):
                    await asyncio.to_thread(_flush_batch, db, job_id, batch)
                    batch.clear()
                    last_flush_ts = now

                # progress
                if job_id is not None and total and not cancel_event.is_set() and not db.is_job_cancelled(job_id):
                    if now - last_progress_push >= 0.5 or done_unique >= total:
                        db.set_job_status(job_id, "running", progress=round(done_unique / total * 100.0, 2))
                        last_progress_push = now

                if cancel_event.is_set():
                    # мягко выходим; оставшиеся воркеры тоже должны выйти
                    # (producer всё равно отправит None, а воркеры пришлют _WORKER_DONE)
                    continue

            if batch:
                await asyncio.to_thread(_flush_batch, db, job_id, batch)

        async def cancel_watcher() -> None:
            if job_id is None:
                return
            while True:
                await asyncio.sleep(0.4)
                if db.is_job_cancelled(job_id):
                    cancel_event.set()
                    return

        # run pipeline
        prod_task = asyncio.create_task(producer())
        worker_tasks = [asyncio.create_task(worker(i)) for i in range(workers)]
        writer_task = asyncio.create_task(writer())
        cancel_task = asyncio.create_task(cancel_watcher()) if job_id is not None else None

        wait_tasks = [writer_task]
        if cancel_task is not None:
            wait_tasks.append(cancel_task)

        done, _ = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)

        if cancel_task is not None and cancel_task in done and cancel_event.is_set():
            prod_task.cancel()
            for t in worker_tasks:
                t.cancel()
            writer_task.cancel()
            await asyncio.gather(prod_task, writer_task, *worker_tasks, return_exceptions=True)
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            if job_id is not None:
                progress = round(done_unique / total * 100.0, 2) if total else None
                db.set_job_status(job_id, "cancelled", progress=progress)
                logger.warning("job cancelled " + kv(job_id=job_id, progress=progress))
            return

        if cancel_task is not None:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

        await prod_task
        await work_q.join()
        await asyncio.gather(*worker_tasks, return_exceptions=True)
        await writer_task

        # финальный статус
        if job_id is not None:
            if db.is_job_cancelled(job_id) or cancel_event.is_set():
                progress = round(done_unique / total * 100.0, 2) if total else None
                db.set_job_status(job_id, "cancelled", progress=progress)
                logger.warning("job cancelled " + kv(job_id=job_id, progress=progress))
                return

        # --- tail retry phase ("догоняем" пропуски) ---
        if (
            TAIL_RETRY_ENABLED
            and not cancel_event.is_set()
            and phase1_outcomes
            and (job_id is None or not db.is_job_cancelled(job_id))
        ):
            retry_keys: set[Tuple[int, int, int]] = set()

            # 1) Всегда пытаемся повторить то, что было помечено как "blocked".
            for k, out in phase1_outcomes.items():
                if out.status == "blocked":
                    retry_keys.add(k)

            # 2) Для частичных маршрутов (есть ok и есть временные ошибки) — догоняем только упавшие пресеты.
            for (sid, rid), keys in keys_by_site_route.items():
                outs = [phase1_outcomes.get(k) for k in keys]
                if not outs or any(o is None for o in outs):
                    continue

                ok_count = sum(1 for o in outs if o and o.status == "ok")
                if ok_count > 0:
                    for k, o in zip(keys, outs):
                        if o and o.status in {"temporary_error", "error"}:
                            retry_keys.add(k)
                    continue

                # 3) Если по конкретному (site,route) вообще нет ok,
                # повторяем ТОЛЬКО когда ВСЕ пресеты упали на сетевых/транспортных ошибках.
                if all(o and o.status == "temporary_error" and _is_networkish_error(o.error) for o in outs):
                    retry_keys.update(keys)

            if retry_keys:
                # ограничиваем объём ретрая
                ordered_keys = sorted(
                    retry_keys,
                    key=lambda k: (
                        0 if phase1_outcomes.get(k) and phase1_outcomes[k].status == "blocked" else 1,
                        0 if phase1_outcomes.get(k) and phase1_outcomes[k].status == "temporary_error" else 1,
                        k[0],
                        k[1],
                        k[2],
                    ),
                )
                if len(ordered_keys) > TAIL_RETRY_MAX_ITEMS:
                    ordered_keys = ordered_keys[:TAIL_RETRY_MAX_ITEMS]

                retry_items: List[WorkItem] = [items_by_key[k] for k in ordered_keys if k in items_by_key]
                sites_in_retry = sorted({wi.site_id for wi in retry_items})

                # ждём чуть-чуть, а также дожидаемся, пока истечёт disabled_until
                sleep_s = max(0.0, float(TAIL_RETRY_DELAY_S))
                now_dt = _utcnow()
                for sid in sites_in_retry:
                    row = cfg_by_site.get(sid) or {}
                    val = row.get("disabled_until")
                    if val:
                        try:
                            until_dt = _parse_iso_dt(str(val))
                            wait = (until_dt - now_dt).total_seconds()
                            if wait > 0:
                                sleep_s = max(sleep_s, wait + 0.25)
                        except Exception:
                            pass

                logger.warning(
                    "tail retry scheduled "
                    + kv(job_id=job_id, retry_items=len(retry_items), sleep_s=round(sleep_s, 2), sites=sites_in_retry)
                )
                if job_id is not None:
                    # прогресс может уже быть 100 — это ок, статус остаётся running
                    db.set_job_status(job_id, "running", progress=round(done_unique / total * 100.0, 2) if total else None)

                if sleep_s > 0:
                    await asyncio.sleep(sleep_s)

                # сбрасываем streak, чтобы прошлые ошибки не мешали "догоняющему" этапу 
                for sid in sites_in_retry:
                    fail_streak[sid] = 0
                    blocked_until.pop(sid, None)

                retry_global_sem = asyncio.Semaphore(max(1, int(TAIL_RETRY_GLOBAL_LIMIT)))
                site_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

                async def _retry_one(wi: WorkItem) -> Outcome | None:
                    if cancel_event.is_set():
                        return None
                    if job_id is not None and db.is_job_cancelled(job_id):
                        cancel_event.set()
                        return None

                    async with retry_global_sem:
                        async with site_locks[wi.site_id]:
                            try:
                                out = await run_one(wi)
                            except Exception as e:
                                msg = _truncate(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
                                logger.exception("tail retry error " + kv(job_id=job_id, site_id=wi.site_id))
                                out = Outcome(
                                    site_id=wi.site_id,
                                    route_id=wi.route_id,
                                    preset_id=wi.preset_id,
                                    price=None,
                                    days=None,
                                    name_tarif=None,
                                    allowances_json=None,
                                    created_ts=time.time(),
                                    status="error",
                                    error=msg,
                                )
                            if TAIL_RETRY_SITE_DELAY_S > 0:
                                await asyncio.sleep(float(TAIL_RETRY_SITE_DELAY_S))
                            return out

                retry_tasks = [asyncio.create_task(_retry_one(wi)) for wi in retry_items]
                retry_outs_raw = await asyncio.gather(*retry_tasks, return_exceptions=False)
                retry_outs: List[Outcome] = [o for o in retry_outs_raw if isinstance(o, Outcome)]

                if retry_outs:
                    rows = [
                        (
                            o.site_id,
                            o.route_id,
                            o.preset_id,
                            o.price,
                            o.days,
                            float(o.created_ts),
                            o.name_tarif,
                            o.allowances_json,
                            o.status,
                            o.error,
                            o.context_json,
                        )
                        for o in retry_outs
                    ]
                    # пишем батчами!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
                    for i in range(0, len(rows), BATCH_SIZE):
                        await asyncio.to_thread(_flush_batch, db, job_id, rows[i : i + BATCH_SIZE])

                logger.warning(
                    "tail retry finished "
                    + kv(job_id=job_id, retried=len(retry_items), wrote=len(retry_outs))
                )

    if job_id is not None:
        db.set_job_status(job_id, "done", progress=100.0)

    logger.info("orchestrator done " + kv(job_id=job_id, done=done_unique, total=total))
