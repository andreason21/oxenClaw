"""Approval registry with optional persistence + identity binding.

A request blocks on an `asyncio.Future` until one of:
- someone calls `resolve(...)` with approved=True/False
- `cancel(...)` fires
- the optional `timeout` expires → result with status TIMED_OUT

When `state_path` is provided, the *snapshot* of currently-pending requests is
written to disk after every change. The futures themselves cannot survive a
restart, but the persistent snapshot lets a new process know the request
existed (so operators can audit) and the new process treats them as
TIMED_OUT on startup.

When `approver_token` is set, `resolve()` requires the caller to present
the same token. This blocks unauthorized approvals from any client that
merely holds the gateway bearer token — approving is a privileged action
distinct from connecting.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sampyclaw.approvals.models import (
    ApprovalRequest,
    ApprovalResult,
    ApprovalStatus,
)
from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("approvals.manager")


EventEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]


class ApprovalAuthError(PermissionError):
    """Raised when a resolve/cancel call lacks the required approver token."""


def _resolve_approver_token(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    return os.environ.get("SAMPYCLAW_APPROVER_TOKEN") or None


class ApprovalManager:
    def __init__(
        self,
        *,
        on_event: EventEmitter | None = None,
        state_path: Path | None = None,
        approver_token: str | None = None,
    ) -> None:
        self._pending: dict[str, tuple[ApprovalRequest, asyncio.Future[ApprovalResult]]] = {}
        self._on_event = on_event
        self._state_path = state_path
        self._approver_token = _resolve_approver_token(approver_token)
        if self._approver_token is None:
            logger.warning(
                "ApprovalManager has no approver token — anyone with gateway "
                "access can approve. Set SAMPYCLAW_APPROVER_TOKEN to lock down."
            )
        # Resurrect any prior snapshot as TIMED_OUT for audit trail purposes.
        self._on_startup_load()

    # ── persistence ──

    def _on_startup_load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("approval state at %s is corrupt; ignoring", self._state_path)
            return
        items = raw.get("pending", []) if isinstance(raw, dict) else []
        if not items:
            return
        # Don't restore live futures — those died with the prior process. Just
        # log so operators know agents holding these IDs will see TIMED_OUT.
        for snap in items:
            try:
                req = ApprovalRequest.model_validate(snap)
            except Exception:
                continue
            logger.info("approval %s carried over from previous run — will be TIMED_OUT", req.id)
        # Wipe the file; we no longer have anything live to track.
        with contextlib.suppress(OSError):
            self._state_path.unlink()

    def _persist(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            data = {"pending": [req.model_dump() for req, _ in self._pending.values()]}
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except OSError:
            logger.exception("failed to persist approvals snapshot")

    # ── auth ──

    def _require_approver(self, offered_token: str | None) -> None:
        if self._approver_token is None:
            return
        if offered_token is None or not hmac.compare_digest(offered_token, self._approver_token):
            raise ApprovalAuthError("invalid or missing approver token")

    # ── public API ──

    async def request(
        self,
        prompt: str,
        *,
        context: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> ApprovalResult:
        req = ApprovalRequest(prompt=prompt, context=context or {})
        fut: asyncio.Future[ApprovalResult] = asyncio.get_running_loop().create_future()
        self._pending[req.id] = (req, fut)
        self._persist()

        if self._on_event is not None:
            try:
                await self._on_event("approval.requested", req.model_dump())
            except Exception:
                logger.exception("approval event emit failed")

        try:
            if timeout is None:
                return await fut
            try:
                return await asyncio.wait_for(fut, timeout=timeout)
            except TimeoutError:
                return ApprovalResult(id=req.id, status=ApprovalStatus.TIMED_OUT)
        finally:
            self._pending.pop(req.id, None)
            self._persist()
            if self._on_event is not None:
                try:
                    await self._on_event("approval.closed", {"id": req.id})
                except Exception:
                    logger.exception("approval close event emit failed")

    def list(self) -> list[ApprovalRequest]:
        return [req for req, _ in self._pending.values()]

    def get(self, request_id: str) -> ApprovalRequest | None:
        entry = self._pending.get(request_id)
        return entry[0] if entry else None

    def resolve(
        self,
        request_id: str,
        *,
        approved: bool,
        reason: str | None = None,
        approver_token: str | None = None,
    ) -> ApprovalResult | None:
        self._require_approver(approver_token)
        entry = self._pending.get(request_id)
        if entry is None:
            return None
        _, fut = entry
        if fut.done():
            return None
        status = ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED
        result = ApprovalResult(id=request_id, status=status, reason=reason)
        fut.set_result(result)
        logger.info(
            "approval %s resolved status=%s reason=%s",
            request_id,
            status.value,
            reason or "",
        )
        return result

    def cancel(
        self,
        request_id: str,
        *,
        reason: str | None = None,
        approver_token: str | None = None,
    ) -> bool:
        # Cancel from operator surfaces is the same trust boundary as resolve.
        # Internal `cancel_all(shutdown)` bypasses this by passing the token
        # we already hold.
        self._require_approver(approver_token)
        entry = self._pending.get(request_id)
        if entry is None:
            return False
        _, fut = entry
        if fut.done():
            return False
        fut.set_result(
            ApprovalResult(id=request_id, status=ApprovalStatus.CANCELLED, reason=reason)
        )
        return True

    def cancel_all(self, *, reason: str | None = "shutdown") -> int:
        # Internal callers bypass the token check.
        count = 0
        for request_id in list(self._pending):
            entry = self._pending.get(request_id)
            if entry is None:
                continue
            _, fut = entry
            if fut.done():
                continue
            fut.set_result(
                ApprovalResult(id=request_id, status=ApprovalStatus.CANCELLED, reason=reason)
            )
            count += 1
        return count

    def __len__(self) -> int:
        return len(self._pending)
