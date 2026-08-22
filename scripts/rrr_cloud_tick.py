"""Laptop-off poster. Uses the RRR social_schedule.json slots.

Platforms: facebook, instagram, threads, youtube, x, telegram, tiktok, snapchat.
Post types: image, photo, post, text, reel, video, short.
Custom X = OAuth 1.0a from env / Desktop\\x keys.txt (free forever, no paid credits).
YouTube = refresh token + video upload.
Video slots: ffmpeg still-to-vertical MP4 when the laptop reel builder is offline.
Every caption includes ORDER NOW → size/color/address checkout.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
PACK_PATH = Path(os.getenv("RRR_PACK_PATH") or (ROOT / "pack.json"))
FIRED_PATH = Path(os.getenv("RRR_FIRED_PATH") or (ROOT / "fired.json"))
VOICE_PATH = Path(os.getenv("RRR_VOICE_PATH") or (ROOT / "voice_settings.json"))
GRAPH = "https://graph.facebook.com/v21.0"
VIDEO_GRAPH = "https://graph-video.facebook.com/v21.0"
STORE = "https://really-raised-rough.printify.me"
IDENTITY = "https://2.3.4.6.1.8:0219/"
TZ_NAME = os.getenv("RRR_TIMEZONE") or "America/Phoenix"
DRY = (os.getenv("RRR_DRY_RUN") or "").strip().lower() in ("1", "true", "yes", "on")
MAX_DUE = 12
_PRODUCT_RE = re.compile(r"/product/(\d+)(?:/([^/?#\s]+))?", re.I)
QUEUE_CATALOG = (
    ("facebook", "image"),
    ("facebook", "reel"),
    ("instagram", "image"),
    ("instagram", "reel"),
    ("threads", "image"),
    ("youtube", "short"),
    ("youtube", "video"),
    ("x", "image"),
    ("x", "video"),
    ("telegram", "image"),
)

PLATFORMS = (
    "facebook",
    "instagram",
    "threads",
    "youtube",
    "x",
    "telegram",
    "tiktok",
    "snapchat",
)
IMAGE_TYPES = ("image", "photo", "post", "text")
VIDEO_TYPES = ("reel", "video", "short")
HUMOR_STYLES = (
    "dark_humor",
    "prison_humor",
    "recovery_dark",
    "past_chaos",
    "enforcer",
    "wiseguy",
    "dry",
    "deadpan",
    "heartfelt",
    "sarcastic",
)
HUMOR_HOOKS = {
    "dark_humor": "Dark humor. Clean living. The joke ships.",
    "prison_humor": "Yard-comedy energy. Free-world fit.",
    "recovery_dark": "Still clean. Still sarcastic. Still loud.",
    "past_chaos": "We joke about the wreckage so we don't go back.",
    "enforcer": "Listen up. No soft merch. No lectures.",
    "wiseguy": "Real talk. This design already knows your excuses.",
    "dry": "Right then. Slightly rude. Properly raised rough.",
    "deadpan": "Deadpan merch for people who stayed. No kumbaya.",
    "heartfelt": "Honest merch for people who stayed. No kumbaya.",
    "sarcastic": "Congrats on surviving yourself. Here's a shirt about it.",
}


def _env(key: str) -> str:
    return (os.getenv(key) or "").strip()


def _load(path: Path, fallback):
    if not path.is_file():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def _save(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _clock():
    try:
        tz = ZoneInfo(TZ_NAME) if ZoneInfo else None
        now = datetime.now(tz) if tz else datetime.now()
    except Exception:
        now = datetime.now()
    return {
        "dow": now.weekday(),
        "hour": now.hour,
        "minute": now.minute,
        "date": now.strftime("%Y-%m-%d"),
        "iso": now.astimezone(timezone.utc).isoformat(),
    }


def _run_key(slot: dict, clock: dict) -> str:
    return f"{clock['date']}-{slot.get('time') or '00:00'}-{slot.get('id') or ''}"


def _success(result) -> bool:
    """Only a real post counts as fired — skipped/failed slots stay due."""
    if isinstance(result, dict):
        result = result.get("result") or ""
    text = str(result or "").strip().lower()
    return text == "ok" or text.startswith("ok:") or text.startswith("posted")


def _platform_ready(plat: str) -> bool:
    """TikTok/Snapchat wait until grants exist so they do not hog due slots."""
    plat = (plat or "").lower()
    if plat == "tiktok":
        return bool(_env("TIKTOK_REFRESH_TOKEN") or _env("TIKTOK_ACCESS_TOKEN"))
    if plat == "snapchat":
        return bool(_env("SNAPCHAT_REFRESH_TOKEN") or _env("SNAPCHAT_ACCESS_TOKEN"))
    return True


def due_slots(pack: dict, fired: dict, clock: dict) -> list[dict]:
    """Same rules as RRR is_slot_due — every platform and post type.

    Shared fired.json lets a second host pick up slots the first host left pending/failed.
    """
    from rrr_slot_lock import is_held, is_success

    data = pack.get("schedule") or {}
    if data.get("stopped_by_user") or data.get("master_enabled") is False:
        return []
    out = []
    for slot in data.get("slots") or []:
        if not slot or slot.get("enabled") is False:
            continue
        try:
            slot_dow = int(slot.get("day_of_week"))
        except (TypeError, ValueError):
            continue
        if slot_dow != int(clock["dow"]):
            continue
        hh, mm = str(slot.get("time") or "00:00").split(":")
        if clock["hour"] < int(hh) or (clock["hour"] == int(hh) and clock["minute"] < int(mm)):
            continue
        key = _run_key(slot, clock)
        entry = fired.get(key)
        if is_success(entry) or is_held(entry):
            continue
        if slot.get("last_fired_key") == key and _success(slot.get("last_result")):
            continue
        plat = str(slot.get("platform") or "").lower()
        if plat not in PLATFORMS:
            continue
        if not _platform_ready(plat):
            continue
        out.append(slot)
    return out[:MAX_DUE]


def queue_on(pack: dict) -> bool:
    """Nothing saved in Start Jarvis still means the unlimited queue is ON."""
    sched = pack.get("schedule") or {}
    if sched.get("stopped_by_user") or sched.get("master_enabled") is False:
        return False
    q = pack.get("queue")
    if not isinstance(q, dict):
        return True
    if q.get("enabled") is False:
        return False
    return True


def _new_queue_job(*, created: int = 0, design_every: int = 6) -> dict:
    plat, ptype = random.choice(QUEUE_CATALOG)
    video = ptype in VIDEO_TYPES
    kind = "design" if created and created % max(1, design_every) == 0 else "post"
    return {
        "id": hashlib.sha1(f"{time.time_ns()}-{random.random()}".encode()).hexdigest()[:12],
        "kind": kind,
        "platform": plat,
        "post_type": ptype,
        "accent": "random",
        "persona": "random",
        "style": "random",
        "duration": "random",
        "voiceover": "on" if video else "random",
        "batch": "unlimited-queue",
    }


def drain_pack_queue(pack: dict, *, n: int | None = None) -> list[dict]:
    """Refill random jobs when the queue is empty so posting never runs out."""
    if not queue_on(pack):
        return []
    q = pack.get("queue") if isinstance(pack.get("queue"), dict) else {}
    pending = [j for j in (q.get("pending") or []) if isinstance(j, dict)]
    try:
        max_n = max(1, min(12, int(n if n is not None else q.get("max_per_tick") or 3)))
    except (TypeError, ValueError):
        max_n = 3
    try:
        design_every = max(1, min(40, int(q.get("design_every") or 6)))
    except (TypeError, ValueError):
        design_every = 6
    created = int(q.get("created") or 0)
    while len(pending) < 24:
        created += 1
        pending.append(_new_queue_job(created=created, design_every=design_every))
    jobs = pending[:max_n]
    q["pending"] = pending[max_n:]
    q["created"] = created
    q["enabled"] = True
    q["controls"] = "Start Jarvis"
    pack["queue"] = q
    return jobs


def execute_queue_job(job: dict, pack: dict, work: Path, clock: dict) -> str:
    kind = str(job.get("kind") or "post").lower()
    if kind == "design":
        try:
            import rrr_cloud_printify

            drop = rrr_cloud_printify._drop_one(kind_mode="queue", new_art=True)
            if drop.get("ok"):
                try:
                    rrr_cloud_printify.sync_mockups_into_pack()
                except Exception:
                    pass
                return "ok:printify_design"
            return f"failed:{drop.get('error') or 'design'}"[:160]
        except Exception as exc:
            return f"failed:{exc}"[:160]
    slot = {
        "id": job.get("id") or "queue",
        "platform": job.get("platform") or "facebook",
        "post_type": job.get("post_type") or "image",
        "enabled": True,
        "accent": job.get("accent") or "random",
        "persona": job.get("persona") or "random",
        "style": job.get("style") or "random",
        "duration": job.get("duration") or "random",
        "voiceover": job.get("voiceover") or "on",
        "batch": "unlimited-queue",
    }
    plat = str(slot["platform"]).lower()
    if not _platform_ready(plat):
        return "skipped_platform_not_ready"
    return execute_slot(slot, pack, work, clock)


def checkout_url(raw: str) -> str:
    text = (raw or "").strip().replace("https:// /", f"{STORE}/").replace("http:// /", f"{STORE}/")
    match = _PRODUCT_RE.search(text)
    if match:
        slug = (match.group(2) or "").strip().strip("/")
        path = f"/product/{match.group(1)}/{slug}" if slug else f"/product/{match.group(1)}"
        return f"{STORE}{path}"
    return STORE


def _rotate_index(*parts, size: int) -> int:
    if size <= 0:
        return 0
    raw = "|".join(str(p or "") for p in parts)
    digest = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()
    return int(digest[:12], 16) % size


def humor_for(slot: dict, clock: dict) -> str:
    pinned = str(slot.get("persona") or slot.get("humor") or "").strip().lower()
    if pinned and pinned != "random" and pinned in HUMOR_STYLES:
        return pinned
    idx = _rotate_index(
        clock.get("date"),
        slot.get("id"),
        slot.get("platform"),
        slot.get("post_type"),
        size=len(HUMOR_STYLES),
    )
    return HUMOR_STYLES[idx]


def order_caption(product: dict, platform: str, *, humor: str = "") -> str:
    title = product.get("product_title") or product.get("title") or "Really Raised Rough"
    url = checkout_url(str(product.get("product_url") or ""))
    hook = HUMOR_HOOKS.get(humor, HUMOR_HOOKS["dark_humor"]) if humor else HUMOR_HOOKS["dark_humor"]
    cap = (
        f"{hook}\n"
        f"🛒 ORDER NOW — pick size, color, enter address, checkout:\n{url}\n"
        f"{title}\n"
        f"Follow: IG @reallyraisedrough · Threads @reallyraisedrough · "
        f"YT @ReallyRRough · X @RRough10304 · TG @ReallyRaisedRough\n"
        f"#reallyraisedrough #soberlife #recovery"
    )
    plat = (platform or "").lower()
    if plat == "x":
        return f"{hook}\n🛒 ORDER NOW:\n{url}"[:275]
    if plat == "threads":
        return cap[:500]
    if plat == "youtube":
        return (
            f"{title} | Really Raised Rough\n\n"
            f"{hook}\n"
            f"🛒 ORDER NOW — pick size, color, enter address, checkout:\n{url}\n"
            f"Full store: {STORE}\n\n"
            "Follow Really Raised Rough:\n"
            "Instagram https://www.instagram.com/reallyraisedrough\n"
            "Threads https://www.threads.net/@reallyraisedrough\n"
            "YouTube https://www.youtube.com/@ReallyRRough\n"
            "X https://x.com/RRough10304\n"
            "Telegram https://t.me/ReallyRaisedRough\n"
        )
    return cap[:1800]


def _pick(pack: dict, slot: dict | None = None, clock: dict | None = None) -> dict:
    posts = ((pack.get("pool") or {}).get("posts")) or []
    if not posts:
        return {
            "product_title": "Really Raised Rough",
            "product_url": STORE,
            "image_url": "",
        }
    if slot and clock:
        idx = _rotate_index(
            clock.get("date"),
            slot.get("id"),
            slot.get("platform"),
            slot.get("post_type"),
            size=len(posts),
        )
        return posts[idx]
    return random.choice(posts)


def _needs_video(slot: dict) -> bool:
    ptype = str(slot.get("post_type") or "image").lower()
    plat = str(slot.get("platform") or "").lower()
    if plat == "youtube":
        return True
    return ptype in VIDEO_TYPES


def _duration(slot: dict) -> int:
    raw = str(slot.get("duration") or "random")
    if raw.isdigit():
        return max(8, min(60, int(raw)))
    ptype = str(slot.get("post_type") or "image").lower()
    return 25 if ptype == "short" else 45


def _download(url: str, dest: Path) -> Path | None:
    if not url:
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "ReallyRaisedRough/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            dest.write_bytes(resp.read())
        return dest if dest.is_file() and dest.stat().st_size > 20 else None
    except Exception:
        return None


def _voice_cfg() -> dict:
    data = _load(VOICE_PATH, {})
    return data if isinstance(data, dict) else {}


_RANDOM_ACCENT_VOICES = (
    ("irish", "en-IE-ConnorNeural", "en-IE-EmilyNeural"),
    ("british", "en-GB-RyanNeural", "en-GB-SoniaNeural"),
    ("australian", "en-AU-WilliamNeural", "en-AU-NatashaNeural"),
    ("southern", "en-US-JasonNeural", "en-US-JennyNeural"),
    ("canadian", "en-CA-LiamNeural", "en-CA-ClaraNeural"),
    ("indian", "en-IN-PrabhatNeural", "en-IN-NeerjaNeural"),
    ("new_york", "en-US-GuyNeural", "en-US-AriaNeural"),
    ("kiwi", "en-NZ-MitchellNeural", "en-NZ-MollyNeural"),
)


def _edge_voice(cfg: dict, *, slot: dict | None = None, clock: dict | None = None) -> str:
    gender = str(cfg.get("gender") or "").lower()
    female = gender in ("female", "woman", "girl")
    idx = _rotate_index(
        (clock or {}).get("date"),
        (slot or {}).get("id"),
        (slot or {}).get("platform"),
        (slot or {}).get("post_type"),
        "accent",
        size=len(_RANDOM_ACCENT_VOICES),
    )
    _name, male_v, female_v = _RANDOM_ACCENT_VOICES[idx]
    cfg["accent"] = _name
    return female_v if female else male_v


def _humor_script(
    product: dict,
    cfg: dict,
    seconds: int,
    *,
    humor: str = "",
    slot: dict | None = None,
    clock: dict | None = None,
) -> str:
    title = str(product.get("product_title") or "this design")
    title = re.sub(r"\s*[|\-—].*$", "", title).strip() or "this design"
    persona = str(humor or cfg.get("personality") or cfg.get("humor") or "").lower()
    openers = (
        f"Listen. {title}.",
        f"Real talk. {title}.",
        f"Don't scroll. {title}.",
        f"This one's loud. {title}.",
        f"Still here. Still shopping. {title}.",
        f"Laugh first. {title}.",
        f"No lecture. {title}.",
        f"Yard energy. {title}.",
    )
    middles = (
        "Unique drop. Same store.",
        "Pick size and color.",
        "The mockup is the proof.",
        "Wear the joke. Skip the meeting.",
        "Checkout takes a minute.",
        "Attitude printed. Lectures not included.",
        "For people who stayed.",
        "Rough honesty. Clean living.",
    )
    ctas = (
        "Order now at reallyraisedrough.com",
        "Tap ORDER NOW. reallyraisedrough.com",
        "Shop the store. reallyraisedrough.com",
        "Size, color, address, checkout. reallyraisedrough.com",
        "Link in the post. reallyraisedrough.com",
    )
    idx = _rotate_index(
        (clock or {}).get("date"),
        (clock or {}).get("iso"),
        (slot or {}).get("id"),
        (slot or {}).get("platform"),
        (slot or {}).get("post_type"),
        persona,
        title,
        size=max(len(openers), len(middles), len(ctas)) * 17,
    )
    script = f"{openers[idx % len(openers)]} {middles[idx % len(middles)]} {ctas[idx % len(ctas)]}"
    if seconds <= 20:
        script = f"{openers[idx % len(openers)]} {ctas[idx % len(ctas)]}"
    return re.sub(r"\s+", " ", script).strip()


def _tts_mp3(text: str, dest: Path, cfg: dict, *, slot: dict | None = None, clock: dict | None = None) -> Path | None:
    voice = _edge_voice(cfg, slot=slot, clock=clock)
    rate = str(cfg.get("rate") or "+0%").strip() or "+0%"
    pitch = str(cfg.get("pitch") or "+0Hz").strip() or "+0Hz"
    if pitch.endswith("%") and "Hz" not in pitch:
        # Start Jarvis may export Hz; ignore bare percent leftover
        pitch = "+0Hz"
    cmd = [
        sys.executable,
        "-m",
        "edge_tts",
        "--voice",
        voice,
        "--rate",
        rate,
        "--pitch",
        pitch,
        "--text",
        text,
        "--write-media",
        str(dest),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=90)
    except Exception:
        return None
    return dest if dest.is_file() and dest.stat().st_size > 200 else None


def _make_video(image: Path, dest: Path, seconds: int, audio: Path | None = None) -> Path | None:
    vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,format=yuv420p"
    if audio and audio.is_file():
        cmd = [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(image),
            "-i",
            str(audio),
            "-shortest",
            "-t",
            str(seconds),
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-r",
            "30",
            "-movflags",
            "+faststart",
            str(dest),
        ]
    else:
        cmd = [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(image),
            "-t",
            str(seconds),
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-movflags",
            "+faststart",
            str(dest),
        ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except Exception:
        return None
    return dest if dest.is_file() and dest.stat().st_size > 200 else None


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


def _multipart(url: str, fields: dict[str, str], files: list[tuple[str, str, bytes, str]], timeout: int = 180, headers: dict | None = None) -> dict:
    boundary = "----RRRCloud" + hashlib.md5(str(time.time()).encode()).hexdigest()
    parts: list[bytes] = []
    for key, val in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{val}\r\n".encode()
        )
    for name, fname, data, ctype in files:
        parts.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                f"filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n"
            ).encode()
            + data
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    hdrs = {"Content-Type": f"multipart/form-data; boundary={boundary}", **(headers or {})}
    req = urllib.request.Request(url, data=body, method="POST", headers=hdrs)
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


def post_facebook_image(image: str, caption: str) -> str:
    token, page = _env("META_PAGE_ACCESS_TOKEN"), _env("META_PAGE_ID")
    if not token or not page or not image:
        return "skipped_no_facebook"
    payload = _form(f"{GRAPH}/{page}/photos", {"url": image, "caption": caption[:6000], "access_token": token})
    ident = payload.get("post_id") or payload.get("id")
    return f"ok:{ident}" if ident else f"failed:{str(payload)[:160]}"


def post_facebook_video(video: Path, caption: str, *, published: bool = True) -> str:
    token, page = _env("META_PAGE_ACCESS_TOKEN"), _env("META_PAGE_ID")
    if not token or not page or not video.is_file():
        return "skipped_no_facebook"
    payload = _multipart(
        f"{VIDEO_GRAPH}/{page}/videos",
        {
            "access_token": token,
            "published": "true" if published else "false",
            "description": caption[:6000],
        },
        [("source", video.name, video.read_bytes(), "video/mp4")],
        timeout=300,
    )
    ident = payload.get("id")
    return f"ok:{ident}" if ident else f"failed:{str(payload)[:160]}"


def post_instagram_image(image: str, caption: str) -> str:
    token, ig = _env("META_PAGE_ACCESS_TOKEN"), _env("META_INSTAGRAM_ACCOUNT_ID")
    if not token or not ig or not image:
        return "skipped_no_instagram"
    created = _form(f"{GRAPH}/{ig}/media", {"image_url": image, "caption": caption[:2200], "access_token": token})
    if not created.get("id"):
        return f"failed:{str(created)[:160]}"
    published = _form(f"{GRAPH}/{ig}/media_publish", {"creation_id": created["id"], "access_token": token})
    return f"ok:{published.get('id')}" if published.get("id") else f"failed:{str(published)[:160]}"


def post_instagram_reel(video: Path, caption: str) -> str:
    token, ig = _env("META_PAGE_ACCESS_TOKEN"), _env("META_INSTAGRAM_ACCOUNT_ID")
    if not token or not ig or not video.is_file():
        return "skipped_no_instagram"
    created = _form(
        f"{GRAPH}/{ig}/media",
        {
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption[:2200],
            "access_token": token,
        },
    )
    creation_id = created.get("id")
    if not creation_id:
        return f"failed:{str(created)[:160]}"
    data = video.read_bytes()
    req = urllib.request.Request(
        f"https://rupload.facebook.com/ig-api-upload/v21.0/{creation_id}",
        data=data,
        method="POST",
        headers={
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(len(data)),
            "Content-Type": "application/octet-stream",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return f"failed:{exc.code}:{exc.read().decode('utf-8', errors='replace')[:120]}"
    except Exception as exc:
        return f"failed:{exc}"[:160]
    published = _form(f"{GRAPH}/{ig}/media_publish", {"creation_id": creation_id, "access_token": token})
    return f"ok:{published.get('id')}" if published.get("id") else f"failed:{str(published)[:160]}"


def post_threads_image(image: str, caption: str) -> str:
    token, uid = _env("META_THREADS_ACCESS_TOKEN"), _env("META_THREADS_USER_ID")
    if not token or not uid:
        return "skipped_no_threads"
    fields = {"media_type": "IMAGE" if image else "TEXT", "text": caption[:500], "access_token": token}
    if image:
        fields["image_url"] = image
    created = _form(f"https://graph.threads.net/v1.0/{uid}/threads", fields)
    if not created.get("id"):
        return f"failed:{str(created)[:160]}"
    published = _form(
        f"https://graph.threads.net/v1.0/{uid}/threads_publish",
        {"creation_id": created["id"], "access_token": token},
    )
    return f"ok:{published.get('id')}" if published.get("id") else f"failed:{str(published)[:160]}"


def post_telegram_image(image: str, caption: str) -> str:
    token = _env("TELEGRAM_BOT_TOKEN")
    chats = [x for x in (_env("TELEGRAM_MARKETING_CHAT_IDS") or _env("TELEGRAM_CHAT_IDS")).replace(",", " ").split() if x]
    if not token or not chats:
        return "skipped_no_telegram"
    last = "failed"
    for chat in chats[:3]:
        if image:
            payload = _form(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                {"chat_id": chat, "photo": image, "caption": caption[:1000]},
            )
        else:
            payload = _form(
                f"https://api.telegram.org/bot{token}/sendMessage",
                {"chat_id": chat, "text": caption[:3500]},
            )
        last = "ok" if payload.get("ok") else f"failed:{str(payload)[:120]}"
    return last


def post_telegram_video(video: Path, caption: str) -> str:
    token = _env("TELEGRAM_BOT_TOKEN")
    chats = [x for x in (_env("TELEGRAM_MARKETING_CHAT_IDS") or _env("TELEGRAM_CHAT_IDS")).replace(",", " ").split() if x]
    if not token or not chats or not video.is_file():
        return "skipped_no_telegram"
    last = "failed"
    for chat in chats[:3]:
        payload = _multipart(
            f"https://api.telegram.org/bot{token}/sendVideo",
            {"chat_id": chat, "caption": caption[:1000]},
            [("video", video.name, video.read_bytes(), "video/mp4")],
        )
        last = "ok" if payload.get("ok") else f"failed:{str(payload)[:120]}"
    return last


def _x_keys() -> tuple[str, str, str, str]:
    key, secret, token, token_secret = (
        _env("X_API_KEY"),
        _env("X_API_SECRET"),
        _env("X_ACCESS_TOKEN"),
        _env("X_ACCESS_TOKEN_SECRET"),
    )
    if key and secret and token and token_secret:
        return key, secret, token, token_secret
    path = Path.home() / "Desktop" / "x keys.txt"
    if not path.is_file():
        return key, secret, token, token_secret
    found: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return key, secret, token, token_secret
    aliases = {
        "consumer_key": "key",
        "consumersecret": "secret",
        "consumer_secret": "secret",
        "access_token": "token",
        "accesstoken": "token",
        "access_token_secret": "token_secret",
        "accesstokensecret": "token_secret",
    }
    for raw in text.replace(",", "\n").splitlines():
        if "=" not in raw:
            continue
        name, _, val = raw.partition("=")
        name = "".join(ch for ch in name.lower() if ch.isalnum() or ch == "_")
        val = val.strip().strip("'\"").strip().rstrip(",")
        dest = aliases.get(name)
        if dest and val:
            found[dest] = val
    return (
        key or found.get("key", ""),
        secret or found.get("secret", ""),
        token or found.get("token", ""),
        token_secret or found.get("token_secret", ""),
    )


def _x_header(method: str, url: str, extra: dict | None = None) -> str:
    key, secret, token, token_secret = _x_keys()
    oauth = {
        "oauth_consumer_key": key,
        "oauth_nonce": hashlib.md5(f"{time.time()}{random.random()}".encode()).hexdigest(),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    sig_params = {**oauth, **(extra or {})}
    encoded = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted(sig_params.items())
    )
    base = "&".join(urllib.parse.quote(part, safe="") for part in (method.upper(), url, encoded))
    digest = hmac.new(f"{secret}&{token_secret}".encode(), base.encode(), hashlib.sha1).digest()
    oauth["oauth_signature"] = base64.b64encode(digest).decode()
    return "OAuth " + ", ".join(
        f'{urllib.parse.quote(k, safe="")}="{urllib.parse.quote(str(v), safe="")}"'
        for k, v in sorted(oauth.items())
    )


def _x_upload(path: Path) -> str:
    url = "https://upload.twitter.com/1.1/media/upload.json"
    mime = "video/mp4" if path.suffix.lower() == ".mp4" else "image/jpeg"
    payload = _multipart(
        url,
        {},
        [("media", path.name, path.read_bytes(), mime)],
        timeout=180,
        headers={"Authorization": _x_header("POST", url)},
    )
    media_id = payload.get("media_id_string") or payload.get("media_id")
    if not media_id:
        raise RuntimeError(f"X media upload failed: {payload}")
    return str(media_id)


def _x_status(caption: str, media_id: str = "") -> str:
    url = "https://api.twitter.com/1.1/statuses/update.json"
    params = {"status": caption[:280]}
    if media_id:
        params["media_ids"] = media_id
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": _x_header("POST", url, params),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        return f"failed:{exc.code}:{detail}"
    ident = payload.get("id_str") or payload.get("id")
    return f"ok:{ident}" if ident else f"failed:{payload}"[:160]


def post_x(caption: str, media: Path | None) -> str:
    key, secret, token, token_secret = _x_keys()
    if not (key and secret and token and token_secret):
        return "skipped_no_x"
    try:
        media_id = _x_upload(media) if media and media.is_file() else ""
        return _x_status(caption, media_id)
    except Exception as exc:
        return f"failed:{exc}"[:160]


def _youtube_token() -> str:
    cid, secret, refresh = _env("YOUTUBE_CLIENT_ID"), _env("YOUTUBE_CLIENT_SECRET"), _env("YOUTUBE_REFRESH_TOKEN")
    if not (cid and secret and refresh):
        return ""
    payload = _form(
        "https://oauth2.googleapis.com/token",
        {
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        },
    )
    return str(payload.get("access_token") or "")


def post_youtube(video: Path, title: str, description: str, *, is_short: bool) -> str:
    token = _youtube_token()
    if not token:
        return "skipped_no_youtube"
    if not video.is_file():
        return "skipped_no_video"
    yt_title = title[:100]
    if is_short and not yt_title.lower().startswith("#shorts"):
        yt_title = f"#shorts {yt_title}"[:100]
    metadata = {
        "snippet": {
            "title": yt_title,
            "description": description[:5000],
            "categoryId": "22",
            "tags": ["sobriety", "recovery", "reallyraisedrough", "shorts"] if is_short else ["sobriety", "recovery"],
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    boundary = "----RRRYTBoundary"
    meta_json = json.dumps(metadata).encode("utf-8")
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="metadata"\r\n',
            b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
            meta_json,
            f"\r\n--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="file"; filename="{video.name}"\r\n'
                "Content-Type: video/mp4\r\n\r\n"
            ).encode(),
            video.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    params = urllib.parse.urlencode({"uploadType": "multipart", "part": "snippet,status"})
    req = urllib.request.Request(
        f"https://www.googleapis.com/upload/youtube/v3/videos?{params}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return f"failed:{exc.code}:{exc.read().decode('utf-8', errors='replace')[:160]}"
    except Exception as exc:
        return f"failed:{exc}"[:160]
    ident = payload.get("id")
    return f"ok:{ident}" if ident else f"failed:{payload}"[:160]


def _tiktok_token() -> str:
    key, secret, refresh, direct = (
        _env("TIKTOK_CLIENT_KEY"),
        _env("TIKTOK_CLIENT_SECRET"),
        _env("TIKTOK_REFRESH_TOKEN"),
        _env("TIKTOK_ACCESS_TOKEN"),
    )
    if key and secret and refresh:
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
        if token:
            return token
    return direct


def post_tiktok(video: Path, caption: str) -> str:
    token = _tiktok_token()
    if not token:
        return "skipped_no_tiktok"
    if not video.is_file():
        return "skipped_no_video"
    size = video.stat().st_size
    init = _form_json(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        {
            "post_info": {
                "title": caption[:2200],
                "privacy_level": _env("TIKTOK_PRIVACY_LEVEL") or "PUBLIC_TO_EVERYONE",
                "disable_duet": False,
                "disable_stitch": False,
                "disable_comment": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": size,
                "total_chunk_count": 1,
            },
        },
        token=token,
    )
    err = (init.get("error") or {}) if isinstance(init.get("error"), dict) else {}
    if str(err.get("code") or "").lower() not in ("", "ok", "success"):
        return f"failed:{err.get('message') or init}"[:160]
    data = init.get("data") or {}
    publish_id = str(data.get("publish_id") or "")
    upload_url = str(data.get("upload_url") or "")
    if not publish_id or not upload_url:
        return f"failed:{init}"[:160]
    body = video.read_bytes()
    req = urllib.request.Request(
        upload_url,
        data=body,
        method="PUT",
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(len(body)),
            "Content-Range": f"bytes 0-{len(body) - 1}/{len(body)}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        return f"failed:{exc.code}:{exc.read().decode('utf-8', errors='replace')[:120]}"
    except Exception as exc:
        return f"failed:{exc}"[:160]
    return f"ok:{publish_id}"


def _snapchat_token() -> str:
    cid, secret, refresh = (
        _env("SNAPCHAT_CLIENT_ID"),
        _env("SNAPCHAT_CLIENT_SECRET"),
        _env("SNAPCHAT_REFRESH_TOKEN"),
    )
    if not (cid and secret and refresh):
        return _env("SNAPCHAT_ACCESS_TOKEN")
    payload = _form(
        "https://accounts.snapchat.com/login/oauth2/access_token",
        {
            "refresh_token": refresh,
            "client_id": cid,
            "client_secret": secret,
            "grant_type": "refresh_token",
        },
    )
    return str(payload.get("access_token") or "")


def post_snapchat(media: Path, caption: str, *, is_video: bool) -> str:
    token = _snapchat_token()
    if not token:
        return "skipped_no_snapchat"
    if not media.is_file():
        return "skipped_no_media"
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except Exception:
        return "skipped_no_cryptography"
    profile = _env("SNAPCHAT_PUBLIC_PROFILE_ID")
    if not profile:
        try:
            req = urllib.request.Request(
                "https://businessapi.snapchat.com/v1/public_profiles/my_profile",
                headers={"Authorization": f"Bearer {token}"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=40) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            for item in payload.get("public_profiles") or []:
                prof = (item or {}).get("public_profile") or item.get("profile") or {}
                profile = str(prof.get("id") or "").strip()
                if profile:
                    break
        except Exception as exc:
            return f"failed:{exc}"[:160]
    if not profile:
        return "skipped_no_snapchat_profile"
    key = os.urandom(32)
    iv = os.urandom(16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    raw = media.read_bytes()
    pad = 16 - (len(raw) % 16)
    encrypted = encryptor.update(raw + bytes([pad]) * pad) + encryptor.finalize()
    key_b64 = base64.b64encode(key).decode("ascii")
    iv_b64 = base64.b64encode(iv).decode("ascii")
    created = _form_json(
        f"https://businessapi.snapchat.com/v1/public_profiles/{profile}/media",
        {
            "type": "VIDEO" if is_video else "IMAGE",
            "name": media.stem[:60],
            "key": key_b64,
            "iv": iv_b64,
        },
        token=token,
        method="POST",
    )
    if str(created.get("request_status") or "").upper() != "SUCCESS":
        return f"failed:{created}"[:160]
    media_id = str(created.get("media_id") or "")
    add_path = str(created.get("add_path") or "")
    finalize_path = str(created.get("finalize_path") or "")
    if not (media_id and add_path and finalize_path):
        return f"failed:{created}"[:160]
    uploaded = _multipart(
        f"https://businessapi.snapchat.com{add_path}",
        {"action": "ADD", "part_number": "1"},
        [("file", "media.enc", encrypted, "application/octet-stream")],
        timeout=300,
        headers={"Authorization": f"Bearer {token}"},
    )
    if str(uploaded.get("request_status") or uploaded.get("error") or "").lower() not in (
        "",
        "success",
        "ok",
    ) and uploaded.get("error"):
        return f"failed:{uploaded}"[:160]
    fin_req = urllib.request.Request(
        f"https://businessapi.snapchat.com{finalize_path}",
        data=urllib.parse.urlencode({"action": "FINALIZE"}).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(fin_req, timeout=120) as resp:
            json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
    except Exception as exc:
        return f"failed:{exc}"[:160]
    endpoint = "spotlights" if is_video else "stories"
    body: dict = {"media_id": media_id}
    if is_video:
        body["skip_save_to_profile"] = False
        body["description"] = caption.replace("\n", " ").strip()[:160]
        body["locale"] = "en_US"
    posted = _form_json(
        f"https://businessapi.snapchat.com/v1/public_profiles/{profile}/{endpoint}",
        body,
        token=token,
        method="POST",
    )
    if str(posted.get("request_status") or "").upper() != "SUCCESS":
        return f"failed:{posted}"[:160]
    ident = posted.get("spotlight_id") or posted.get("story_id") or media_id
    return f"ok:{ident}"


def _form_json(url: str, body: dict, *, token: str = "", method: str = "POST") -> dict:
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
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


def execute_slot(slot: dict, pack: dict, work: Path, clock: dict | None = None) -> str:
    plat = str(slot.get("platform") or "").lower()
    ptype = str(slot.get("post_type") or "image").lower()
    clock = clock or _clock()
    humor = humor_for(slot, clock)
    product = _pick(pack, slot, clock)
    image_url = str(product.get("image_url") or product.get("image") or "")
    cap = order_caption(product, plat, humor=humor)
    if DRY:
        return f"dry:{plat}/{ptype}/{humor}"
    image_path = None
    if image_url:
        image_path = _download(image_url, work / "post.jpg")
    video_path = None
    if _needs_video(slot):
        if image_path:
            seconds = _duration(slot)
            audio = None
            cfg = dict(_voice_cfg())
            cfg["humor"] = humor
            cfg["personality"] = humor
            script = _humor_script(product, cfg, seconds, humor=humor, slot=slot, clock=clock)
            audio = _tts_mp3(script, work / "vo.mp3", cfg, slot=slot, clock=clock)
            video_path = _make_video(image_path, work / "post.mp4", seconds, audio)
        if plat == "youtube" and not video_path:
            return "skipped_no_ffmpeg"
    try:
        if plat == "facebook":
            if ptype in IMAGE_TYPES or not video_path:
                return post_facebook_image(image_url, cap)
            return post_facebook_video(video_path, cap)
        if plat == "instagram":
            if ptype in IMAGE_TYPES or not video_path:
                return post_instagram_image(image_url, cap)
            return post_instagram_reel(video_path, cap)
        if plat == "threads":
            return post_threads_image(image_url, cap)
        if plat == "telegram":
            if video_path:
                return post_telegram_video(video_path, cap)
            return post_telegram_image(image_url, cap)
        if plat == "x":
            return post_x(cap, video_path or image_path)
        if plat == "youtube":
            title = str(product.get("product_title") or "Really Raised Rough")[:80]
            if "reallyraisedrough.com" not in title.lower():
                title = f"{title} | reallyraisedrough.com"
            return post_youtube(video_path or Path(), title, cap, is_short=(ptype != "video"))
        if plat == "tiktok":
            if not video_path:
                return "skipped_no_tiktok_video"
            return post_tiktok(video_path, cap)
        if plat == "snapchat":
            media = video_path or image_path
            if not media:
                return "skipped_no_snapchat_media"
            return post_snapchat(Path(media), cap, is_video=bool(video_path))
        return f"skipped_unknown_{plat}"
    except Exception as exc:
        return f"failed:{exc}"[:160]


def run() -> dict:
    pack = _load(PACK_PATH, {})
    fired = _load(FIRED_PATH, {})
    if not isinstance(fired, dict):
        fired = {}
    if pack.get("timezone") and not os.getenv("RRR_TIMEZONE"):
        global TZ_NAME
        tz = str(pack.get("timezone") or "").strip()
        if tz and tz.lower() != "local":
            TZ_NAME = tz
    clock = _clock()
    due = due_slots(pack, fired, clock)
    results = []
    claimed: list[dict] = []
    if not DRY:
        from rrr_slot_lock import HOST_ID, claim, finish

        for slot in due:
            key = _run_key(slot, clock)
            if claim(fired, key, now_iso=clock["iso"]):
                claimed.append(slot)
        _save(FIRED_PATH, fired)
        if os.getenv("RRR_PUSH_FIRED", "").strip() in ("1", "true", "yes", "on"):
            try:
                import rrr_cloud_sync

                rrr_cloud_sync.push()
            except Exception:
                pass
    else:
        claimed = due
        from rrr_slot_lock import HOST_ID, finish  # noqa: F401
    qjobs = drain_pack_queue(pack)
    claimed_q: list[tuple[str, dict]] = []
    if not DRY:
        from rrr_slot_lock import claim as _qclaim

        for job in qjobs:
            qkey = f"{clock['date']}-queue-{job.get('id') or 'x'}"
            if _qclaim(fired, qkey, now_iso=clock["iso"]):
                claimed_q.append((qkey, job))
        if claimed_q:
            _save(FIRED_PATH, fired)
    else:
        claimed_q = [(f"dry-queue-{j.get('id')}", j) for j in qjobs]
    with tempfile.TemporaryDirectory(prefix="rrr-cloud-") as tmp:
        work = Path(tmp)
        for slot in claimed:
            humor = humor_for(slot, clock)
            result = execute_slot(slot, pack, work, clock)
            key = _run_key(slot, clock)
            plat = str(slot.get("platform") or "").lower()
            slot["last_run_at"] = clock["iso"]
            slot["last_result"] = result[:400]
            slot["last_humor"] = humor
            if DRY:
                continue
            from rrr_slot_lock import finish as _finish

            _finish(
                fired,
                key,
                result,
                now_iso=clock["iso"],
                extra={"platform": plat, "humor": humor},
            )
            if _success(result):
                slot["last_fired_key"] = key
            results.append(
                {
                    "id": slot.get("id"),
                    "platform": plat,
                    "post_type": slot.get("post_type"),
                    "humor": humor,
                    "host": os.getenv("RRR_HOST_ID") or "cloud",
                    "result": result,
                }
            )
        for qkey, job in claimed_q:
            humor = humor_for(job, clock)
            result = execute_queue_job(job, pack, work, clock)
            plat = str(job.get("platform") or "").lower()
            if DRY:
                results.append(
                    {
                        "id": job.get("id"),
                        "platform": plat,
                        "post_type": job.get("post_type"),
                        "kind": job.get("kind"),
                        "source": "unlimited-queue",
                        "result": result,
                    }
                )
                continue
            from rrr_slot_lock import finish as _qfinish

            _qfinish(
                fired,
                qkey,
                result,
                now_iso=clock["iso"],
                extra={"platform": plat, "humor": humor, "source": "unlimited-queue"},
            )
            results.append(
                {
                    "id": job.get("id"),
                    "platform": plat,
                    "post_type": job.get("post_type"),
                    "kind": job.get("kind"),
                    "humor": humor,
                    "source": "unlimited-queue",
                    "host": os.getenv("RRR_HOST_ID") or "cloud",
                    "result": result,
                }
            )
    if not DRY:
        _save(PACK_PATH, pack)
        _save(FIRED_PATH, fired)
    qstate = pack.get("queue") if isinstance(pack.get("queue"), dict) else {}
    summary = {
        "ok": True,
        "at": clock["iso"],
        "tz": TZ_NAME,
        "due": len(due),
        "claimed": len(claimed),
        "queue_claimed": len(claimed_q),
        "queue_pending": len(qstate.get("pending") or []),
        "queue_on": queue_on(pack),
        "host": os.getenv("RRR_HOST_ID") or "cloud",
        "results": results,
        "identity": IDENTITY,
        "laptop_required": False,
        "platforms": list(PLATFORMS),
        "x_mode": "oauth1_free",
        "controls": "Start Jarvis",
        "dry": DRY,
    }
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:200]}))
        sys.exit(1)
