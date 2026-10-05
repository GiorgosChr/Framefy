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
import urllib.parse
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
assert framefy.options({"hide": "artists"})["hide"] == ["artists"]
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

# collections: several Spotify albums, keyless
assert framefy.mosaic([gradient] * 5).size == (1998, 1998) and framefy.mosaic([white], 100).size == (100, 100)
assert framefy.source(ID) == ID and framefy.source(f"{ID},{ID}") == (ID, ID) and framefy.source("jellyfin") == "jellyfin"
for bad in ("", "jellyfin,x", f"{ID},", ",".join([ID] * 101)):
    try:
        framefy.source(bad)
        raise AssertionError(f"accepted {bad}")
    except ValueError:
        pass
real_fetch, framefy.token = framefy.fetch, lambda: None
framefy.fetch = lambda url, *a: (embed if "/embed/" in url else page).encode() if "open.spotify" in url else wide.getvalue()
framefy.assets(ID)  # cached, like the collection below, for the UI checks further down
two = (ID, "b" * 22)
shown, img = framefy.poster(two, scale=0.1)
assert shown["title"] == "Albums" and shown["artist"] == "2 albums" and shown["tracks"] == ["An Album — Some Artist"] * 2
assert img.size == (248, 351) and framefy.collection(two)[1].size == (2000, 2000)
assert framefy.poster(two, "A5", "landscape", scale=0.1, hide=["artists", "strip"])[0]["tracks"] == ["An Album"] * 2
framefy.fetch = real_fetch


# collections: Jellyfin favourites, from a fake server
class Jelly(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        if self.headers["Authorization"] != 'MediaBrowser Token="key"':
            return self.send_error(401)
        if url.path == "/Users":
            body = json.dumps([{"Name": "Other", "Id": "u0"}, {"Name": "Me", "Id": "u1"}]).encode()
        elif url.path == "/Items" and q == {"userId": "u1", "Filters": "IsFavorite", "IncludeItemTypes": "MusicAlbum",
                                            "Recursive": "true", "SortBy": "SortName"}:
            body = json.dumps({"Items": [
                {"Name": "First", "AlbumArtist": "Band", "Id": "i1", "ImageTags": {"Primary": "t"}},
                {"Name": "No Art", "Id": "i2", "ImageTags": {}}]}).encode()
        elif url.path == "/Items/i1/Images/Primary":
            body = wide.getvalue()
        else:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


jelly = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Jelly)
threading.Thread(target=jelly.serve_forever, daemon=True).start()
os.environ.update(JELLYFIN_URL=f"http://127.0.0.1:{jelly.server_port}/", JELLYFIN_API_KEY="key", JELLYFIN_USER="me")
shown, img = framefy.poster("jellyfin", scale=0.1)
assert shown["title"] == "Favourites" and shown["artist"] == "2 albums" and shown["tracks"] == ["First — Band", "No Art"]
assert framefy.poster("jellyfin", scale=0.1, hide=["artists"])[0]["tracks"] == ["First", "No Art"]
for env, error in (({"JELLYFIN_USER": "nobody"}, ValueError), ({"JELLYFIN_API_KEY": "wrong"}, RuntimeError)):
    os.environ.update(env)
    try:
        framefy.favourites()
        raise AssertionError(f"accepted {env}")
    except error:
        pass
os.environ.update(JELLYFIN_API_KEY="key", JELLYFIN_USER="me")
try:
    framefy.parse_public(embed.replace(json.dumps(entity), "null").replace('"state"', '"status"'))
    raise AssertionError("accepted a missing album")
except ValueError:
    pass

# UI
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), framefy.UI)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"
home = urllib.request.urlopen(base + "/").read()
assert b"<title>Framefy</title>" in home and b'id="jellyfin" type="button" >' in home  # shown: JELLYFIN_URL is set
links = urllib.parse.quote(f"spotify:album:{ID} https://open.spotify.com/album/{'b' * 22} spotify:album:{ID}")
assert json.loads(urllib.request.urlopen(f"{base}/search?q={links}").read()) == {"id": f"{ID},{'b' * 22}"}
for ids, albums in ((ID, 1), (",".join(two), 2)):  # albums: the names in the UI's selected list
    assert json.loads(urllib.request.urlopen(f"{base}/album?id={ids}").read())["albums"] == ["An Album — Some Artist"] * albums
shown = json.loads(urllib.request.urlopen(base + "/album?id=jellyfin").read())
assert shown["title"] == "Favourites" and shown["albums"] == ["First — Band", "No Art"]
assert urllib.request.urlopen(base + "/poster?id=jellyfin&preview=1&hide=artists").read()[:4] == b"\x89PNG"
key = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/upload", data=wide.getvalue())).read())["upload"]
assert framefy.UPLOADS[key].size == (200, 200)
for bad in (urllib.request.Request(base + "/album?id=not-an-album"), urllib.request.Request(base + "/upload", data=b"junk")):
    try:
        urllib.request.urlopen(bad)
        raise AssertionError("bad request accepted")
    except urllib.error.HTTPError as e:
        assert e.code == 400 and "error" in json.loads(e.read())
srv.shutdown()
jelly.shutdown()

print("ok")
