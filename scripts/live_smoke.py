#!/usr/bin/env python3
"""Real two-process HTTP recovery test.

This script starts two uvicorn services with separate SQLite files.  It tests:

* opening papers interleaved with a transfer;
* origin/receiver score submission while a transfer is active;
* quota rejection, restoration, and receiver idempotency;
* a receiver commit whose only response receipt is destroyed by a fault proxy;
* restarting the origin, retrying by order number, and preventing duplicate
  receiver entries or reopening transferred papers.

It is intentionally standalone so it can run in a container/CI job with the
project requirements installed:

    python scripts/live_smoke.py
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]


def wait_health(base_url: str, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(f"{base_url}/health", timeout=1.0, trust_env=False)
            if response.status_code == 200:
                return
        except Exception as exc:  # startup/connection race
            last = exc
        time.sleep(0.25)
    raise RuntimeError(f"service at {base_url} did not become healthy: {last!r}")


def start_service(name: str, port: int, db_path: Path, extra_env: dict[str, str]) -> subprocess.Popen:
    log_path = db_path.with_suffix(".log")
    log = log_path.open("wb")
    env = os.environ.copy()
    env.update(
        {
            "SITE_ID": name,
            "DATABASE_PATH": str(db_path),
            "RECOVERY_ENABLED": "true",
            "RECOVERY_INTERVAL_SECONDS": "1",
            "HTTP_TIMEOUT": "3",
            "PYTHONUNBUFFERED": "1",
        }
    )
    env.update(extra_env)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_health(f"http://127.0.0.1:{port}")
    except Exception:
        process.terminate()
        try:
            out = Path(log_path).read_text(errors="replace")[-4000:]
        except Exception:
            out = ""
        raise RuntimeError(f"{name} failed to start; log tail:\n{out}")
    return process


def stop_service(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def restart_service(
    old: subprocess.Popen,
    name: str,
    port: int,
    db_path: Path,
    extra_env: dict[str, str],
) -> subprocess.Popen:
    stop_service(old)
    return start_service(name, port, db_path, extra_env)


class ReceiptLossProxy:
    """Forward one POST to B after B commits, then destroy the receipt."""

    def __init__(self, target_port: int, port: int = 0) -> None:
        self.target_port = target_port
        self.port = port
        self.failures_left = 1
        self.forwarded = 0
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                return

            def _handle(self) -> None:
                length = int(self.headers.get("content-length", "0"))
                body = self.rfile.read(length) if length else b""
                should_fail = False
                if self.command == "POST" and self.path == "/internal/transfers":
                    with outer._lock:
                        outer.forwarded += 1
                        if outer.failures_left > 0:
                            outer.failures_left -= 1
                            should_fail = True

                upstream = http.client.HTTPConnection("127.0.0.1", outer.target_port, timeout=10)
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in {"host", "connection", "keep-alive", "transfer-encoding"}
                }
                headers["Host"] = f"127.0.0.1:{outer.target_port}"
                headers["Connection"] = "close"
                upstream.request(self.command, self.path, body=body, headers=headers)
                response = upstream.getresponse()
                data = response.read()
                status = response.status
                response_headers = response.getheaders()
                upstream.close()

                if should_fail:
                    # At this point the receiver's HTTP response exists only in
                    # this proxy.  Close without returning it to the origin.
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return

                self.send_response(status)
                for key, value in response_headers:
                    if key.lower() in {"connection", "keep-alive", "transfer-encoding", "content-length"}:
                        continue
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = _handle
            do_POST = _handle

        self._Handler = Handler

    def __enter__(self) -> "ReceiptLossProxy":
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), self._Handler)
        actual_port = self._server.server_address[1]
        self.port = actual_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


def assert_status(response: httpx.Response, expected: int, context: str) -> dict[str, Any]:
    if response.status_code != expected:
        raise AssertionError(
            f"{context}: expected HTTP {expected}, got {response.status_code}: {response.text}"
        )
    if response.content:
        return response.json()
    return {}


def expect_conflict(response: httpx.Response, context: str) -> dict[str, Any]:
    return assert_status(response, 409, context)


def run_interleaving_and_rejection(base_a: str, base_b: str) -> list[str]:
    checks: list[str] = []

    # Open A-001 before the transfer is受理; it must stay scorable at A.
    r = httpx.post(f"{base_a}/papers/A-001/open", timeout=5, trust_env=False)
    data = assert_status(r, 200, "open A-001 before transfer")
    assert "匿名答卷 A-001" in data["body"]
    checks.append("开卷 A-001 并取得正文")

    # A-002/A-003 are unopened. B starts with one unscored paper and quota 3,
    # so this two-paper candidate batch fits exactly.
    payload = {
        "order_no": "TR-INTERLEAVE-001",
        "paper_nos": ["A-001", "A-002", "A-003"],
        "target_base_url": base_b,
        "target_site": "point-b",
    }
    r = httpx.post(f"{base_a}/transfers", json=payload, timeout=8, trust_env=False)
    data = assert_status(r, 200, "interleaving transfer")
    assert data["decision"] == "accepted", data
    assert data["transferred_paper_nos"] == ["A-002", "A-003"], data
    assert data["kept_at_origin_paper_nos"] == ["A-001"], data
    checks.append("调卷时 A-001 已开卷留下，A-002/A-003 转走")

    # The kept opened paper can be scored at the origin.
    r = httpx.post(f"{base_a}/papers/A-001/score", json={"score": 88}, timeout=5, trust_env=False)
    data = assert_status(r, 200, "score kept A-001")
    assert data["status"] == "scored" and data["score"] == 88
    checks.append("已开卷留下的 A-001 在原点交分")

    # Transferred papers cannot be opened at the origin.
    r = httpx.post(f"{base_a}/papers/A-002/open", timeout=5, trust_env=False)
    expect_conflict(r, "origin open transferred A-002")
    checks.append("原点不能再开已转走的 A-002")

    # They can be opened and scored at B, and the body traveled over HTTP.
    r = httpx.post(f"{base_b}/papers/A-002/open", timeout=5, trust_env=False)
    data = assert_status(r, 200, "receiver open A-002")
    assert "匿名答卷 A-002" in data["body"]
    r = httpx.post(f"{base_b}/papers/A-002/score", json={"score": 92}, timeout=5, trust_env=False)
    assert_status(r, 200, "receiver score A-002")
    checks.append("接收点开 A-002 取到交接正文并交分")

    # Other papers at both points remain independently usable.
    r = httpx.post(f"{base_a}/papers/A-004/open", timeout=5, trust_env=False)
    assert_status(r, 200, "other A paper opens")
    r = httpx.post(f"{base_a}/papers/A-004/score", json={"score": 75}, timeout=5, trust_env=False)
    assert_status(r, 200, "other A paper scores")
    r = httpx.post(f"{base_b}/papers/B-001/open", timeout=5, trust_env=False)
    assert_status(r, 200, "other B paper opens")
    r = httpx.post(f"{base_b}/papers/B-001/score", json={"score": 81}, timeout=5, trust_env=False)
    assert_status(r, 200, "other B paper scores")
    checks.append("两点其他卷仍可照常开卷、交分")

    # B currently has two unscored papers: B-002 and A-003.  With quota 4,
    # a three-paper batch would make five unscored papers and is rejected.
    r = httpx.get(f"{base_b}/capacity", timeout=5, trust_env=False)
    cap = assert_status(r, 200, "B capacity")
    assert cap["unscored_count"] == 2, cap
    assert cap["remaining_quota"] == 2, cap

    reject_payload = {
        "order_no": "TR-REJECT-001",
        "paper_nos": ["A-005", "A-006", "A-007"],
        "target_base_url": base_b,
    }
    r = httpx.post(f"{base_a}/transfers", json=reject_payload, timeout=8, trust_env=False)
    data = assert_status(r, 200, "quota rejection")
    assert data["decision"] == "rejected", data
    assert data["transferred_paper_nos"] == [], data
    assert set(data["kept_at_origin_paper_nos"]) == {"A-005", "A-006", "A-007"}, data
    assert data["receiver_response"]["reason"] == "quota_exceeded"
    checks.append("超限额批次被明确拒收，结果列为留在原点")

    # Rejected candidates regain opening eligibility.
    r = httpx.post(f"{base_a}/papers/A-005/open", timeout=5, trust_env=False)
    assert_status(r, 200, "A-005 restored and opens")
    checks.append("拒收后 A-005 恢复原点开卷资格")

    # Retrying the same order returns the recorded rejection even after quota
    # is freed by scoring A-003 at B.
    r = httpx.post(f"{base_b}/papers/A-003/score", json={"score": 70}, timeout=5, trust_env=False)
    assert_status(r, 200, "free B quota by scoring A-003")
    r = httpx.post(f"{base_a}/transfers", json=reject_payload, timeout=8, trust_env=False)
    data = assert_status(r, 200, "retry rejected order at origin")
    assert data["decision"] == "rejected", data

    # A direct receiver retry must explicitly report the idempotent replay.
    receiver_retry_payload = {
        "order_no": "TR-REJECT-001",
        "source_site": "point-a",
        "paper_nos": ["A-005", "A-006", "A-007"],
    }
    r = httpx.post(f"{base_b}/internal/transfers", json=receiver_retry_payload, timeout=8, trust_env=False)
    receiver_retry = assert_status(r, 200, "retry rejected order at receiver")
    assert receiver_retry["decision"] == "rejected", receiver_retry
    assert receiver_retry["idempotent_replay"] is True, receiver_retry

    r = httpx.get(f"{base_b}/internal/transfers/TR-REJECT-001", timeout=5, trust_env=False)
    stored = assert_status(r, 200, "get stored rejection")
    assert stored["decision"] == "rejected"
    checks.append("原单号重试查回首次拒收，不因后来有空额而改变")

    # A new order can use newly available quota. A-005 is now open and thus
    # stays; A-006 and A-007 are unopened and fit after A-003 was scored.
    r = httpx.post(
        f"{base_a}/transfers",
        json={"order_no": "TR-ACCEPT-NEW-001", "paper_nos": ["A-005", "A-006", "A-007"], "target_base_url": base_b},
        timeout=8,
        trust_env=False,
    )
    data = assert_status(r, 200, "new order after quota freed")
    assert data["kept_at_origin_paper_nos"] == ["A-005"], data
    assert data["transferred_paper_nos"] == ["A-006", "A-007"], data
    checks.append("新单号在空额出现后可正常整批接收，已开卷 A-005 留下")

    return checks


def run_lost_receipt_restart(
    port_a: int,
    port_b: int,
    base_a_direct: str,
    base_b: str,
    proxy_url: str,
    db_a: Path,
    a_env: dict[str, str],
    restart_env: dict[str, str],
) -> tuple[list[str], subprocess.Popen]:
    checks: list[str] = []
    process_a = start_service("point-a-recovery", port_a, db_a, a_env)

    payload = {
        "order_no": "TR-LOST-RECEIPT-001",
        "paper_nos": ["R-001", "R-002"],
        "target_base_url": proxy_url,
        "target_site": "point-b-recovery",
    }
    # These paper numbers need to exist in this separate A database. It was
    # seeded with prefix R; aliases above are symbolic in the returned check.
    payload["paper_nos"] = ["R-001", "R-002"]

    r = httpx.post(f"{base_a_direct}/transfers", json=payload, timeout=8, trust_env=False)
    if r.status_code not in {502, 503, 504}:
        raise AssertionError(f"expected uncertain receipt error, got {r.status_code}: {r.text}")
    err = r.json()["error"]
    assert err["code"] == "transfer_receipt_uncertain", err
    checks.append("首次交接回执被故障代理吞掉，原点返回‘结果未知’")

    # While uncertain, origin durably pauses opening.
    r = httpx.post(f"{base_a_direct}/papers/R-001/open", timeout=5, trust_env=False)
    expect_conflict(r, "open paused R-001 after receipt loss")
    order = err["details"]["order"]
    assert order["status"] == "pending_receiver", order
    assert order["paused_paper_nos"] == ["R-001", "R-002"], order
    checks.append("回执丢失后待转卷在原点持续暂停开卷")

    # The receiver nevertheless durably accepted exactly one batch.
    r = httpx.get(f"{base_b}/internal/transfers/TR-LOST-RECEIPT-001", timeout=5, trust_env=False)
    stored = assert_status(r, 200, "receiver stored accepted despite lost receipt")
    assert stored["decision"] == "accepted", stored
    r = httpx.get(f"{base_b}/papers?status=unopened", timeout=5, trust_env=False)
    papers = assert_status(r, 200, "B papers after lost receipt")["papers"]
    recovery_papers = [p for p in papers if p["paper_no"] in {"R-001", "R-002"}]
    assert len(recovery_papers) == 2, papers
    checks.append("核对接收点：回执虽丢失，接收决定和两卷已原子入账")

    # Restart origin.  Restart configuration enables the background outbox
    # reconciler, proving recovery state comes from SQLite and can proceed
    # automatically using the original order number.
    process_a = restart_service(process_a, "point-a-recovery", port_a, db_a, restart_env)
    deadline = time.time() + 10
    data: dict[str, Any] = {}
    while time.time() < deadline:
        r = httpx.get(f"{base_a_direct}/transfers/TR-LOST-RECEIPT-001", timeout=5, trust_env=False)
        data = assert_status(r, 200, "poll order after origin restart")
        if data["status"] == "accepted":
            break
        time.sleep(0.4)
    assert data.get("status") == "accepted", data
    assert data["transferred_paper_nos"] == ["R-001", "R-002"], data
    assert data["receiver_response"]["idempotent_replay"] is True, data
    checks.append("原点重启后后台凭原单号自动对账，查回接收点原接收结果")

    # An explicit operator retry is also safe and returns the terminal result.
    r = httpx.post(
        f"{base_a_direct}/transfers/TR-LOST-RECEIPT-001/retry",
        timeout=8,
        trust_env=False,
    )
    data = assert_status(r, 200, "explicit retry after automatic recovery")
    assert data["decision"] == "accepted", data
    checks.append("再次用原单号显式重试，原点返回同一最终结果")

    # Direct retry against receiver must not insert duplicates.
    r = httpx.post(f"{base_b}/internal/transfers", json=payload | {"target_base_url": None}, timeout=8, trust_env=False)
    data = assert_status(r, 200, "direct receiver idempotent retry")
    assert data["decision"] == "accepted" and data["idempotent_replay"] is True, data
    r = httpx.get(f"{base_b}/papers?limit=1000", timeout=5, trust_env=False)
    all_b = assert_status(r, 200, "all B papers")["papers"]
    r_numbers = [p["paper_no"] for p in all_b if p["paper_no"] in {"R-001", "R-002"}]
    assert sorted(r_numbers) == ["R-001", "R-002"], r_numbers
    assert len(all_b) == 2, f"separate recovery B DB should contain exactly two papers: {all_b}"
    checks.append("接收点按单号幂等，重复请求未重复入账")

    # Origin now marks both transferred and still forbids opening them.
    for paper_no in ["R-001", "R-002"]:
        r = httpx.post(f"{base_a_direct}/papers/{paper_no}/open", timeout=5, trust_env=False)
        expect_conflict(r, f"open transferred {paper_no} after recovery")
    checks.append("恢复完成后原点不会再开已转走的卷")

    return checks, process_a


def main() -> int:
    temp_root = Path(tempfile.mkdtemp(prefix="grading-live-"))
    print(f"临时 SQLite/日志目录: {temp_root}")
    processes: list[subprocess.Popen] = []
    proxy: ReceiptLossProxy | None = None
    try:
        # Scenario 1: normal two points and quota rejection.
        db_a = temp_root / "a.db"
        db_b = temp_root / "b.db"
        port_a, port_b = 18081, 18082
        a_env = {"SEED_PREFIX": "A", "SEED_COUNT": "8", "MAX_UNSCORED": "30"}
        b_env = {"SEED_PREFIX": "B", "SEED_COUNT": "2", "MAX_UNSCORED": "4"}
        process_a = start_service("point-a", port_a, db_a, a_env)
        process_b = start_service("point-b", port_b, db_b, b_env)
        processes.extend([process_a, process_b])
        base_a = f"http://127.0.0.1:{port_a}"
        base_b = f"http://127.0.0.1:{port_b}"

        checks1 = run_interleaving_and_rejection(base_a, base_b)

        # Independently restart B and verify SQLite-backed idempotent record.
        process_b = restart_service(process_b, "point-b", port_b, db_b, b_env)
        processes.append(process_b)
        r = httpx.get(f"{base_b}/internal/transfers/TR-REJECT-001", timeout=5, trust_env=False)
        assert_status(r, 200, "B order survives restart")
        checks1.append("接收点独立重启后，SQLite 中的拒收结果仍可查回")

        print("\n[1/2] 开卷/调卷交错与拒收实测通过：")
        for item in checks1:
            print(f"  ✓ {item}")

        # Scenario 2: separate DBs/ports and a receipt-destroying proxy.
        db_a2 = temp_root / "a-recovery.db"
        db_b2 = temp_root / "b-recovery.db"
        port_a2, port_b2 = 18084, 18085
        a2_env = {
            "SEED_PREFIX": "R",
            "SEED_COUNT": "2",
            "MAX_UNSCORED": "10",
            "RECOVERY_ENABLED": "false",
        }
        a2_restart_env = {
            **a2_env,
            "RECOVERY_ENABLED": "true",
            "RECOVERY_INTERVAL_SECONDS": "0.5",
        }
        b2_env = {"SEED_PREFIX": "S", "SEED_COUNT": "0", "MAX_UNSCORED": "10"}
        process_b2 = start_service("point-b-recovery", port_b2, db_b2, b2_env)
        processes.append(process_b2)

        with ReceiptLossProxy(target_port=port_b2) as fault_proxy:
            proxy = fault_proxy
            proxy_url = f"http://127.0.0.1:{fault_proxy.port}"
            checks2, process_a2 = run_lost_receipt_restart(
                port_a2,
                port_b2,
                f"http://127.0.0.1:{port_a2}",
                f"http://127.0.0.1:{port_b2}",
                proxy_url,
                db_a2,
                a2_env,
                a2_restart_env,
            )
            processes.append(process_a2)
        proxy = None

        # Restart receiver too and confirm no duplication after both sides restart.
        process_b2 = restart_service(process_b2, "point-b-recovery", port_b2, db_b2, b2_env)
        processes.append(process_b2)
        r = httpx.get(f"http://127.0.0.1:{port_b2}/papers?limit=1000", timeout=5, trust_env=False)
        papers = assert_status(r, 200, "B2 papers after restart")["papers"]
        assert len(papers) == 2, papers
        checks2.append("接收点重启后仍只有两卷，接收记录持久化")

        print("\n[2/2] 丢回执 + 原点重启恢复实测通过：")
        for item in checks2:
            print(f"  ✓ {item}")

        print("\nLIVE_SMOKE_OK")
        print(f"数据库保留在: {temp_root}")
        return 0
    finally:
        if proxy is not None:
            proxy.__exit__(None, None, None)
        for process in processes:
            stop_service(process)
        # Keep temp dir on failure? On success it is harmless in /tmp; remove
        # only when CLEANUP_TEMP=1 to leave evidence by default.
        if os.getenv("CLEANUP_TEMP") == "1":
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\nLIVE_SMOKE_FAILED: {exc}", file=sys.stderr)
        raise
