#!/usr/bin/env python3
"""Make a poster from a Spotify album."""
import argparse
import base64
import calendar
import functools
import http.server
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

API = "https://api.spotify.com/v1"
ALBUM_RE = re.compile(r"(?:open\.spotify\.com/(?:intl-\w+/)?album/|spotify:album:)([A-Za-z0-9]{22})")
FONT = str(Path(__file__).parent / "fonts" / "Montserrat.ttf")
DPI = 300
SIZES = {"A4": (2480, 3508), "A3": (3508, 4961)}  # short, long side in px at 300 dpi
ORIENTATIONS = ("portrait", "landscape")
FORMATS = ("png", "pdf")
UA = {"User-Agent": "Mozilla/5.0"}  # Spotify's public pages refuse the default urllib agent


# --- Spotify ---

def fetch(url, headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def load_env(path=Path(__file__).parent / ".env"):
    """Read KEY=value lines into the environment; variables already set win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_token = (None, 0.0)  # value, time it stops being valid


def token():
    """Spotify API token, or None when .env holds no working credentials (keyless mode)."""
    global _token
    if time.time() < _token[1]:
        return _token[0]
    load_env()
    cid, secret = os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
    _token = (None, time.time() + 3600)
    if cid and secret:
        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        try:
            body = json.loads(fetch("https://accounts.spotify.com/api/token",
                                    {"Authorization": f"Basic {auth}"}, b"grant_type=client_credentials"))
            _token = (body["access_token"], time.time() + body["expires_in"] - 60)
        except urllib.error.HTTPError:
            print("warning: Spotify rejected the credentials in .env, continuing without them", file=sys.stderr)
    return _token[0]


def api(path, tok):
    url = path if path.startswith("http") else API + path
    return json.loads(fetch(url, {"Authorization": f"Bearer {tok}"}))


def search(query, tok, offset=0):
    q = urllib.parse.urlencode({"q": query, "type": "album", "limit": 10, "offset": offset})
    return [{"id": a["id"], "title": a["name"], "artist": ", ".join(x["name"] for x in a["artists"]),
             "year": a["release_date"][:4]} for a in api(f"/search?{q}", tok)["albums"]["items"] if a]


def pick(query, tok):
    """Browse search results ten at a time in the terminal and return the chosen album id."""
    offset = 0
    while True:
        items = search(query, tok, offset)
        if not items:
            if offset == 0:
                sys.exit("No albums found.")
            print("No more results.")
            offset -= 10
            continue
        for i, a in enumerate(items, 1):
            print(f"{i:2}. {a['title']} — {a['artist']} ({a['year']})")
        ans = input("Number to pick, n next, p previous, q quit: ").strip().lower()
        if ans == "q":
            sys.exit()
        elif ans == "n":
            offset += 10
        elif ans == "p":
            offset = max(0, offset - 10)
        elif ans.isdigit() and 1 <= int(ans) <= len(items):
            return items[int(ans) - 1]["id"]


def fmt_date(d):
    """'2014-01-28' -> 'Jan 28, 2014'; also takes '2014-01' and '2014'."""
    p = d.split("-")
    if len(p) == 1:
        return p[0]
    month = calendar.month_abbr[int(p[1])]
    return f"{month} {int(p[2])}, {p[0]}" if len(p) == 3 else f"{month} {p[0]}"


def fmt_runtime(ms):
    s = round(ms / 1000)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return (f"{h}h " if h else "") + f"{m}min {s}s"


def load_album(album_id, tok):
    a = api(f"/albums/{album_id}", tok)
    tracks, page = [], a["tracks"]
    while True:
        tracks += page["items"]
        if not page.get("next"):
            break
        page = api(page["next"], tok)
    return {
        "id": a["id"],
        "title": a["name"],
        "artist": ", ".join(x["name"] for x in a["artists"]),
        "artist_id": a["artists"][0]["id"],
        "date": fmt_date(a["release_date"]),
        "runtime": fmt_runtime(sum(t["duration_ms"] for t in tracks)),
        "tracks": [t["name"] for t in tracks],
        "cover_url": a["images"][0]["url"],
    }


def parse_public(embed_html, album_html):
    """Same dictionary as load_album, read from Spotify's public embed and album pages (no key)."""
    # ponytail: unofficial; breaks if Spotify changes these pages, and the embed may cut very long albums
    data = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', embed_html, re.S)
    if not data:
        raise RuntimeError("Spotify's public page has changed; add credentials to .env instead.")
    e = json.loads(data.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    date = re.search(r'name="music:release_date" content="([\d-]+)"', album_html)
    artist = re.search(r'name="music:musician" content="[^"]*/artist/(\w{22})"', album_html)
    return {
        "id": e["id"],
        "title": e["name"],
        "artist": e["subtitle"],
        "artist_id": artist and artist.group(1),
        "date": fmt_date(date.group(1)) if date else "",
        "runtime": fmt_runtime(sum(t["duration"] for t in e["trackList"])),
        "tracks": [t["title"] for t in e["trackList"]],
        "cover_url": max(e["visualIdentity"]["image"], key=lambda i: i["maxWidth"])["url"],
    }


def load_image(url):
    # ponytail: undocumented larger variants of Spotify image URLs; falls back if Spotify drops them
    big = url.replace("ab67616d0000b273", "ab67616d000082c1").replace("ab67616100005174", "ab6761610000e5eb")
    try:
        return Image.open(io.BytesIO(fetch(big))).convert("RGB")
    except OSError:
        return Image.open(io.BytesIO(fetch(url))).convert("RGB")


def load_code(album_id):
    """Spotify scan code as a mask (white = ink), or None if it cannot be fetched."""
    url = f"https://scannables.scdn.co/uri/plain/png/000000/white/1280/spotify:album:{album_id}"
    try:
        return Image.open(io.BytesIO(fetch(url))).convert("L")
    except OSError:
        print("warning: could not fetch the Spotify code, leaving it out", file=sys.stderr)


@functools.lru_cache(maxsize=8)  # a cover is ~12 MB in memory
def assets(album_id):
    """Album details, cover and scan code: from the API with a key, from public pages without."""
    tok, album = token(), None
    if tok:
        try:
            album = load_album(album_id, tok)
        except urllib.error.HTTPError as e:
            print(f"warning: Spotify API error {e.code}, using the public pages", file=sys.stderr)
    if not album:
        album = parse_public(*(fetch(f"https://open.spotify.com/{p}album/{album_id}", UA).decode()
                               for p in ("embed/", "")))
    return album, load_image(album["cover_url"]), load_code(album_id)


@functools.lru_cache(maxsize=8)
def artist_image(artist_id):
    tok = token()
    if tok:
        images = api(f"/artists/{artist_id}", tok)["images"]
        url = images[0]["url"] if images else ""
    else:
        url = json.loads(fetch(f"https://open.spotify.com/oembed?url=spotify:artist:{artist_id}", UA)).get("thumbnail_url")
    return load_image(url) if url else None


# --- Rendering ---

@functools.lru_cache(maxsize=None)
def font(size, weight):
    f = ImageFont.truetype(FONT, max(1, round(size)))
    f.set_variation_by_axes([weight])
    return f


def lum(c):
    r, g, b = ((v / 255) ** 2.2 for v in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def palette(cover):
    """Background, text and three strip colours taken from the cover."""
    # ponytail: simple heuristic, no taste involved; add --bg/--text overrides if it picks badly
    raw = cover.convert("RGB").resize((64, 64)).tobytes()
    px = list(zip(raw[0::3], raw[1::3], raw[2::3]))
    px = [p for p in px if min(p) <= 250] or px  # near-white would otherwise win on most covers
    q = Image.new("RGB", (len(px), 1))
    q.putdata(px)
    q = q.quantize(8)
    pal = q.getpalette()
    cols = [tuple(pal[i * 3:i * 3 + 3]) for _, i in sorted(q.getcolors(), reverse=True)]
    bg = cols[0]
    # text: the most colourful palette entry, pushed towards black or white until readable on bg
    base = max(cols, key=lambda c: max(c) - min(c))
    target = (0, 0, 0) if lum(bg) > 0.18 else (255, 255, 255)
    for t in range(21):
        fg = mix(base, target, t / 20)
        if contrast(fg, bg) >= 4.5:
            break
    strip = sorted(cols[1:], key=lambda c: contrast(c, bg), reverse=True)  # ones that show up on bg first
    return bg, fg, (strip + [fg] * 3)[:3]


def wrap(d, text, f, max_w):
    lines = [""]
    for word in text.split():
        trial = f"{lines[-1]} {word}".strip()
        if not lines[-1] or d.textlength(trial, font=f) <= max_w:
            lines[-1] = trial
        else:
            lines.append(word)
    return lines


def fit(d, text, weight, max_size, max_w, max_lines, u):
    """Largest size (in layout units) at which text fits max_w within max_lines."""
    for size in range(max_size, 19, -1):
        f = font(size * u, weight)
        lines = wrap(d, text, f, max_w)
        if len(lines) <= max_lines and all(d.textlength(l, font=f) <= max_w for l in lines):
            break
    return f, size, lines


def draw_tracks(d, tracks, x, y, max_w, bottom, u, fill):
    # ponytail: one very long track name shrinks the whole list; truncate with an ellipsis if that looks bad
    names = [f"{i}. {t}" for i, t in enumerate(tracks, 1)]
    for size in range(27, 9, -1):
        f, pitch = font(size * u, 700), size * 37 / 27 * u
        rows = max(1, int((bottom - y) / pitch) + 1)
        cols = [names[i:i + rows] for i in range(0, len(names), rows)]
        widths = [max(d.textlength(n, font=f) for n in c) for c in cols]
        if sum(widths) + 30 * u * (len(cols) - 1) <= max_w:
            break
    for c, w in zip(cols, widths):
        for r, n in enumerate(c):
            d.text((x, y + r * pitch), n, fill, f, anchor="ls")
        x += w + 30 * u


def render(album, cover, code=None, size="A4", orientation="portrait", artist_img=None):
    """Layout numbers are in units of short side / 1414, measured off the reference posters."""
    short, long = SIZES[size]
    land = orientation == "landscape"
    W, H = (long, short) if land else (short, long)
    u = short / 1414
    bg, fg, strip = palette(cover)
    muted = mix(fg, bg, 0.35)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    # square cover on the short side, fading into the background towards the text
    start, end = (0.70, 0.88) if land else (0.75, 0.92)
    ramp = [min(1, max(0, (end - i / short) / (end - start))) for i in range(short)]
    mask = Image.new("L", (1, short))
    mask.putdata([round(255 * t * t * (3 - 2 * t)) for t in ramp])
    mask = mask.resize((short, short))
    if land:
        mask = mask.transpose(Image.Transpose.TRANSPOSE)
    img.paste(cover.convert("RGB").resize((short, short), Image.Resampling.LANCZOS), (0, 0), mask)

    x0 = (1360 if land else 92) * u
    max_w = W - 92 * u - x0
    foot = (1188 if land else 1886) * u  # baseline of the "Release date" label

    f, s, lines = fit(d, album["title"], 700, 113, max_w, 3 if land else 1, u)
    last = (92 + 0.72 * s + (len(lines) - 1) * 1.1 * s if land else 1425) * u
    for i, line in enumerate(lines):
        d.text((x0, last - (len(lines) - 1 - i) * 1.1 * s * u), line, fg, f, anchor="ls")
    ax = x0
    if artist_img:  # round profile picture in front of the artist name
        dia = round(64 * u)
        disc = Image.new("L", (dia * 4, dia * 4))
        ImageDraw.Draw(disc).ellipse((0, 0, dia * 4 - 1, dia * 4 - 1), fill=255)
        img.paste(ImageOps.fit(artist_img.convert("RGB"), (dia, dia)),
                  (round(x0), round(last + 61 * u - dia / 2)), disc.resize((dia, dia)))
        ax += dia + 18 * u
    f, _, lines = fit(d, album["artist"], 700, 61, max_w - (ax - x0), 1, u)
    d.text((ax, last + 82 * u), " ".join(lines), fg, f, anchor="ls")
    draw_tracks(d, album["tracks"], x0, last + 155 * u, max_w, foot - 76 * u, u, fg)

    x = x0
    for label, value in (("Release date", album["date"]), ("Runtime", album["runtime"])):
        d.text((x, foot), label, fg, font(38 * u, 700), anchor="ls")
        d.text((x, foot + 46 * u), value, muted, font(33 * u, 700), anchor="ls")
        x += d.textlength(label, font=font(38 * u, 700)) + 59 * u

    # scan code with the palette strip under it: bottom right in portrait, under the dates in landscape
    cx, cy = (x0, foot + 74 * u) if land else (W - (92 + 248) * u, foot - 37 * u)
    if code:
        code = code.crop(code.getbbox())
        code = code.resize((round(248 * u), round(248 * u * code.height / code.width)), Image.Resampling.LANCZOS)
        img.paste(fg, (round(cx), round(cy + (62 * u - code.height) / 2)), code)
    for i, c in enumerate(strip):
        d.rectangle([cx + i * 248 * u / 3, cy + 71 * u, cx + (i + 1) * 248 * u / 3, cy + 88 * u], fill=c)
    return img


def save(img, out, fmt):
    # ponytail: the PDF is a raster page, not vector text; use reportlab if print shops complain
    if fmt == "pdf":
        img.save(out, "PDF", resolution=DPI)
    else:
        img.save(out, "PNG", dpi=(DPI, DPI))


def poster(album_id, size="A4", orientation="portrait", artist=False):
    album, cover, code = assets(album_id)
    pic = artist_image(album["artist_id"]) if artist and album["artist_id"] else None
    return album, render(album, cover, code, size, orientation, pic)


def file_name(album, fmt):
    return re.sub(r'[\\/:*?"<>|]', "_", f"{album['artist']} - {album['title']}") + f".{fmt}"


# --- UI ---

def valid_id(s):
    if not re.fullmatch(r"[A-Za-z0-9]{22}", s or ""):
        raise ValueError("That is not a Spotify album.")
    return s


RENDERING = threading.Lock()  # one poster at a time: an A3 render needs ~150 MB


class UI(http.server.BaseHTTPRequestHandler):
    # ponytail: stdlib server with no rate limiting, fine for a hobby site; move to gunicorn if it gets real traffic
    def reply(self, body, ctype, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}")
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, status=200):
        self.reply(json.dumps(obj).encode(), "application/json", status)

    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if url.path == "/":
                self.reply(PAGE.encode(), "text/html; charset=utf-8")
            elif url.path == "/font":
                self.reply(Path(FONT).read_bytes(), "font/ttf")
            elif url.path == "/search":
                m, tok = ALBUM_RE.search(q.get("q", "")), token()
                if m:
                    self.json({"id": m.group(1)})
                elif tok:
                    self.json({"results": search(q.get("q", ""), tok, int(q.get("offset", 0)))})
                else:
                    raise ValueError("Searching by name needs Spotify credentials in .env. Paste an album link instead.")
            elif url.path == "/album":
                album, cover, _ = assets(valid_id(q.get("id")))
                bg, fg, strip = palette(cover)
                self.json({"title": album["title"], "artist": album["artist"],
                           "colors": ["#%02x%02x%02x" % c for c in (bg, fg, *strip)]})
            elif url.path == "/poster":
                size, orientation, fmt = q.get("size", "A4"), q.get("orientation", "portrait"), q.get("format", "png")
                if size not in SIZES or orientation not in ORIENTATIONS or fmt not in FORMATS:
                    raise ValueError("Unknown option.")
                preview = "preview" in q  # same proportions at any size, so previews are always A4
                buf = io.BytesIO()
                with RENDERING:
                    album, img = poster(valid_id(q.get("id")), "A4" if preview else size, orientation, "artist" in q)
                    if preview:
                        img.thumbnail((1400, 1400))
                        img.save(buf, "PNG")
                    else:
                        save(img, buf, fmt)
                    del img
                if preview:
                    self.reply(buf.getvalue(), "image/png")
                else:
                    self.reply(buf.getvalue(), "application/pdf" if fmt == "pdf" else "image/png",
                               filename=file_name(album, fmt))
            else:
                self.send_error(404)
        except urllib.error.HTTPError as e:
            self.json({"error": "Album not found on Spotify." if e.code == 404 else f"Spotify returned an error ({e.code})."}, 502)
        except (ValueError, RuntimeError, OSError) as e:
            self.json({"error": str(e)}, 400)


def serve():
    hosted = "PORT" in os.environ  # set by hosts such as Render: listen publicly and do not open a browser
    host, port = ("0.0.0.0", int(os.environ["PORT"])) if hosted else ("127.0.0.1", 8765)
    try:
        srv = http.server.ThreadingHTTPServer((host, port), UI)
    except OSError:  # port taken: let the system pick one
        if hosted:
            raise
        srv = http.server.ThreadingHTTPServer((host, 0), UI)
    url = f"http://{host}:{srv.server_port}"
    print(f"Framefy is running at {url} (Ctrl+C to stop)", flush=True)
    if not hosted:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Framefy</title>
<style>
@font-face { font-family: Montserrat; src: url(/font); font-weight: 100 900; }
:root { --bg: #e8e5de; --fg: #2e2b28; --c1: #b5ad9f; --c2: #8b8375; --c3: #5f584d;
        --muted: color-mix(in srgb, var(--fg) 62%, var(--bg)); }
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; min-height: 100vh; display: grid; grid-template-columns: minmax(0, 1fr) minmax(340px, 470px);
       background: var(--bg); color: var(--fg); font: 700 15px/1.45 Montserrat, system-ui, sans-serif;
       transition: background .5s, color .5s; }
.stage { display: grid; place-items: center; padding: 5vh 4vw; min-height: 100vh; }
.stage img { max-width: 100%; max-height: 90vh; box-shadow: 0 18px 60px rgb(0 0 0 / .3); transition: opacity .2s; }
.stage img.busy { opacity: .45; }
.stage img:not([src]) { display: none; }
.empty { width: min(100%, 60vh); aspect-ratio: 1 / 1.414; border: 3px dashed var(--muted); color: var(--muted);
         display: grid; place-items: center; text-align: center; padding: 2em; font-size: 19px; }
.panel { padding: 6.5vh 44px 6.5vh 8px; display: flex; flex-direction: column; gap: 28px; }
h1 { font-size: clamp(34px, 3.3vw, 52px); line-height: 1.05; margin: 0; overflow-wrap: anywhere; }
h2 { font-size: 25px; line-height: 1.2; margin: 8px 0 0; }
label, .opts span { display: block; font-size: 19px; }
label { margin-bottom: 4px; }
input { width: 100%; border: 0; border-bottom: 3px solid var(--fg); background: none; color: inherit;
        font: inherit; padding: 8px 0; outline: 0; }
input::placeholder { color: var(--muted); }
button, a { font: inherit; color: inherit; cursor: pointer; }
:is(button, a, input):focus-visible { outline: 2px solid var(--fg); outline-offset: 4px; }
#status { margin: 8px 0 0; color: var(--muted); min-height: 1.45em; }
#results { margin: 0; padding: 0 0 0 1.6em; max-height: 34vh; overflow: auto; }
#results li { padding: 3px 0; }
#results button, #more, .opts button { border: 0; background: none; padding: 0; text-align: left; }
small, #more, .opts button { color: var(--muted); font-size: 15px; }
#more { margin-top: 8px; text-decoration: underline; }
.opts { display: grid; grid-template-columns: 1fr 1fr; gap: 22px 30px; }
.opts button { margin-right: 16px; border-bottom: 3px solid transparent; }
.opts button.on { color: var(--fg); border-bottom-color: var(--fg); }
#download { align-self: start; padding: 13px 30px; background: var(--fg); color: var(--bg); text-decoration: none;
            font-size: 19px; transition: background .5s, color .5s; }
#download:not([href]) { opacity: .35; pointer-events: none; }
.strip { display: flex; width: 190px; height: 13px; margin-top: auto; }
.strip i { flex: 1; transition: background .5s; }
@media (max-width: 860px) { body { grid-template-columns: 1fr; } .stage { min-height: 0; } .panel { padding: 24px; } }
</style>
</head>
<body>
<main class="stage">
  <div class="empty">Paste a Spotify album link to make its poster</div>
  <img id="preview" alt="Poster preview">
</main>
<aside class="panel">
  <header><h1>Framefy</h1><h2>Posters from Spotify albums</h2></header>
  <form>
    <label for="q">Album</label>
    <input id="q" placeholder="Spotify album link, or a name to search" autocomplete="off" autofocus>
    <p id="status" role="status"></p>
    <ol id="results"></ol>
    <button id="more" type="button" hidden>More results</button>
  </form>
  <div class="opts">
    <div><span>Size</span><button data-k="size" data-v="A4" class="on">A4</button><button data-k="size" data-v="A3">A3</button></div>
    <div><span>Format</span><button data-k="format" data-v="png" class="on">PNG</button><button data-k="format" data-v="pdf">PDF</button></div>
    <div><span>Orientation</span><button data-k="orientation" data-v="portrait" class="on">Portrait</button><button data-k="orientation" data-v="landscape">Landscape</button></div>
    <div><span>Artist picture</span><button data-k="artist" data-v="" class="on">Off</button><button data-k="artist" data-v="1">On</button></div>
  </div>
  <a id="download">Download</a>
  <div class="strip"><i style="background: var(--c1)"></i><i style="background: var(--c2)"></i><i style="background: var(--c3)"></i></div>
</aside>
<script>
const $ = s => document.querySelector(s);
const state = {id: '', size: 'A4', orientation: 'portrait', format: 'png', artist: ''};
let query = '', offset = 0;
const say = t => { $('#status').textContent = t; };
const get = (path, p) => fetch(path + '?' + new URLSearchParams(p)).then(r => r.json())
  .catch(() => ({error: 'Could not reach Framefy. Is it still running?'}));

function refresh(preview) {
  if (!state.id) return;
  const p = new URLSearchParams(state);
  $('#download').href = '/poster?' + p;
  if (!preview) return;
  p.set('preview', 1);
  $('#preview').classList.add('busy');
  $('#preview').src = '/poster?' + p;
}
$('#preview').onload = e => { e.target.classList.remove('busy'); $('.empty').hidden = true; say(''); };
$('#preview').onerror = e => { e.target.classList.remove('busy'); say('Could not make the poster.'); };

async function choose(id) {
  say('Loading…');
  const a = await get('/album', {id});
  if (a.error) return say(a.error);
  state.id = id;
  location.hash = id;  // so a reload keeps the album
  $('h1').textContent = a.title;
  $('h2').textContent = a.artist;
  ['--bg', '--fg', '--c1', '--c2', '--c3'].forEach((v, i) => document.documentElement.style.setProperty(v, a.colors[i]));
  refresh(true);
}

async function find(more) {
  if (!more) { query = $('#q').value.trim(); offset = 0; $('#results').replaceChildren(); }
  if (!query) return;
  say('Searching…');
  $('#more').hidden = true;
  const r = await get('/search', {q: query, offset});
  if (r.error) return say(r.error);
  if (r.id) return choose(r.id);
  say(r.results.length || offset ? '' : 'No albums found.');
  for (const a of r.results) {
    const li = document.createElement('li'), b = document.createElement('button'), s = document.createElement('small');
    b.type = 'button';
    b.textContent = a.title;
    s.textContent = ` ${a.artist} · ${a.year}`;
    b.append(s);
    b.onclick = () => choose(a.id);
    li.append(b);
    $('#results').append(li);
  }
  offset += 10;
  $('#more').hidden = r.results.length < 10;
}
$('form').onsubmit = e => { e.preventDefault(); find(false); };
if (location.hash) choose(location.hash.slice(1));
$('#more').onclick = () => find(true);
document.querySelectorAll('.opts button').forEach(b => {
  b.type = 'button';
  b.setAttribute('aria-pressed', b.classList.contains('on'));
  b.onclick = () => {
    state[b.dataset.k] = b.dataset.v;
    b.parentNode.querySelectorAll('button').forEach(o => {
      o.classList.toggle('on', o === b);
      o.setAttribute('aria-pressed', o === b);
    });
    refresh(b.dataset.k === 'orientation' || b.dataset.k === 'artist');
  };
});
</script>
</body>
</html>
"""


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("album", nargs="?", help="Spotify album link, or text to search for; leave out to open the UI")
    p.add_argument("--size", type=str.upper, choices=list(SIZES), default="A4")
    p.add_argument("--orientation", type=str.lower, choices=ORIENTATIONS, default="portrait")
    p.add_argument("--format", type=str.lower, choices=FORMATS, default="png")
    p.add_argument("--artist-image", action="store_true", help="show the artist's profile picture next to their name")
    p.add_argument("-o", "--output", help="output file (default: '<artist> - <album>.<format>')")
    args = p.parse_args()
    if not args.album:
        return serve()
    try:
        m, tok = ALBUM_RE.search(args.album), token()
        if not m and not tok:
            sys.exit("Searching by name needs Spotify credentials in .env; pass an album link instead.")
        album, img = poster(m.group(1) if m else pick(args.album, tok), args.size, args.orientation, args.artist_image)
    except urllib.error.HTTPError as e:
        sys.exit(f"Spotify error {e.code}: {e.read().decode(errors='replace')[:300]}")
    out = args.output or file_name(album, args.format)
    save(img, out, args.format)
    print(out)


if __name__ == "__main__":
    main()
