"""Offline self-check: python test_framefy.py"""
import http.server
import io
import json
import os
import pathlib
import re
import tempfile
import threading
import urllib.error
import urllib.request

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

# options
o = framefy.options({"bg": "#ECEBE6", "fg": "000000", "hide": "tracks,code", "columns": "2", "clean": "1"})
assert o["bg"] == (236, 235, 230) and o["fg"] == (0, 0, 0) and o["hide"] == ["tracks", "code"]
assert o["columns"] == 2 and o["clean"] and not o["durations"] and o["cover"] == "fade"
for bad in ({"bg": "red"}, {"hide": "everything"}, {"columns": "9"}, {"font": "comic"}, {"bleed": "50"}):
    try:
        framefy.options(bad)
        raise AssertionError(f"accepted {bad}")
    except ValueError:
        pass

for name, want in (("Hold You Under (feat. Marcus Bridge)", "Hold You Under"), ("Song - Remastered 2011", "Song"),
                   ("Song (2011 Remaster)", "Song"), ("Song [ft. X] (Live)", "Song (Live)"), ("Remix", "Remix")):
    assert framefy.clean(name) == want, (name, framefy.clean(name))

# rendering: every size and orientation, with the defaults and with everything switched on
g = Image.linear_gradient("L")
gradient, white = Image.merge("RGB", (g, g.rotate(90), g.rotate(180))), Image.new("RGB", (640, 640), "white")
code = Image.new("L", (640, 160))
code.paste(255, (20, 40, 620, 120))
album = {"title": "A Fairly Long Album Title: With A Subtitle", "artist": "Some Artist", "date": "Jan 28, 2014",
         "runtime": "1h 10min 8s", "tracks": [f"Track number {i} (feat. X)" for i in range(40)],
         "durations": [200_000] * 40}
busy = framefy.options({"hide": "date,strip", "durations": "1", "clean": "1", "columns": "3", "cover": "framed",
                        "layout": "cover-right", "font": "playfair", "weight": "black", "caption": "A caption",
                        "title": "Other", "bg": "101010", "fg": "f0f0f0"})
for size, (short, long) in framefy.SIZES.items():
    for orientation in ("portrait", "landscape"):
        scale = 350 / long  # small renders keep this quick; one full-size render follows
        for img in (framefy.render(album, gradient, code, size, orientation, gradient, scale, **busy),
                    framefy.render({**album, "tracks": []}, white, None, size, orientation, scale=scale)):
            assert abs(img.width - round((long if orientation == "landscape" else short) * scale)) <= 1, img.size
for cover in framefy.FADES:
    for layout in framefy.LAYOUTS:
        framefy.render(album, gradient, code, "LETTER", "landscape", scale=0.1, cover=cover, layout=layout)
assert framefy.render(album, gradient, code, "A4").size == (2480, 3508)
assert framefy.render(album, gradient, code, "A4", bleed=3).size == (2480 + 70, 3508 + 70)  # 3 mm = 35 px a side

buf = io.BytesIO()
framefy.save(framefy.render({**album, "tracks": []}, gradient), buf, "pdf")
w, h = map(float, re.search(rb"/MediaBox \[ *0 0 ([\d.]+) ([\d.]+)", buf.getvalue()).groups())
assert abs(w - 595.3) < 1 and abs(h - 841.9) < 1, (w, h)  # A4 in points

wide = io.BytesIO()
Image.new("RGB", (300, 200), "red").save(wide, "PNG")
assert framefy.own_cover(io.BytesIO(wide.getvalue())).size == (200, 200)

# keyless album parsing
entity = {"id": ID, "name": "An Album", "subtitle": "Some Artist",
          "trackList": [{"title": "One", "duration": 60_000}, {"title": "Two", "duration": 61_000}],
          "visualIdentity": {"image": [{"url": "small", "maxWidth": 64}, {"url": "big", "maxWidth": 640}]}}
embed = ('<script id="__NEXT_DATA__" type="application/json">%s</script>'
         % json.dumps({"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}))
page = ('<meta name="music:musician" content="https://open.spotify.com/artist/%s"/>'
        '<meta name="music:release_date" content="2016-01-29"/>' % ("a" * 22))
assert framefy.parse_public(embed, page) == {
    "id": ID, "title": "An Album", "artist": "Some Artist", "artist_id": "a" * 22, "date": "Jan 29, 2016",
    "runtime": "2min 1s", "tracks": ["One", "Two"], "durations": [60_000, 61_000], "cover_url": "big"}

# UI
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), framefy.UI)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"
assert b"<title>Framefy</title>" in urllib.request.urlopen(base + "/").read()
key = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/upload", data=wide.getvalue())).read())["upload"]
assert framefy.UPLOADS[key].size == (200, 200)
for bad in (urllib.request.Request(base + "/album?id=not-an-album"), urllib.request.Request(base + "/upload", data=b"junk")):
    try:
        urllib.request.urlopen(bad)
        raise AssertionError("bad request accepted")
    except urllib.error.HTTPError as e:
        assert e.code == 400 and "error" in json.loads(e.read())
srv.shutdown()

print("ok")
