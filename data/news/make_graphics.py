"""NDCConnect announcement graphics (1600x900 PNG, no photos)."""
import sys
from PIL import Image, ImageDraw, ImageFont

OUT = sys.argv[1]
W, H = 1600, 900
BLUE, DEEP, RED, WHITE = (46, 43, 125), (30, 28, 88), (200, 16, 46), (255, 255, 255)
SOFT = (196, 196, 232)
F = "/usr/share/fonts/opentype/inter/Inter-{}.otf"
def font(w, s): return ImageFont.truetype(F.format(w), s)

def base(kicker, title_lines, sub):
    im = Image.new("RGB", (W, H), BLUE)
    d = ImageDraw.Draw(im)
    # soft diagonal band
    d.polygon([(1080, 0), (W, 0), (W, H), (930, H)], fill=DEEP)
    d.rectangle([0, 0, W, 14], fill=RED)
    # wordmark
    d.rounded_rectangle([90, 80, 150, 140], 14, fill=RED)
    d.text((120, 110), "N", font=font("Bold", 40), fill=WHITE, anchor="mm")
    d.text((170, 110), "NDCConnect", font=font("Bold", 40), fill=WHITE, anchor="lm")
    d.text((90, 250), kicker.upper(), font=font("SemiBold", 30), fill=SOFT)
    y = 300
    for ln in title_lines:
        d.text((90, y), ln, font=font("ExtraBold", 84), fill=WHITE)
        y += 100
    d.rectangle([90, y + 30, 230, y + 40], fill=RED)
    d.text((90, y + 75), sub, font=font("Regular", 34), fill=SOFT)
    return im, d

# 1 welcome: connected nodes
im, d = base("Now live", ["Politics beyond", "election day"], "Follow candidates · join your community · volunteer")
cx, cy = 1250, 450
pts = [(cx, cy), (cx - 170, cy - 170), (cx + 170, cy - 150), (cx - 190, cy + 150), (cx + 160, cy + 180), (cx, cy - 290), (cx + 10, cy + 300)]
for p in pts[1:]:
    d.line([pts[0], p], fill=SOFT, width=6)
for a, b in [(1, 5), (2, 5), (3, 6), (4, 6), (1, 3), (2, 4)]:
    d.line([pts[a], pts[b]], fill=(110, 106, 190), width=4)
for i, (x, y) in enumerate(pts):
    r = 70 if i == 0 else 38
    d.ellipse([x - r, y - r, x + r, y + r], fill=RED if i == 0 else WHITE, outline=WHITE, width=6)
im.save(f"{OUT}/welcome.png", optimize=True)

# 2 PVC: a generic card (no INEC marks)
im, d = base("Before 16 January 2027", ["Verify your PVC.", "Collect your card."], "Collection returns to the wards from 1 December")
x0, y0 = 1010, 270
d.rounded_rectangle([x0 + 18, y0 + 22, x0 + 498, y0 + 352], 28, fill=(20, 18, 60))
d.rounded_rectangle([x0, y0, x0 + 480, y0 + 330], 28, fill=WHITE)
d.rounded_rectangle([x0, y0, x0 + 480, y0 + 70], 28, fill=RED)
d.rectangle([x0, y0 + 40, x0 + 480, y0 + 70], fill=RED)
d.text((x0 + 30, y0 + 35), "VOTER CARD", font=font("Bold", 30), fill=WHITE, anchor="lm")
d.rounded_rectangle([x0 + 30, y0 + 100, x0 + 150, y0 + 250], 14, fill=SOFT)
d.ellipse([x0 + 62, y0 + 120, x0 + 118, y0 + 176], fill=BLUE)
d.pieslice([x0 + 45, y0 + 180, x0 + 135, y0 + 290], 180, 360, fill=BLUE)
for i, w in enumerate([260, 220, 240, 160]):
    d.rounded_rectangle([x0 + 180, y0 + 110 + i * 40, x0 + 180 + w, y0 + 128 + i * 40], 9, fill=(220, 220, 236))
d.text((x0 + 180, y0 + 285), "VIN •••• ••••", font=font("SemiBold", 26), fill=BLUE, anchor="lm")
# tick badge
bx, by = x0 + 450, y0 + 320
d.ellipse([bx - 70, by - 70, bx + 70, by + 70], fill=(22, 128, 61), outline=WHITE, width=8)
d.line([(bx - 32, by + 2), (bx - 8, by + 28), (bx + 36, by - 26)], fill=WHITE, width=14, joint="curve")
im.save(f"{OUT}/pvc.png", optimize=True)

# 3 Settings: light/dark panels, Aa, toggle
im, d = base("New in Settings", ["Dark mode, larger", "text, more control"], "Accessibility · Notifications · Profile · Privacy")
x0, y0 = 960, 230
d.rounded_rectangle([x0, y0, x0 + 260, y0 + 420], 26, fill=WHITE)
d.rounded_rectangle([x0 + 220, y0 + 40, x0 + 500, y0 + 460], 26, fill=(18, 18, 30), outline=SOFT, width=4)
for i in range(4):
    d.rounded_rectangle([x0 + 30, y0 + 60 + i * 60, x0 + 200 - i * 20, y0 + 80 + i * 60], 10, fill=(220, 220, 236))
    d.rounded_rectangle([x0 + 260, y0 + 120 + i * 60, x0 + 450 - i * 25, y0 + 140 + i * 60], 10, fill=(70, 70, 100))
d.text((x0 + 260, y0 + 65), "Aa", font=font("ExtraBold", 44), fill=WHITE)
d.text((x0 + 380, y0 + 72), "Aa", font=font("Bold", 30), fill=SOFT)
tx, ty = x0 + 270, y0 + 380
d.rounded_rectangle([tx, ty, tx + 120, ty + 56], 28, fill=RED)
d.ellipse([tx + 68, ty + 6, tx + 112, ty + 50], fill=WHITE)
# moon
mx, my = x0 + 440, y0 + 400
d.ellipse([mx - 34, my - 34, mx + 34, my + 34], fill=(250, 220, 120))
d.ellipse([mx - 16, my - 44, mx + 48, my + 20], fill=(18, 18, 30))
im.save(f"{OUT}/settings.png", optimize=True)
print("ok")
