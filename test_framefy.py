"""Offline self-check: python test_framefy.py"""
import io
import os
import pathlib
import re
import tempfile

from PIL import Image

import framefy

ID = "4aawyAB9vmqN3uQ7FjRGTy"
assert framefy.ALBUM_RE.search(f"https://open.spotify.com/album/{ID}?si=abc").group(1) == ID
assert framefy.ALBUM_RE.search(f"https://open.spotify.com/intl-de/album/{ID}").group(1) == ID
assert framefy.ALBUM_RE.search(f"spotify:album:{ID}").group(1) == ID
assert not framefy.ALBUM_RE.search("periphery clear")

assert framefy.fmt_runtime(1_789_000) == "29min 49s"
assert framefy.fmt_runtime(4_208_000) == "1h 10min 8s"
assert framefy.fmt_date("2014-01-28") == "Jan 28, 2014"
assert framefy.fmt_date("2014-01") == "Jan 2014"
assert framefy.fmt_date("2014") == "2014"

env = pathlib.Path(tempfile.mkdtemp()) / ".env"
env.write_text("# comment\nFRAMEFY_TEST_A = 'abc'\nFRAMEFY_TEST_B=x=y\n")
os.environ["FRAMEFY_TEST_B"] = "kept"
framefy.load_env(env)
assert os.environ["FRAMEFY_TEST_A"] == "abc" and os.environ["FRAMEFY_TEST_B"] == "kept"

g = Image.linear_gradient("L")
covers = [Image.merge("RGB", (g, g.rotate(90), g.rotate(180))), Image.new("RGB", (640, 640), "white")]
code = Image.new("L", (640, 160))
code.paste(255, (20, 40, 620, 120))
album = {"title": "A Fairly Long Album Title: With A Subtitle", "artist": "Some Artist",
         "date": "Jan 28, 2014", "runtime": "1h 10min 8s"}

for size, (short, long) in framefy.SIZES.items():
    for orientation in ("portrait", "landscape"):
        for cover, tracks in zip(covers, ([f"Track number {i}" for i in range(40)], [])):
            img = framefy.render({**album, "tracks": tracks}, cover, code if tracks else None, size, orientation)
            assert img.size == ((long, short) if orientation == "landscape" else (short, long))

buf = io.BytesIO()
framefy.save(framefy.render({**album, "tracks": []}, covers[0]), buf, "pdf")
w, h = map(float, re.search(rb"/MediaBox \[ *0 0 ([\d.]+) ([\d.]+)", buf.getvalue()).groups())
assert abs(w - 595.3) < 1 and abs(h - 841.9) < 1, (w, h)  # A4 in points

print("ok")
