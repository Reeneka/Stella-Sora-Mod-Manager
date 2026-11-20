"""Build icon.ico and icon.png from the "Summit Show: Scent of a Witch" art (the ghost in the top right)."""
import io
import urllib.request

from PIL import Image

SOURCE = "https://static.wikitide.net/stellasorawiki/7/70/Summit_Show_Scent_of_a_Witch.png"
BOX = (1264, 135, 1264 + 534, 135 + 534)  # square around the ghost, in the 2048x1451 original

req = urllib.request.Request(SOURCE, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=60) as r:
    art = Image.open(io.BytesIO(r.read())).convert("RGBA")

img = art.crop(BOX).resize((256, 256), Image.LANCZOS)
img.save("icon.png")
img.save("icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote icon.ico and icon.png")
