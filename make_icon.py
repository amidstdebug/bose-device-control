"""Generates assets/icon.ico (run once; output is committed)."""
from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((8, 8, S - 8, S - 8), radius=56, fill=(24, 27, 34, 255))
# soundbar body
d.rounded_rectangle((36, 96, S - 36, 160), radius=30, fill=(59, 130, 246, 255))
for i, x in enumerate(range(70, 190, 24)):
    h = 14 + (i % 3) * 8
    d.rounded_rectangle((x, 128 - h, x + 10, 128 + h), radius=5, fill=(15, 17, 21, 255))
# sound waves
for r in (22, 40):
    d.arc((S - 60 - r, 128 - r - 60, S - 60 + r, 128 + r - 60), -50, 50, fill=(74, 222, 128, 255), width=7)
img.save("assets/icon.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
