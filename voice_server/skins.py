"""Screen skins for the device.

A skin is a small JSON document:
  base       "classic" (camera-lens eyes) | "circuit" (circuit-board face)
  palette    color stops for phosphor intensity 0..1, e.g. [[0,"#000000"],[0.7,"#ffa03a"],[1,"#fff0c8"]]
  eyes       optional {"lx","ly","rx","ry","r"} in the 120x120 phosphor grid
  texts      optional {"unit": "UNIT-07", "header": "VOICE INTERFACE"}
  background optional base64 of a 120x120 8-bit intensity layer (drawn as a dim static phosphor image)

Built-in skins live in code; custom ones in skins/<id>.json. The device fetches /skins/active.json.
"""
from __future__ import annotations

import base64
import io
import json
import re
import time
from pathlib import Path

from .config import ROOT

SKINS_DIR = ROOT / "skins"
LW = LH = 120

PALETTES = {
    "amber": [[0, "#000000"], [0.3, "#4a2000"], [0.72, "#ffa03a"], [1, "#fff2cf"]],
    "green": [[0, "#000000"], [0.35, "#06380c"], [0.75, "#46f064"], [1, "#eaffea"]],
    "ice": [[0, "#000000"], [0.35, "#00263f"], [0.75, "#3cc8ff"], [1, "#e8fbff"]],
    "aurora": [[0, "#000000"], [0.3, "#0b1640"], [0.65, "#2e7bff"], [0.85, "#7fe3ff"], [1, "#ffffff"]],
    "mint": [[0, "#000000"], [0.35, "#003d33"], [0.8, "#2dffd2"], [1, "#eafffa"]],
    "lime": [[0, "#000000"], [0.4, "#1f4000"], [0.8, "#9dff3a"], [1, "#f4ffe0"]],
    "gold": [[0, "#000000"], [0.35, "#3a2800"], [0.75, "#ffc83a"], [1, "#fff8de"]],
    "sunset": [[0, "#000000"], [0.3, "#3a0a00"], [0.7, "#ff6a00"], [1, "#ffe0a0"]],
    "crimson": [[0, "#000000"], [0.35, "#3d0008"], [0.75, "#ff3b4e"], [1, "#ffe0e4"]],
    "magenta": [[0, "#000000"], [0.35, "#3a0030"], [0.75, "#ff3ad2"], [1, "#ffe6fa"]],
    "violet": [[0, "#000000"], [0.35, "#22083f"], [0.75, "#b46bff"], [1, "#f4e8ff"]],
    "steel": [[0, "#000000"], [0.35, "#1a2633"], [0.75, "#9fd2ff"], [1, "#ffffff"]],
    "white": [[0, "#000000"], [0.4, "#2a2a2a"], [0.8, "#d8d8d8"], [1, "#ffffff"]],
}
PALETTE_NAMES = {"amber": "янтарный", "green": "зелёный", "ice": "ледяной", "aurora": "аврора", "mint": "мятный",
                 "lime": "лайм", "gold": "золотой", "sunset": "закат", "crimson": "алый", "magenta": "пурпур",
                 "violet": "фиолетовый", "steel": "сталь", "white": "белый"}
# base id -> (name, one-line description, default palette). Order = gallery order.
BASES = {
    "classic": ("Объективы", "ретро-фантастика: ЭЛТ-люминофор, механические диафрагмы", "amber"),
    "circuit": ("Схема", "лицо робота из дорожек печатной платы", "green"),
    "pixel": ("8-бит", "пиксельные глаза из старой игры", "lime"),
    "hud": ("Голограмма", "ядро ИИ: вращающиеся кольца, радар, волна голоса", "ice"),
    "orb": ("Сфера", "современный ассистент: живой светящийся шар", "aurora"),
    "pet": ("Питомец", "мягкие выразительные глаза робота-питомца", "mint"),
    "minimal": ("Минимал", "две точки и линия, которая становится голосом", "white"),
    "eye": ("Глаз", "реалистичный глаз с радужкой, бликами и ресницами", "steel"),
}
BASE_NAMES = {k: v[0] for k, v in BASES.items()}


def builtin_skins() -> list[dict]:
    return [{"id": base, "name": name, "desc": desc, "base": base, "palette": PALETTES[pal], "palette_name": pal,
             "builtin": True} for base, (name, desc, pal) in BASES.items()]


def _legacy(skin_id: str) -> dict | None:
    """Old ids like 'circuit-green' = base + palette."""
    base, _, pal = skin_id.partition("-")
    if base in BASES and pal in PALETTES:
        s = next(b for b in builtin_skins() if b["id"] == base)
        return dict(s, id=skin_id, palette=PALETTES[pal], palette_name=pal)
    return None


def _slug(name: str) -> str:
    tr = str.maketrans("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                       "abvgdeezziiklmnoprstufhccss_y_eua")
    s = re.sub(r"[^a-z0-9]+", "-", name.lower().translate(tr)).strip("-")
    return s or f"skin-{int(time.time())}"


def list_skins() -> list[dict]:
    skins = builtin_skins()
    SKINS_DIR.mkdir(exist_ok=True)
    for f in sorted(SKINS_DIR.glob("*.json")):
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
            s["builtin"] = False
            skins.append(s)
        except (OSError, ValueError):
            continue
    return skins


def get_skin(skin_id: str) -> dict | None:
    return next((s for s in list_skins() if s["id"] == skin_id), None) or _legacy(skin_id)


def resolve_palette(p) -> list | None:
    """A palette given by name or as stops; None = keep the skin's own."""
    if not p:
        return None
    if isinstance(p, str):
        return PALETTES.get(p)
    return validate({"palette": p})["palette"]


def validate(skin: dict) -> dict:
    """Normalize a user-provided skin; raises ValueError with a readable message."""
    base = skin.get("base", "classic")
    if base not in BASE_NAMES:
        raise ValueError("неизвестная основа скина: " + ", ".join(BASE_NAMES))
    stops = skin.get("palette") or PALETTES["amber"]
    if isinstance(stops, str):
        stops = PALETTES.get(stops) or PALETTES["amber"]
    clean = []
    for pos, color in stops:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValueError(f"цвет {color} не в формате #rrggbb")
        clean.append([max(0.0, min(1.0, float(pos))), color.lower()])
    clean.sort(key=lambda s: s[0])
    if len(clean) < 2:
        raise ValueError("в палитре нужно минимум 2 цвета")
    out = {"id": skin.get("id") or _slug(skin.get("name", "skin")), "name": (skin.get("name") or "Мой скин")[:40],
           "base": base, "palette": clean}
    if skin.get("eyes"):
        e = skin["eyes"]
        out["eyes"] = {k: max(0, min(119, int(e[k]))) for k in ("lx", "ly", "rx", "ry")} | \
                      {"r": max(8, min(30, int(e.get("r", 18))))}
    t = skin.get("texts") or {}
    texts = {k: str(t[k])[:24] for k in ("unit", "header") if t.get(k)}
    if texts:
        out["texts"] = texts
    if isinstance(skin.get("fx"), dict):
        fx = {k: float(v) for k, v in skin["fx"].items() if k in ("scan", "noise", "flicker", "vign", "smooth", "decay")}
        if fx:
            out["fx"] = fx
    if skin.get("background"):
        raw = base64.b64decode(skin["background"])
        if len(raw) != LW * LH:
            raise ValueError("фон должен быть 120x120")
        out["background"] = skin["background"]
    if out["id"] in {s["id"] for s in builtin_skins()} or _legacy(out["id"]):
        out["id"] = out["id"] + "-custom"
    return out


def save_skin(skin: dict) -> dict:
    skin = validate(skin)
    SKINS_DIR.mkdir(exist_ok=True)
    skin["updated"] = int(time.time())
    (SKINS_DIR / f"{skin['id']}.json").write_text(json.dumps(skin, ensure_ascii=False), encoding="utf-8")
    return skin


def delete_skin(skin_id: str) -> bool:
    p = SKINS_DIR / f"{skin_id}.json"
    if p.exists() and p.resolve().parent == SKINS_DIR.resolve():
        p.unlink()
        return True
    return False


def image_to_background(data: bytes, brightness: float = 0.45, invert: str = "auto") -> str:
    """Any picture -> 120x120 phosphor layer (base64). Bright = glowing."""
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(data))
    if img.mode in ("RGBA", "LA") or "transparency" in img.info:   # transparent areas = dark
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
        bg.alpha_composite(rgba)
        img = bg
    g = ImageOps.fit(img.convert("L"), (LW, LH), Image.Resampling.LANCZOS)
    mean = sum(g.getdata()) / (LW * LH)
    if invert == "yes" or (invert == "auto" and mean > 140):   # dark drawing on white paper
        g = ImageOps.invert(g)
    g = ImageOps.autocontrast(g, cutoff=1)
    k = max(0.05, min(1.0, brightness))
    return base64.b64encode(bytes(int(v * k) for v in g.getdata())).decode()


def palette_lut(stops) -> list[tuple[int, int, int]]:
    """256-entry RGB lookup by linear interpolation between stops."""
    pts = [(p, tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))) for p, c in stops]
    out = []
    for i in range(256):
        f = i / 255
        for (p0, c0), (p1, c1) in zip(pts, pts[1:]):
            if f <= p1:
                t = 0 if p1 == p0 else (f - p0) / (p1 - p0)
                out.append(tuple(round(c0[k] + (c1[k] - c0[k]) * max(0, t)) for k in range(3)))
                break
        else:
            out.append(pts[-1][1])
    return out


def to_device(skin: dict, palette_override=None) -> dict:
    """Compact form the firmware parses: palette as 256 RGB565 values in hex."""
    lut = palette_lut(resolve_palette(palette_override) or skin["palette"])
    rgb565 = "".join(f"{((r & 0xf8) << 8) | ((g & 0xfc) << 3) | (b >> 3):04x}" for r, g, b in lut)
    out = {"id": skin["id"], "base": skin["base"], "palette565": rgb565}
    for k in ("eyes", "texts", "background", "fx"):
        if skin.get(k):
            out[k] = skin[k]
    return out
