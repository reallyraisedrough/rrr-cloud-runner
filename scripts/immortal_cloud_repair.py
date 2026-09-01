"""Cloud-safe OAuth / token / secret repair.

Runs on GitHub Actions (stdlib + urllib). Never prints secret values.
Refreshes YouTube, TikTok, Snapchat, and Meta when grants are present.
If GH_PAT is set, writes rotated tokens back to Actions secrets so the next
30-minute tick stays immortal with the laptop off.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAPH = "https://graph.facebook.com/v21.0"


def _env(key: str) -> str:
    return (os.getenv(key) or "").strip()


def _form(url: str, fields: dict, timeout: int = 40) -> dict:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip().startswith("{") else {"raw": raw[:200]}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")[:300]
        try:
            return json.loads(raw)
        except Exception:
            return {"error": raw or str(exc)}
    except Exception as exc:
        return {"error": str(exc)[:200]}


def _get(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip().startswith("{") else {"raw": raw[:200]}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")[:300]
        try:
            return json.loads(raw)
        except Exception:
            return {"error": raw or str(exc)}
    except Exception as exc:
        return {"error": str(exc)[:200]}


def _ok(detail: str) -> dict:
    return {"ok": True, "detail": detail[:180]}


def _fail(detail: str) -> dict:
    return {"ok": False, "detail": detail[:180]}


def repair_youtube() -> dict:
    cid, secret, refresh = _env("YOUTUBE_CLIENT_ID"), _env("YOUTUBE_CLIENT_SECRET"), _env("YOUTUBE_REFRESH_TOKEN")
    if not (cid and secret and refresh):
        return _fail("not configured")
    payload = _form(
        "https://oauth2.googleapis.com/token",
        {
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        },
    )
    if payload.get("access_token"):
        new_refresh = str(payload.get("refresh_token") or "").strip()
        out = _ok("access token live")
        if new_refresh and new_refresh != refresh:
            out["secrets"] = {"YOUTUBE_REFRESH_TOKEN": new_refresh}
        return out
    err = str(payload.get("error") or payload.get("error_description") or payload)[:160]
    return _fail(err)


def repair_tiktok() -> dict:
    key, secret, refresh = _env("TIKTOK_CLIENT_KEY"), _env("TIKTOK_CLIENT_SECRET"), _env("TIKTOK_REFRESH_TOKEN")
    if not (key and secret and refresh):
        if _env("TIKTOK_ACCESS_TOKEN"):
            return _ok("access token present")
        return _fail("not configured")
    payload = _form(
        "https://open.tiktokapis.com/v2/oauth/token/",
        {
            "client_key": key,
            "client_secret": secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        },
    )
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    token = str((data or {}).get("access_token") or payload.get("access_token") or "")
    if not token:
        return _fail(str(payload.get("error") or payload)[:160])
    secrets: dict[str, str] = {"TIKTOK_ACCESS_TOKEN": token}
    new_refresh = str((data or {}).get("refresh_token") or payload.get("refresh_token") or "").strip()
    open_id = str((data or {}).get("open_id") or payload.get("open_id") or "").strip()
    if new_refresh:
        secrets["TIKTOK_REFRESH_TOKEN"] = new_refresh
    if open_id:
        secrets["TIKTOK_OPEN_ID"] = open_id
    return {"ok": True, "detail": "access token live — auto-refresh on", "secrets": secrets}


def repair_snapchat() -> dict:
    cid, secret, refresh = _env("SNAPCHAT_CLIENT_ID"), _env("SNAPCHAT_CLIENT_SECRET"), _env("SNAPCHAT_REFRESH_TOKEN")
    if not (cid and secret and refresh):
        return _fail("not configured")
    payload = _form(
        "https://accounts.snapchat.com/login/oauth2/access_token",
        {
            "refresh_token": refresh,
            "client_id": cid,
            "client_secret": secret,
            "grant_type": "refresh_token",
        },
    )
    token = str(payload.get("access_token") or "")
    if not token:
        return _fail(str(payload.get("error") or payload)[:160])
    secrets: dict[str, str] = {"SNAPCHAT_ACCESS_TOKEN": token}
    new_refresh = str(payload.get("refresh_token") or "").strip()
    if new_refresh:
        secrets["SNAPCHAT_REFRESH_TOKEN"] = new_refresh
    return {"ok": True, "detail": "access token live", "secrets": secrets}


def repair_meta() -> dict:
    app_id, app_secret, user_seed = _env("META_APP_ID"), _env("META_APP_SECRET"), _env("META_LONG_LIVED_USER_TOKEN")
    page_token, page_id = _env("META_PAGE_ACCESS_TOKEN"), _env("META_PAGE_ID")
    if page_token and page_id:
        probe = _get(f"{GRAPH}/{page_id}?fields=id,name&access_token={urllib.parse.quote(page_token)}")
        if probe.get("id"):
            return _ok(str(probe.get("name") or "page live"))
    if not (app_id and app_secret and user_seed):
        return _fail("page token stale and no app seed to refresh")
    exchanged = _get(
        f"{GRAPH}/oauth/access_token?"
        + urllib.parse.urlencode(
            {
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": user_seed,
            }
        )
    )
    long_user = str(exchanged.get("access_token") or "")
    if not long_user:
        return _fail(str(exchanged.get("error") or exchanged)[:160])
    secrets: dict[str, str] = {"META_LONG_LIVED_USER_TOKEN": long_user}
    if page_id:
        page = _get(f"{GRAPH}/{page_id}?fields=access_token&access_token={urllib.parse.quote(long_user)}")
        new_page = str(page.get("access_token") or "")
        if new_page:
            secrets["META_PAGE_ACCESS_TOKEN"] = new_page
    return {"ok": True, "detail": "Meta long-lived renewed", "secrets": secrets}


def repair_x() -> dict:
    import hashlib

    key, secret, token, token_secret = (
        _env("X_API_KEY"),
        _env("X_API_SECRET"),
        _env("X_ACCESS_TOKEN"),
        _env("X_ACCESS_TOKEN_SECRET"),
    )
    if not (key and secret and token and token_secret):
        return _fail("OAuth 1.0a not configured")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:10]
    return _ok(f"OAuth 1.0a seeds present sha={digest} lens={len(key)}/{len(secret)}/{len(token)}/{len(token_secret)}")


def repair_telegram() -> dict:
    token = _env("TELEGRAM_BOT_TOKEN")
    if not token:
        return _fail("not configured")
    payload = _get(f"https://api.telegram.org/bot{token}/getMe")
    if payload.get("ok"):
        uname = ((payload.get("result") or {}) if isinstance(payload.get("result"), dict) else {}).get("username")
        return _ok(str(uname or "bot live"))
    return _fail(str(payload.get("description") or payload)[:160])


def repair_instagram() -> dict:
    token, ig = _env("META_PAGE_ACCESS_TOKEN"), _env("META_INSTAGRAM_ACCOUNT_ID")
    if not token or not ig:
        return _fail("not configured")
    probe = _get(
        f"{GRAPH}/{urllib.parse.quote(ig)}?fields=id,username&access_token={urllib.parse.quote(token)}"
    )
    if probe.get("id"):
        return _ok(str(probe.get("username") or "instagram live"))
    err = probe.get("error")
    if isinstance(err, dict):
        return _fail(str(err.get("message") or err)[:160])
    return _fail(str(probe)[:160])


def repair_printify() -> dict:
    token = _env("PRINTIFY_API_TOKEN")
    if not token:
        return _fail("not configured")
    shop_id = _env("PRINTIFY_SHOP_ID")
    req = urllib.request.Request(
        "https://api.printify.com/v1/shops.json",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "ReallyRaisedRough-cloud/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw) if raw.strip().startswith("[") or raw.strip().startswith("{") else {}
        shops = data if isinstance(data, list) else data.get("data") or []
        if not shops:
            return _fail("token live but no shops")
        if shop_id:
            match = next((s for s in shops if str(s.get("id") or "") == shop_id), None)
            if not match:
                return _fail("shop id not in this Printify account")
            title = str(match.get("title") or shop_id)[:60]
            return _ok(f"{title} ({len(shops)} shop(s))")
        return _fail("token live but PRINTIFY_SHOP_ID empty")
    except Exception as exc:
        return _fail(str(exc)[:160])


def _gh_pat() -> str:
    return _env("GH_PAT") or _env("RRR_GITHUB_TOKEN") or _env("GH_TOKEN")


def _set_secret(name: str, value: str) -> dict:
    if not value:
        return {"ok": False, "error": "empty"}
    repo = _env("GITHUB_REPOSITORY") or "reallyraisedrough/rrr-always-on"
    pat = _gh_pat()
    if not pat:
        return {"ok": False, "error": "no GH_PAT — secret not written"}
    gh = _which_gh()
    if not gh:
        return {"ok": False, "error": "gh CLI missing"}
    env = os.environ.copy()
    env["GH_TOKEN"] = pat
    try:
        proc = subprocess.run(
            [gh, "secret", "set", name, "--repo", repo],
            input=value.encode("utf-8"),
            capture_output=True,
            timeout=60,
            env=env,
            check=False,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:120]}
    if proc.returncode == 0:
        return {"ok": True}
    err = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")[:160]
    return {"ok": False, "error": err or f"exit {proc.returncode}"}


def _which_gh() -> str:
    for cand in ("gh", "gh.exe"):
        try:
            proc = subprocess.run([cand, "--version"], capture_output=True, timeout=10, check=False)
            if proc.returncode == 0:
                return cand
        except Exception:
            continue
    return ""


REPAIRERS = (
    ("youtube", repair_youtube),
    ("tiktok", repair_tiktok),
    ("snapchat", repair_snapchat),
    ("facebook", repair_meta),
    ("instagram", repair_instagram),
    ("x", repair_x),
    ("telegram", repair_telegram),
    ("printify", repair_printify),
)


def run() -> dict:
    platforms = []
    secret_updates: dict[str, str] = {}
    for name, fn in REPAIRERS:
        result = fn()
        row = {"platform": name, "ok": bool(result.get("ok")), "detail": result.get("detail") or ""}
        secrets = result.get("secrets") if isinstance(result.get("secrets"), dict) else {}
        if secrets:
            row["rotated"] = sorted(secrets.keys())
            for key, val in secrets.items():
                if val and val != _env(key):
                    secret_updates[key] = val
        platforms.append(row)
    written = []
    errors = []
    for key, val in secret_updates.items():
        put = _set_secret(key, val)
        if put.get("ok"):
            written.append(key)
        else:
            errors.append(f"{key}: {put.get('error')}")
    summary = {
        "ok": all(p["ok"] or p["detail"] == "not configured" for p in platforms),
        "laptop_required": False,
        "platforms": platforms,
        "secrets_written": written,
        "secret_errors": errors,
    }
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:200]}))
        sys.exit(1)
