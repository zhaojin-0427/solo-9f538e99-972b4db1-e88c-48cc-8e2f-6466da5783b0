"""Core grading and transfer use cases.

The important invariant is that an origin paper is durably moved to
``transfer_pending`` *before* the HTTP call to the receiver.  It therefore
cannot be opened while the receiver outcome is unknown.  Receiver decisions
are keyed by the caller's order number, making retries safe after a lost
response or an origin restart.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .config import Settings
from .db import begin_immediate, connect, now_iso
from .errors import ApiError

OPEN_LIKE_STATUSES = {"open", "scored"}
UNAVAILABLE_AT_ORIGIN = {"transfer_pending", "transferred_out"}
TERMINAL_ORDER_STATUSES = {"accepted", "rejected", "completed_locally"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def normalize_paper_nos(value: Any) -> list[str]:
    """Accept arrays, comma/newline text, numbers, or objects with an ID key."""

    if value is None:
        return []

    raw_values: list[Any]
    if isinstance(value, str):
        raw_values = [part for chunk in value.replace("，", ",").split(",") for part in chunk.splitlines()]
        # Whitespace-separated batches are also accepted.
        expanded: list[Any] = []
        for item in raw_values:
            expanded.extend(item.split())
        raw_values = expanded
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raw_values = [value]

    result: list[str] = []
    seen: set[str] = set()

    def add(raw: Any) -> None:
        if raw is None:
            return
        if isinstance(raw, dict):
            raw = (
                raw.get("paper_no")
                or raw.get("paperNo")
                or raw.get("paper_id")
                or raw.get("paperId")
                or raw.get("id")
                or raw.get("卷号")
            )
        text = str(raw).strip()
        if not text:
            return
        if text not in seen:
            seen.add(text)
            result.append(text)

    for item in raw_values:
        add(item)

    return result


def _row_to_paper(row: Any, include_body: bool = False) -> dict[str, Any]:
    data = dict(row)
    if not include_body:
        data.pop("body", None)
    return data


def capacity(settings: Settings) -> dict[str, Any]:
    conn = connect(settings.database_path)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM papers
            WHERE status NOT IN ('scored', 'transferred_out')
            """
        ).fetchone()
        unscored = int(row["n"])
        limit = settings.max_unscored
        remaining = None if limit is None else limit - unscored
        return {
            "site_id": settings.site_id,
            "unscored_count": unscored,
            "max_unscored": limit,
            "remaining_quota": remaining,
            "unlimited": limit is None,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Paper opening and scoring
# ---------------------------------------------------------------------------


def list_papers(
    settings: Settings,
    status_filter: str | None = None,
    include_body: bool = False,
    limit: int = 500,
) -> list[dict[str, Any]]:
    conn = connect(settings.database_path)
    try:
        safe_limit = max(1, min(int(limit or 500), 1000))
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM papers WHERE status = ? ORDER BY paper_no LIMIT ?",
                (status_filter, safe_limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM papers ORDER BY paper_no LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [_row_to_paper(row, include_body=include_body) for row in rows]
    finally:
        conn.close()


def get_paper(settings: Settings, paper_no: str, include_body: bool = False) -> dict[str, Any]:
    conn = connect(settings.database_path)
    try:
        row = conn.execute("SELECT * FROM papers WHERE paper_no = ?", (paper_no,)).fetchone()
        if row is None:
            raise ApiError(404, "paper_not_found", f"找不到卷号 {paper_no}", {"paper_no": paper_no})
        return _row_to_paper(row, include_body=include_body)
    finally:
        conn.close()


def open_paper(settings: Settings, paper_no: str) -> dict[str, Any]:
    paper_no = paper_no.strip()
    conn = connect(settings.database_path)
    try:
        begin_immediate(conn)
        row = conn.execute("SELECT * FROM papers WHERE paper_no = ?", (paper_no,)).fetchone()
        if row is None:
            raise ApiError(404, "paper_not_found", f"找不到卷号 {paper_no}", {"paper_no": paper_no})

        if row["status"] in UNAVAILABLE_AT_ORIGIN:
            raise ApiError(
                409,
                "paper_transfer_paused",
                f"卷号 {paper_no} 已因调卷暂停开卷或已转走，原点不能再开卷",
                {
                    "paper_no": paper_no,
                    "status": row["status"],
                    "transfer_order_no": row["transfer_order_no"],
                },
            )

        already_open = row["status"] in {"open", "scored"}
        timestamp = now_iso()
        if row["status"] == "unopened":
            conn.execute(
                """
                UPDATE papers
                SET status = 'open', opened_at = ?, updated_at = ?
                WHERE paper_no = ?
                """,
                (timestamp, timestamp, paper_no),
            )
        conn.commit()

        row = conn.execute("SELECT * FROM papers WHERE paper_no = ?", (paper_no,)).fetchone()
        result = _row_to_paper(row, include_body=True)
        result["already_open"] = already_open
        result["message"] = "已开卷，正文见 body 字段" if not already_open else "该卷此前已开卷，返回原正文"
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def submit_score(
    settings: Settings,
    paper_no: str,
    score: float,
    comment: str | None = None,
    grader: str | None = None,
) -> dict[str, Any]:
    paper_no = paper_no.strip()
    try:
        score = float(score)
    except (TypeError, ValueError):
        raise ApiError(422, "invalid_score", "分数必须是数字") from None

    if score != score or score in (float("inf"), float("-inf")):
        raise ApiError(422, "invalid_score", "分数不能是 NaN 或无穷大")
    if score < settings.score_min or score > settings.score_max:
        raise ApiError(
            422,
            "score_out_of_range",
            f"分数需在 {settings.score_min}–{settings.score_max} 之间",
            {"min": settings.score_min, "max": settings.score_max},
        )

    conn = connect(settings.database_path)
    try:
        begin_immediate(conn)
        row = conn.execute("SELECT * FROM papers WHERE paper_no = ?", (paper_no,)).fetchone()
        if row is None:
            raise ApiError(404, "paper_not_found", f"找不到卷号 {paper_no}", {"paper_no": paper_no})

        if row["status"] in UNAVAILABLE_AT_ORIGIN:
            raise ApiError(
                409,
                "paper_not_at_origin",
                f"卷号 {paper_no} 正在调卷或已转走，不能在原点交分",
                {
                    "paper_no": paper_no,
                    "status": row["status"],
                    "transfer_order_no": row["transfer_order_no"],
                },
            )

        timestamp = now_iso()
        previously_scored = row["status"] == "scored"
        # Lenient workflow: a direct score call also reveals/opens the paper.
        conn.execute(
            """
            UPDATE papers
            SET status = 'scored',
                score = ?,
                score_comment = ?,
                grader = COALESCE(?, grader),
                opened_at = COALESCE(opened_at, ?),
                scored_at = COALESCE(scored_at, ?),
                updated_at = ?
            WHERE paper_no = ?
            """,
            (score, comment, grader, timestamp, timestamp, timestamp, paper_no),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM papers WHERE paper_no = ?", (paper_no,)).fetchone()
        result = _row_to_paper(row, include_body=False)
        result["previously_scored"] = previously_scored
        result["message"] = "分数已提交" + ("；原分数已更新" if previously_scored else "")
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Origin-side transfer order
# ---------------------------------------------------------------------------


def _transfer_dict(conn: Any, row: Any) -> dict[str, Any]:
    data = dict(row)
    requested = _json_loads(data.pop("request_json", None), {})
    requested_nos = _json_loads(data.pop("requested_nos_json", None), [])
    candidate_nos = _json_loads(data.pop("candidate_nos_json", None), [])
    kept_nos = _json_loads(data.pop("kept_nos_json", None), [])
    receiver_response = _json_loads(data.pop("receiver_response_json", None), None)

    status = data["status"]
    if status == "accepted":
        transferred = list(candidate_nos)
        kept_final = list(kept_nos)
        paused: list[str] = []
        restored: list[str] = []
    elif status == "rejected":
        transferred = []
        restored = list(candidate_nos)
        # Preserve the leader's batch ordering in the combined "kept" list.
        kept_set = set(kept_nos) | set(candidate_nos)
        kept_final = [no for no in requested_nos if no in kept_set]
        paused = []
    elif status == "completed_locally":
        transferred = []
        kept_final = list(kept_nos)
        paused = []
        restored = []
    else:
        transferred = []
        kept_final = list(kept_nos)
        paused = list(candidate_nos)
        restored = []

    item_rows = conn.execute(
        "SELECT paper_no, role, origin_status, disposition, position FROM transfer_items WHERE order_no = ? ORDER BY position",
        (data["order_no"],),
    ).fetchall()
    items = [dict(item) for item in item_rows]

    result = {
        **data,
        "request": requested,
        "requested_paper_nos": requested_nos,
        "candidate_paper_nos": candidate_nos,
        "kept_open_paper_nos": kept_nos,
        "receiver_response": receiver_response,
        "transferred_paper_nos": transferred,
        "kept_at_origin_paper_nos": kept_final,
        "paused_paper_nos": paused,
        "restored_paper_nos": restored,
        "items": items,
        # Short aliases used by the README and convenient for assertions.
        "transferred": transferred,
        "kept_at_origin": kept_final,
        "transferred_ids": transferred,
        "kept_ids": kept_final,
        "kept_at_origin_ids": kept_final,
        "转走卷号": transferred,
        "留在原点卷号": kept_final,
        "order_id": data["order_no"],
    }
    if status == "accepted":
        result["decision"] = "accepted"
    elif status == "rejected":
        result["decision"] = "rejected"
    elif status == "completed_locally":
        result["decision"] = "not_needed"
    else:
        result["decision"] = "pending"
    return result


def get_transfer(settings: Settings, order_no: str) -> dict[str, Any]:
    conn = connect(settings.database_path)
    try:
        row = conn.execute("SELECT * FROM transfer_orders WHERE order_no = ?", (order_no,)).fetchone()
        if row is None:
            raise ApiError(404, "transfer_not_found", f"找不到调卷单号 {order_no}", {"order_no": order_no})
        return _transfer_dict(conn, row)
    finally:
        conn.close()


def list_transfers(settings: Settings, status_filter: str | None = None) -> list[dict[str, Any]]:
    conn = connect(settings.database_path)
    try:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM transfer_orders WHERE status = ? ORDER BY created_at DESC, order_no",
                (status_filter,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM transfer_orders ORDER BY created_at DESC, order_no",
            ).fetchall()
        return [_transfer_dict(conn, row) for row in rows]
    finally:
        conn.close()


def create_transfer(
    settings: Settings,
    order_no: str,
    raw_paper_nos: Any,
    target_base_url: str | None = None,
    target_site: str | None = None,
) -> tuple[dict[str, Any], bool]:
    order_no = order_no.strip()
    paper_nos = normalize_paper_nos(raw_paper_nos)
    if not order_no:
        raise ApiError(422, "invalid_order_no", "单号不能为空")
    if not paper_nos:
        raise ApiError(422, "invalid_paper_nos", "卷号列表不能为空")

    target_url = (target_base_url or settings.default_target_base_url or "").strip().rstrip("/") or None
    normalized_request = {
        "order_no": order_no,
        "paper_nos": paper_nos,
        "target_base_url": target_url,
        "target_site": target_site,
    }

    conn = connect(settings.database_path)
    created = False
    try:
        begin_immediate(conn)
        existing = conn.execute(
            "SELECT * FROM transfer_orders WHERE order_no = ?", (order_no,)
        ).fetchone()
        if existing is not None:
            old_requested = _json_loads(existing["requested_nos_json"], [])
            if set(old_requested) != set(paper_nos):
                detail = _transfer_dict(conn, existing)
                raise ApiError(
                    409,
                    "order_no_conflict",
                    f"单号 {order_no} 已记录，但卷号批次与首次请求不一致；请使用原批次或新单号",
                    {"existing_order": detail},
                )
            if target_url and existing["target_base_url"] and target_url != existing["target_base_url"]:
                detail = _transfer_dict(conn, existing)
                raise ApiError(
                    409,
                    "order_target_conflict",
                    f"单号 {order_no} 已指定其他接收点",
                    {"existing_order": detail},
                )
            conn.commit()
            return _transfer_dict(conn, existing), False

        placeholders = ",".join("?" for _ in paper_nos)
        rows = conn.execute(
            f"SELECT * FROM papers WHERE paper_no IN ({placeholders})", paper_nos
        ).fetchall()
        by_no = {row["paper_no"]: row for row in rows}
        missing = [no for no in paper_nos if no not in by_no]
        if missing:
            raise ApiError(
                404,
                "paper_not_found",
                "批次中存在本原点没有的卷号，整批未受理",
                {"missing_paper_nos": missing},
            )

        busy = [
            {"paper_no": no, "status": by_no[no]["status"], "order_no": by_no[no]["transfer_order_no"]}
            for no in paper_nos
            if by_no[no]["status"] in UNAVAILABLE_AT_ORIGIN
        ]
        if busy:
            raise ApiError(
                409,
                "paper_already_transferring",
                "批次中已有卷被其他调卷单暂停或转走，整批未受理",
                {"papers": busy},
            )

        candidate_nos: list[str] = []
        kept_nos: list[str] = []
        item_records: list[tuple[Any, ...]] = []
        timestamp = now_iso()
        for position, no in enumerate(paper_nos):
            row = by_no[no]
            if row["status"] == "unopened":
                candidate_nos.append(no)
                conn.execute(
                    """
                    UPDATE papers
                    SET status = 'transfer_pending',
                        transfer_order_no = ?,
                        updated_at = ?
                    WHERE paper_no = ? AND status = 'unopened'
                    """,
                    (order_no, timestamp, no),
                )
                role = "candidate"
                disposition = "paused_before_receiver"
            else:
                # open or scored: already opened papers stay at the origin.
                kept_nos.append(no)
                role = "kept"
                disposition = "stay_at_origin"

            item_records.append((order_no, no, role, row["status"], disposition, position))

        if candidate_nos and not target_url:
            raise ApiError(
                400,
                "missing_target",
                "存在未开卷、需要转走的卷，但未提供 target_base_url，且服务未配置 DEFAULT_TARGET_BASE_URL",
                {"candidate_paper_nos": candidate_nos},
            )

        local_only = not candidate_nos
        conn.execute(
            """
            INSERT INTO transfer_orders (
                order_no, target_site, target_base_url, status,
                request_json, requested_nos_json, candidate_nos_json,
                kept_nos_json, receiver_decision, receiver_reason,
                receiver_response_json, last_error,
                created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_no,
                target_site,
                target_url,
                "completed_locally" if local_only else "pending_receiver",
                _json_dumps(normalized_request),
                _json_dumps(paper_nos),
                _json_dumps(candidate_nos),
                _json_dumps(kept_nos),
                "not_needed" if local_only else None,
                "all_selected_papers_already_opened" if local_only else None,
                None,
                None,
                timestamp,
                timestamp,
                timestamp if local_only else None,
            ),
        )
        conn.executemany(
            """
            INSERT INTO transfer_items (
                order_no, paper_no, role, origin_status,
                disposition, position
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            item_records,
        )
        conn.commit()
        created = True
        row = conn.execute("SELECT * FROM transfer_orders WHERE order_no = ?", (order_no,)).fetchone()
        return _transfer_dict(conn, row), created
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _receiver_payload(settings: Settings, order: dict[str, Any]) -> dict[str, Any]:
    candidate_nos = order["candidate_paper_nos"]
    if not candidate_nos:
        return {
            "order_no": order["order_no"],
            "source_site": settings.site_id,
            "target_site": order.get("target_site"),
            "paper_nos": [],
            "papers": [],
        }

    conn = connect(settings.database_path)
    try:
        placeholders = ",".join("?" for _ in candidate_nos)
        rows = conn.execute(
            f"SELECT paper_no, body FROM papers WHERE paper_no IN ({placeholders})",
            candidate_nos,
        ).fetchall()
        bodies = {row["paper_no"]: row["body"] for row in rows}
    finally:
        conn.close()

    return {
        "order_no": order["order_no"],
        "source_site": settings.site_id,
        "target_site": order.get("target_site"),
        "paper_nos": candidate_nos,
        "papers": [{"paper_no": no, "body": bodies.get(no, "")} for no in candidate_nos],
    }


def _record_origin_transport_error(settings: Settings, order_no: str, message: str) -> None:
    conn = connect(settings.database_path)
    try:
        conn.execute(
            "UPDATE transfer_orders SET last_error = ?, updated_at = ? WHERE order_no = ?",
            (message[:2000], now_iso(), order_no),
        )
    finally:
        conn.close()


def finalize_origin_from_receiver(
    settings: Settings,
    order_no: str,
    receiver_result: dict[str, Any],
) -> dict[str, Any]:
    decision = receiver_result.get("decision") or ("accepted" if receiver_result.get("accepted") else "rejected")
    if decision not in {"accepted", "rejected"}:
        raise ApiError(502, "bad_receiver_response", "接收点返回了无法识别的决定", {"receiver_response": receiver_result})

    conn = connect(settings.database_path)
    try:
        begin_immediate(conn)
        row = conn.execute("SELECT * FROM transfer_orders WHERE order_no = ?", (order_no,)).fetchone()
        if row is None:
            raise ApiError(404, "transfer_not_found", f"找不到调卷单号 {order_no}")
        if row["status"] in TERMINAL_ORDER_STATUSES:
            conn.commit()
            return _transfer_dict(conn, row)

        candidate_nos = _json_loads(row["candidate_nos_json"], [])
        receiver_nos = normalize_paper_nos(receiver_result.get("paper_nos"))
        if receiver_nos and set(receiver_nos) != set(candidate_nos):
            raise ApiError(
                502,
                "receiver_batch_mismatch",
                "接收点确认的卷号与原点待转批次不一致；为安全起见继续暂停开卷",
                {"expected": candidate_nos, "receiver": receiver_nos},
            )

        timestamp = now_iso()
        if decision == "accepted":
            conn.execute(
                """
                UPDATE papers
                SET status = 'transferred_out',
                    transferred_at = ?,
                    updated_at = ?
                WHERE transfer_order_no = ?
                  AND status = 'transfer_pending'
                """,
                (timestamp, timestamp, order_no),
            )
            conn.execute(
                """
                UPDATE transfer_items
                SET disposition = 'transferred'
                WHERE order_no = ? AND role = 'candidate'
                """,
                (order_no,),
            )
            status = "accepted"
            reason = receiver_result.get("reason")
        else:
            conn.execute(
                """
                UPDATE papers
                SET status = 'unopened',
                    transfer_order_no = NULL,
                    updated_at = ?
                WHERE transfer_order_no = ?
                  AND status = 'transfer_pending'
                """,
                (timestamp, order_no),
            )
            conn.execute(
                """
                UPDATE transfer_items
                SET disposition = 'returned_to_pool'
                WHERE order_no = ? AND role = 'candidate'
                """,
                (order_no,),
            )
            status = "rejected"
            reason = receiver_result.get("reason") or "rejected_by_receiver"

        conn.execute(
            """
            UPDATE transfer_orders
            SET status = ?,
                receiver_decision = ?,
                receiver_reason = ?,
                receiver_response_json = ?,
                last_error = NULL,
                updated_at = ?,
                completed_at = ?
            WHERE order_no = ?
            """,
            (
                status,
                decision,
                reason,
                _json_dumps(receiver_result),
                timestamp,
                timestamp,
                order_no,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM transfer_orders WHERE order_no = ?", (order_no,)).fetchone()
        return _transfer_dict(conn, row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def attempt_receiver(settings: Settings, order_no: str) -> dict[str, Any]:
    order = get_transfer(settings, order_no)
    if order["status"] in TERMINAL_ORDER_STATUSES:
        return order

    target_url = order.get("target_base_url") or settings.default_target_base_url
    if not target_url:
        raise ApiError(400, "missing_target", "没有可用的接收点地址")

    payload = _receiver_payload(settings, order)
    url = target_url.rstrip("/") + "/internal/transfers"
    try:
        with httpx.Client(timeout=settings.http_timeout, trust_env=False) as client:
            response = client.post(url, json=payload)
    except httpx.HTTPError as exc:
        message = f"调用接收点失败，回执状态未知：{type(exc).__name__}: {exc}"
        _record_origin_transport_error(settings, order_no, message)
        latest = get_transfer(settings, order_no)
        raise ApiError(
            503,
            "transfer_receipt_uncertain",
            "接收点调用失败或回执丢失；接收决定可能已经入账。卷号继续暂停开卷，请用原单号重试。",
            {"order": latest, "transport_error": str(exc)},
        ) from exc

    if response.status_code != 200:
        message = f"接收点返回 HTTP {response.status_code}: {response.text[:1000]}"
        _record_origin_transport_error(settings, order_no, message)
        latest = get_transfer(settings, order_no)
        # A 4xx could be a validation bug.  We deliberately do not restore the
        # papers unless the receiver durably records an explicit rejection.
        raise ApiError(
            502,
            "receiver_not_confirmed",
            "接收点未给出可入账的明确接收/拒收结果；卷号继续暂停开卷，可修复后凭原单号重试",
            {"order": latest, "receiver_status_code": response.status_code, "receiver_body": response.text[:2000]},
        )

    try:
        receiver_result = response.json()
    except ValueError as exc:
        message = f"接收点返回了非 JSON 回执：{response.text[:1000]}"
        _record_origin_transport_error(settings, order_no, message)
        latest = get_transfer(settings, order_no)
        raise ApiError(
            502,
            "bad_receiver_response",
            "接收点回执无法解析；为避免重复入账，请凭原单号重试",
            {"order": latest},
        ) from exc

    return finalize_origin_from_receiver(settings, order_no, receiver_result)


def create_and_dispatch(
    settings: Settings,
    order_no: str,
    raw_paper_nos: Any,
    target_base_url: str | None = None,
    target_site: str | None = None,
) -> dict[str, Any]:
    order, _created = create_transfer(settings, order_no, raw_paper_nos, target_base_url, target_site)
    if order["status"] in TERMINAL_ORDER_STATUSES:
        return order
    return attempt_receiver(settings, order_no)


def retry_transfer(settings: Settings, order_no: str) -> dict[str, Any]:
    # 404 if unknown.
    get_transfer(settings, order_no)
    return attempt_receiver(settings, order_no)


def recover_pending(settings: Settings) -> dict[str, Any]:
    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            "SELECT order_no FROM transfer_orders WHERE status = 'pending_receiver' ORDER BY created_at"
        ).fetchall()
        order_nos = [row["order_no"] for row in rows]
    finally:
        conn.close()

    recovered: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for order_no in order_nos:
        try:
            recovered.append(retry_transfer(settings, order_no))
        except ApiError as exc:
            errors.append({"order_no": order_no, "code": exc.code, "message": exc.message})
        except Exception as exc:  # background loop must stay alive
            errors.append({"order_no": order_no, "code": "recovery_error", "message": str(exc)})
    return {"pending_found": len(order_nos), "recovered": recovered, "errors": errors}


def start_background_recovery(settings: Settings) -> None:
    if not settings.recovery_enabled:
        return

    import threading

    def loop() -> None:
        # Give uvicorn/Docker networking a brief moment, then reconcile.
        time.sleep(0.5)
        while True:
            try:
                recover_pending(settings)
            except Exception:
                # The next interval will retry.  Avoid busy-looping.
                pass
            time.sleep(max(0.5, settings.recovery_interval_seconds))

    thread = threading.Thread(target=loop, name="transfer-recovery", daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# Receiver-side durable idempotent intake
# ---------------------------------------------------------------------------


def _normalize_receiver_batch(req: Any) -> tuple[list[str], dict[str, str]]:
    bodies: dict[str, str] = {}
    nos_from_objects: list[str] = []

    raw_papers = getattr(req, "papers", None)
    if isinstance(raw_papers, str):
        # A comma/newline batch can be accepted directly when paper_nos is absent.
        string_nos = normalize_paper_nos(raw_papers)
        explicit_nos = normalize_paper_nos(getattr(req, "paper_nos", None))
        return explicit_nos or string_nos, bodies
    if raw_papers:
        normalized_objects: list[dict[str, Any]] = []
        for item in raw_papers:
            if isinstance(item, dict):
                normalized_objects.append(item)
            elif isinstance(item, str):
                try:
                    parsed = json.loads(item)
                    if isinstance(parsed, dict):
                        normalized_objects.append(parsed)
                        continue
                except json.JSONDecodeError:
                    pass
                normalized_objects.append({"paper_no": item})
        for item in normalized_objects:
            no = normalize_paper_nos([item])
            if no:
                paper_no = no[0]
                nos_from_objects.append(paper_no)
                body = item.get("body") or item.get("content") or item.get("answer_body") or item.get("text")
                if body is not None:
                    bodies[paper_no] = str(body)

    explicit_nos = normalize_paper_nos(getattr(req, "paper_nos", None))
    if explicit_nos:
        paper_nos = explicit_nos
    else:
        paper_nos = normalize_paper_nos(nos_from_objects)

    # Preserve body map for all normalized IDs.
    for item in raw_papers or []:
        if isinstance(item, dict):
            nos = normalize_paper_nos([item])
            if nos:
                body = item.get("body") or item.get("content") or item.get("answer_body") or item.get("text")
                if body is not None:
                    bodies[nos[0]] = str(body)

    return paper_nos, bodies


def receive_transfer(settings: Settings, req: Any) -> dict[str, Any]:
    order_no = req.order_no.strip()
    source_site = (getattr(req, "source_site", None) or "unknown-origin").strip()
    paper_nos, bodies = _normalize_receiver_batch(req)
    request_snapshot = {
        "order_no": order_no,
        "source_site": source_site,
        "paper_nos": paper_nos,
        "papers": [{"paper_no": no, "has_body": no in bodies} for no in paper_nos],
    }

    conn = connect(settings.database_path)
    try:
        begin_immediate(conn)

        existing = conn.execute(
            "SELECT response_json FROM inbound_orders WHERE order_no = ?", (order_no,)
        ).fetchone()
        if existing is not None:
            conn.commit()
            response = _json_loads(existing["response_json"], {})
            response["idempotent_replay"] = True
            response["replayed"] = True
            return response

        unscored_before = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM papers WHERE status NOT IN ('scored', 'transferred_out')"
            ).fetchone()["n"]
        )
        limit = settings.max_unscored

        def reject(reason: str, message: str, conflicts: list[str] | None = None) -> dict[str, Any]:
            response = {
                "order_no": order_no,
                "accepted": False,
                "decision": "rejected",
                "status": "rejected",
                "reason": reason,
                "message": message,
                "paper_nos": paper_nos,
                "unscored_count_before": unscored_before,
                "unscored_count_after": unscored_before,
                "max_unscored": limit,
                "conflicts": conflicts or [],
                "idempotent_replay": False,
            }
            timestamp = now_iso()
            conn.execute(
                """
                INSERT INTO inbound_orders (
                    order_no, source_site, decision, reason,
                    request_json, response_json,
                    unscored_count_before, unscored_count_after,
                    quota_limit, created_at, decided_at
                ) VALUES (?, ?, 'rejected', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_no,
                    source_site,
                    reason,
                    _json_dumps(request_snapshot),
                    _json_dumps(response),
                    unscored_before,
                    unscored_before,
                    limit,
                    timestamp,
                    timestamp,
                ),
            )
            for position, paper_no in enumerate(paper_nos):
                conn.execute(
                    "INSERT OR IGNORE INTO inbound_items(order_no, paper_no, position) VALUES (?, ?, ?)",
                    (order_no, paper_no, position),
                )
            conn.commit()
            return response

        if not paper_nos:
            return reject("empty_batch", "接收批次没有有效卷号")

        placeholders = ",".join("?" for _ in paper_nos)
        existing_paper_rows = conn.execute(
            f"SELECT paper_no FROM papers WHERE paper_no IN ({placeholders})", paper_nos
        ).fetchall()
        conflicts = [row["paper_no"] for row in existing_paper_rows]
        if conflicts:
            return reject("paper_already_exists", "接收点已有同号答卷，为避免覆盖而整批拒收", conflicts)

        if limit is not None and unscored_before + len(paper_nos) > limit:
            return reject(
                "quota_exceeded",
                (
                    f"接收点尚未交分卷数为 {unscored_before}，限额 {limit}；"
                    f"本批 {len(paper_nos)} 卷无法整批收下"
                ),
            )

        timestamp = now_iso()
        unscored_after = unscored_before + len(paper_nos)
        response = {
            "order_no": order_no,
            "accepted": True,
            "decision": "accepted",
            "status": "accepted",
            "reason": None,
            "message": f"整批 {len(paper_nos)} 卷已入账，可以开卷",
            "paper_nos": paper_nos,
            "unscored_count_before": unscored_before,
            "unscored_count_after": unscored_after,
            "max_unscored": limit,
            "remaining_quota": None if limit is None else limit - unscored_after,
            "idempotent_replay": False,
        }
        conn.execute(
            """
            INSERT INTO inbound_orders (
                order_no, source_site, decision, reason,
                request_json, response_json,
                unscored_count_before, unscored_count_after,
                quota_limit, created_at, decided_at
            ) VALUES (?, ?, 'accepted', NULL, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_no,
                source_site,
                _json_dumps(request_snapshot),
                _json_dumps(response),
                unscored_before,
                unscored_after,
                limit,
                timestamp,
                timestamp,
            ),
        )

        for position, paper_no in enumerate(paper_nos):
            body = bodies.get(paper_no)
            if not body:
                body = (
                    f"接收点入账卷 {paper_no}\n"
                    "HTTP 交接未包含正文；卷号和调卷记录已入账。"
                )
            conn.execute(
                """
                INSERT INTO papers (
                    paper_no, status, body, score, score_comment, grader,
                    source_site, transfer_order_no, opened_at, scored_at,
                    transferred_at, created_at, updated_at
                ) VALUES (?, 'unopened', ?, NULL, NULL, NULL, ?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (paper_no, body, source_site, order_no, timestamp, timestamp),
            )
            conn.execute(
                "INSERT INTO inbound_items(order_no, paper_no, position) VALUES (?, ?, ?)",
                (order_no, paper_no, position),
            )

        conn.commit()
        return response
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_inbound_order(settings: Settings, order_no: str) -> dict[str, Any]:
    conn = connect(settings.database_path)
    try:
        row = conn.execute("SELECT * FROM inbound_orders WHERE order_no = ?", (order_no,)).fetchone()
        if row is None:
            raise ApiError(404, "inbound_order_not_found", f"接收点没有单号 {order_no} 的记录")
        response = _json_loads(row["response_json"], {})
        response["stored_decision"] = row["decision"]
        response["source_site"] = row["source_site"]
        response["created_at"] = row["created_at"]
        response["decided_at"] = row["decided_at"]
        return response
    finally:
        conn.close()
