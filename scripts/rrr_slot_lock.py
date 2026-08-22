"""Shared claim lock so two cloud hosts never double-post the same slot.

A host writes status=pending before posting. The other host skips that key
until it succeeds, fails (retry), or the claim goes stale.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

HOST_ID = (os.getenv("RRR_HOST_ID") or "cloud").strip() or "cloud"
CLAIM_TTL_SEC = int(os.getenv("RRR_CLAIM_TTL_SEC") or "1500")  # 25 min


def _parse_at(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_success(entry: Any) -> bool:
    if isinstance(entry, dict):
        result = str(entry.get("result") or entry.get("status") or "")
    else:
        result = str(entry or "")
    text = result.strip().lower()
    return text in {"ok", "success"} or text.startswith("ok:") or text.startswith("posted")


def is_held(entry: Any, *, now: datetime | None = None) -> bool:
    if not isinstance(entry, dict):
        return is_success(entry)
    if is_success(entry):
        return True
    if str(entry.get("status") or "").lower() != "pending":
        return False
    then = _parse_at(str(entry.get("at") or ""))
    if then is None:
        return False
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - then).total_seconds() < CLAIM_TTL_SEC


def claim(fired: dict, key: str, *, now_iso: str) -> bool:
    existing = fired.get(key)
    if is_held(existing):
        return False
    fired[key] = {
        "status": "pending",
        "host": HOST_ID,
        "at": now_iso,
    }
    return True


def finish(fired: dict, key: str, result: str, *, now_iso: str, extra: dict | None = None) -> None:
    row = {
        "status": "ok" if is_success(result) else "retry",
        "result": str(result)[:400],
        "host": HOST_ID,
        "at": now_iso,
    }
    if extra:
        row.update(extra)
    if row["status"] == "retry":
        # Drop the hold so the other host can finish this slot.
        fired[key] = row
        return
    fired[key] = row
