"""Pull/push pack + fired state between the public runner and the private pack repo.

Never prints secret values. Uses GH_PAT.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(os.getenv("RRR_PACK_DIR") or Path.cwd())
BRAND_OWNER = "reallyraisedrough"
OWNER = (os.getenv("RRR_PACK_OWNER") or BRAND_OWNER).strip() or BRAND_OWNER
# Never let an old IamMrZam override redirect cloud state to the wrong account.
if OWNER.lower() != BRAND_OWNER:
    OWNER = BRAND_OWNER
PACK_REPO = os.getenv("RRR_PACK_REPO") or "rrr-always-on"
BRANCH = os.getenv("RRR_PACK_BRANCH") or "main"
FILES = (
    "pack.json",
    "fired.json",
    "printify_drop_state.json",
    "voice_settings.json",
    "unlimited_post_queue.json",
)


def _pull_video_library(pack: dict) -> list[dict]:
    """Materialize checked-in video assets for the public runner.

    The private pack contains only metadata.  GitHub's Contents endpoint does
    not return base64 for files over 1 MiB, so fetch each blob by SHA instead
    of relying on a raw URL that would not authenticate a private repository.
    """
    library = pack.get("video_library") if isinstance(pack, dict) else None
    files = library.get("files") if isinstance(library, dict) else []
    if not isinstance(files, list):
        return []
    destination = ROOT / "media" / "videos"
    destination.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        filename = Path(str(item.get("filename") or "").strip()).name
        if not filename or filename != str(item.get("filename") or "").strip():
            results.append({"file": filename or "?", "ok": False, "error": "unsafe filename"})
            continue
        target = destination / filename
        expected = str(item.get("sha256") or "").strip().lower()
        if target.is_file() and expected:
            try:
                digest = hashlib.sha256(target.read_bytes()).hexdigest().lower()
                if digest == expected:
                    results.append({"file": filename, "ok": True, "unchanged": True})
                    continue
            except OSError:
                pass
        rel = f"media/videos/{filename}"
        meta = _req("GET", f"/repos/{OWNER}/{PACK_REPO}/contents/{urllib.parse.quote(rel, safe='/')}?ref={BRANCH}")
        sha = str(meta.get("sha") or "").strip()
        if not sha:
            results.append({"file": filename, "ok": False, "error": meta.get("error") or "missing metadata"})
            continue
        blob = _req("GET", f"/repos/{OWNER}/{PACK_REPO}/git/blobs/{sha}")
        encoded = blob.get("content") if isinstance(blob, dict) else None
        if not encoded:
            results.append({"file": filename, "ok": False, "error": blob.get("error") or "missing blob"})
            continue
        try:
            raw = base64.b64decode(str(encoded), validate=False)
            target.write_bytes(raw)
            results.append({"file": filename, "ok": target.is_file(), "bytes": len(raw)})
        except Exception as exc:  # noqa: BLE001
            results.append({"file": filename, "ok": False, "error": str(exc)[:120]})
    return results


def _token() -> str:
    for key in ("GH_PAT", "RRR_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return ""


def _req(method: str, path: str, body: dict | None = None) -> dict:
    token = _token()
    if not token:
        return {"ok": False, "error": "no GH_PAT"}
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "rrr-cloud-sync",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"https://api.github.com{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip().startswith("{") else {"ok": True}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        return {"ok": False, "error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}


def pull() -> dict:
    ROOT.mkdir(parents=True, exist_ok=True)
    out = []
    for name in FILES:
        payload = _req("GET", f"/repos/{OWNER}/{PACK_REPO}/contents/{name}?ref={BRANCH}")
        if not payload.get("content"):
            out.append({"file": name, "ok": False, "error": payload.get("error") or "missing"})
            continue
        raw = base64.b64decode(payload["content"])
        (ROOT / name).write_bytes(raw)
        out.append({"file": name, "ok": True, "bytes": len(raw)})
    # The public runner checks out a separate repository.  Pull the private
    # pack's video blobs into its local media/videos directory so scheduled
    # posts use the same Desktop library and shuffle-without-repeat state.
    media = []
    try:
        pack = json.loads((ROOT / "pack.json").read_text(encoding="utf-8"))
        media = _pull_video_library(pack)
    except Exception as exc:  # noqa: BLE001
        media = [{"ok": False, "error": str(exc)[:120]}]
    if media:
        out.append({"file": "media/videos", "ok": all(item.get("ok") for item in media), "files": media})
    summary = {"ok": any(r["ok"] for r in out if r["file"] in {"pack.json", "fired.json"}), "files": out}
    print(json.dumps(summary, indent=2))
    return summary


def push() -> dict:
    out = []
    for name in FILES:
        path = ROOT / name
        if not path.is_file():
            continue
        body_text = path.read_text(encoding="utf-8", errors="replace")
        last = {"ok": False, "error": "no attempt"}
        for attempt in range(4):
            existing = _req("GET", f"/repos/{OWNER}/{PACK_REPO}/contents/{name}?ref={BRANCH}")
            put = {
                "message": f"runner sync {name}",
                "content": base64.b64encode(body_text.encode("utf-8")).decode("ascii"),
                "branch": BRANCH,
            }
            if existing.get("sha"):
                put["sha"] = existing["sha"]
            last = _req("PUT", f"/repos/{OWNER}/{PACK_REPO}/contents/{name}", put)
            if last.get("content") or last.get("commit") or not last.get("error"):
                out.append({"file": name, "ok": True})
                last = {"ok": True}
                break
            if "409" not in str(last.get("error") or ""):
                out.append({"file": name, "ok": False, "error": last.get("error")})
                break
            time.sleep(0.7 * (attempt + 1))
        else:
            out.append({"file": name, "ok": False, "error": last.get("error")})
    summary = {"ok": all(r.get("ok") for r in out) if out else False, "files": out}
    print(json.dumps(summary, indent=2))
    return summary


def main() -> int:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "pull").strip().lower()
    if cmd == "push":
        return 0 if push().get("ok") else 1
    return 0 if pull().get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
