"""SQLite 持久化与发布状态机。

一致性保证：
- 每个有效提交在单个事务（BEGIN IMMEDIATE）内完成：写入新版本、
  删除旧脱敏副本、原子切换发布指针 —— 旧副本随提交立即撤下；
- 全部读写经过同一把可重入锁，发布与再次提交交错时不会发布过期清单，
  公开下载也不会读到跨版本的撕裂数据；
- 人口表按病例版本登记，整表替换在单事务内完成，校验或保存失败时
  上次登记保持不变；报告在同一把锁内读取同一时刻有效的病例副本与
  人口表，并各自标明版本号；
- 状态全部落盘（service_state.published_version 是唯一发布指针），
  重启后已撤下的副本不会被恢复（旧副本行在撤下时即被物理删除）。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone

from .generalize import (
    CaseRecord,
    age_band_label,
    evaluate,
    infeasible_reason,
    zip_label,
)
from .models import PopulationIn, SubmissionIn
from .population import REASONS, PopRow, match_group

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    version      INTEGER PRIMARY KEY AUTOINCREMENT,
    k            INTEGER NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('published', 'withdrawn')),
    reason       TEXT,
    age_band     INTEGER,
    zip_prefix   INTEGER,
    retained     INTEGER NOT NULL DEFAULT 0,
    suppressed   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    version   INTEGER NOT NULL REFERENCES submissions(version),
    case_id   TEXT NOT NULL,
    age       INTEGER NOT NULL,
    zip       TEXT NOT NULL,
    disease   TEXT NOT NULL,
    suppressed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (version, case_id)
);
-- 脱敏副本：只保存当前发布版本的逐行副本，撤下即删除
CREATE TABLE IF NOT EXISTS publication_rows (
    version  INTEGER NOT NULL,
    age_band TEXT NOT NULL,
    zip      TEXT NOT NULL,
    disease  TEXT NOT NULL
);
-- 单例状态行：published_version 为 NULL 表示当前无已发布副本（撤下状态）
CREATE TABLE IF NOT EXISTS service_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    published_version INTEGER
);
INSERT OR IGNORE INTO service_state (id, published_version) VALUES (1, NULL);
-- 人口登记：每个病例版本至多一份当前人口表（重新登记在事务内整表替换）。
-- pop_version 单调递增，作为报告中人口数据自身的版本号。
CREATE TABLE IF NOT EXISTS population_registrations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    case_version  INTEGER NOT NULL UNIQUE REFERENCES submissions(version),
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS population_rows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pop_version INTEGER NOT NULL REFERENCES population_registrations(id),
    age_lo      INTEGER NOT NULL CHECK (age_lo BETWEEN 0 AND 100),
    age_hi      INTEGER NOT NULL CHECK (age_hi BETWEEN 0 AND 100 AND age_hi >= age_lo),
    zip_prefix  TEXT NOT NULL DEFAULT '',
    population  INTEGER NOT NULL CHECK (population >= 0)
);
"""

SUBMISSION_COLS = "version, k, status, reason, age_band, zip_prefix, retained, suppressed, created_at"


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ submit
    def submit(self, payload: SubmissionIn) -> tuple[dict, int]:
        """处理一个（已通过合法性校验的）新清单。

        返回 (响应体, HTTP 状态码)。无论新版能否发布，旧副本都在本事务内撤下。
        """
        records = [
            CaseRecord(c.case_id, c.age, c.zip, c.disease) for c in payload.cases
        ]
        best, diagnostics = evaluate(records, payload.k)
        now = datetime.now(timezone.utc).isoformat()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # 立即撤下旧副本：删除旧脱敏行并清空发布指针
                self._conn.execute("DELETE FROM publication_rows")
                self._conn.execute(
                    "UPDATE service_state SET published_version = NULL WHERE id = 1"
                )

                if best is None:
                    reason = infeasible_reason(payload.k, diagnostics)
                    cur = self._conn.execute(
                        f"INSERT INTO submissions ({SUBMISSION_COLS}) VALUES (NULL, ?, 'withdrawn', ?, NULL, NULL, 0, 0, ?)",
                        (payload.k, reason, now),
                    )
                    version = cur.lastrowid
                    self._insert_cases(version, records, set())
                    self._conn.execute("COMMIT")
                    body = {
                        "version": version,
                        "status": "withdrawn",
                        "k": payload.k,
                        "message": "旧脱敏副本已撤下；新版无法达标，保持撤下状态。",
                        "reason": reason,
                        "details": diagnostics,
                    }
                    return body, 201

                suppressed_ids = set(best.suppressed_ids)
                retained = len(records) - best.suppressed_count
                cur = self._conn.execute(
                    f"INSERT INTO submissions ({SUBMISSION_COLS}) VALUES (NULL, ?, 'published', NULL, ?, ?, ?, ?, ?)",
                    (payload.k, best.age_band, best.zip_prefix, retained,
                     best.suppressed_count, now),
                )
                version = cur.lastrowid
                self._insert_cases(version, records, suppressed_ids)
                for r in records:
                    if r.case_id in suppressed_ids:
                        continue
                    self._conn.execute(
                        "INSERT INTO publication_rows (version, age_band, zip, disease) VALUES (?, ?, ?, ?)",
                        (version, age_band_label(r.age, best.age_band),
                         zip_label(r.zip, best.zip_prefix), r.disease),
                    )
                self._conn.execute(
                    "UPDATE service_state SET published_version = ? WHERE id = 1",
                    (version,),
                )
                self._conn.execute("COMMIT")
                body = {
                    "version": version,
                    "status": "published",
                    "k": payload.k,
                    "params": {
                        "age_band_years": best.age_band,
                        "zip_prefix_digits": best.zip_prefix,
                    },
                    "retained": retained,
                    "suppressed": best.suppressed_count,
                    "suppressed_case_ids": sorted(suppressed_ids),
                    "download": "/api/publication",
                }
                return body, 201
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _insert_cases(self, version: int, records, suppressed_ids: set[str]) -> None:
        for r in records:
            self._conn.execute(
                "INSERT INTO cases (version, case_id, age, zip, disease, suppressed) VALUES (?, ?, ?, ?, ?, ?)",
                (version, r.case_id, r.age, r.zip, r.disease,
                 1 if r.case_id in suppressed_ids else 0),
            )

    # -------------------------------------------------------------- publication
    def publication(self) -> tuple[dict, int]:
        """公开下载：只返回同一完整版本的副本与参数；撤下状态下返回 404 及原因。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT published_version FROM service_state WHERE id = 1"
            ).fetchone()
            published = row["published_version"]
            if published is None:
                last = self._conn.execute(
                    f"SELECT {SUBMISSION_COLS} FROM submissions ORDER BY version DESC LIMIT 1"
                ).fetchone()
                body = {
                    "detail": "当前无已发布的脱敏副本",
                    "state": "withdrawn" if last else "empty",
                    "latest_version": last["version"] if last else None,
                    "reason": last["reason"] if last else "尚未收到任何有效清单",
                }
                return body, 404
            sub = self._conn.execute(
                f"SELECT {SUBMISSION_COLS} FROM submissions WHERE version = ?",
                (published,),
            ).fetchone()
            rows = self._conn.execute(
                "SELECT age_band, zip, disease FROM publication_rows WHERE version = ? ORDER BY rowid",
                (published,),
            ).fetchall()
            body = {
                "version": sub["version"],
                "k": sub["k"],
                "params": {
                    "age_band_years": sub["age_band"],
                    "zip_prefix_digits": sub["zip_prefix"],
                },
                "retained": sub["retained"],
                "suppressed": sub["suppressed"],
                "rows": [dict(r) for r in rows],
            }
            return body, 200

    # ------------------------------------------------------------------- status
    def status(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT published_version FROM service_state WHERE id = 1"
            ).fetchone()
            published = row["published_version"]
            last = self._conn.execute(
                f"SELECT {SUBMISSION_COLS} FROM submissions ORDER BY version DESC LIMIT 1"
            ).fetchone()
            state = "published" if published is not None else ("withdrawn" if last else "empty")
            return {
                "state": state,
                "published_version": published,
                "latest_submission": dict(last) if last else None,
            }

    def history(self) -> list[dict]:
        """提交历史（仅元数据，不含原始病例明细）。"""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {SUBMISSION_COLS} FROM submissions ORDER BY version"
            ).fetchall()
            return [dict(r) for r in rows]

    # --------------------------------------------------------- population 登记
    def register_population(self, payload: PopulationIn) -> tuple[dict, int]:
        """为某个病例版本整表登记/替换人口表。

        字段级与重叠校验已由 Pydantic 完成（非法请求 422，不触达本方法）。
        本方法在单事务内整表替换：任一步失败则回滚，沿用上次登记。
        病例版本不存在时返回 422。
        """
        now = datetime.now(timezone.utc).isoformat()
        rows = [r.to_row() for r in payload.rows]
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM submissions WHERE version = ?", (payload.version,)
            ).fetchone()
            if exists is None:
                return {
                    "detail": f"病例版本 {payload.version} 不存在，无法登记人口表",
                    "reason_code": "version_not_found",
                }, 422

            self._conn.execute("BEGIN IMMEDIATE")
            try:
                old = self._conn.execute(
                    "SELECT id FROM population_registrations WHERE case_version = ?",
                    (payload.version,),
                ).fetchone()
                if old is not None:
                    # 整表替换：先删旧行与旧登记，异常回滚即恢复上次登记
                    self._conn.execute(
                        "DELETE FROM population_rows WHERE pop_version = ?", (old["id"],)
                    )
                    self._conn.execute(
                        "DELETE FROM population_registrations WHERE id = ?", (old["id"],)
                    )
                cur = self._conn.execute(
                    "INSERT INTO population_registrations (case_version, created_at) VALUES (?, ?)",
                    (payload.version, now),
                )
                pop_version = cur.lastrowid
                self._conn.executemany(
                    "INSERT INTO population_rows "
                    "(pop_version, age_lo, age_hi, zip_prefix, population) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [(pop_version, r.age_lo, r.age_hi, r.zip_prefix, r.population)
                     for r in rows],
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return {
            "pop_version": pop_version,
            "case_version": payload.version,
            "rows": len(rows),
            "total_population": sum(r.population for r in rows),
            "message": "人口表登记成功",
        }, 201

    def get_population(self, version: int) -> tuple[dict, int]:
        """查询某病例版本当前登记的人口表（含人口表自身版本号）。"""
        with self._lock:
            sub = self._conn.execute(
                "SELECT version FROM submissions WHERE version = ?", (version,)
            ).fetchone()
            if sub is None:
                return {
                    "detail": f"病例版本 {version} 不存在",
                    "reason_code": "version_not_found",
                }, 422
            reg = self._conn.execute(
                "SELECT id, created_at FROM population_registrations WHERE case_version = ?",
                (version,),
            ).fetchone()
            if reg is None:
                return {
                    "case_version": version,
                    "pop_version": None,
                    "message": REASONS["missing_population_table"],
                    "reason_code": "missing_population_table",
                    "rows": [],
                }, 200
            rows = self._conn.execute(
                "SELECT age_lo, age_hi, zip_prefix, population "
                "FROM population_rows WHERE pop_version = ? ORDER BY id",
                (reg["id"],),
            ).fetchall()
            return {
                "case_version": version,
                "pop_version": reg["id"],
                "created_at": reg["created_at"],
                "rows": [
                    {
                        "age_lo": r["age_lo"],
                        "age_hi": r["age_hi"],
                        "zip_prefix": r["zip_prefix"] or None,
                        "population": r["population"],
                    }
                    for r in rows
                ],
            }, 200

    def _load_population_rows(self, pop_version: int) -> list[PopRow]:
        rows = self._conn.execute(
            "SELECT age_lo, age_hi, zip_prefix, population "
            "FROM population_rows WHERE pop_version = ? ORDER BY id",
            (pop_version,),
        ).fetchall()
        return [
            PopRow(r["age_lo"], r["age_hi"], r["zip_prefix"] or "", r["population"])
            for r in rows
        ]

    # ------------------------------------------------------------------ report
    def report(self) -> tuple[dict, int]:
        """人口关联报告：当前匿名副本的可见分组 × 病例数/人口数/每万人病例数。

        无当前副本（撤下或从未发布）时不生成报告（404）。病例与人口在同一把
        锁内读取，保证报告中的两份数据是查询期间同一时刻有效的一对。
        """
        with self._lock:
            state = self._conn.execute(
                "SELECT published_version FROM service_state WHERE id = 1"
            ).fetchone()
            published = state["published_version"]
            if published is None:
                last = self._conn.execute(
                    f"SELECT {SUBMISSION_COLS} FROM submissions ORDER BY version DESC LIMIT 1"
                ).fetchone()
                return {
                    "detail": "当前无已发布的脱敏副本，不生成报告",
                    "state": "withdrawn" if last else "empty",
                    "latest_version": last["version"] if last else None,
                    "reason": last["reason"] if last else "尚未收到任何有效清单",
                }, 404

            sub = self._conn.execute(
                f"SELECT {SUBMISSION_COLS} FROM submissions WHERE version = ?",
                (published,),
            ).fetchone()
            pub_rows = self._conn.execute(
                "SELECT age_band, zip FROM publication_rows WHERE version = ? ORDER BY rowid",
                (published,),
            ).fetchall()

            # 病例分组计数（只列当前副本中的可见分组）
            counts: dict[tuple[str, str], int] = {}
            for r in pub_rows:
                key = (r["age_band"], r["zip"])
                counts[key] = counts.get(key, 0) + 1

            # 只认当前病例版本登记的人口表；换版后旧版本人口表不会被使用
            reg = self._conn.execute(
                "SELECT id FROM population_registrations WHERE case_version = ?",
                (published,),
            ).fetchone()
            pop_rows = self._load_population_rows(reg["id"]) if reg else []
            band = sub["age_band"]

            groups = []
            for (age_label, zip_lab), case_count in sorted(
                counts.items(), key=lambda kv: _group_sort_key(kv[0])
            ):
                entry: dict = {
                    "age_band": age_label,
                    "zip": zip_lab,
                    "case_count": case_count,
                }
                if reg is None:
                    entry["population"] = None
                    entry["cases_per_10000"] = None
                    entry["reason_code"] = "missing_population_table"
                    entry["reason"] = REASONS["missing_population_table"]
                else:
                    age_lo, age_hi = _age_label_bounds(age_label, band)
                    prefix = zip_lab[:sub["zip_prefix"]]
                    match = match_group(pop_rows, age_lo, age_hi, prefix)
                    if match.ok:
                        entry["population"] = match.population
                        entry["cases_per_10000"] = round(
                            case_count * 10000.0 / match.population, 4
                        )
                        entry["reason_code"] = None
                        entry["reason"] = None
                    else:
                        entry["population"] = None
                        entry["cases_per_10000"] = None
                        entry["reason_code"] = match.reason_code
                        entry["reason"] = match.reason
                groups.append(entry)

            return {
                "case_version": published,
                "pop_version": reg["id"] if reg else None,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "groups": groups,
            }, 200


def _age_label_bounds(label: str, band: int) -> tuple[int, int]:
    """年龄段标签 -> 按源数据 0~100 岁口径截断后的实际闭区间。

    例如 5 岁段下 '100-104' 实际只含 100 岁，故返回 (100, 100)；
    其余标签形如 '20-24'，直接解析。
    """
    lo_s, hi_s = label.split("-")
    lo, hi = int(lo_s), int(hi_s)
    return max(lo, 0), min(hi, 100)


def _group_sort_key(key: tuple[str, str]) -> tuple[int, str]:
    age_label, zip_label_value = key
    lo = int(age_label.split("-")[0])
    return lo, zip_label_value
