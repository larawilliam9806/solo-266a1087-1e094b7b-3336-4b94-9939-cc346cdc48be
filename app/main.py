"""FastAPI 入口：哨点病例脱敏副本发布服务。

接口：
- POST /api/submissions   整批提交新清单（<=40 条）；有效提交立即撤下旧副本，
                          能达标则发布新版本，否则保持撤下并说明原因；
                          非法提交返回 422，不影响旧版本。
- GET  /api/publication   公开下载当前发布的脱敏副本与参数（同一完整版本）；
                          撤下状态下返回 404 及原因。
- GET  /api/status        当前发布状态。
- GET  /api/submissions   提交历史（仅元数据）。
- POST /api/population    为指定病例版本整表登记人口（行覆盖不得重叠；
                          失败时沿用上次登记）。
- GET  /api/population    查询某病例版本当前登记的人口表。
- GET  /api/report        当前副本各可见分组的病例数与人口关联报告；
                          无当前副本时不生成（404）。
- GET  /health            健康检查。
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .models import PopulationIn, SubmissionIn
from .store import Store

DEFAULT_DB_PATH = os.environ.get("APP_DB_PATH", "./app.db")


def create_app(db_path: str | None = None) -> FastAPI:
    store = Store(db_path or os.environ.get("APP_DB_PATH", "./app.db"))
    app = FastAPI(title="哨点病例脱敏副本发布服务", version="1.1.0")
    app.state.store = store

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/submissions", status_code=201)
    def submit(payload: SubmissionIn):
        body, status = store.submit(payload)
        return JSONResponse(body, status_code=status)

    @app.get("/api/publication")
    def publication():
        body, status = store.publication()
        return JSONResponse(body, status_code=status)

    @app.get("/api/status")
    def status():
        return store.status()

    @app.get("/api/submissions")
    def submissions():
        return {"submissions": store.history()}

    @app.post("/api/population", status_code=201)
    def register_population(payload: PopulationIn):
        body, status = store.register_population(payload)
        return JSONResponse(body, status_code=status)

    @app.get("/api/population")
    def get_population(version: int):
        body, status = store.get_population(version)
        return JSONResponse(body, status_code=status)

    @app.get("/api/report")
    def report():
        body, status = store.report()
        return JSONResponse(body, status_code=status)

    return app


app = create_app(DEFAULT_DB_PATH)
