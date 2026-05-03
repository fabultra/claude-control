#!/bin/bash
# Genere l'icone Sekoia pour Claude Control
# Necessite : uv (pour pillow ephemere) + iconutil (macOS natif)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR=$(mktemp -d)
cd "$WORKDIR"

if ! command -v uv >/dev/null; then
    echo "uv requis. Install : curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

uv run --no-project --with pillow --quiet python3 - << 'PYEOF'
import urllib.request, math, os
from io import BytesIO
from PIL import Image, ImageDraw

LOGO_URL = "https://sekoia.ca/wp-content/uploads/2023/04/sekoia-icon-white-1.png"
req = urllib.request.Request(LOGO_URL, headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req, timeout=15) as r:
    logo_orig = Image.open(BytesIO(r.read())).convert('RGBA')
print(f"  Logo Sekoia : {logo_orig.size}")

def squircle_mask(size):
    mask = Image.new('L', (size, size), 0)
    draw = ImageDraw.Draw(mask)
    n = 5
    cx = cy = size / 2
    rx = ry = size / 2
    pts = []
    for i in range(720):
        theta = i * math.pi / 360
        ct, st = math.cos(theta), math.sin(theta)
        pts.append((
            cx + rx * math.copysign(abs(ct)**(2/n), ct),
            cy + ry * math.copysign(abs(st)**(2/n), st),
        ))
    draw.polygon(pts, fill=255)
    return mask

def create_icon(size):
    bg = Image.new('RGBA', (size, size), (0,0,0,0))
    px = bg.load()
    for y in range(size):
        t = y / size
        ts = t * t * (3 - 2 * t)
        r = int(0x2C * (1-ts) + 0x14 * ts)
        g = int(0x5F * (1-ts) + 0x30 * ts)
        b = int(0x3F * (1-ts) + 0x1F * ts)
        for x in range(size):
            px[x, y] = (r, g, b, 255)
    bg.putalpha(squircle_mask(size))
    logo_size = int(size * 0.62)
    logo = logo_orig.resize((logo_size, logo_size), Image.LANCZOS)
    overlay = Image.new('RGBA', (size, size), (0,0,0,0))
    pos = (size - logo_size) // 2
    overlay.paste(logo, (pos, pos), logo)
    accent = Image.new('RGBA', (size, size), (0,0,0,0))
    sd = ImageDraw.Draw(accent)
    sx, sy = int(size * 0.78), int(size * 0.22)
    sr = int(size * 0.05)
    for r_off in range(int(sr*1.8), 0, -1):
        alpha = max(0, int(80 * (1 - r_off / (sr*1.8))))
        sd.ellipse([sx-r_off, sy-r_off, sx+r_off, sy+r_off], fill=(0xD9, 0x77, 0x57, alpha))
    sd.ellipse([sx-sr, sy-sr, sx+sr, sy+sr], fill=(0xD9, 0x77, 0x57, 240))
    ir = int(sr * 0.4)
    sd.ellipse([sx-ir, sy-ir, sx+ir, sy+ir], fill=(255, 250, 240, 250))
    final = Image.alpha_composite(bg, overlay)
    final = Image.alpha_composite(final, accent)
    return final

os.makedirs('icon.iconset', exist_ok=True)
mappings = [(16, 'icon_16x16.png'), (32, 'icon_16x16@2x.png'),
    (32, 'icon_32x32.png'), (64, 'icon_32x32@2x.png'),
    (128, 'icon_128x128.png'), (256, 'icon_128x128@2x.png'),
    (256, 'icon_256x256.png'), (512, 'icon_256x256@2x.png'),
    (512, 'icon_512x512.png'), (1024, 'icon_512x512@2x.png')]
cache = {}
for s, name in mappings:
    if s not in cache: cache[s] = create_icon(s)
    cache[s].save(f'icon.iconset/{name}')
print("  10 PNGs generes")
PYEOF

iconutil -c icns icon.iconset -o icon.icns
mv icon.icns "$SCRIPT_DIR/icon.icns"
echo "  icon.icns -> $SCRIPT_DIR/icon.icns"

cd ~ && rm -rf "$WORKDIR"
echo "  ✓ Icone generee"
