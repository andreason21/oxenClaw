"""Soak test for oxenClaw — long-running stability validation.

Drives the gateway via WS RPCs in a loop while sampling resource usage
(memory / file descriptors / thread count). Exits non-zero if growth
crosses a configured threshold or if any RPC fails.

Usage::

    python scripts/soak.py --duration 3600 --rps 5

Reports a summary at the end and writes a CSV trace to ./soak.csv.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import resource
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from oxenclaw.config.paths import OxenclawPaths  # noqa: E402
from oxenclaw.gateway import (  # noqa: E402
    ChatSendParams,
    ChatSendResult,
    GatewayServer,
    Router,
)


@dataclass
class Sample:
    t: float
    rss_kb: int
    fd_count: int
    thread_count: int
    rpc_total: int
    rpc_errors: int


@dataclass
class SoakResult:
    samples: list[Sample] = field(default_factory=list)
    rpc_total: int = 0
    rpc_errors: int = 0
    duration_seconds: float = 0.0
    started_at: float = 0.0

    def memory_growth_kb(self) -> int:
        if len(self.samples) < 2:
            return 0
        return self.samples[-1].rss_kb - self.samples[0].rss_kb

    def fd_growth(self) -> int:
        if len(self.samples) < 2:
            return 0
        return self.samples[-1].fd_count - self.samples[0].fd_count


def _read_proc_status() -> tuple[int, int]:
    """Return `(rss_kb, thread_count)` from /proc/self/status. Linux-only."""
    rss_kb = 0
    threads = 0
    status = Path("/proc/self/status")
    if not status.exists():
        # Fall back to resource.getrusage if /proc isn't available.
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # ru_maxrss is in kB on Linux, bytes on macOS — soak test is
        # primarily Linux.
        return usage.ru_maxrss, 1
    for line in status.read_text(encoding="utf-8").splitlines():
        if line.startswith("VmRSS:"):
            rss_kb = int(line.split()[1])
        elif line.startswith("Threads:"):
            threads = int(line.split()[1])
    return rss_kb, threads


def _count_fds() -> int:
    """Count open file descriptors. Returns -1 on platforms without /proc
    (Windows native, WSL1) — soak runs primarily on Linux/WSL2."""
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.exists():
        return -1
    try:
        return sum(1 for _ in fd_dir.iterdir())
    except OSError:
        return -1


def _pick_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _build_router() -> Router:
    r = Router()

    @r.method("chat.send", ChatSendParams)
    async def _send(p: ChatSendParams) -> ChatSendResult:
        return ChatSendResult(message_id=f"{p.chat_id}:sent", timestamp=time.time())

    return r


async def _client_loop(
    url: str,
    deadline: float,
    rps: int,
    counter: dict[str, int],
) -> None:
    period = 1.0 / max(1, rps)
    async with websockets.connect(url) as ws:
        rpc_id = 0
        while time.monotonic() < deadline:
            rpc_id += 1
            payload = {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "chat.send",
                "params": {
                    "channel": "soak",
                    "account_id": "main",
                    "chat_id": str(rpc_id),
                    "text": f"soak-{rpc_id}",
                },
            }
            try:
                await ws.send(json.dumps(payload))
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                msg = json.loads(raw)
                if "error" in msg:
                    counter["errors"] += 1
                else:
                    counter["ok"] += 1
            except Exception:
                counter["errors"] += 1
            await asyncio.sleep(period)


async def run_soak(
    *,
    duration_seconds: float,
    rps: int,
    sample_interval_seconds: float,
    csv_path: Path,
) -> SoakResult:
    server = GatewayServer(_build_router(), shutdown_drain_seconds=2.0)
    port = _pick_port()
    server_task = asyncio.create_task(
        server.serve(host="127.0.0.1", port=port)
    )
    await asyncio.sleep(0.2)

    result = SoakResult(started_at=time.time())
    counter = {"ok": 0, "errors": 0}
    deadline = time.monotonic() + duration_seconds

    client_task = asyncio.create_task(
        _client_loop(f"ws://127.0.0.1:{port}", deadline, rps, counter)
    )

    csv_path.write_text("t,rss_kb,fd_count,thread_count,rpc_total,rpc_errors\n")
    try:
        while time.monotonic() < deadline:
            rss_kb, threads = _read_proc_status()
            fd = _count_fds()
            sample = Sample(
                t=time.time(),
                rss_kb=rss_kb,
                fd_count=fd,
                thread_count=threads,
                rpc_total=counter["ok"] + counter["errors"],
                rpc_errors=counter["errors"],
            )
            result.samples.append(sample)
            with csv_path.open("a") as fh:
                fh.write(
                    f"{sample.t},{sample.rss_kb},{sample.fd_count},"
                    f"{sample.thread_count},{sample.rpc_total},"
                    f"{sample.rpc_errors}\n"
                )
            await asyncio.sleep(sample_interval_seconds)
    finally:
        client_task.cancel()
        try:
            await client_task
        except (asyncio.CancelledError, Exception):
            pass
        server.request_shutdown()
        try:
            await asyncio.wait_for(server_task, timeout=5.0)
        except (asyncio.CancelledError, Exception):
            pass

    result.rpc_total = counter["ok"] + counter["errors"]
    result.rpc_errors = counter["errors"]
    result.duration_seconds = time.time() - result.started_at
    return result


def _summary_line(result: SoakResult) -> str:
    if not result.samples:
        return "(no samples collected)"
    first = result.samples[0]
    last = result.samples[-1]
    return (
        f"duration={result.duration_seconds:.1f}s  "
        f"rpc_total={result.rpc_total}  "
        f"rpc_errors={result.rpc_errors}  "
        f"rss start={first.rss_kb} end={last.rss_kb} "
        f"(Δ {result.memory_growth_kb():+d} kB)  "
        f"fd start={first.fd_count} end={last.fd_count} "
        f"(Δ {result.fd_growth():+d})  "
        f"threads end={last.thread_count}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration", type=float, default=60.0, help="seconds to run"
    )
    parser.add_argument(
        "--rps", type=int, default=5, help="requests per second"
    )
    parser.add_argument(
        "--sample-interval",
        type=float,
        default=5.0,
        help="seconds between resource samples",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("soak.csv"),
        help="resource trace output",
    )
    parser.add_argument(
        "--max-rss-growth-kb",
        type=int,
        default=51200,
        help="fail if RSS grows by more than this many kB (default 50 MiB)",
    )
    parser.add_argument(
        "--max-fd-growth",
        type=int,
        default=20,
        help="fail if FD count grows by more than this many (default 20)",
    )
    parser.add_argument(
        "--max-error-rate",
        type=float,
        default=0.0,
        help="fail if RPC error rate exceeds this fraction (default 0.0)",
    )
    args = parser.parse_args()

    result = asyncio.run(
        run_soak(
            duration_seconds=args.duration,
            rps=args.rps,
            sample_interval_seconds=args.sample_interval,
            csv_path=args.csv,
        )
    )

    print(_summary_line(result))
    print(f"trace written to {args.csv}")

    failures: list[str] = []
    if result.memory_growth_kb() > args.max_rss_growth_kb:
        failures.append(
            f"RSS growth {result.memory_growth_kb()} kB exceeds "
            f"--max-rss-growth-kb {args.max_rss_growth_kb}"
        )
    if result.fd_growth() > args.max_fd_growth:
        failures.append(
            f"FD growth {result.fd_growth()} exceeds "
            f"--max-fd-growth {args.max_fd_growth}"
        )
    if result.rpc_total > 0:
        rate = result.rpc_errors / result.rpc_total
        if rate > args.max_error_rate:
            failures.append(
                f"RPC error rate {rate:.4f} exceeds "
                f"--max-error-rate {args.max_error_rate}"
            )

    if failures:
        for msg in failures:
            print(f"FAIL: {msg}", file=sys.stderr)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
