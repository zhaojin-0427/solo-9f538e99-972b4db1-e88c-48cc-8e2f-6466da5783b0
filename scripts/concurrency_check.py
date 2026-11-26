#!/usr/bin/env python3
"""Check open-vs-transfer race invariants with real uvicorn processes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def wait_health(url: str) -> None:
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=1, trust_env=False).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"not healthy: {url}")


def start(port: int, db: Path, site: str, prefix: str, count: int, quota: int) -> subprocess.Popen:
    log = db.with_suffix(".log").open("wb")
    env = os.environ.copy()
    env.update(
        {
            "SITE_ID": site,
            "DATABASE_PATH": str(db),
            "SEED_PREFIX": prefix,
            "SEED_COUNT": str(count),
            "MAX_UNSCORED": str(quota),
            "RECOVERY_ENABLED": "false",
            "PYTHONUNBUFFERED": "1",
        }
    )
    p = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    wait_health(f"http://127.0.0.1:{port}")
    return p


def stop(p: subprocess.Popen) -> None:
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="grading-race-"))
    pa = pb = None
    try:
        pa = start(18091, tmp / "a.db", "race-a", "C", 24, 1000)
        pb = start(18092, tmp / "b.db", "race-b", "D", 0, 1000)
        a = "http://127.0.0.1:18091"
        b = "http://127.0.0.1:18092"

        def race_one(i: int) -> tuple[str, int, dict, int, dict]:
            paper = f"C-{i:03d}"
            order = f"RACE-{i:03d}"

            def do_open() -> tuple[int, dict]:
                r = httpx.post(f"{a}/papers/{paper}/open", timeout=10, trust_env=False)
                return r.status_code, (r.json() if r.content else {})

            def do_transfer() -> tuple[int, dict]:
                r = httpx.post(
                    f"{a}/transfers",
                    json={"order_no": order, "paper_nos": [paper], "target_base_url": b},
                    timeout=10,
                    trust_env=False,
                )
                return r.status_code, (r.json() if r.content else {})

            with ThreadPoolExecutor(max_workers=2) as pool:
                fo = pool.submit(do_open)
                ft = pool.submit(do_transfer)
                open_code, open_body = fo.result()
                transfer_code, transfer_body = ft.result()
            return paper, open_code, open_body, transfer_code, transfer_body

        outcomes = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(race_one, range(1, 25)))

        accepted = 0
        kept = 0
        for paper, oc, ob, tc, tb in outcomes:
            if oc == 200 and tc == 200 and tb.get("status") == "completed_locally":
                assert tb["kept_at_origin_paper_nos"] == [paper], tb
                kept += 1
            elif oc == 409 and tc == 200 and tb.get("decision") == "accepted":
                assert tb["transferred_paper_nos"] == [paper], tb
                accepted += 1
            else:
                raise AssertionError(f"inconsistent race outcome {paper}: open={oc} {ob}; transfer={tc} {tb}")

        r = httpx.get(f"{b}/papers?limit=1000", timeout=5, trust_env=False)
        b_papers = r.json()["papers"]
        assert len(b_papers) == accepted, (len(b_papers), accepted)

        r = httpx.get(f"{a}/papers?limit=1000", timeout=5, trust_env=False)
        a_papers = {p["paper_no"]: p["status"] for p in r.json()["papers"]}
        for paper, _oc, _ob, _tc, tb in outcomes:
            if tb.get("decision") == "accepted":
                assert a_papers[paper] == "transferred_out"
            else:
                assert a_papers[paper] == "open"

        print(f"RACE_OK: {kept} opened-and-kept, {accepted} transferred; no inconsistent outcome")
        return 0
    finally:
        if pa:
            stop(pa)
        if pb:
            stop(pb)


if __name__ == "__main__":
    raise SystemExit(main())
