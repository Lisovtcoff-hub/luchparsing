
"""Модуль start_calc.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List

from core.contracts import CalcParams, Dimensions, CarrierAdapter
from core.logging_setup import kv
from core.orchestrator import run_orchestrator
from database.db import Database
from parsers.dellin import DellinAdapter
from parsers.energiya import EnergiyaAdapter
from parsers.pec import PecAdapter
from parsers.tkkit import TkkitAdapter
from parsers.vozovoz import VozovozAdapter
from parsers.expressavto import ExpressAvtoAdapter
from parsers.jde import JdeAdapter
from parsers.magic_trans import MagicTransAdapter
from parsers.fastrans import FastransAdapter
from parsers.dpd import DpdAdapter
from parsers.baykal import BaykalAdapter
from parsers.sdek import CdekAdapter


logger = logging.getLogger(__name__)

def _build_plan(db: Database):
    """Формирует adapters, items и site_limits для запуска оркестратора."""
    sites = db.fetch_sites()
    routes = db.fetch_routes()
    presets = db.fetch_presets_ordered()

    adapters: Dict[int, CarrierAdapter] = {}

    for s in sites:
        name = (s.get("name") or "").lower()
        if "деловы" in name:
            adapters[s["id"]] = DellinAdapter()
            continue
        if "пэк" in name:
            adapters[s["id"]] = PecAdapter()
            continue
        if "кит" in name:
            adapters[s["id"]] = TkkitAdapter()
            continue
        if "энергия" in name:
            adapters[s["id"]] = EnergiyaAdapter()
            continue
        if "возовоз" in name:
            adapters[s["id"]] = VozovozAdapter()
            continue
        if "экспрессавто" in name:
            adapters[s["id"]] = ExpressAvtoAdapter()
            continue
        if "желдорэксп" in name:
            adapters[s["id"]] = JdeAdapter()
            continue
        if "magic trans" in name:
            adapters[s["id"]] = MagicTransAdapter()
            continue
        if "фастранс" in name:
            adapters[s["id"]] = FastransAdapter()
            continue
        if "dpd" in name:
            adapters[s["id"]] = DpdAdapter()
            continue
        if "байкал" in name:
            adapters[s["id"]] = BaykalAdapter()
            continue
        # if "сдэк" in name:
        #     adapters[s["id"]] = CdekAdapter()
        #     continue

        

    dims_by_preset: Dict[int, Dimensions] = {}
    for p in presets:
        dims_json = p.get("dims_cm_json")
        dims = None

        if isinstance(dims_json, str) and dims_json.strip():
            try:
                d = json.loads(dims_json)
                length = float(d.get("length") or d.get("l") or 0)
                width = float(d.get("width") or d.get("w") or 0)
                height = float(d.get("height") or d.get("h") or 0)
                if length > 0 and width > 0 and height > 0:
                    dims = Dimensions(length_cm=length, width_cm=width, height_cm=height)
            except (json.JSONDecodeError, TypeError, ValueError):
                dims = None

        if dims is None:
            dims = Dimensions(length_cm=100, width_cm=100, height_cm=100)

        dims_by_preset[int(p["id"])] = dims

    total_items = 0
    for s in sites:
        if s["id"] in adapters:
            total_items += len(routes) * len(presets)

    def _iter_items():
        for s in sites:
            site_id = s["id"]
            if site_id not in adapters:
                continue

            for r in routes:
                for p in presets:
                    preset_id = int(p["id"])
                    dims = dims_by_preset[preset_id]

                    params = CalcParams(
                        from_city=r["from_city"],
                        to_city=r["to_city"],
                        places=int(p["places"]),
                        weight_kg=float(p["weight_kg"]),
                        volume_m3=float(p["volume_m3"]),
                        dims=dims,
                    )
                    yield (site_id, r["id"], p["id"], params)

    cfg = {row["id_site"]: row for row in db.get_sites_config()}
    site_limits: Dict[int, int] = {}
    for sid in adapters.keys():
        if sid in cfg and cfg[sid].get("parallel_limit"):
            site_limits[sid] = int(cfg[sid]["parallel_limit"])

    return adapters, _iter_items(), site_limits, total_items


async def run_job(job_id: int) -> None:
    """Функция run_job.

    Параметры:
        job_id: Описание параметра.
    """
    db = Database()
    db.set_job_status(job_id, "queued", progress=0.0)

    adapters, items, site_limits, total_items = _build_plan(db)
    logger.info("job start " + kv(job_id=job_id, items=total_items, adapters=len(adapters)))

    try:
        await run_orchestrator(
            db=db,
            adapters=adapters,
            items=items,
            total_items=total_items,
            site_limits=site_limits,
            job_id=job_id,
        )
        if db.is_job_cancelled(job_id):
            logger.warning("job cancelled " + kv(job_id=job_id))
            return
        db.set_last_update_now()
        logger.info("job done " + kv(job_id=job_id))
    except Exception as e:
        logger.exception("job failed " + kv(job_id=job_id, err=f"{type(e).__name__}: {e}"))
        db.set_job_status(job_id, "failed", progress=0.0, errors_json=None)
        raise
