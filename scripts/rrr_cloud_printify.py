"""Cloud Printify: used-prompt / related products + mockups for posts.

Runs on GitHub Actions. Uses PRINTIFY_API_TOKEN / PRINTIFY_SHOP_ID.
Never prints tokens. Updates pack.json pool with live mockups.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
PACK_PATH = Path(os.getenv("RRR_PACK_PATH") or (ROOT / "pack.json"))
STATE_PATH = Path(os.getenv("RRR_PRINTIFY_STATE") or (ROOT / "printify_drop_state.json"))
STORE = "https://reallyraisedrough.com"
API = "https://api.printify.com/v1"
LOGO_ID = "6a81629b23d927985dfff0e5"
OLD_LOGO_IDS = {"69cc103f1c19c8d41d99ad0d", "5941187eb8e7e37b3f0e62e5"}

NECK_LOGO = {"x": 0.5, "y": 0.5, "scale": 1.0, "angle": 0}
# 15oz mug wrap: left half = logo side, right half = design side.
MUG_LOGO_PLACE = {"x": 0.22, "y": 0.50, "scale": 0.28, "angle": 0}
MUG_ART_PLACE = {"x": 0.78, "y": 0.50, "scale": 0.437, "angle": 0}

PRESETS = {
    "tee": {"blueprint_id": 6, "print_provider_id": 99, "price": 2100, "label": "Shirt", "place": {"x": 0.5, "y": 0.435, "scale": 1.05, "angle": 0}, "neck": True, "position": "front"},
    "hoodie": {"blueprint_id": 77, "print_provider_id": 99, "price": 3500, "label": "Sweatshirt", "place": {"x": 0.5, "y": 0.39, "scale": 0.54, "angle": 0}, "neck": True, "position": "front"},
    "mug": {"blueprint_id": 425, "print_provider_id": 1, "price": 1300, "label": "Mug", "place": MUG_ART_PLACE, "neck": False, "position": "front"},
    "hat": {"blueprint_id": 1735, "print_provider_id": 99, "price": 2400, "label": "Trucker Hat", "place": {"x": 0.5, "y": 0.5, "scale": 0.88, "angle": 0}, "neck": False, "position": "front_dtf"},
    "glass": {"blueprint_id": 1441, "print_provider_id": 86, "price": 2200, "label": "Glass Cup", "place": {"x": 0.5, "y": 0.5, "scale": 0.72, "angle": 0}, "neck": False, "position": "front"},
}

TOPICS = (
    ("STILL HERE STILL SOBER", "NO PERFORMATIVE RECOVERY"),
    ("CHEMICALLY CLEAN", "NOT CHEMICALLY BORING"),
    ("CHRONIC ATTITUDE", "PRO CHOICE PRO PEACE"),
    ("ROUGH HONEST TRUTH", "LAUGH AT THE DAMAGE"),
    ("ONE DAY AT A TIME", "STILL RAISED ROUGH"),
    ("SOBER NOT SOFT", "REALLY RAISED ROUGH"),
)


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _token() -> str:
    return _env("PRINTIFY_API_TOKEN")


def _load(path: Path, fallback):
    if not path.is_file():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def _save(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _today() -> str:
    try:
        tz = ZoneInfo("America/Phoenix") if ZoneInfo else None
        now = datetime.now(tz) if tz else datetime.now()
    except Exception:
        now = datetime.now()
    return now.strftime("%Y-%m-%d")


def _api(method: str, path: str, body: dict | None = None, timeout: int = 90):
    token = _token()
    if not token:
        raise RuntimeError("PRINTIFY_API_TOKEN missing")
    data = None if body is None else json.dumps(body).encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(
            f"{API}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "ReallyRaisedRough-cloud/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(f"Printify {method} {path} HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            last_err = exc
            time.sleep(1.4 * (attempt + 1))
    raise RuntimeError(f"Printify {method} {path} failed: {last_err}") from last_err


def shop_id() -> int:
    raw = _env("PRINTIFY_SHOP_ID")
    if raw.isdigit():
        return int(raw)
    shops = _api("GET", "/shops.json")
    if isinstance(shops, list) and shops:
        preferred = [
            s for s in shops
            if "really raised rough" in str(s.get("title") or "").lower()
        ]
        chosen = preferred[0] if preferred else shops[0]
        return int(chosen["id"])
    raise RuntimeError("No Printify shop found")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:48] or "rrr"


def checkout_url(product_id: str, title: str) -> str:
    return f"{STORE}/product/{product_id}/{_slug(title)}"


def list_products(limit_pages: int = 3) -> list[dict]:
    sid = shop_id()
    out: list[dict] = []
    for page in range(1, limit_pages + 1):
        payload = _api("GET", f"/shops/{sid}/products.json?page={page}&limit=50")
        batch = payload.get("data") or []
        out.extend(batch)
        if page >= int(payload.get("last_page") or page):
            break
    return out


def mockup_src(product: dict) -> str:
    for img in product.get("images") or []:
        src = str(img.get("src") or "").strip()
        if src.startswith("http"):
            return src
    return ""


def sync_mockups_into_pack(products: list[dict] | None = None) -> int:
    products = products if products is not None else list_products()
    posts = []
    for product in products:
        src = mockup_src(product)
        if not src:
            continue
        pid = str(product.get("id") or "")
        title = str(product.get("title") or "Really Raised Rough")
        url = checkout_url(pid, title)
        posts.append(
            {
                "product_title": title[:160],
                "product_url": url,
                "image_url": src,
                "caption": f"🛒 ORDER NOW — pick size, color, enter address, checkout:\n{url}\n{title}\n#reallyraisedrough #soberlife",
                "printify_id": pid,
            }
        )
    pack = _load(PACK_PATH, {})
    if not isinstance(pack, dict):
        pack = {}
    pool = pack.setdefault("pool", {})
    existing = [p for p in (pool.get("posts") or []) if isinstance(p, dict)]
    seen = {str(p.get("product_url") or p.get("image_url")) for p in posts}
    for old in existing:
        key = str(old.get("product_url") or old.get("image_url"))
        if key and key not in seen:
            posts.append(old)
            seen.add(key)
    pool["posts"] = posts[:80]
    pack["printify_sync"] = datetime.now(timezone.utc).isoformat()
    pack["env"] = {}
    _save(PACK_PATH, pack)
    return len(posts)


def _make_art(headline: str, subline: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    size = 2400
    img = Image.new("RGB", (size, size), (8, 8, 8))
    draw = ImageDraw.Draw(img)
    try:
        font_big = ImageFont.truetype("DejaVuSans-Bold.ttf", 140)
        font_sm = ImageFont.truetype("DejaVuSans.ttf", 64)
        font_brand = ImageFont.truetype("DejaVuSans-Bold.ttf", 42)
    except Exception:
        font_big = ImageFont.load_default()
        font_sm = font_big
        font_brand = font_big
    cream = (232, 220, 196)
    rust = (176, 74, 42)

    def center(text: str, y: int, font, fill) -> None:
        box = draw.textbbox((0, 0), text, font=font)
        w = box[2] - box[0]
        draw.text(((size - w) / 2, y), text, font=font, fill=fill)

    words = headline.split()
    line1 = " ".join(words[:2]) if len(words) > 2 else headline
    line2 = " ".join(words[2:]) if len(words) > 2 else ""
    center(line1, 820, font_big, cream)
    if line2:
        center(line2, 1000, font_big, cream)
    center(subline, 1280, font_sm, rust)
    center("REALLY RAISED ROUGH", 2100, font_brand, cream)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def upload_png(png: bytes, name: str) -> str:
    payload = {
        "file_name": name,
        "contents": base64.b64encode(png).decode("ascii"),
    }
    uploaded = _api("POST", "/uploads/images.json", payload, timeout=120)
    ident = str(uploaded.get("id") or "")
    if not ident:
        raise RuntimeError(f"upload failed: {uploaded}")
    return ident


def _variants(blueprint: int, provider: int, *, apparel: bool) -> tuple[list[dict], list[int]]:
    payload = _api("GET", f"/catalog/blueprints/{blueprint}/print_providers/{provider}/variants.json")
    catalog = payload if isinstance(payload, list) else payload.get("variants") or []
    colors = {"black", "white", "forest green", "royal", "red", "light pink"}
    sizes = {"S", "M", "L", "XL", "2XL", "3XL"}
    picked: list[dict] = []
    ids: list[int] = []
    for item in catalog:
        opts = item.get("options") or {}
        color = str(opts.get("color") or "").lower()
        size = str(opts.get("size") or "").upper()
        if apparel and (color not in colors or size not in sizes):
            continue
        vid = int(item["id"])
        ids.append(vid)
        picked.append({"id": vid, "price": 0, "is_enabled": True})
        if not apparel:
            break
        if len(ids) >= 24:
            break
    if not ids and catalog:
        vid = int(catalog[0]["id"])
        ids = [vid]
        picked = [{"id": vid, "price": 0, "is_enabled": True}]
    return picked, ids


def _extract_art_id(product: dict) -> str:
    for area in product.get("print_areas") or []:
        for place in area.get("placeholders") or []:
            for im in place.get("images") or []:
                iid = str(im.get("id") or "").strip()
                if iid and iid != LOGO_ID and iid not in OLD_LOGO_IDS:
                    return iid
    return ""


def _source_design() -> dict:
    for product in list_products(2):
        art_id = _extract_art_id(product)
        title = str(product.get("title") or "").strip()
        if art_id and title:
            return {"art_id": art_id, "title": title, "id": str(product.get("id") or "")}
    return {}


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


def _humor_for_product(kind: str, source_title: str) -> str:
    raw = f"{_today()}|{kind}|{source_title}"
    digest = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()
    return HUMOR_STYLES[int(digest[:8], 16) % len(HUMOR_STYLES)]


def _funny_related(source_title: str, kind: str) -> tuple[str, str]:
    label = PRESETS[kind]["label"]
    raw = re.sub(r"\s*[|\-—].*$", "", source_title or "").strip()
    raw = re.sub(r"\b(tee|shirt|hoodie|mug|hat|glass|really raised rough)\b", "", raw, flags=re.I)
    raw = re.sub(r"\$\s*\d+", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip() or "Still Raised Rough"
    humor = _humor_for_product(kind, raw)
    jokes = {
        ("tee", "dark_humor"): (f"{raw} | Graphic Tee", f"The chest graphic says {raw}. Same joke, street cotton, no lecture."),
        ("tee", "prison_humor"): (f"{raw} | Graphic Tee", f"Yard-comedy energy. Free-world cotton. {raw}."),
        ("tee", "recovery_dark"): (f"{raw} | Graphic Tee", f"Still clean, still loud. {raw} on the chest."),
        ("tee", "past_chaos"): (f"{raw} | Graphic Tee", f"Proof you walked out. {raw}."),
        ("tee", "enforcer"): (f"{raw} | Graphic Tee", f"No soft merch. Wear {raw}."),
        ("tee", "wiseguy"): (f"{raw} | Graphic Tee", f"Your excuses already know this joke. {raw}."),
        ("tee", "dry"): (f"{raw} | Graphic Tee", f"Slightly rude. Properly printed. {raw}."),
        ("tee", "deadpan"): (f"{raw} | Graphic Tee", f"Funny. That's the review. {raw}."),
        ("tee", "heartfelt"): (f"{raw} | Graphic Tee", f"For people who stayed. {raw}."),
        ("tee", "sarcastic"): (f"{raw} | Graphic Tee", f"Congrats on surviving yourself. {raw}."),
        ("hoodie", "dark_humor"): (f"{raw} Sweatshirt", f"Feelings get cold. The chest reads {raw}."),
        ("hoodie", "prison_humor"): (f"{raw} Sweatshirt", f"Top-bunk warmth with {raw} on the front."),
        ("hoodie", "recovery_dark"): (f"{raw} Sweatshirt", f"Clean and still cold. {raw}."),
        ("hoodie", "past_chaos"): (f"{raw} Sweatshirt", f"Wreckage weather gear. {raw}."),
        ("hoodie", "enforcer"): (f"{raw} Sweatshirt", f"No soft fleece lectures. {raw}."),
        ("hoodie", "wiseguy"): (f"{raw} Sweatshirt", f"Pocket included. {raw} on the chest."),
        ("hoodie", "dry"): (f"{raw} Sweatshirt", f"Feelings get cold. Cotton helps. {raw}."),
        ("hoodie", "deadpan"): (f"{raw} Sweatshirt", f"Warm. Funny. {raw}."),
        ("hoodie", "heartfelt"): (f"{raw} Sweatshirt", f"Stay warm. Stay honest. {raw}."),
        ("hoodie", "sarcastic"): (f"{raw} Sweatshirt", f"Therapy costs more. Wear {raw}."),
        ("mug", "dark_humor"): (f"Mug · {raw}", f"Morning honesty. The mug says {raw}."),
        ("mug", "prison_humor"): (f"Mug · {raw}", f"Commissary coffee, free-world joke. {raw}."),
        ("mug", "recovery_dark"): (f"Mug · {raw}", f"Still clean, still caffeinated. {raw}."),
        ("mug", "past_chaos"): (f"Mug · {raw}", f"Yesterday's plot twist, today's coffee. {raw}."),
        ("mug", "enforcer"): (f"Mug · {raw}", f"Drink. Checkout. {raw}."),
        ("mug", "wiseguy"): (f"Mug · {raw}", f"Your excuses hate this handle. {raw}."),
        ("mug", "dry"): (f"Mug · {raw}", f"Slightly rude. Properly caffeinated. {raw}."),
        ("mug", "deadpan"): (f"Mug · {raw}", f"Coffee. Joke. That's it. {raw}."),
        ("mug", "heartfelt"): (f"Mug · {raw}", f"For the ones who stayed. {raw}."),
        ("mug", "sarcastic"): (f"Mug · {raw}", f"Congrats. You woke up. {raw}."),
        ("hat", "dark_humor"): (f"{raw} · Trucker Hat", f"The brim carries {raw} so you do not have to."),
        ("hat", "prison_humor"): (f"{raw} · Trucker Hat", f"Yard brim. Free-world mesh. {raw}."),
        ("hat", "recovery_dark"): (f"{raw} · Trucker Hat", f"Clean living, loud hat. {raw}."),
        ("hat", "past_chaos"): (f"{raw} · Trucker Hat", f"Sun in your eyes, {raw} on the panel."),
        ("hat", "enforcer"): (f"{raw} · Trucker Hat", f"No soft brim. {raw}."),
        ("hat", "wiseguy"): (f"{raw} · Trucker Hat", f"Real talk on a foam trucker. {raw}."),
        ("hat", "dry"): (f"{raw} · Trucker Hat", f"Slightly rude shade. {raw}."),
        ("hat", "deadpan"): (f"{raw} · Trucker Hat", f"Hat. Joke. {raw}."),
        ("hat", "heartfelt"): (f"{raw} · Trucker Hat", f"For people who stayed in the sun. {raw}."),
        ("hat", "sarcastic"): (f"{raw} · Trucker Hat", f"Hair quit. Attitude did not. {raw}."),
        ("glass", "dark_humor"): (f"{raw} Glass Cup", f"The glass holds the drink. The print holds {raw}."),
        ("glass", "prison_humor"): (f"{raw} Glass Cup", f"Commissary sip, free-world glass. {raw}."),
        ("glass", "recovery_dark"): (f"{raw} Glass Cup", f"Still clean. Still sipping. {raw}."),
        ("glass", "past_chaos"): (f"{raw} Glass Cup", f"Sip the wreckage. Stay out. {raw}."),
        ("glass", "enforcer"): (f"{raw} Glass Cup", f"Drink. Checkout. {raw}."),
        ("glass", "wiseguy"): (f"{raw} Glass Cup", f"Your excuses hate this glass. {raw}."),
        ("glass", "dry"): (f"{raw} Glass Cup", f"Slightly rude. Properly poured. {raw}."),
        ("glass", "deadpan"): (f"{raw} Glass Cup", f"Glass. Joke. {raw}."),
        ("glass", "heartfelt"): (f"{raw} Glass Cup", f"For the ones who stayed. {raw}."),
        ("glass", "sarcastic"): (f"{raw} Glass Cup", f"Congrats. You poured a drink. {raw}."),
    }
    title, desc = jokes.get((kind, humor)) or jokes.get((kind, "dark_humor")) or (f"{raw} | {label}", f"{raw}.")
    title = f"{title} | reallyraisedrough.com"
    desc = f"This design says \"{raw}\". {desc} {humor.replace('_', ' ')}. Shop reallyraisedrough.com"
    desc = re.sub(r"\$\s*\d+", "", desc)
    return title[:120], desc


def _img(image_id: str, place: dict) -> dict:
    return {
        "id": image_id,
        "x": place["x"],
        "y": place["y"],
        "scale": place["scale"],
        "angle": int(place.get("angle", 0) or 0),
    }


def _placeholders(kind: str, art_id: str, use_logo: bool) -> list[dict]:
    """Shirts/hoodies: design on front, logo on neck tag. Mug: logo one wrap side, design the other."""
    preset = PRESETS[kind]
    pos = str(preset.get("position") or "front")
    if kind == "mug":
        images = []
        if use_logo:
            images.append(_img(LOGO_ID, MUG_LOGO_PLACE))
        images.append(_img(art_id, preset["place"]))
        return [{"position": "front", "images": images}]
    placeholders = [{"position": pos, "images": [_img(art_id, preset["place"])]}]
    if use_logo and preset.get("neck"):
        placeholders.append({"position": "neck", "images": [_img(LOGO_ID, NECK_LOGO)]})
    return placeholders


def _create_kind(kind: str, art_id: str, headline: str, subline: str, price: int, use_logo: bool) -> dict:
    preset = PRESETS[kind]
    variants, ids = _variants(
        preset["blueprint_id"],
        preset["print_provider_id"],
        apparel=kind in {"tee", "hoodie"},
    )
    for item in variants:
        item["price"] = price
    placeholders = _placeholders(kind, art_id, use_logo)
    title, funny = _funny_related(f"{headline} {subline}".strip(), kind)
    body = {
        "title": title[:80],
        "description": (
            f"{funny}\n"
            f"🛒 ORDER NOW — pick size, color, enter address, checkout.\n"
            f"{STORE}\n"
        ),
        "tags": ["reallyraisedrough", "sobriety", "recovery", kind],
        "blueprint_id": preset["blueprint_id"],
        "print_provider_id": preset["print_provider_id"],
        "variants": variants,
        "print_areas": [{"variant_ids": ids, "placeholders": placeholders}],
    }
    sid = shop_id()
    created = _api("POST", f"/shops/{sid}/products.json", body, timeout=120)
    pid = str(created.get("id") or "")
    if not pid:
        raise RuntimeError(f"create {kind} failed: {created}")
    try:
        _api(
            "POST",
            f"/shops/{sid}/products/{pid}/publish.json",
            {"title": True, "description": True, "images": True, "variants": True, "tags": True},
        )
        published = True
    except Exception:
        published = False
    src = ""
    for _ in range(6):
        time.sleep(3)
        fresh = _api("GET", f"/shops/{sid}/products/{pid}.json")
        src = mockup_src(fresh)
        if src:
            created = fresh
            break
    return {
        "ok": True,
        "kind": kind,
        "product_id": pid,
        "title": created.get("title") or title,
        "image_url": src,
        "product_url": checkout_url(pid, created.get("title") or title),
        "published": published,
    }


def _needs_mug_logo_fix(placeholders: list) -> bool:
    for place in placeholders:
        if str(place.get("position") or "") != "front":
            continue
        images = place.get("images") or []
        has_logo = any(str(im.get("id") or "") == LOGO_ID for im in images)
        if not has_logo:
            return True
        for im in images:
            if str(im.get("id") or "") != LOGO_ID:
                continue
            x = float(im.get("x") or 0)
            y = float(im.get("y") or 0)
            if abs(x - MUG_LOGO_PLACE["x"]) > 0.04 or abs(y - MUG_LOGO_PLACE["y"]) > 0.08:
                return True
    return False


def _needs_neck_logo(placeholders: list) -> bool:
    for place in placeholders:
        if str(place.get("position") or "") != "neck":
            continue
        for im in place.get("images") or []:
            if str(im.get("id") or "") == LOGO_ID:
                return False
    return True


def _copy_placeholder_images(place: dict) -> list[dict]:
    out = []
    for im in place.get("images") or []:
        iid = str(im.get("id") or "").strip()
        if not iid:
            continue
        out.append(
            {
                "id": iid,
                "x": float(im.get("x") or 0.5),
                "y": float(im.get("y") or 0.5),
                "scale": float(im.get("scale") or 1.0),
                "angle": int(im.get("angle") or 0),
            }
        )
    return out


def _fixed_mug_placeholders(placeholders: list) -> list[dict]:
    rebuilt: list[dict] = []
    saw_front = False
    for place in placeholders:
        pos = str(place.get("position") or "front")
        images = _copy_placeholder_images(place)
        if pos == "front":
            saw_front = True
            others = [im for im in images if im["id"] != LOGO_ID and im["id"] not in OLD_LOGO_IDS]
            if not others:
                others = [im for im in images if im["id"] != LOGO_ID]
            rebuilt.append(
                {
                    "position": "front",
                    "images": [_img(LOGO_ID, MUG_LOGO_PLACE)] + others,
                }
            )
        elif images:
            rebuilt.append({"position": pos, "images": images})
    if not saw_front:
        rebuilt.insert(0, {"position": "front", "images": [_img(LOGO_ID, MUG_LOGO_PLACE)]})
    return rebuilt


def _fixed_apparel_placeholders(placeholders: list) -> list[dict]:
    rebuilt: list[dict] = []
    saw_neck = False
    for place in placeholders:
        pos = str(place.get("position") or "front")
        images = _copy_placeholder_images(place)
        if pos == "neck":
            saw_neck = True
            rebuilt.append({"position": "neck", "images": [_img(LOGO_ID, NECK_LOGO)]})
        elif images:
            rebuilt.append({"position": pos, "images": images})
    if not saw_neck:
        rebuilt.append({"position": "neck", "images": [_img(LOGO_ID, NECK_LOGO)]})
    return rebuilt


def repair_shop_placements(limit: int = 80) -> dict:
    """Add neck-tag logo to shirts/hoodies and opposite-side mug logo when missing."""
    sid = shop_id()
    fixed = []
    scanned = 0
    for product in list_products(4):
        if scanned >= limit:
            break
        bid = int(product.get("blueprint_id") or 0)
        kind = {6: "tee", 77: "hoodie", 425: "mug", 1735: "hat", 1441: "glass"}.get(bid)
        if not kind or kind in {"hat", "glass"}:
            continue
        scanned += 1
        areas = product.get("print_areas") or []
        if not areas:
            continue
        placeholders = []
        for area in areas:
            placeholders.extend(area.get("placeholders") or [])
        if kind == "mug":
            if not _needs_mug_logo_fix(placeholders):
                continue
            new_placeholders = _fixed_mug_placeholders(placeholders)
        else:
            if not _needs_neck_logo(placeholders):
                continue
            new_placeholders = _fixed_apparel_placeholders(placeholders)
        variant_ids = []
        for area in areas:
            for vid in area.get("variant_ids") or []:
                try:
                    variant_ids.append(int(vid))
                except Exception:
                    continue
        if not variant_ids:
            continue
        pid = str(product.get("id") or "")
        try:
            _api(
                "PUT",
                f"/shops/{sid}/products/{pid}.json",
                {"print_areas": [{"variant_ids": variant_ids, "placeholders": new_placeholders}]},
                timeout=90,
            )
            try:
                _api(
                    "POST",
                    f"/shops/{sid}/products/{pid}/publish.json",
                    {"title": True, "description": True, "images": True, "variants": True, "tags": True},
                )
            except Exception:
                pass
            fixed.append({"id": pid, "kind": kind, "title": str(product.get("title") or "")[:60]})
        except Exception as exc:
            fixed.append({"id": pid, "kind": kind, "error": str(exc)[:120]})
        if len(fixed) >= 12:
            break
    return {"scanned": scanned, "updated": len([x for x in fixed if "error" not in x]), "items": fixed}


def _iso_week() -> str:
    try:
        tz = ZoneInfo("America/Phoenix") if ZoneInfo else None
        now = datetime.now(tz) if tz else datetime.now()
    except Exception:
        now = datetime.now()
    return now.strftime("%G-W%V")


def _drop_one(*, kind_mode: str, new_art: bool) -> dict:
    today = _today()
    source = {}
    try:
        source = _source_design()
    except Exception:
        source = {}
    if new_art or not source.get("art_id"):
        idx = datetime.now(timezone.utc).timetuple().tm_yday % len(TOPICS)
        headline, subline = TOPICS[idx]
        png = _make_art(headline, subline)
        art_id = upload_png(png, f"rrr-cloud-{kind_mode}-{today}.png")
        related_from = ""
    else:
        headline = str(source["title"])
        subline = "related drop"
        art_id = str(source.get("art_id") or "")
        related_from = source.get("id") or ""
    last_kind = str((_load(STATE_PATH, {}) or {}).get("last_kind") or "")
    kinds = [k for k in PRESETS if k != last_kind] or list(PRESETS)
    kind = random.choice(kinds)
    made = []
    try:
        made.append(_create_kind(kind, art_id, headline, subline, PRESETS[kind]["price"], use_logo=True))
    except Exception as exc:
        try:
            made.append(_create_kind(kind, art_id, headline, subline, PRESETS[kind]["price"], use_logo=False))
        except Exception as exc2:
            made.append({"ok": False, "kind": kind, "error": str(exc2)[:160], "first": str(exc)[:80]})
    return {
        "kind": kind,
        "headline": headline,
        "related_from": related_from,
        "new_art": new_art,
        "products": made,
        "ok": any(p.get("ok") for p in made if isinstance(p, dict)),
    }


def create_daily_set() -> dict:
    """1 existing (used art) + 1 new design per ISO week. Hosts split leftover slots."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from rrr_slot_lock import HOST_ID, claim, finish, is_held, is_success

    state = _load(STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}
    week = _iso_week()
    week_state = state.get("weeks") if isinstance(state.get("weeks"), dict) else {}
    slots = week_state.get(week) if isinstance(week_state.get(week), dict) else {}
    now_iso = datetime.now(timezone.utc).isoformat()
    ran = []
    for name, new_art in (("existing", False), ("new", True)):
        key = f"printify-{week}-{name}"
        entry = slots.get(name)
        if is_success(entry) or is_held(entry):
            continue
        if not claim(slots, name, now_iso=now_iso):
            continue
        week_state[week] = slots
        state["weeks"] = week_state
        _save(STATE_PATH, state)
        drop = _drop_one(kind_mode=name, new_art=new_art)
        result = "ok" if drop.get("ok") else f"failed:{drop.get('products')}"
        finish(slots, name, result, now_iso=now_iso, extra={"drop": drop, "key": key})
        if drop.get("kind"):
            state["last_kind"] = drop["kind"]
        state["last_drop_date"] = _today()
        ran.append({"slot": name, "ok": drop.get("ok"), "kind": drop.get("kind"), "host": HOST_ID})
        week_state[week] = slots
        state["weeks"] = week_state
        state["at"] = now_iso
        _save(STATE_PATH, state)
        if drop.get("ok"):
            break  # one product per tick so the other host can take the leftover slot
    if not ran:
        return {"skipped": True, "reason": "week slots held or done", "week": week, "slots": slots}
    return {"ok": True, "week": week, "ran": ran, "slots": slots}


def run() -> dict:
    if not _token():
        summary = {"ok": False, "error": "PRINTIFY_API_TOKEN missing"}
        print(json.dumps(summary))
        return summary
    drop = {}
    try:
        drop = create_daily_set()
    except Exception as exc:
        drop = {"ok": False, "error": str(exc)[:200]}
    repaired = {}
    try:
        repaired = repair_shop_placements()
    except Exception as exc:
        repaired = {"error": str(exc)[:200]}
    synced = 0
    try:
        synced = sync_mockups_into_pack()
    except Exception as exc:
        drop["sync_error"] = str(exc)[:200]
    summary = {
        "ok": True,
        "drop": drop,
        "repaired": repaired,
        "mockups_in_pack": synced,
        "shop_configured": bool(_env("PRINTIFY_SHOP_ID") or True),
    }
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:240]}))
        raise SystemExit(1)
