"""Draw the app icon (amber CRT eye) -> assets/app.ico.  uv run python tools/make_icon.py"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((8, 8, S - 8, S - 8), radius=56, fill=(20, 18, 15, 255), outline=(60, 52, 40, 255), width=4)
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
g = ImageDraw.Draw(glow)
c = S // 2
g.ellipse((c - 84, c - 84, c + 84, c + 84), outline=(255, 160, 50, 255), width=14)
g.ellipse((c - 34, c - 34, c + 34, c + 34), fill=(255, 190, 90, 255))
img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(10)))
d.ellipse((c - 84, c - 84, c + 84, c + 84), outline=(255, 179, 71, 255), width=10)
for a in range(0, 360, 30):          # aperture ticks
    d.arc((c - 64, c - 64, c + 64, c + 64), a, a + 18, fill=(255, 150, 40, 255), width=10)
d.ellipse((c - 30, c - 30, c + 30, c + 30), fill=(255, 205, 120, 255))
d.ellipse((c - 16, c - 22, c - 4, c - 10), fill=(255, 255, 255, 230))
scan = Image.new("RGBA", (S, S), (0, 0, 0, 0))
sd = ImageDraw.Draw(scan)
for y in range(20, S - 20, 8):       # faint scanlines
    sd.line((20, y, S - 20, y), fill=(0, 0, 0, 70), width=2)
img.alpha_composite(scan)
(ROOT / "assets").mkdir(exist_ok=True)
img.save(ROOT / "assets" / "app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save(ROOT / "assets" / "app.png")
print("ok")
