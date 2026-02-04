
"""Модуль db.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any


DB_DIR = Path(__file__).parent
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "database.db"


class Database:
    """Слой доступа к SQLite для сервиса парсинга."""

    def __init__(self, db_path: Path | None = None) -> None:
        """Открывает соединение с SQLite и гарантирует актуальную схему."""
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.execute("PRAGMA journal_mode = WAL;")
        self.conn.execute("PRAGMA synchronous = NORMAL;")
        self.lock = threading.RLock()
        self.ensure_schema()

    def close(self) -> None:
        """Закрывает соединение с базой данных."""
        with self.lock:
            self.conn.close()

    def ensure_schema(self) -> None:
        """Создаёт/мигрирует схему БД до текущей версии и индексов."""
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sites (
                  id    INTEGER PRIMARY KEY AUTOINCREMENT,
                  name  TEXT NOT NULL UNIQUE,
                  link  TEXT
                );

                CREATE TABLE IF NOT EXISTS routes (
                  id           INTEGER PRIMARY KEY AUTOINCREMENT,
                  from_city    TEXT NOT NULL,
                  to_city      TEXT NOT NULL,
                  distance_km  INTEGER,
                  UNIQUE(from_city, to_city)
                );
                CREATE INDEX IF NOT EXISTS idx_routes_cities ON routes(from_city, to_city);

                CREATE TABLE IF NOT EXISTS presets (
                  id           INTEGER PRIMARY KEY AUTOINCREMENT,
                  places       INTEGER NOT NULL DEFAULT 1 CHECK(places >= 1),
                  weight_kg    REAL NOT NULL CHECK(weight_kg >= 0),
                  volume_m3    REAL NOT NULL CHECK(volume_m3 >= 0),
                  dims_cm_json TEXT
                );

                CREATE TABLE IF NOT EXISTS last_update (
                  id   INTEGER PRIMARY KEY CHECK (id=1),
                  date TEXT
                );

                CREATE TABLE IF NOT EXISTS sites_config(
                  id_site        INTEGER PRIMARY KEY,
                  enabled        INTEGER NOT NULL DEFAULT 1,
                  parallel_limit INTEGER NOT NULL DEFAULT 4,
                  timeout_s      INTEGER NOT NULL DEFAULT 30,
                  notes          TEXT,
                  disabled_until DATETIME,
                  last_error_at  DATETIME,
                  FOREIGN KEY(id_site) REFERENCES sites(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS jobs(
                  id                INTEGER PRIMARY KEY AUTOINCREMENT,
                  created_at        DATETIME NOT NULL DEFAULT (datetime('now')),
                  status            TEXT NOT NULL DEFAULT 'queued',
                  progress          REAL NOT NULL DEFAULT 0.0,
                  errors_json       TEXT,
                  cancel_requested  INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS cache(
                  key        TEXT PRIMARY KEY,
                  value      BLOB,
                  expire_at  DATETIME
                );

                CREATE TABLE IF NOT EXISTS results(
                  id          INTEGER PRIMARY KEY AUTOINCREMENT,
                  job_id      INTEGER,
                  id_site     INTEGER NOT NULL,
                  id_route    INTEGER NOT NULL,
                  id_preset   INTEGER NOT NULL,
                  price       REAL,
                  days        TEXT,
                  currency    TEXT NOT NULL DEFAULT 'RUB',
                  source      TEXT,
                  name_tarif  TEXT,
                  allowances  TEXT,
                  status      TEXT NOT NULL DEFAULT 'ok',
                  error       TEXT,
                  created_at  DATETIME NOT NULL DEFAULT (datetime('now')),
                  FOREIGN KEY(id_site) REFERENCES sites(id) ON DELETE CASCADE,
                  FOREIGN KEY(id_route) REFERENCES routes(id) ON DELETE CASCADE,
                  FOREIGN KEY(id_preset) REFERENCES presets(id) ON DELETE CASCADE,
                  FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL
                );
                """
            )


            if not self.conn.execute("SELECT 1 FROM last_update WHERE id=1").fetchone():
                self.conn.execute("INSERT INTO last_update(id, date) VALUES (1, NULL)")



            cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(sites_config)").fetchall()]
            if "last_error" not in cols:
                self.conn.execute("ALTER TABLE sites_config ADD COLUMN last_error TEXT")

            # results schema migrations (non-breaking)
            rcols = [r["name"] for r in self.conn.execute("PRAGMA table_info(results)").fetchall()]
            if "status" not in rcols:
                self.conn.execute("ALTER TABLE results ADD COLUMN status TEXT NOT NULL DEFAULT 'ok'")
            if "error" not in rcols:
                self.conn.execute("ALTER TABLE results ADD COLUMN error TEXT")
            if "context_json" not in rcols:
                self.conn.execute("ALTER TABLE results ADD COLUMN context_json TEXT")

            self.conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_results_lookup
                ON results(id_site, id_route, id_preset);

                CREATE INDEX IF NOT EXISTS idx_results_created
                ON results(created_at);

                CREATE INDEX IF NOT EXISTS idx_results_job
                ON results(job_id, id_site, id_route, id_preset);

                CREATE INDEX IF NOT EXISTS idx_sites_name
                ON sites(name);
                """
            )

            self.seed_sites_config_defaults()
            self.conn.commit()

    def list_sites(self) -> list[dict]:
        """Возвращает список перевозчиков/источников (sites)."""
        with self.lock:
            rows = self.conn.execute("SELECT id, name, link FROM sites ORDER BY name").fetchall()
            return [dict(r) for r in rows]

    def create_site(self, name: str, link: str | None = None) -> int:
        """Создаёт запись сайта/перевозчика и возвращает его id."""
        with self.lock:
            self.conn.execute("INSERT INTO sites(name, link) VALUES(?, ?)", (name, link))
            site_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.conn.commit()
            return int(site_id)

    def update_site(self, site_id: int, *, name: str | None = None, link: str | None = None) -> None:
        """Обновляет имя и/или ссылку сайта."""
        parts: list[str] = []
        params: list[Any] = []
        if name is not None:
            parts.append("name=?")
            params.append(name)
        if link is not None:
            parts.append("link=?")
            params.append(link)
        if not parts:
            return
        params.append(int(site_id))
        sql = f"UPDATE sites SET {', '.join(parts)} WHERE id=?"
        with self.lock:
            self.conn.execute(sql, params)
            self.conn.commit()

    def delete_site(self, site_id: int) -> None:
        """Удаляет сайт/перевозчика по id."""
        with self.lock:
            self.conn.execute("DELETE FROM sites WHERE id=?", (int(site_id),))
            self.conn.commit()

    def fetch_sites(self) -> list[dict]:
        """Возвращает сайты, отсортированные по id (удобно для планировщика)."""
        with self.lock:
            cur = self.conn.execute("SELECT id, name, link FROM sites ORDER BY id ASC")
            return [dict(row) for row in cur.fetchall()]

    def list_routes(self) -> list[dict]:
        """Возвращает список маршрутов."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, from_city, to_city, distance_km FROM routes ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def create_route(self, from_city: str, to_city: str, distance_km: int | None = None) -> int:
        """Создаёт маршрут и возвращает его id."""
        with self.lock:
            self.conn.execute(
                "INSERT INTO routes(from_city, to_city, distance_km) VALUES(?,?,?)",
                (from_city, to_city, distance_km),
            )
            rid = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.conn.commit()
            return int(rid)

    def update_route(self, route_id: int, from_city: str, to_city: str, distance_km: int | None = None) -> None:
        """Обновляет маршрут по id."""
        with self.lock:
            self.conn.execute(
                """
                UPDATE routes
                SET from_city=?, to_city=?, distance_km=?
                WHERE id=?
                """,
                (from_city, to_city, distance_km, int(route_id)),
            )
            self.conn.commit()


    def delete_route(self, route_id: int) -> None:
        """Удаляет маршрут по id."""
        with self.lock:
            self.conn.execute("DELETE FROM routes WHERE id=?", (int(route_id),))
            self.conn.commit()

    def fetch_routes(self) -> list[dict]:
        """Возвращает маршруты, отсортированные по id (удобно для планировщика)."""
        with self.lock:
            cur = self.conn.execute("SELECT id, from_city, to_city, distance_km FROM routes ORDER BY id ASC")
            return [dict(row) for row in cur.fetchall()]

    def list_presets(self) -> list[dict]:
        """Возвращает список пресетов."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, places, weight_kg, volume_m3, dims_cm_json FROM presets ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def create_preset(self, places: int, weight_kg: float, volume_m3: float, dims_cm_json: str | None) -> int:
        """Создаёт пресет и возвращает его id."""
        with self.lock:
            self.conn.execute(
                "INSERT INTO presets(places, weight_kg, volume_m3, dims_cm_json) VALUES(?,?,?,?)",
                (int(places), float(weight_kg), float(volume_m3), dims_cm_json),
            )
            pid = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.conn.commit()
            return int(pid)

    def update_preset(self, preset_id: int, places: int, weight_kg: float, volume_m3: float, dims_cm_json: str | None) -> None:
        """Обновляет пресет по id."""
        with self.lock:
            self.conn.execute(
                """
                UPDATE presets
                SET places=?, weight_kg=?, volume_m3=?, dims_cm_json=?
                WHERE id=?
                """,
                (int(places), float(weight_kg), float(volume_m3), dims_cm_json, int(preset_id)),
            )
            self.conn.commit()


    def delete_preset(self, preset_id: int) -> None:
        """Удаляет пресет по id."""
        with self.lock:
            self.conn.execute("DELETE FROM presets WHERE id=?", (int(preset_id),))
            self.conn.commit()

    def fetch_presets_ordered(self) -> list[dict]:
        """Возвращает пресеты в стабильном порядке (удобно для планировщика)."""
        with self.lock:
            cur = self.conn.execute(
                """
                SELECT id, places, weight_kg, volume_m3, dims_cm_json
                FROM presets
                ORDER BY weight_kg ASC, places ASC, volume_m3 ASC, id ASC
                """
            )
            return [dict(row) for row in cur.fetchall()]

    def get_last_update(self) -> str | None:
        """Возвращает строку последнего обновления для UI."""
        with self.lock:
            row = self.conn.execute("SELECT date FROM last_update WHERE id=1").fetchone()
            return row["date"] if row and row["date"] else None

    def set_last_update_now(self) -> None:
        """Записывает текущее время (localtime) как последнее обновление."""
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO last_update(id, date)
                VALUES(1, datetime('now','localtime'))
                ON CONFLICT(id) DO UPDATE SET date=datetime('now','localtime')
                """
            )
            self.conn.commit()

    def get_sites_config(self) -> list[dict]:
        """Возвращает настройки сайтов (enabled/лимиты/таймауты)."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id_site, enabled, parallel_limit, timeout_s, disabled_until, last_error_at, last_error, notes FROM sites_config"
            ).fetchall()
            return [dict(r) for r in rows]

    def upsert_site_config(
        self,
        site_id: int,
        *,
        enabled: int = 1,
        parallel_limit: int = 4,
        timeout_s: int = 30,
        notes: str | None = None,
    ) -> None:
        """Создаёт или обновляет строку конфигурации сайта."""
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO sites_config(id_site, enabled, parallel_limit, timeout_s, notes)
                VALUES(?,?,?,?,?)
                ON CONFLICT(id_site) DO UPDATE SET
                  enabled=excluded.enabled,
                  parallel_limit=excluded.parallel_limit,
                  timeout_s=excluded.timeout_s,
                  notes=excluded.notes
                """,
                (int(site_id), int(enabled), int(parallel_limit), int(timeout_s), notes),
            )
            self.conn.commit()

    def set_site_disabled_until(self, site_id: int, until_iso: str | None) -> None:
        """Устанавливает временную блокировку сайта до ISO datetime (UTC)."""
        with self.lock:
            self.conn.execute("UPDATE sites_config SET disabled_until=? WHERE id_site=?", (until_iso, int(site_id)))
            self.conn.commit()

    def set_site_last_error(self, site_id: int, error_text: str | None) -> None:
        """
        Записывает/очищает последнюю ошибку по сайту (для ops/UI).

        - если error_text задан: обновляет last_error_at и last_error (обрезает до 2000 символов)
        - если error_text None: очищает last_error_at и last_error
        """
        with self.lock:
            if error_text:
                self.conn.execute(
                    "UPDATE sites_config SET last_error_at=datetime('now'), last_error=? WHERE id_site=?",
                    (str(error_text)[:2000], int(site_id)),
                )
            else:
                self.conn.execute(
                    "UPDATE sites_config SET last_error_at=NULL, last_error=NULL WHERE id_site=?",
                    (int(site_id),),
                )
            self.conn.commit()

    def seed_sites_config_defaults(self, enabled: int = 1, parallel_limit: int = 4, timeout_s: int = 30) -> None:
        """Гарантирует наличие строки sites_config для каждого сайта."""
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO sites_config(id_site, enabled, parallel_limit, timeout_s)
                SELECT s.id, ?, ?, ?
                FROM sites s
                LEFT JOIN sites_config c ON c.id_site = s.id
                WHERE c.id_site IS NULL
                """,
                (int(enabled), int(parallel_limit), int(timeout_s)),
            )
            self.conn.commit()

    def create_job(self) -> int:
        """Создаёт задачу (job) и возвращает её id."""
        with self.lock:
            self.conn.execute("INSERT INTO jobs(status, progress, errors_json) VALUES('queued', 0.0, NULL)")
            job_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.conn.commit()
            return int(job_id)

    def update_job(
        self,
        job_id: int,
        *,
        status: str,
        progress: float | None = None,
        errors_json: str | None = None,
    ) -> None:
        """Обновляет статус/прогресс/ошибки задачи."""
        if status == "running" and self.is_job_cancelled(job_id):
            return
        parts = ["status=?"]
        params: list[Any] = [status]
        if progress is not None:
            parts.append("progress=?")
            params.append(float(progress))
        if errors_json is not None:
            parts.append("errors_json=?")
            params.append(errors_json)
        params.append(int(job_id))
        sql = f"UPDATE jobs SET {', '.join(parts)} WHERE id=?"
        with self.lock:
            self.conn.execute(sql, params)
            self.conn.commit()

    def set_job_status(self, job_id: int, status: str, progress: float | None = None, errors_json: str | None = None):
        """Совместимость: обновляет задачу (используется оркестратором)."""
        self.update_job(job_id, status=status, progress=progress, errors_json=errors_json)

    def get_job(self, job_id: int) -> dict | None:
        """Возвращает задачу по id."""
        with self.lock:
            row = self.conn.execute(
                "SELECT id, created_at, status, progress, errors_json, cancel_requested FROM jobs WHERE id=?",
                (int(job_id),),
            ).fetchone()
            return dict(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict]:
        """Возвращает последние задачи."""
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT id, created_at, status, progress, errors_json, cancel_requested
                FROM jobs
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]

    def request_job_cancel(self, job_id: int) -> None:
        """Помечает задачу как отменяемую."""
        with self.lock:
            self.conn.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (int(job_id),))
            self.conn.commit()

    def is_job_cancelled(self, job_id: int) -> bool:
        """Проверяет, запрошена ли отмена задачи."""
        with self.lock:
            row = self.conn.execute("SELECT cancel_requested FROM jobs WHERE id=?", (int(job_id),)).fetchone()
            return bool(row and row["cancel_requested"])

    def get_latest_job_id(self) -> int | None:
        """Возвращает id самой свежей задачи."""
        with self.lock:
            row = self.conn.execute("SELECT id FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
            return int(row["id"]) if row else None

    def get_latest_done_job_ids(self, limit: int = 2) -> list[int]:
        """Возвращает id последних завершённых задач."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id FROM jobs WHERE status='done' ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [int(r["id"]) for r in rows]

    def insert_result(
        self,
        *,
        site_id: int,
        route_id: int,
        preset_id: int,
        price: float | None,
        days: str | None = None,
        currency: str = "RUB",
        source: str | None = None,
        job_id: int | None = None,
        name_tarif: str | None = None,
        allowances: str | None = None,
        status: str = "ok",
        error: str | None = None,
        context_json: str | None = None,
    ) -> int:
        """Вставляет одну строку результата и возвращает её id."""
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO results(job_id, id_site, id_route, id_preset, price, days, currency, source, name_tarif, allowances, status, error, context_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    int(site_id),
                    int(route_id),
                    int(preset_id),
                    price,
                    days,
                    currency or "RUB",
                    source,
                    name_tarif,
                    allowances,
                    status or "ok",
                    error,
                    context_json,
                ),
            )
            rid = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.conn.commit()
            return int(rid)

    def insert_results_batch(
        self,
        rows: list[tuple],
        *,
        job_id: int | None = None,
        currency: str = "RUB",
        source: str | None = None,
    ) -> None:
        """Batch insert results.

        Поддерживаем 2 формата rows для обратной совместимости:

        8 полей:
          (site_id, route_id, preset_id, price, days_str, created_at_unix_ts, name_tarif, allowances)

        10 полей:
          (site_id, route_id, preset_id, price, days_str, created_at_unix_ts, name_tarif, allowances, status, error)
        """
        if not rows:
            return

        # Нормализуем к 10 полям
        norm: list[tuple] = []
        for row in rows:
            if len(row) == 8:
                site_id, route_id, preset_id, price, days_str, created_ts, name_tarif, allowances = row
                status = "ok" if price is not None else "no_price"
                error = None
                context_json = None
            elif len(row) == 10:
                site_id, route_id, preset_id, price, days_str, created_ts, name_tarif, allowances, status, error = row
                context_json = None
            elif len(row) == 11:
                site_id, route_id, preset_id, price, days_str, created_ts, name_tarif, allowances, status, error, context_json = row
            else:
                raise ValueError(f"Unsupported row format in insert_results_batch: len={len(row)}")

            norm.append(
                (
                    job_id,
                    int(site_id),
                    int(route_id),
                    int(preset_id),
                    price,
                    days_str,
                    currency or "RUB",
                    source,
                    name_tarif,
                    allowances,
                    status or "ok",
                    error,
                    context_json,
                    float(created_ts),
                )
            )

        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE;")
            self.conn.executemany(
                """
                INSERT INTO results(
                  job_id, id_site, id_route, id_preset,
                  price, days, currency, source,
                  name_tarif, allowances,
                  status, error, context_json,
                  created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime(?,'unixepoch'))
                """,
                norm,
            )
            self.conn.commit()

    def add_res_price_many(self, rows):
        """Batch insert without job_id.

        Pads 6-field rows with (name_tarif=None, allowances=None).
        """
        padded = [(s, r, p, price, days, ts, None, None, ("ok" if price is not None else "no_price"), None) for (s, r, p, price, days, ts) in rows]
        self.insert_results_batch(padded, job_id=None)

    def add_res_price_many_v2(self, job_id: int, rows):
        """Batch insert with job_id.

        Pads 6-field rows with (name_tarif=None, allowances=None).
        """
        padded = [(s, r, p, price, days, ts, None, None, ("ok" if price is not None else "no_price"), None) for (s, r, p, price, days, ts) in rows]
        self.insert_results_batch(padded, job_id=int(job_id))

    def get_results(
        self,
        *,
        job_id: int | None = None,
        site_id: int | None = None,
        route_id: int | None = None,
        preset_id: int | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Возвращает результаты с фильтрацией для UI."""
        params: list[Any] = []
        where: list[str] = []
        if job_id is not None:
            where.append("job_id=?")
            params.append(int(job_id))
        if site_id is not None:
            where.append("id_site=?")
            params.append(int(site_id))
        if route_id is not None:
            where.append("id_route=?")
            params.append(int(route_id))
        if preset_id is not None:
            where.append("id_preset=?")
            params.append(int(preset_id))

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        # Внутри одного job мы можем писать повторные попытки (tail retry). Чтобы UI не видел
        # "дубликаты" одной и той же пары (site, route, preset), возвращаем только самую
        # свежую запись по каждому ключу.
        if job_id is not None:
            sql = f"""
            WITH latest AS (
                SELECT MAX(id) AS id
                FROM results
                {where_sql}
                GROUP BY id_site, id_route, id_preset
            )
            SELECT r.id, r.job_id, r.id_site, r.id_route, r.id_preset, r.price, r.days, r.currency, r.source,
                   r.name_tarif, r.allowances, r.status, r.error, r.context_json, r.created_at
            FROM results r
            JOIN latest l ON l.id = r.id
            ORDER BY r.id DESC
            LIMIT ?
            """
        else:
            sql = f"""
            SELECT id, job_id, id_site, id_route, id_preset, price, days, currency, source, name_tarif, allowances, status, error, context_json, created_at
            FROM results
            {where_sql}
            ORDER BY id DESC
            LIMIT ?
            """

        params.append(int(limit))

        with self.lock:
            cur = self.conn.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def get_results_raw(
        self,
        *,
        job_id: int,
        site_id: int | None = None,
        limit: int = 2000,
    ) -> list[dict]:
        """Возвращает результаты без дедупликации (для логов)."""
        params: list[Any] = [int(job_id)]
        where: list[str] = ["job_id=?"]
        if site_id is not None:
            where.append("id_site=?")
            params.append(int(site_id))

        where_sql = "WHERE " + " AND ".join(where)
        sql = f"""
        SELECT id, job_id, id_site, id_route, id_preset, price, days, currency, source,
               name_tarif, allowances, status, error, context_json, created_at
        FROM results
        {where_sql}
        ORDER BY id DESC
        LIMIT ?
        """
        params.append(int(limit))
        with self.lock:
            cur = self.conn.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def get_results_by_keys(
        self,
        *,
        job_id: int,
        keys: list[tuple[int, int, int]],
    ) -> list[dict]:
        """Returns results for specific (site, route, preset) keys."""
        if not keys:
            return []
        def _iter_chunks(src: list[tuple[int, int, int]], size: int):
            for i in range(0, len(src), size):
                yield src[i:i + size]
        out: list[dict] = []
        with self.lock:
            for chunk in _iter_chunks(keys, 300):
                placeholders = ",".join(["(?,?,?)"] * len(chunk))
                params: list[Any] = [int(job_id)]
                for site_id, route_id, preset_id in chunk:
                    params.extend([int(site_id), int(route_id), int(preset_id)])
                sql = f"""
                SELECT r.id, r.job_id, r.id_site, r.id_route, r.id_preset, r.price, r.days, r.currency, r.source,
                       r.name_tarif, r.allowances, r.status, r.error, r.context_json, r.created_at
                FROM results r
                JOIN (
                    SELECT MAX(id) AS id
                    FROM results
                    WHERE job_id=? AND (id_site, id_route, id_preset) IN ({placeholders})
                    GROUP BY id_site, id_route, id_preset
                ) t ON t.id = r.id
                """
                cur = self.conn.execute(sql, params)
                out.extend(dict(r) for r in cur.fetchall())
        return out
    def get_results_full_export(self) -> list[dict]:
        """Возвращает все результаты (для экспорта/аналитики)."""
        with self.lock:
            cur = self.conn.execute(
                """
                SELECT job_id, id_site, id_route, id_preset, price, days, currency, source, name_tarif, allowances, status, error, context_json, created_at
                FROM results
                ORDER BY job_id, id_site, id_route, id_preset, created_at
                """
            )
            return [dict(r) for r in cur.fetchall()]

    def delete_all_results(self) -> None:
        """Удаляет все результаты и сбрасывает счётчик AUTOINCREMENT."""
        with self.lock:
            self.conn.execute("DELETE FROM results")
            self.conn.execute("DELETE FROM sqlite_sequence WHERE name='results';")
            self.conn.commit()

    def get_active_job(self) -> dict | None:
        """Возвращает последний активный job (queued/running), если он есть."""
        with self.lock:
            row = self.conn.execute(
                """
                SELECT id, created_at, status, progress, errors_json, cancel_requested
                FROM jobs
                WHERE status IN ('queued','running')
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            return dict(row) if row else None
