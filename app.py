"""Модуль app.

Содержит прикладную логику и точки входа проекта.
"""

import os
import base64
import asyncio
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, Response, Body, HTTPException, Path as FPath, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv

from database.db import Database
import logging
import time
from core.logging_setup import setup_logging, kv

logger = logging.getLogger(__name__)

load_dotenv()

app = FastAPI(title="Луч — панель")

PANEL_USER = os.environ.get("PANEL_USER")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD")


@app.on_event("startup")
def _init_schema():
    """Инициализирует соединение с БД и проверяет наличие схемы."""
    setup_logging()
    logger.info("app startup")
    db = Database()
    db.ensure_schema()
    app.state.db = db


def _db(request: Request) -> Database:
    """Возвращает объект Database, сохранённый в состоянии приложения."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        db = Database()
        db.ensure_schema()
        request.app.state.db = db
    return db

@app.middleware("http")
async def request_logging(request: Request, call_next):
    """Функция request_logging.

    Параметры:
        request: Описание параметра.
        call_next: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    start = time.time()
    try:
        resp = await call_next(request)
        ms = round((time.time() - start) * 1000, 2)

        if not request.url.path.startswith("/"):
            return resp
        logger.info(
            "http request " + kv(
                method=request.method,
                path=request.url.path,
                status=resp.status_code,
                ms=ms,
            )
        )
        return resp
    except Exception:
        ms = round((time.time() - start) * 1000, 2)
        logger.exception("http unhandled exception " + kv(method=request.method, path=request.url.path, ms=ms))
        raise


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Функция basic_auth.

    Параметры:
        request: Описание параметра.
        call_next: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    if request.url.path.startswith("/health"):
        return await call_next(request)

    if not (PANEL_USER and PANEL_PASSWORD):
        return await call_next(request)

    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Basic "):
        logger.warning("auth missing/invalid header " + kv(path=request.url.path))
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="LuchPanel"'})

    try:
        encoded = auth.split(" ", 1)[1]
        username, password = base64.b64decode(encoded).decode("utf-8").split(":", 1)
    except Exception:
        logger.warning("auth header decode failed " + kv(path=request.url.path))
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="LuchPanel"'})

    if username != PANEL_USER or password != PANEL_PASSWORD:
        logger.warning("auth failed " + kv(path=request.url.path))
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="LuchPanel"'})

    return await call_next(request)


@app.get("/health")
def health():
    """Функция health.

    Возвращает:
        Результат выполнения функции.
    """
    return {"status": "ok"}


@app.get("/api/sites")
def sites_list(request: Request):
    """Функция sites_list.

    Параметры:
        request: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    return _db(request).list_sites()


@app.get("/api/routes")
def routes_list(request: Request):
    """Функция routes_list.

    Параметры:
        request: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    return _db(request).list_routes()


@app.post("/api/routes")
def routes_create(request: Request, item: dict = Body(...)):
    """Функция routes_create.

    Параметры:
        request: Описание параметра.
        item: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    from_city = (item.get("from_city") or "").strip()
    to_city = (item.get("to_city") or "").strip()
    distance_km = item.get("distance_km")

    if distance_km is not None:
        try:
            distance_km = int(distance_km)
        except (TypeError, ValueError):
            raise HTTPException(400, "distance_km must be integer or null")

    if not from_city or not to_city:
        raise HTTPException(400, "from_city/to_city required")

    try:
        rid = _db(request).create_route(from_city, to_city, distance_km)
        return {"id": rid, "from_city": from_city, "to_city": to_city, "distance_km": distance_km}
    except Exception:
        raise HTTPException(409, "route already exists")


@app.delete("/api/routes/{route_id}")
def routes_delete(request: Request, route_id: int = FPath(..., ge=1)):
    """Функция routes_delete.

    Параметры:
        request: Описание параметра.
        route_id: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    _db(request).delete_route(route_id)
    return {"ok": True}

@app.put("/api/routes/{route_id}")
def routes_update(request: Request, route_id: int = FPath(..., ge=1), item: dict = Body(...)):
    """Обновляет маршрут."""
    from_city = (item.get("from_city") or "").strip()
    to_city = (item.get("to_city") or "").strip()
    distance_km = item.get("distance_km")

    if distance_km is not None:
        try:
            distance_km = int(distance_km)
        except (TypeError, ValueError):
            raise HTTPException(400, "distance_km must be integer or null")

    if not from_city or not to_city:
        raise HTTPException(400, "from_city/to_city required")

    try:
        _db(request).update_route(route_id, from_city, to_city, distance_km)
        return {"ok": True, "id": route_id, "from_city": from_city, "to_city": to_city, "distance_km": distance_km}
    except Exception:
        raise HTTPException(409, "route already exists")



@app.get("/api/presets")
def presets_list(request: Request):
    """Функция presets_list.

    Параметры:
        request: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    return _db(request).list_presets()


@app.post("/api/presets")
def presets_create(request: Request, item: dict = Body(...)):
    """Функция presets_create.

    Параметры:
        request: Описание параметра.
        item: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    try:
        places = int(item.get("places") or 1)
        weight_kg = float(item.get("weight_kg") or 0.0)
        volume_m3 = float(item.get("volume_m3") or 0.0)
    except Exception:
        raise HTTPException(400, "invalid numeric fields")

    if places < 1 or weight_kg < 0 or volume_m3 < 0:
        raise HTTPException(400, "invalid numeric ranges")

    dims_cm_json = item.get("dims_cm_json")
    pid = _db(request).create_preset(places, weight_kg, volume_m3, dims_cm_json)
    return {
        "id": pid,
        "places": places,
        "weight_kg": weight_kg,
        "volume_m3": volume_m3,
        "dims_cm_json": dims_cm_json,
    }


@app.delete("/api/presets/{preset_id}")
def presets_delete(request: Request, preset_id: int = FPath(..., ge=1)):
    """Функция presets_delete.

    Параметры:
        request: Описание параметра.
        preset_id: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    _db(request).delete_preset(preset_id)
    return {"ok": True}

@app.put("/api/presets/{preset_id}")
def presets_update(request: Request, preset_id: int = FPath(..., ge=1), item: dict = Body(...)):
    """Обновляет пресет."""
    try:
        places = int(item.get("places") or 1)
        weight_kg = float(item.get("weight_kg") or 0.0)
        volume_m3 = float(item.get("volume_m3") or 0.0)
    except Exception:
        raise HTTPException(400, "invalid numeric fields")

    if places < 1 or weight_kg < 0 or volume_m3 < 0:
        raise HTTPException(400, "invalid numeric ranges")

    dims_cm_json = item.get("dims_cm_json")
    _db(request).update_preset(preset_id, places, weight_kg, volume_m3, dims_cm_json)
    return {"ok": True, "id": preset_id, "places": places, "weight_kg": weight_kg, "volume_m3": volume_m3, "dims_cm_json": dims_cm_json}



@app.get("/api/last_update")
def last_update(request: Request):
    """Функция last_update.

    Параметры:
        request: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    return {"last_update": _db(request).get_last_update()}


@app.post("/api/buttons/start_calc")
async def start_calc_button(request: Request):
    """Создаёт job и запускает расчёт в фоновой задаче."""
    db = _db(request)
    active = db.get_active_job()
    if active:
        if active.get("status") == "queued":
            try:
                created_at = datetime.fromisoformat(str(active.get("created_at")))
                age_s = (datetime.now(created_at.tzinfo) - created_at).total_seconds()
            except Exception:
                age_s = 0.0
            if age_s >= 10.0:
                from button.start_calc import run_job
                asyncio.create_task(run_job(active["id"]))
                return {
                    "ok": True,
                    "job_id": active["id"],
                    "status": active["status"],
                    "already_running": True,
                    "restarted_queued": True,
                }
        return {
            "ok": True,
            "job_id": active["id"],
            "status": active["status"],
            "already_running": True,
        }
    job_id = db.create_job()
    db.set_job_status(job_id, "queued", progress=0.0)

    from button.start_calc import run_job
    asyncio.create_task(run_job(job_id))

    return {"ok": True, "job_id": job_id}


@app.get("/api/buttons/export")
def export_xlsx_button_get(request: Request):
    """Функция export_xlsx_button_get.

    Параметры:
        request: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    from button.export_xlsx import export_xlsx

    path = export_xlsx(_db(request))
    filename = os.path.basename(path)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


@app.get("/api/buttons/export_db")
def export_db_button_get(request: Request, background: BackgroundTasks):
    """Экспорт БД (SQLite) для скачивания."""
    db = _db(request)
    with db.lock:
        try:
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            pass
        src = Path(db.db_path)

    if not src.exists():
        raise HTTPException(404, "database file not found")

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp_path = Path(tmp.name)
    tmp.close()

    shutil.copy2(src, tmp_path)
    background.add_task(os.unlink, str(tmp_path))

    filename = f"luch_db_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    return FileResponse(
        str(tmp_path),
        media_type="application/octet-stream",
        filename=filename,
        background=background,
    )


@app.post("/api/buttons/export_xlsx")
def export_xlsx_button_post(request: Request):
    """Функция export_xlsx_button_post.

    Параметры:
        request: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    from button.export_xlsx import export_xlsx

    path = export_xlsx(_db(request))
    filename = os.path.basename(path)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


@app.get("/api/jobs")
def api_jobs(request: Request):
    """Функция api_jobs.

    Параметры:
        request: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    return _db(request).list_jobs(limit=100)


@app.get("/api/job/{job_id}")
def api_job(request: Request, job_id: int):
    """Функция api_job.

    Параметры:
        request: Описание параметра.
        job_id: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    job = _db(request).get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.post("/api/job/{job_id}/cancel")
def api_job_cancel(request: Request, job_id: int):
    """Функция api_job_cancel.

    Параметры:
        request: Описание параметра.
        job_id: Описание параметра.

    Возвращает:
        Результат выполнения функции.
    """
    db = _db(request)
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if job["status"] in ("done", "cancelled"):
        return {"ok": True, "status": job["status"]}
    db.request_job_cancel(job_id)
    if job["status"] in ("queued", "running"):
        db.set_job_status(job_id, "cancelled", progress=job.get("progress"))
    return {"ok": True, "status": "cancelled"}


@app.get("/api/results")
def api_results(
    request: Request,
    job_id: int | None = None,
    site_id: int | None = None,
    route_id: int | None = None,
    preset_id: int | None = None,
    limit: int = 500,
    prev_job_id: int | None = None,
):
    """Возвращает результаты для таблицы и дельту по цене."""
    db = _db(request)

    if job_id is None:
        last_done = db.get_latest_done_job_ids(limit=1)
        job_id = last_done[0] if last_done else None

    rows = db.get_results(job_id=job_id, site_id=site_id, route_id=route_id, preset_id=preset_id, limit=limit)

    delta_map = {}
    base_prev_job = prev_job_id
    if base_prev_job is None:
        ids = db.get_latest_done_job_ids(limit=2)
        if len(ids) >= 2:
            base_prev_job = ids[1]

    if base_prev_job is not None:
        if rows:
            keys = [(r["id_site"], r["id_route"], r["id_preset"]) for r in rows]
            prev_rows = db.get_results_by_keys(job_id=base_prev_job, keys=keys)
            for r in prev_rows:
                key = (r["id_site"], r["id_route"], r["id_preset"])
                delta_map[key] = r.get("price")
    out = []
    for r in rows:
        key = (r["id_site"], r["id_route"], r["id_preset"])
        cur = r.get("price")
        prv = delta_map.get(key)
        if cur is None or prv is None:
            delta = "na"
        else:
            delta = "flat" if cur == prv else ("up" if cur > prv else "down")

        out.append(
            {
                "job_id": r.get("job_id", job_id),
                "id_site": r["id_site"],
                "id_route": r["id_route"],
                "id_preset": r["id_preset"],
                "price": r.get("price"),
                "days": r.get("days"),
                "currency": r.get("currency", "RUB"),
                "created_at": r.get("created_at"),
                "name_tarif": r.get("name_tarif"),
                "allowances": r.get("allowances"),
                "delta": delta,
                "context_json": r.get("context_json"),
            }
        )

    return {"job_id": job_id, "items": out}


@app.get("/api/logs")
def api_logs(
    request: Request,
    job_id: int | None = None,
    site_id: int | None = None,
    limit: int = 2000,
):
    """Возвращает логи последнего запуска (строки results без дедупликации)."""
    db = _db(request)
    if job_id is None:
        last_done = db.get_latest_done_job_ids(limit=1)
        job_id = last_done[0] if last_done else None
    if job_id is None:
        return {"job_id": None, "items": []}

    if hasattr(db, "get_results_raw"):
        rows = db.get_results_raw(job_id=job_id, site_id=site_id, limit=limit)
    else:
        # fallback for old containers without get_results_raw
        rows = db.get_results(job_id=job_id, site_id=site_id, limit=limit)
    out = []
    for r in rows:
        out.append(
            {
                "id": r.get("id"),
                "job_id": r.get("job_id", job_id),
                "id_site": r["id_site"],
                "id_route": r["id_route"],
                "id_preset": r["id_preset"],
                "price": r.get("price"),
                "days": r.get("days"),
                "currency": r.get("currency", "RUB"),
                "created_at": r.get("created_at"),
                "name_tarif": r.get("name_tarif"),
                "allowances": r.get("allowances"),
                "status": r.get("status"),
                "error": r.get("error"),
                "context_json": r.get("context_json"),
            }
        )
    return {"job_id": job_id, "items": out}


WEB_DIR = Path(__file__).parent / "web"
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="static")
