
"""Модуль orchestrator.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Tuple

import httpx

from core.contracts import CalcParams, CalcResult, CarrierAdapter, TemporaryError
from core.logging_setup import kv
from database.db import Database

logger = logging.getLogger(__name__)

HTTP_CONNECT_TIMEOUT = 15.0
HTTP_READ_TIMEOUT = 30.0
DEFAULT_SITE_LIMIT = 4
MAX_RETRIES = 3

CIRCUIT_FAIL_THRESHOLD = 5
CIRCUIT_BLOCK_MINUTES = 5
BACKOFF_BASE = 0.5


def _params_key(p: CalcParams) -> Tuple[Any, ...]:
    """Функция _params_key.

    Параметры:
        p: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
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
    """Функция _to_allowances_json.

    Параметры:
        value: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    if isinstance(value, dict) and value:
        return json.dumps(value, ensure_ascii=False)
    return None


async def _calc_with_retry(adapter: CarrierAdapter, client: httpx.AsyncClient, params: CalcParams) -> CalcResult:
    """Функция _calc_with_retry.

    Параметры:
        adapter: Описание параметра.
        client: Описание параметра.
        params: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    delay = BACKOFF_BASE
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return await adapter.calc(client, params)
        except TemporaryError as e:
            last_exc = e
            if attempt == MAX_RETRIES:
                raise
            await asyncio.sleep(delay + (0.1 * delay))
            delay *= 2
        except (httpx.TimeoutException, httpx.HTTPError) as e:
            last_exc = e
            if attempt == MAX_RETRIES:
                raise TemporaryError(str(e))
            await asyncio.sleep(delay + (0.1 * delay))
            delay *= 2
        except Exception as e:

            raise e

    if last_exc:
        raise last_exc
    raise RuntimeError("retry loop failure")


def _flush_batch(
    db: Database,
    job_id: int | None,
    batch: List[tuple[int, int, int, float | None, str | None, float, str | None, str | None]],
) -> None:
    """
    Пишет батч результатов в БД.

    Формат batch:
      (site_id, route_id, preset_id, price, days_str, run_ts_unix, name_tarif, allowances_json)

    Стратегия:
    1) если есть insert_results_batch(rows=..., job_id=...) и она принимает расширенный формат — используем её;
    2) иначе — вставляем построчно через insert_result(...);
    3) иначе — откатываемся на старые add_res_price_many/_v2 (без новых полей).
    """
    if not batch:
        return


    if hasattr(db, "insert_results_batch"):
        try:

            db.insert_results_batch(batch, job_id=job_id)
            return
        except (TypeError, ValueError):

            pass
        except Exception:

            raise


    if hasattr(db, "insert_result"):
        for (site_id, route_id, preset_id, price, days_str, _ts, name_tarif, allowances_json) in batch:
            db.insert_result(
                site_id=site_id,
                route_id=route_id,
                preset_id=preset_id,
                price=price,
                days=days_str,
                job_id=job_id,
                name_tarif=name_tarif,
                allowances=allowances_json,
            )
        return


    stripped = [(s, r, p, price, days_str, ts) for (s, r, p, price, days_str, ts, _nt, _al) in batch]
    if job_id is not None and hasattr(db, "add_res_price_many_v2"):
        db.add_res_price_many_v2(job_id, stripped)
        return
    if hasattr(db, "add_res_price_many"):
        db.add_res_price_many(stripped)
        return

    raise RuntimeError("Database has no suitable method to write results batch.")


async def run_orchestrator(
    *,
    db: Database,
    adapters: Dict[int, CarrierAdapter],
    items: Iterable[Tuple[int, int, int, CalcParams]],
    site_limits: Dict[int, int] | None = None,
    job_id: int | None = None,
) -> None:
    """Функция run_orchestrator.

    Возвращает:
        Результат выполнения функции.
    """
    site_limits = site_limits or {}
    cfg_rows = db.get_sites_config()
    cfg_by_site = {r["id_site"]: r for r in cfg_rows}

    sem_by_site: Dict[int, asyncio.Semaphore] = {}

    def _limit_for(site_id: int) -> int:
        """Функция _limit_for.

        Параметры:
            site_id: Описание параметра.

        Возвращает:
            Результат выполнения функции.
        """
        if site_id in site_limits:
            return max(1, int(site_limits[site_id]))
        row = cfg_by_site.get(site_id)
        if row and row.get("parallel_limit"):
            return max(1, int(row["parallel_limit"]))
        return DEFAULT_SITE_LIMIT

    for sid in adapters.keys():
        sem_by_site[sid] = asyncio.Semaphore(_limit_for(sid))

    def _is_blocked(site_id: int) -> bool:
        """Функция _is_blocked.

        Параметры:
            site_id: Описание параметра.

        Возвращает:
            Результат выполнения функции.
        """
        row = cfg_by_site.get(site_id)
        if not row:
            return False
        val = row.get("disabled_until")
        if not val:
            return False
        try:
            until = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return until > datetime.utcnow().astimezone(until.tzinfo)
        except Exception:
            return False

    run_ts = time.time()
    fail_streak: Dict[int, int] = defaultdict(int)

    items_list = list(items)
    total = len(items_list)
    done = 0

    if job_id is not None:
        db.set_job_status(job_id, "running", progress=0.0)

    logger.info("orchestrator start " + kv(job_id=job_id, items=total, adapters=len(adapters)))

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(HTTP_CONNECT_TIMEOUT, read=HTTP_READ_TIMEOUT),
        http2=True,
    ) as client:
        batch: List[tuple[int, int, int, float | None, str | None, float, str | None, str | None]] = []
        inflight: Dict[Tuple[int, Tuple[Any, ...]], asyncio.Future] = {}

        async def run_one(site_id: int, route_id: int, preset_id: int, params: CalcParams):
            """Функция run_one.

            Параметры:
                site_id: Описание параметра.
                route_id: Описание параметра.
                preset_id: Описание параметра.
                params: Описание параметра.

            Возвращает:
                Результат выполнения функции.
            """
            cfg = cfg_by_site.get(site_id)
            if cfg and int(cfg.get("enabled", 1)) == 0:
                return (site_id, route_id, preset_id, None, None, None, None)
            if _is_blocked(site_id):
                return (site_id, route_id, preset_id, None, None, None, None)

            adapter = adapters.get(site_id)
            if adapter is None:
                return (site_id, route_id, preset_id, None, None, None, None)

            key = (site_id, _params_key(params))
            fut = inflight.get(key)

            if fut is None:

                async def _do():
                    """Функция _do.

                    Возвращает:
                        Результат выполнения функции.
                    """
                    async with sem_by_site[site_id]:
                        try:
                            res: CalcResult = await _calc_with_retry(adapter, client, params)


                            fail_streak[site_id] = 0
                            if hasattr(db, "set_site_last_error"):
                                db.set_site_last_error(site_id, None)

                            price = float(res.price) if res.price is not None else None
                            days = (str(res.days).strip() if res.days is not None else None)

                            name_tarif = None
                            if getattr(res, "name_tarif", None):
                                name_tarif = str(res.name_tarif).strip() or None

                            allowances_json = _to_allowances_json(getattr(res, "allowances", None))

                        except TemporaryError as e:
                            fail_streak[site_id] += 1
                            price, days, name_tarif, allowances_json = None, None, None, None

                            if hasattr(db, "set_site_last_error"):
                                db.set_site_last_error(site_id, f"TemporaryError: {e}")

                            if fail_streak[site_id] >= CIRCUIT_FAIL_THRESHOLD:
                                until = (datetime.utcnow() + timedelta(minutes=CIRCUIT_BLOCK_MINUTES)).isoformat()
                                db.set_site_disabled_until(site_id, until)
                                logger.warning(
                                    "circuit breaker opened "
                                    + kv(job_id=job_id, site_id=site_id, disabled_until=until, fail_streak=fail_streak[site_id])
                                )

                        except Exception as e:

                            if hasattr(db, "set_site_last_error"):
                                db.set_site_last_error(site_id, f"{type(e).__name__}: {e}")

                            logger.exception(
                                "adapter failed "
                                + kv(
                                    job_id=job_id,
                                    site_id=site_id,
                                    route_id=route_id,
                                    preset_id=preset_id,
                                    adapter=getattr(adapter, "code", adapter.__class__.__name__),
                                )
                            )
                            price, days, name_tarif, allowances_json = None, None, None, None

                        return price, days, name_tarif, allowances_json

                fut = asyncio.create_task(_do())
                inflight[key] = fut

            price, days, name_tarif, allowances_json = await fut
            return (site_id, route_id, preset_id, price, days, name_tarif, allowances_json)

        CHUNK = 500
        last_progress_push = 0.0

        for i in range(0, len(items_list), CHUNK):
            if job_id is not None and db.is_job_cancelled(job_id):
                progress = round(done / total * 100.0, 2) if total else 0.0
                db.set_job_status(job_id, "cancelled", progress=progress)
                logger.warning("job cancelled " + kv(job_id=job_id, progress=progress))
                return

            chunk = items_list[i : i + CHUNK]
            tasks = [asyncio.create_task(run_one(s, r, p, params)) for (s, r, p, params) in chunk]

            for fut in asyncio.as_completed(tasks):
                if job_id is not None and db.is_job_cancelled(job_id):
                    if batch:
                        _flush_batch(db, job_id, batch)
                        batch.clear()
                    progress = round(done / total * 100.0, 2) if total else 0.0
                    db.set_job_status(job_id, "cancelled", progress=progress)
                    logger.warning("job cancelled " + kv(job_id=job_id, progress=progress))
                    return

                site_id, route_id, preset_id, price, days, name_tarif, allowances_json = await fut

                batch.append((site_id, route_id, preset_id, price, days, run_ts, name_tarif, allowances_json))
                done += 1

                if len(batch) >= 200:
                    _flush_batch(db, job_id, batch)
                    batch.clear()

                if job_id is not None and total:
                    now = time.time()
                    if now - last_progress_push >= 0.5 or done == total:
                        db.set_job_status(job_id, "running", progress=round(done / total * 100.0, 2))
                        last_progress_push = now

        if batch:
            _flush_batch(db, job_id, batch)

    if job_id is not None:
        db.set_job_status(job_id, "done", progress=100.0)

    logger.info("orchestrator done " + kv(job_id=job_id, done=done, total=total))
