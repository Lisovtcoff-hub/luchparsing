
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
    ) -> int:
        """Вставляет одну строку результата и возвращает её id."""
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO results(job_id, id_site, id_route, id_preset, price, days, currency, source, name_tarif, allowances)
                VALUES(?,?,?,?,?,?,?,?,?,?)
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
                ),
            )
            rid = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.conn.commit()
            return int(rid)

    def insert_results_batch(
        self,
        rows: list[tuple[int, int, int, float | None, str | None, float]],
        *,
        job_id: int | None = None,
        currency: str = "RUB",
        source: str | None = None,
        name_tarif: str | None = None,
        allowances: str | None = None,
    ) -> None:
        """
        Вставляет пачку результатов.

        Формат rows: (site_id, route_id, preset_id, price, days_str, created_at_unix_ts).
        """
        if not rows:
            return
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE;")
            self.conn.executemany(
                """
                INSERT INTO results(job_id, id_site, id_route, id_preset, price, days, currency, source, name_tarif, allowances, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime(?,'unixepoch'))
                """,
                [
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
                        float(created_ts),
                    )
                    for (site_id, route_id, preset_id, price, days_str, created_ts) in rows
                ],
            )
            self.conn.commit()

    def add_res_price_many(self, rows):
        """Совместимость: пакетная вставка результатов без job_id."""
        self.insert_results_batch(rows, job_id=None)

    def add_res_price_many_v2(self, job_id: int, rows):
        """Совместимость: пакетная вставка результатов с job_id."""
        self.insert_results_batch(rows, job_id=int(job_id))

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

        sql = f"""
        SELECT id, job_id, id_site, id_route, id_preset, price, days, currency, source, name_tarif, allowances, created_at
        FROM results
        {"WHERE " + " AND ".join(where) if where else ""}
        ORDER BY id DESC
        LIMIT ?
        """
        params.append(int(limit))

        with self.lock:
            cur = self.conn.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def get_results_full_export(self) -> list[dict]:
        """Возвращает все результаты (для экспорта/аналитики)."""
        with self.lock:
            cur = self.conn.execute(
                """
                SELECT job_id, id_site, id_route, id_preset, price, days, currency, source, name_tarif, allowances, created_at
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
