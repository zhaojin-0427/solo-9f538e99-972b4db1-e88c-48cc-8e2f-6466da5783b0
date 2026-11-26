"""FastAPI entry point for one independently restartable grading point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import service
from .config import Settings
from .db import connect, init_db
from .errors import ApiError
from .schemas import OpenRequest, ReceiveTransferRequest, ScoreRequest, TransferRequest

settings = Settings.from_env()
init_db(settings)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Reconcile orders left pending by a lost HTTP response / process restart.
    service.start_background_recovery(settings)
    yield


app = FastAPI(
    title=f"联考阅卷服务 · {settings.site_id}",
    version="1.0.0",
    description=(
        "两个阅卷点各自运行本服务并使用独立 SQLite。\n\n"
        "1. 在 **Papers** 中调用开卷接口取得匿名答卷正文；\n"
        "2. 调用交分接口提交分数；\n"
        "3. 组长用原单号和一批卷号发起调卷；\n"
        "4. 已开卷/已交分的卷留在原点，未开卷的候选卷先暂停开卷，"
        "接收点整批收下后才在原点标记转出；明确拒收后恢复开卷资格；\n"
        "5. 回执丢失或服务重启后，用同一单号重试，接收点按单号幂等返回原结果。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "ok": False,
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
            # FastAPI-style convenience field.
            "detail": exc.message,
        },
    )


@app.get("/", tags=["system"], summary="服务说明")
def root() -> dict[str, Any]:
    return {
        "service": "joint-exam-grading",
        "site_id": settings.site_id,
        "docs": "/docs",
        "health": "/health",
        "papers": "/papers",
        "transfers": "/transfers",
        "capacity": "/capacity",
        "default_target_base_url": settings.default_target_base_url,
    }


@app.get("/health", tags=["system"], summary="健康检查和当前限额")
def health() -> dict[str, Any]:
    conn = connect(settings.database_path)
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    return {"status": "ok", "site_id": settings.site_id, **service.capacity(settings)}


@app.get("/capacity", tags=["system"], summary="接收点未交分卷数与限额")
def get_capacity() -> dict[str, Any]:
    return service.capacity(settings)


@app.get("/papers", tags=["papers"], summary="列出内置/已交接答卷")
def get_papers(
    status: str | None = Query(default=None, description="按状态过滤"),
    include_body: bool = Query(default=False, description="是否同时返回正文"),
    limit: int = Query(default=500, ge=1, le=1000),
) -> dict[str, Any]:
    items = service.list_papers(settings, status, include_body, limit)
    return {"site_id": settings.site_id, "count": len(items), "papers": items}


@app.get("/papers/{paper_no}", tags=["papers"], summary="查询单卷状态")
def get_paper(
    paper_no: str,
    include_body: bool = Query(default=False, description="已开卷后可设 true 查看正文"),
) -> dict[str, Any]:
    return service.get_paper(settings, paper_no, include_body=include_body)


@app.post(
    "/papers/{paper_no}/open",
    tags=["grading"],
    summary="开卷并取得匿名答卷正文",
)
@app.post("/open/{paper_no}", tags=["grading"], include_in_schema=False)
def open_paper_by_path(paper_no: str) -> dict[str, Any]:
    return service.open_paper(settings, paper_no)


@app.post("/open", tags=["grading"], summary="用请求体中的卷号开卷")
def open_paper_by_body(payload: OpenRequest) -> dict[str, Any]:
    return service.open_paper(settings, payload.paper_no)


def _submit_score(
    paper_no: str | None,
    payload: ScoreRequest | None,
    score_query: float | None,
    comment_query: str | None,
    grader_query: str | None,
) -> dict[str, Any]:
    resolved_no = paper_no or (payload.paper_no if payload else None)
    if not resolved_no:
        raise ApiError(422, "missing_paper_no", "必须提供卷号")
    score = payload.score if payload is not None else score_query
    if score is None:
        raise ApiError(422, "missing_score", "必须提供分数 score")
    comment = payload.comment if payload is not None and payload.comment is not None else comment_query
    grader = payload.grader if payload is not None and payload.grader is not None else grader_query
    return service.submit_score(settings, resolved_no, score, comment, grader)


@app.post(
    "/papers/{paper_no}/score",
    tags=["grading"],
    summary="提交分数（支持 JSON body 或 ?score=）",
)
@app.post("/papers/{paper_no}/scores", tags=["grading"], include_in_schema=False)
@app.post("/scores/{paper_no}", tags=["grading"], include_in_schema=False)
def score_paper_by_path(
    paper_no: str,
    payload: ScoreRequest | None = Body(default=None),
    score: float | None = Query(default=None),
    comment: str | None = Query(default=None),
    grader: str | None = Query(default=None),
) -> dict[str, Any]:
    return _submit_score(paper_no, payload, score, comment, grader)


@app.post("/score", tags=["grading"], summary="用请求体提交分数")
@app.post("/scores", tags=["grading"], summary="交分（别名）")
def score_paper_by_body(payload: ScoreRequest) -> dict[str, Any]:
    return _submit_score(None, payload, None, None, None)


@app.post(
    "/transfers",
    tags=["transfers"],
    summary="凭单号和一批卷号发起/重试调卷",
)
@app.post("/transfer", tags=["transfers"], include_in_schema=False)
@app.post("/api/transfers", tags=["transfers"], include_in_schema=False)
@app.post("/api/transfer", tags=["transfers"], include_in_schema=False)
def create_transfer(payload: TransferRequest) -> dict[str, Any]:
    return service.create_and_dispatch(
        settings,
        order_no=payload.order_no,
        raw_paper_nos=payload.paper_nos,
        target_base_url=payload.target_base_url,
        target_site=payload.target_site,
    )


@app.get("/transfers", tags=["transfers"], summary="列出原点调卷单")
def list_transfers(status: str | None = Query(default=None)) -> dict[str, Any]:
    items = service.list_transfers(settings, status)
    return {"site_id": settings.site_id, "count": len(items), "transfers": items}


@app.post("/transfers/recover-pending", tags=["transfers"], summary="立即恢复所有回执未知的调卷单")
@app.post("/transfers/recover", tags=["transfers"], include_in_schema=False)
def recover_pending() -> dict[str, Any]:
    return service.recover_pending(settings)


@app.post(
    "/transfers/{order_no}/retry",
    tags=["transfers"],
    summary="用原单号重试未完成的调卷",
)
@app.post("/transfer/{order_no}/retry", tags=["transfers"], include_in_schema=False)
def retry_transfer(order_no: str) -> dict[str, Any]:
    return service.retry_transfer(settings, order_no)


@app.get(
    "/transfers/{order_no}",
    tags=["transfers"],
    summary="查询原点调卷单和转走/留下卷号",
)
@app.get("/transfer/{order_no}", tags=["transfers"], include_in_schema=False)
def get_transfer(order_no: str) -> dict[str, Any]:
    return service.get_transfer(settings, order_no)


@app.post(
    "/internal/transfers",
    tags=["receiver-internal"],
    summary="接收点受理 HTTP 交接（按单号幂等）",
)
@app.post("/internal/transfer", tags=["receiver-internal"], include_in_schema=False)
@app.post("/receive/transfers", tags=["receiver-internal"], include_in_schema=False)
def receive_transfer(payload: ReceiveTransferRequest) -> dict[str, Any]:
    return service.receive_transfer(settings, payload)


@app.get(
    "/internal/transfers/{order_no}",
    tags=["receiver-internal"],
    summary="按原单号查回接收点的首次接收/拒收结果",
)
def get_inbound_transfer(order_no: str) -> dict[str, Any]:
    return service.get_inbound_order(settings, order_no)
