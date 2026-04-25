"""End-to-end smoke test for the memory subsystem against a real Ollama.

Exercises every P0 feature path with real embeddings so regressions in any
layer (chunker / walker / embedding cache / sqlite-vec / FTS5 / inbox /
retriever / CLI / gateway JSON-RPC) surface immediately.

Requires Ollama at http://127.0.0.1:11434 with `nomic-embed-text` pulled.
Run: `python scripts/smoke_memory.py` from the project root.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from websockets.asyncio.client import connect as ws_connect

from sampyclaw.config.paths import SampyclawPaths
from sampyclaw.memory import (
    MemoryRetriever,
    MemoryStore,
    OpenAIEmbeddings,
)
from sampyclaw.memory.embedding_cache import EmbeddingCache

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")

FIXTURES = {
    "preferences.md": """# Drink preferences

The user prefers green tea in the morning and coffee after lunch.

# Food preferences

The user avoids dairy. Favourite dish is spicy tofu stew.
""",
    "projects/sampyclaw.md": """# sampyClaw project

Python port of the openclaw TypeScript monorepo. Phase B pilot ports the
Telegram extension end-to-end.

## Build system

Uses hatch with uv for install speed. Python 3.11+ only.
""",
    "notes/daily-2026-04-25.md": """# Daily log 2026-04-25

Finished the chunk-of-file memory rewrite. 485 tests passing. Need to
validate against real Ollama before moving on to P1.
""",
}


def _passed(label: str, detail: str = "") -> None:
    print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))


def _failed(label: str, detail: str) -> None:
    print(f"  FAIL  {label}  -- {detail}")


def _section(name: str) -> None:
    print(f"\n== {name} ==")


def _write_fixtures(memory_dir: Path) -> None:
    for rel, body in FIXTURES.items():
        dest = memory_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")


def _build_retriever(home: Path) -> MemoryRetriever:
    paths = SampyclawPaths(home=home)
    paths.ensure_home()
    (home / "memory").mkdir(parents=True, exist_ok=True)
    embeddings = OpenAIEmbeddings(base_url=OLLAMA_BASE_URL, model=EMBED_MODEL)
    store = MemoryStore(home / "memory" / "index.sqlite")
    cache = EmbeddingCache(provider=embeddings, store=store)
    return MemoryRetriever(
        store=store,
        embeddings_cache=cache,
        memory_dir=home / "memory",
        inbox_path=home / "memory" / "inbox.md",
    )


async def scenario_1_initial_sync(retriever: MemoryRetriever) -> bool:
    report = await retriever.sync()
    ok_files = report.added == 3 and report.changed == 0 and report.deleted == 0
    chunks_ok = retriever.store.count_chunks() >= 3
    if ok_files and chunks_ok:
        _passed(
            "initial sync indexes all fixtures",
            f"added={report.added} chunks={report.chunks_embedded}",
        )
        return True
    _failed(
        "initial sync",
        f"added={report.added} changed={report.changed} deleted={report.deleted} "
        f"chunks_total={retriever.store.count_chunks()}",
    )
    return False


async def scenario_2_idempotent_resync(retriever: MemoryRetriever) -> bool:
    r = await retriever.sync()
    if r.added == 0 and r.changed == 0 and r.deleted == 0 and r.chunks_embedded == 0:
        _passed("re-sync is a no-op when nothing changes")
        return True
    _failed("idempotent resync", f"report={r}")
    return False


async def scenario_3_semantic_recall(retriever: MemoryRetriever) -> bool:
    hits = await retriever.search(query="what does the user drink?", k=3)
    if not hits:
        _failed("semantic recall", "no hits")
        return False
    top = hits[0]
    citation_ok = top.chunk.path == "preferences.md"
    text_ok = "tea" in top.chunk.text.lower() or "coffee" in top.chunk.text.lower()
    if citation_ok and text_ok:
        _passed(
            "semantic recall surfaces the right chunk",
            f"citation={top.citation} score={top.score:.3f}",
        )
        return True
    _failed(
        "semantic recall",
        f"top={top.chunk.path}:{top.chunk.start_line}-{top.chunk.end_line} "
        f"score={top.score:.3f}",
    )
    return False


async def scenario_4_save_roundtrip(retriever: MemoryRetriever) -> bool:
    fact = "The user is planning a trip to Jeju Island in June 2026."
    await retriever.save(fact, tags=["travel", "plans"])
    inbox = retriever.inbox_path
    if not inbox.exists():
        _failed("save appended inbox.md", "inbox.md missing")
        return False
    body = inbox.read_text(encoding="utf-8")
    if fact not in body or "travel" not in body:
        _failed("inbox content", "body missing fact or tag")
        return False
    hits = await retriever.search(query="where is the user travelling", k=3)
    found = any("jeju" in h.chunk.text.lower() for h in hits)
    if found:
        _passed("memory_save -> search round-trip recalls the fact")
        return True
    _failed(
        "save search round-trip",
        "top hits: " + ", ".join(f"{h.chunk.path}:{h.chunk.start_line}" for h in hits),
    )
    return False


async def scenario_5_embedding_cache_hits(retriever: MemoryRetriever) -> bool:
    # `clear_all` wipes files/chunks/vec but keeps embedding_cache. A
    # re-sync against unchanged source files should re-embed nothing —
    # every chunk's text still matches a cache row keyed on its hash.
    chunks_before = retriever.store.count_chunks()
    retriever.store.clear_all()
    r = await retriever.sync()
    expected_hits = chunks_before  # same corpus → same chunk texts
    if (
        r.cache_hits == expected_hits
        and r.chunks_embedded == 0
        and retriever.store.count_chunks() == chunks_before
    ):
        _passed(
            "embedding_cache serves every chunk after clear_all",
            f"cache_hits={r.cache_hits} embedded={r.chunks_embedded}",
        )
        return True
    _failed(
        "embedding cache hits",
        f"expected hits={expected_hits} got hits={r.cache_hits} "
        f"embedded={r.chunks_embedded} chunks_after={retriever.store.count_chunks()}",
    )
    return False


async def scenario_6_incremental_change(retriever: MemoryRetriever) -> bool:
    f = retriever.memory_dir / "projects/sampyclaw.md"
    original = f.read_text(encoding="utf-8")
    f.write_text(
        original + "\n## Status\n\nP0 memory rewrite shipped 2026-04-25.\n",
        encoding="utf-8",
    )
    r = await retriever.sync()
    f.write_text(original, encoding="utf-8")
    await retriever.sync()  # restore
    if r.changed == 1 and r.added == 0 and r.deleted == 0:
        _passed("modifying one file reindexes only that file", f"changed={r.changed}")
        return True
    _failed(
        "incremental change",
        f"added={r.added} changed={r.changed} deleted={r.deleted}",
    )
    return False


async def scenario_7_deletion(retriever: MemoryRetriever) -> bool:
    doomed = retriever.memory_dir / "notes/daily-2026-04-25.md"
    body = doomed.read_text(encoding="utf-8")
    doomed.unlink()
    r = await retriever.sync()
    if r.deleted == 1:
        _passed("deleted file is removed from the index")
        doomed.write_text(body, encoding="utf-8")  # restore
        await retriever.sync()
        return True
    _failed("deletion", f"deleted={r.deleted} (expected 1)")
    doomed.write_text(body, encoding="utf-8")
    await retriever.sync()
    return False


async def scenario_8_rebuild(retriever: MemoryRetriever) -> bool:
    # After rebuild, semantic search should still return the right chunk.
    hits = await retriever.search(query="what does the user drink?", k=3)
    if hits and hits[0].chunk.path == "preferences.md":
        _passed(
            "post-rebuild semantic search still works",
            f"top={hits[0].citation} score={hits[0].score:.3f}",
        )
        return True
    _failed(
        "post-rebuild search",
        "top=" + (hits[0].citation if hits else "(empty)"),
    )
    return False


def scenario_9_cli(home: Path) -> bool:
    env = os.environ.copy()
    env["SAMPYCLAW_HOME"] = str(home)
    # `sampyclaw memory stats` should succeed and print a total_chunks line.
    res = subprocess.run(
        [sys.executable, "-m", "sampyclaw.cli", "memory", "stats"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if res.returncode != 0:
        _failed("cli stats", f"exit={res.returncode} stderr={res.stderr[:200]}")
        return False
    if "chunks" not in res.stdout.lower() and "files" not in res.stdout.lower():
        _failed("cli stats output shape", res.stdout[:200])
        return False
    # `sampyclaw memory search "..."` should print at least one line.
    res2 = subprocess.run(
        [
            sys.executable, "-m", "sampyclaw.cli", "memory", "search",
            "what does the user drink", "-k", "2",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if res2.returncode != 0 or not res2.stdout.strip():
        _failed("cli search", f"exit={res2.returncode} stdout={res2.stdout[:200]}")
        return False
    _passed("cli memory stats + search run end-to-end")
    return True


async def scenario_10_gateway_jsonrpc(home: Path) -> bool:
    env = os.environ.copy()
    env["SAMPYCLAW_HOME"] = str(home)
    port = "47331"
    proc = subprocess.Popen(
        [sys.executable, "-m", "sampyclaw.cli", "gateway", "start", "--port", port],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        uri = f"ws://127.0.0.1:{port}/"
        deadline = time.monotonic() + 15.0
        last_err = ""
        while time.monotonic() < deadline:
            try:
                async with ws_connect(uri, open_timeout=3) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "memory.stats",
                                "params": {},
                            }
                        )
                    )
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    payload = json.loads(raw)
                    if "result" in payload:
                        result = payload["result"]
                        if "total_chunks" in result or "total_files" in result:
                            _passed(
                                "gateway memory.stats returns shape",
                                f"files={result.get('total_files')} "
                                f"chunks={result.get('total_chunks')}",
                            )
                            return True
                        _failed("gateway result shape", str(result)[:200])
                        return False
                    _failed("gateway error", str(payload)[:200])
                    return False
            except (OSError, ConnectionError) as exc:
                last_err = str(exc)
                await asyncio.sleep(0.5)
        _failed("gateway start", f"did not respond: {last_err}")
        return False
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


async def main() -> int:
    _section("smoke setup")
    with tempfile.TemporaryDirectory(prefix="sampyclaw-smoke-") as tmp:
        home = Path(tmp)
        _write_fixtures(home / "memory")
        print(f"  corpus at {home}/memory  ({len(FIXTURES)} fixture files)")
        print(f"  ollama   {OLLAMA_BASE_URL}  model={EMBED_MODEL}")

        retriever = _build_retriever(home)
        try:
            _section("sync / recall / save")
            results = [
                await scenario_1_initial_sync(retriever),
                await scenario_2_idempotent_resync(retriever),
                await scenario_3_semantic_recall(retriever),
                await scenario_4_save_roundtrip(retriever),
                await scenario_5_embedding_cache_hits(retriever),
                await scenario_6_incremental_change(retriever),
                await scenario_7_deletion(retriever),
                await scenario_8_rebuild(retriever),
            ]
        finally:
            await retriever.aclose()
            retriever.store.close()

        _section("CLI")
        results.append(scenario_9_cli(home))

        _section("gateway JSON-RPC")
        results.append(await scenario_10_gateway_jsonrpc(home))

    _section("summary")
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"  {passed}/{total} scenarios passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
