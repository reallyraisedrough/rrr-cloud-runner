"""Shared Hub -> server controls for Printify design creation.

The GitHub worker does not run the Tk Hub or load the laptop's dotenv file.
This small dependency-free module keeps the control contract identical on
both sides and lets the posting pack carry the authoritative values.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PACK_PATH = Path(os.getenv("RRR_PACK_PATH") or (ROOT / "pack.json"))

DEFAULT_DESIGN_CONTROLS: dict[str, Any] = {
    "enabled": True,
    "preview_only": True,
    "limit_per_week": 2,
    "categories": ["serious_sobriety", "dark_humor", "funny", "heartfelt"],
    "styles": ["professional", "creative", "gritty", "comic_shaded"],
    "product_types": ["tee", "hoodie", "mug", "hat", "glass"],
    "layouts": ["auto", "classic", "center", "wide"],
}

ENV_KEYS = {
    "enabled": "PRINTIFY_SHIRT_DESIGNER_ENABLED",
    "preview_only": "PRINTIFY_SHIRT_DESIGNER_PREVIEW_ONLY",
    "limit_per_week": "PRINTIFY_SHIRT_DESIGNER_LIMIT_PER_WEEK",
    "categories": "PRINTIFY_SHIRT_DESIGNER_CATEGORIES",
    "styles": "PRINTIFY_SHIRT_DESIGNER_STYLES",
    "product_types": "PRINTIFY_SHIRT_DESIGNER_PRODUCT_TYPES",
    "layouts": "PRINTIFY_SHIRT_DESIGNER_LAYOUTS",
}

PRODUCT_TYPES = ("tee", "hoodie", "sweatshirt", "jacket", "mug", "hat", "glass")


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _values(value: Any, fallback: list[str], *, allowed: set[str] | None = None) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = re.split(r"[,\n]", str(value or ""))
    out: list[str] = []
    for item in raw:
        value = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(item).strip().lower()).strip("_")
        if not value or (allowed is not None and value not in allowed) or value in out:
            continue
        out.append(value)
    return out or list(fallback)


def normalize_design_controls(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return safe, serializable controls with deterministic defaults."""
    incoming = raw if isinstance(raw, dict) else {}
    try:
        limit = int(incoming.get("limit_per_week", DEFAULT_DESIGN_CONTROLS["limit_per_week"]) or 0)
    except (TypeError, ValueError):
        limit = int(DEFAULT_DESIGN_CONTROLS["limit_per_week"])
    categories = _values(incoming.get("categories"), DEFAULT_DESIGN_CONTROLS["categories"])
    styles = _values(incoming.get("styles"), DEFAULT_DESIGN_CONTROLS["styles"])
    products = _values(
        incoming.get("product_types"),
        DEFAULT_DESIGN_CONTROLS["product_types"],
        allowed=set(PRODUCT_TYPES),
    )
    layouts = _values(incoming.get("layouts"), DEFAULT_DESIGN_CONTROLS["layouts"])
    return {
        "enabled": _bool(incoming.get("enabled"), bool(DEFAULT_DESIGN_CONTROLS["enabled"])),
        "preview_only": _bool(incoming.get("preview_only"), bool(DEFAULT_DESIGN_CONTROLS["preview_only"])),
        "limit_per_week": max(0, min(20, limit)),
        "categories": categories,
        "styles": styles,
        "product_types": products,
        "layouts": layouts,
    }


def settings_to_design_controls(settings_obj: Any) -> dict[str, Any]:
    if settings_obj is None:
        return normalize_design_controls()
    return normalize_design_controls(
        {
            "enabled": getattr(settings_obj, "printify_shirt_designer_enabled", None),
            "preview_only": getattr(settings_obj, "printify_shirt_designer_preview_only", None),
            "limit_per_week": getattr(settings_obj, "printify_shirt_designer_limit_per_week", None),
            "categories": getattr(settings_obj, "printify_shirt_designer_categories", None),
            "styles": getattr(settings_obj, "printify_shirt_designer_styles", None),
            "product_types": getattr(settings_obj, "printify_shirt_designer_product_types", None),
            "layouts": getattr(settings_obj, "printify_shirt_designer_layouts", None),
        }
    )


def design_controls_to_env(controls: dict[str, Any]) -> dict[str, str]:
    value = normalize_design_controls(controls)
    return {
        ENV_KEYS["enabled"]: "true" if value["enabled"] else "false",
        ENV_KEYS["preview_only"]: "true" if value["preview_only"] else "false",
        ENV_KEYS["limit_per_week"]: str(value["limit_per_week"]),
        ENV_KEYS["categories"]: ",".join(value["categories"]),
        ENV_KEYS["styles"]: ",".join(value["styles"]),
        ENV_KEYS["product_types"]: ",".join(value["product_types"]),
        ENV_KEYS["layouts"]: ",".join(value["layouts"]),
    }


def _pack_controls(pack_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(pack_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("designer_controls")
    if not isinstance(raw, dict):
        raw = payload.get("designer")
    return raw if isinstance(raw, dict) else {}


def load_design_controls(*, settings_obj: Any = None, pack_path: Path | None = None) -> dict[str, Any]:
    """Load the same controls locally and on GitHub.

    A local dotenv-backed Settings object is authoritative on the laptop. On
    the headless worker there is no dotenv, so the synced pack is authoritative;
    explicit process environment values always win (useful for a one-off run).
    """
    path = pack_path or PACK_PATH
    controls = normalize_design_controls(_pack_controls(path))
    env_file_exists = (ROOT / ".env").is_file()
    if settings_obj is None and env_file_exists:
        try:
            from app.config import settings as settings_obj  # type: ignore
        except Exception:
            settings_obj = None
    if settings_obj is not None and env_file_exists:
        controls = settings_to_design_controls(settings_obj)
    explicit_env: dict[str, Any] = {}
    for field, env_key in ENV_KEYS.items():
        if env_key in os.environ:
            explicit_env[field] = os.environ.get(env_key)
    if explicit_env:
        merged = dict(controls)
        merged.update(explicit_env)
        controls = normalize_design_controls(merged)
    return controls


def controls_for_pack(settings_obj: Any = None) -> dict[str, Any]:
    """Build controls for a new worker pack without leaking secrets."""
    if settings_obj is None:
        try:
            from app.config import settings as settings_obj  # type: ignore
        except Exception:
            settings_obj = None
    if settings_obj is not None:
        return settings_to_design_controls(settings_obj)
    return load_design_controls()

