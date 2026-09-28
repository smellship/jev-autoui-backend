"""FastAPI 装配：错误体 / CORS / 路由 / 队列 / 保留策略（§14）。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.v1 import router as v1_router
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.db import SessionLocal, init_db
from app.services import retention, run_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("ui_web")


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_db()
        run_service.queue.concurrency = max(1, settings.run_concurrency)
        run_service.queue.start()
        retention.start_background()
        log.info("平台已就绪：db=%s artifacts=%s kernel=%s",
                 settings.db_url, settings.artifacts_dir, settings.kernel_cmd)
        try:
            yield
        finally:
            run_service.queue.stop()

    app = FastAPI(title="UI 测试平台 API", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_list,
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    app.include_router(v1_router)

    @app.get("/api/v1/health")
    def health() -> dict:
        db_state = "ok"
        try:
            with SessionLocal() as session:
                session.execute(text("SELECT 1"))
        except Exception as exc:  # 健康检查不能自己抛
            db_state = f"error: {exc}"
        return {
            "ok": db_state == "ok",
            "time": datetime.now().isoformat(timespec="seconds"),
            "db": db_state,
            "kernel": settings.kernel_cmd,
            "queue": {"pending": run_service.queue.pending(),
                      "concurrency": run_service.queue.concurrency},
            "artifacts_dir": str(settings.artifacts_dir),
            "retention_enabled": settings.retention_enabled,
        }

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return ("<h1>UI 测试平台 API</h1>"
                "<p>接口前缀 <code>/api/v1</code>；前端构建产物若放在 <code>frontend/dist</code> 会自动托管。</p>")

    dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")

    return app


app = create_app()
