#!/usr/bin/env python3
"""Make a poster from a Spotify album, or one mosaic poster from several albums."""
import argparse
import base64
import calendar
import concurrent.futures
import functools
import http.server
import io
import itertools
import json
import math
import os
import re
import secrets
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
VERSION = "1.3.0"
FONT_DIR = Path(__file__).parent / "fonts"
FONTS = {"montserrat": "Montserrat.ttf", "playfair": "PlayfairDisplay.ttf"}
WEIGHTS = {"light": 500, "bold": 700, "black": 900}
DPI = 300
SIZES = {"A5": (1748, 2480), "A4": (2480, 3508), "A3": (3508, 4961), "A2": (4961, 7016),
         "LETTER": (2550, 3300), "50X70": (5906, 8268)}  # short, long side in px at 300 dpi
PARTS = ("tracks", "date", "runtime", "code", "strip")  # what --hide can leave out
FADES = {"fade": 0.17, "soft": 0.4, "sharp": 0, "framed": None}  # share of the cover that fades out
LAYOUTS = ("standard", "cover-right", "minimal")
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
        "durations": [t["duration_ms"] for t in tracks],
        "cover_url": a["images"][0]["url"],
    }


def parse_public(embed_html, album_html=""):
    """Same dictionary as load_album, read from Spotify's public embed and album pages (no key)."""
    # ponytail: unofficial; breaks if Spotify changes these pages, and the embed may cut very long albums
    data = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', embed_html, re.S)
    if not data:
        raise RuntimeError("Spotify's public page has changed; add credentials to .env instead.")
    props = json.loads(data.group(1))["props"]["pageProps"]
    if "state" not in props:  # the embed page answers 200 for an album that does not exist
        raise ValueError("Album not found on Spotify.")
    e = props["state"]["data"]["entity"]
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
        "durations": [t["duration"] for t in e["trackList"]],
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


def details(album_id, pages=("embed/", "")):
    """Album details: from the API with a key, from public pages without."""
    tok = token()
    if tok:
        try:
            return load_album(album_id, tok)
        except urllib.error.HTTPError as e:
            print(f"warning: Spotify API error {e.code}, using the public pages", file=sys.stderr)
    return parse_public(*(fetch(f"https://open.spotify.com/{p}album/{album_id}", UA).decode() for p in pages))


@functools.lru_cache(maxsize=8)  # a cover is ~12 MB in memory
def assets(album_id):
    """Album details, cover and scan code."""
    album = details(album_id)
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


# --- Collections: several albums on one poster ---

def jellyfin(path, **params):
    """Response body from the Jellyfin server named in .env."""
    load_env()
    base, key = os.environ.get("JELLYFIN_URL"), os.environ.get("JELLYFIN_API_KEY")
    if not (base and key):
        raise ValueError("Jellyfin needs JELLYFIN_URL and JELLYFIN_API_KEY in .env.")
    try:
        return fetch(f"{base.rstrip('/')}{path}?{urllib.parse.urlencode(params)}",
                     {"Authorization": f'MediaBrowser Token="{key}"'})
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Jellyfin returned an error ({e.code}).") from None
    except OSError as e:
        raise RuntimeError(f"Could not reach Jellyfin: {e}") from None


def favourites():
    """Favourite albums of JELLYFIN_USER (the first user when unset): title, artist and the item id of the cover."""
    users = json.loads(jellyfin("/Users"))
    name = os.environ.get("JELLYFIN_USER", "")
    user = next((u for u in users if not name or u["Name"].lower() == name.lower()), None)
    if not user:
        raise ValueError(f"Jellyfin has no user called {name}.")
    items = json.loads(jellyfin("/Items", userId=user["Id"], Filters="IsFavorite", IncludeItemTypes="MusicAlbum",
                                Recursive="true", SortBy="SortName"))["Items"]
    return [(i["Name"], i.get("AlbumArtist") or "", i["Id"] if i.get("ImageTags", {}).get("Primary") else None)
            for i in items]


def mosaic(covers, side=2000):
    """Square grid of the covers."""
    # ponytail: a count that is not a square number repeats covers from the first one to fill the grid
    if not covers:
        return Image.new("RGB", (side, side), (40, 40, 40))
    g = math.isqrt(len(covers) - 1) + 1
    cell = side // g
    out = Image.new("RGB", (cell * g, cell * g))
    for i, c in zip(range(g * g), itertools.cycle(covers)):
        out.paste(ImageOps.fit(c.convert("RGB"), (cell, cell)), (i % g * cell, i // g * cell))
    return out


@functools.lru_cache(maxsize=4)
def collection(src, stamp=0):
    """(title, artist) of every album and a mosaic of their covers; src is a tuple of Spotify ids or "jellyfin"."""
    # ponytail: Jellyfin albums without art stay in the list but get no cell in the mosaic; a list too long
    # for the page (roughly 100 albums on a portrait sheet) runs off it, so hide the tracks for those;
    # adding one album in the UI downloads every cover again, cache the cells per album if that gets slow
    jobs = favourites() if src == "jellyfin" else src
    if not jobs:
        raise ValueError("Jellyfin has no favourite albums.")
    side = 2000 // (math.isqrt(len(jobs) - 1) + 1)
    if src != "jellyfin":
        token()  # fetched once here rather than by every thread

    def cell(job):
        if src == "jellyfin":
            title, artist, item = job
            cover = item and Image.open(io.BytesIO(jellyfin(f"/Items/{item}/Images/Primary", maxWidth=1000)))
        else:
            a = details(job, ("embed/",))
            title, artist, cover = a["title"], a["artist"], load_image(a["cover_url"])
        return title, artist, cover and ImageOps.fit(cover.convert("RGB"), (side, side))  # small: there may be 100

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        cells = list(pool.map(cell, jobs))
    return [c[:2] for c in cells], mosaic([c[2] for c in cells if c[2]])


def gather(src, hide=()):
    """assets() of one album; for a collection the cover is a mosaic and the tracks are its albums."""
    if isinstance(src, str) and src != "jellyfin":
        return assets(src)
    jelly = src == "jellyfin"
    items, art = collection(src, time.time() // 300 if jelly else 0)  # favourites are read again after 5 minutes
    return {"title": "Favourites" if jelly else "Albums", "artist": f"{len(items)} album{'s' * (len(items) != 1)}",
            "artist_id": None,
            "tracks": [t if "artists" in hide or not a else f"{t} — {a}" for t, a in items]}, art, None


# --- Rendering ---

@functools.lru_cache(maxsize=256)
def font(size, weight, name="montserrat"):
    f = ImageFont.truetype(str(FONT_DIR / FONTS[name]), max(1, round(size)))
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


def readable(colour, bg):
    """colour pushed towards black or white until it can be read on bg."""
    target = (0, 0, 0) if lum(bg) > 0.18 else (255, 255, 255)
    for t in range(21):
        out = mix(colour, target, t / 20)
        if contrast(out, bg) >= 4.5:
            break
    return out


def palette(cover):
    """Background, text, three strip colours and the whole palette, taken from the cover."""
    # ponytail: simple heuristic, no taste involved; the bg/fg options override it
    raw = cover.convert("RGB").resize((64, 64)).tobytes()
    px = list(zip(raw[0::3], raw[1::3], raw[2::3]))
    px = [p for p in px if min(p) <= 250] or px  # near-white would otherwise win on most covers
    q = Image.new("RGB", (len(px), 1))
    q.putdata(px)
    q = q.quantize(8)
    pal = q.getpalette()
    cols = [tuple(pal[i * 3:i * 3 + 3]) for _, i in sorted(q.getcolors(), reverse=True)]
    bg = cols[0]
    fg = readable(max(cols, key=lambda c: max(c) - min(c)), bg)  # text: the most colourful palette entry
    strip = sorted(cols[1:], key=lambda c: contrast(c, bg), reverse=True)  # ones that show up on bg first
    return bg, fg, (strip + [fg] * 3)[:3], cols


def options(raw):
    """Poster options, validated, from the strings of a query string or the command line."""
    def choice(key, allowed, default):
        v = raw.get(key) or default
        if v not in allowed:
            raise ValueError(f"Unknown {key}: {v}")
        return v

    def colour(key):
        v = (raw.get(key) or "").lstrip("#")
        if v and not re.fullmatch(r"[0-9a-fA-F]{6}", v):
            raise ValueError(f"{key} must be a colour like #1a2b3c")
        return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4)) if v else None

    hide = [p for p in (raw.get("hide") or "").split(",") if p]
    if set(hide) - set(PARTS) - {"artists"}:  # artists: those of a collection's albums
        raise ValueError(f"hide takes: {', '.join(PARTS)}, artists")
    return {
        "bg": colour("bg"), "fg": colour("fg"), "hide": hide,
        "title": (raw.get("title") or "")[:200], "artist": (raw.get("artist") or "")[:200],
        "caption": (raw.get("caption") or "")[:200],
        "clean": bool(raw.get("clean")), "durations": bool(raw.get("durations")),
        "columns": int(choice("columns", ("0", "1", "2", "3", "4"), "0")),
        "bleed": int(choice("bleed", tuple(map(str, range(11))), "0")),
        "cover": choice("cover", FADES, "fade"), "layout": choice("layout", LAYOUTS, "standard"),
        "font": choice("font", FONTS, "montserrat"), "weight": choice("weight", WEIGHTS, "bold"),
    }


CLEAN_RE = re.compile(
    r"\s*[(\[](?:feat|ft|with)\b[^)\]]*[)\]]"  # (feat. Someone)
    r"|\s*[(\[][^)\]]*\b(?:remaster(?:ed)?|version|edit|bonus track)\b[^)\]]*[)\]]"  # (2011 Remaster)
    r"|\s+-\s+[^-]*\b(?:remaster(?:ed)?|version|edit|bonus track)\b.*$", re.I)  # - Remastered 2011


def clean(name):
    return CLEAN_RE.sub("", name).strip() or name


def own_cover(source):
    """Someone's own image (path or file object) as a square cover."""
    try:
        img = Image.open(source)
        if img.width * img.height > 40_000_000:
            raise ValueError
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception:
        raise ValueError("That file is not an image Framefy can use (40 megapixels at most).") from None
    img = ImageOps.fit(img, (min(img.size),) * 2)
    img.thumbnail((2000, 2000))
    return img


def wrap(d, text, f, max_w):
    lines = [""]
    for word in text.split():
        trial = f"{lines[-1]} {word}".strip()
        if not lines[-1] or d.textlength(trial, font=f) <= max_w:
            lines[-1] = trial
        else:
            lines.append(word)
    return lines


def fit(d, text, F, max_size, max_w, max_lines):
    """Largest size (in layout units) at which text fits max_w within max_lines; F makes a font of a size."""
    for size in range(max_size, 19, -1):
        f = F(size)
        lines = wrap(d, text, f, max_w)
        if len(lines) <= max_lines and all(d.textlength(l, font=f) <= max_w for l in lines):
            break
    return f, size, lines


def draw_tracks(d, items, F, x, y, max_w, bottom, u, fill, muted, columns=0):
    """items are (name, duration) pairs; columns=0 uses as many as the height needs."""
    # ponytail: one very long track name shrinks the whole list; truncate with an ellipsis if that looks bad
    if not items:
        return
    for size in range(27, 9, -1):
        f, pitch = F(size), size * 37 / 27 * u
        fit_rows = max(1, int((bottom - y) / pitch) + 1)
        rows = -(-len(items) // columns) if columns else fit_rows
        cols = [items[i:i + rows] for i in range(0, len(items), rows)]
        widths = [max(d.textlength(n + ("   " + t if t else ""), font=f) for n, t in c) for c in cols]
        if rows <= fit_rows and sum(widths) + 30 * u * (len(cols) - 1) <= max_w:
            break
    for c, w in zip(cols, widths):
        for r, (n, t) in enumerate(c):
            d.text((x, y + r * pitch), n, fill, f, anchor="ls")
            d.text((x + d.textlength(n + "   ", font=f), y + r * pitch), t, muted, f, anchor="ls")
        x += w + 30 * u


def add_bleed(img, px):
    """Extend the page by px on every side, stretching the edge pixels outwards for the print shop to trim."""
    w, h = img.size
    out = ImageOps.expand(img, px, img.getpixel((0, h - 1)))
    for box, pos, size in (((0, 0, w, 1), (px, 0), (w, px)), ((0, h - 1, w, h), (px, h + px), (w, px)),
                           ((0, 0, 1, h), (0, px), (px, h)), ((w - 1, 0, w, h), (w + px, px), (px, h))):
        out.paste(img.crop(box).resize(size), pos)
    return out


def render(album, art, code=None, size="A4", orientation="portrait", artist_img=None, scale=1, **opts):
    """Layout numbers are in units of short side / 1414, measured off the A4 reference posters (2000 units long)."""
    o = {**options({}), **opts}
    short, long = (round(v * scale) for v in SIZES[size])
    land = orientation == "landscape"
    W, H = (long, short) if land else (short, long)
    u = short / 1414
    off = long / u - 2000  # paper squarer than A-series is shorter: the text block and fade move up by this
    hide = set(PARTS) if o["layout"] == "minimal" else set(o["hide"])
    flip = land and o["layout"] == "cover-right"
    bg, fg, strip, _ = palette(art)
    bg = o["bg"] or bg
    fg = o["fg"] or readable(fg, bg)  # keeps the automatic text legible on a chosen background
    muted = mix(fg, bg, 0.35)

    def F(size):
        return font(size * u, WEIGHTS[o["weight"]], o["font"])

    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    m = round(92 * u)  # page margin

    art = art.convert("RGB")
    if o["cover"] == "framed":  # the whole cover inside the margins, no fade
        side = round((1230 + min(0, off)) * u)
        pos = (W - m - side if flip else m, (H - side) // 2) if land else ((W - side) // 2, m)
        img.paste(art.resize((side, side), Image.Resampling.LANCZOS), pos)
    else:  # square cover on the short side, fading into the background towards the text
        end, width = (0.88 if land else 0.92) + off / 1414, FADES[o["cover"]]
        ramp = [min(1, max(0, (end - i / short) / width)) if width else float(i / short < end) for i in range(short)]
        mask = Image.new("L", (1, short))
        mask.putdata([round(255 * t * t * (3 - 2 * t)) for t in ramp])
        mask = mask.resize((short, short))
        if land:
            mask = mask.transpose(Image.Transpose.TRANSPOSE)
        if flip:
            mask = mask.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        img.paste(art.resize((short, short), Image.Resampling.LANCZOS), (W - short if flip else 0, 0), mask)

    x0 = (1360 + off) * u if land and not flip else m
    max_w = 548 * u if land else W - 2 * m
    foot = (1188 if land else 1886 + off) * u  # baseline of the "Release date" label

    f, s, lines = fit(d, o["title"] or album["title"], F, 113, max_w, 3 if land else 1)
    block = (0.72 + 1.1 * (len(lines) - 1)) * s  # top of the first line to the last baseline
    if land:
        last = ((1414 - block - 100) / 2 if o["layout"] == "minimal" else 92) * u + block * u
    else:
        last = ((1620 if o["layout"] == "minimal" else 1425) + off) * u
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
    f, _, lines = fit(d, o["artist"] or album["artist"], F, 61, max_w - (ax - x0), 1)
    d.text((ax, last + 82 * u), " ".join(lines), fg, f, anchor="ls")

    if "tracks" not in hide:
        times = [f"{ms // 60000}:{ms // 1000 % 60:02}" for ms in album.get("durations", [])] if o["durations"] else []
        items = [(f"{i}. {clean(t) if o['clean'] else t}", times[i - 1] if times else "")
                 for i, t in enumerate(album["tracks"], 1)]
        bottom = foot - 76 * u if set(PARTS[1:]) - hide else H - m  # the footer's room is free when it is hidden
        draw_tracks(d, items, F, x0, last + 155 * u, max_w, bottom, u, fg, muted, o["columns"])

    x = x0
    for part, label in (("date", "Release date"), ("runtime", "Runtime")):
        if part not in hide:
            d.text((x, foot), label, fg, F(38), anchor="ls")
            d.text((x, foot + 46 * u), album[part], muted, F(33), anchor="ls")
            x += d.textlength(label, font=F(38)) + 59 * u

    # scan code with the palette strip under it: bottom right in portrait, under the dates in landscape
    cx, cy = (x0, foot + 74 * u) if land else (W - m - 248 * u, foot - 37 * u)
    if code and "code" not in hide:
        code = code.crop(code.getbbox())
        code = code.resize((round(248 * u), round(248 * u * code.height / code.width)), Image.Resampling.LANCZOS)
        img.paste(fg, (round(cx), round(cy + (62 * u - code.height) / 2)), code)
    if "strip" not in hide:
        for i, c in enumerate(strip):
            d.rectangle([cx + i * 248 * u / 3, cy + 71 * u, cx + (i + 1) * 248 * u / 3, cy + 88 * u], fill=c)
    if o["caption"]:
        d.text((x0, H - 34 * u), o["caption"], muted, F(20), anchor="ls")

    px = round(o["bleed"] / 25.4 * DPI * scale)
    return add_bleed(img, px) if px else img


def save(img, out, fmt):
    # ponytail: the PDF is a raster page, not vector text; use reportlab if print shops complain
    if fmt == "pdf":
        img.save(out, "PDF", resolution=DPI)
    else:
        img.save(out, "PNG", dpi=(DPI, DPI))


def poster(src, size="A4", orientation="portrait", picture=False, own=None, scale=1, **opts):
    album, art, code = gather(src, opts.get("hide", ()))
    if "date" not in album:  # a collection has no date, runtime or scan code
        opts["hide"] = [*opts.get("hide", ()), "date", "runtime", "code"]
    pic = artist_image(album["artist_id"]) if picture and album["artist_id"] else None
    return album, render(album, own or art, code, size, orientation, pic, scale, **opts)


def file_name(album, fmt):
    return re.sub(r'[\\/:*?"<>|]', "_", f"{album['artist']} - {album['title']}") + f".{fmt}"


# --- UI ---

def source(s):
    """What a request wants a poster of: an album id, a tuple of several, or "jellyfin"."""
    ids = tuple((s or "").split(","))
    if s == "jellyfin":
        return s
    if not all(re.fullmatch(r"[A-Za-z0-9]{22}", i) for i in ids):
        raise ValueError("That is not a Spotify album.")
    if len(ids) > 100:
        raise ValueError("A poster takes 100 albums at most.")
    return ids[0] if len(ids) == 1 else ids


# ponytail: one poster at a time, since A3 needs ~150 MB and A2 or 50x70 several times that;
# the largest sizes can exceed a 512 MB host
RENDERING = threading.Lock()
UPLOADS = {}  # key -> own cover; only the latest few are kept


def uploaded(q):
    key = q.get("upload")
    if key and key not in UPLOADS:
        raise ValueError("The uploaded cover has expired; upload it again.")
    return UPLOADS.get(key)


class UI(http.server.BaseHTTPRequestHandler):
    # ponytail: stdlib server with no rate limiting, fine for a hobby site; move to gunicorn if it gets real traffic
    def reply(self, body, ctype, status=200, filename=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if filename:
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}")
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:  # the browser dropped a request it no longer needs, e.g. a superseded preview
            pass

    def json(self, obj, status=200):
        self.reply(json.dumps(obj).encode(), "application/json", status)

    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if self.path != "/upload" or not 0 < n <= 15_000_000:
                raise ValueError("Upload an image of at most 15 MB.")
            key = secrets.token_urlsafe(8)
            UPLOADS[key] = own_cover(io.BytesIO(self.rfile.read(n)))
            while len(UPLOADS) > 4:
                UPLOADS.pop(next(iter(UPLOADS)))
            self.json({"upload": key})
        except ValueError as e:
            self.json({"error": str(e)}, 400)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if url.path == "/":
                load_env()
                self.reply(PAGE.replace("__JELLYFIN__", "" if os.environ.get("JELLYFIN_URL") else "hidden").encode(),
                           "text/html; charset=utf-8")
            elif url.path == "/font":
                self.reply((FONT_DIR / FONTS["montserrat"]).read_bytes(), "font/ttf")
            elif url.path == "/search":
                ids, tok = dict.fromkeys(ALBUM_RE.findall(q.get("q", ""))), token()
                if ids:  # several links make a collection
                    self.json({"id": ",".join(ids)})
                elif tok:
                    self.json({"results": search(q.get("q", ""), tok, int(q.get("offset", 0)))})
                else:
                    raise ValueError("Searching by name needs Spotify credentials in .env. Paste an album link instead.")
            elif url.path == "/album":
                album, cover, _ = gather(source(q.get("id")))
                bg, fg, strip, cols = palette(uploaded(q) or cover)
                bg = options(q)["bg"] or bg
                fg = readable(fg, bg)
                names = album["tracks"] if "date" not in album else [f"{album['title']} — {album['artist']}"]
                self.json({"title": album["title"], "artist": album["artist"], "albums": names,  # colors: page theme
                           "colors": ["#%02x%02x%02x" % c for c in (bg, fg, *strip)],
                           "swatches": ["#%02x%02x%02x" % c for c in cols]})
            elif url.path == "/poster":
                size, orientation, fmt = q.get("size", "A4"), q.get("orientation", "portrait"), q.get("format", "png")
                if size not in SIZES or orientation not in ORIENTATIONS or fmt not in FORMATS:
                    raise ValueError("Unknown option.")
                opts, preview = options(q), "preview" in q
                buf = io.BytesIO()
                with RENDERING:
                    album, img = poster(source(q.get("id")), size, orientation, "picture" in q, uploaded(q),
                                        1400 / SIZES[size][1] if preview else 1, **opts)
                    save(img, buf, "png" if preview else fmt)
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
body { margin: 0; display: grid; grid-template-columns: minmax(0, 1fr) minmax(340px, 480px);
       background: var(--bg); color: var(--fg); font: 700 15px/1.45 Montserrat, system-ui, sans-serif;
       transition: background .5s, color .5s; }
.stage { position: sticky; top: 0; height: 100vh; display: grid; place-items: center; padding: 5vh 4vw; }
.stage img { max-width: 100%; max-height: 90vh; box-shadow: 0 18px 60px rgb(0 0 0 / .3); transition: opacity .2s; }
.stage img.busy { opacity: .45; }
.stage img:not([src]) { display: none; }
.empty { width: min(100%, 60vh); aspect-ratio: 1 / 1.414; border: 3px dashed var(--muted); color: var(--muted);
         display: grid; place-items: center; text-align: center; padding: 2em; font-size: 19px; }
.panel { padding: 6.5vh 44px 6.5vh 8px; display: flex; flex-direction: column; gap: 28px; }
h1 { font-size: clamp(34px, 3.3vw, 52px); line-height: 1.05; margin: 0; overflow-wrap: anywhere; }
h2 { font-size: 25px; line-height: 1.2; margin: 8px 0 0; }
label, .opts span { display: block; font-size: 19px; }
input:not([type]) { width: 100%; border: 0; border-bottom: 3px solid var(--fg); background: none; color: inherit;
        font: inherit; font-size: 15px; padding: 8px 0; outline: 0; }
input::placeholder { color: var(--muted); }
button, a { font: inherit; color: inherit; cursor: pointer; }
:is(button, a, input):focus-visible { outline: 2px solid var(--fg); outline-offset: 4px; }
#status { margin: 8px 0 0; color: var(--muted); min-height: 1.45em; }
.lists { display: grid; grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr); gap: 20px; }
#results, #picked ol { margin: 0; padding: 0 0 0 1.6em; max-height: 34vh; overflow: auto; }
#results li, #picked li { padding: 3px 0; }
#results button, #more, #jellyfin, #mode button, #picked button, .opts button { border: 0; background: none; padding: 0; text-align: left; }
small, #more, #jellyfin, #mode button, #picked button, .opts button { color: var(--muted); font-size: 15px; }
#mode { margin-top: 6px; }
#picked button { margin-left: 8px; }
#results button { vertical-align: top; }  /* keeps the number beside the first line of a wrapped result */
#results:empty { display: none; }  /* leaves the whole width to the selected list */
#more, #jellyfin { display: block; margin-top: 8px; text-decoration: underline; }
body:not(.multi) .multi, body.multi .single { display: none; }
.opts { display: grid; grid-template-columns: 1fr 1fr; gap: 20px 30px; }
.opts .wide { grid-column: 1 / -1; }
.opts button, #mode button { margin: 0 16px 4px 0; border-bottom: 3px solid transparent; }
.opts button.on, #mode button.on { color: var(--fg); border-bottom-color: var(--fg); }
.opts button.sw { width: 24px; height: 24px; margin-right: 8px; border: 2px solid var(--muted); vertical-align: middle; }
.opts button.sw.on { outline: 2px solid var(--fg); outline-offset: 2px; }
.opts input[type=color] { width: 30px; height: 26px; padding: 0; border: 0; background: none; vertical-align: middle; cursor: pointer; }
.opts input[type=file] { font: inherit; font-size: 13px; color: var(--muted); max-width: 100%; }
#download { align-self: start; padding: 13px 30px; background: var(--fg); color: var(--bg); text-decoration: none;
            font-size: 19px; transition: background .5s, color .5s; }
#download:not([href]) { opacity: .35; pointer-events: none; }
footer { display: flex; align-items: center; gap: 16px; color: var(--muted); font-size: 13px; }
.strip { display: flex; width: 190px; height: 13px; }
.strip i { flex: 1; transition: background .5s; }
@media (max-width: 860px) { body { grid-template-columns: 1fr; } .stage { position: static; height: auto; } .panel { padding: 24px; } }
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
    <div id="mode"></div>
    <input id="q" placeholder="Spotify album link (or several), or a name to search" autocomplete="off" autofocus>
    <p id="status" role="status"></p>
    <div class="lists">
      <ol id="results"></ol>
      <div id="picked" hidden><small>Selected</small><ol></ol></div>
    </div>
    <button id="more" type="button" hidden>More results</button>
    <button id="jellyfin" type="button" __JELLYFIN__>Jellyfin favourites</button>
  </form>
  <div class="opts" id="opts"></div>
  <a id="download">Download</a>
  <footer><div class="strip"><i style="background: var(--c1)"></i><i style="background: var(--c2)"></i><i style="background: var(--c3)"></i></div>v__VERSION__</footer>
</aside>
<script>
const $ = s => document.querySelector(s);
const el = (tag, text, cls) => { const e = document.createElement(tag); if (text) e.textContent = text; if (cls) e.className = cls; return e; };
const state = {id: '', size: 'A4', format: 'png', orientation: 'portrait', layout: 'standard', cover: 'fade',
  font: 'montserrat', weight: 'bold', columns: '0', bleed: '0', picture: '', durations: '', clean: '',
  title: '', artist: '', caption: '', bg: '', fg: '', upload: ''};
const hidden = new Set();   // poster parts switched off
const marks = [];           // [button, isOn] pairs, repainted by mark()
const names = {};           // album id -> "Title — Artist", for the selected list
let album = null, query = '', offset = 0, timer, soon, many = false;   // many: picking adds to the selection

const CHOICES = [
  ['Size', 'size', [['A5', 'A5'], ['A4', 'A4'], ['A3', 'A3'], ['A2', 'A2'], ['LETTER', 'Letter'], ['50X70', '50×70']], true],
  ['Orientation', 'orientation', [['portrait', 'Portrait'], ['landscape', 'Landscape']]],
  ['Format', 'format', [['png', 'PNG'], ['pdf', 'PDF']]],
  ['Layout', 'layout', [['standard', 'Standard'], ['cover-right', 'Cover right'], ['minimal', 'Minimal']], true],
  ['Cover', 'cover', [['fade', 'Fade'], ['soft', 'Soft'], ['sharp', 'Sharp'], ['framed', 'Framed']], true],
  ['Font', 'font', [['montserrat', 'Montserrat'], ['playfair', 'Playfair']]],
  ['Weight', 'weight', [['light', 'Light'], ['bold', 'Bold'], ['black', 'Black']]],
  ['Track columns', 'columns', [['0', 'Auto'], ['1', '1'], ['2', '2'], ['3', '3']]],
  ['Bleed', 'bleed', [['0', 'None'], ['3', '3 mm'], ['5', '5 mm']]],
];
// a third entry is a class: single = one album only, multi = several albums only
const SHOW = [['Tracklist', 'tracks'], ['Release date', 'date', 'single'], ['Runtime', 'runtime', 'single'],
  ['Scan code', 'code', 'single'], ['Colour strip', 'strip'], ['Album artists', 'artists', 'multi']];
const FLAGS = [['Artist picture', 'picture', 'single'], ['Track durations', 'durations', 'single'], ['Clean track names', 'clean']];
const TEXTS = [['Title', 'title'], ['Artist', 'artist'], ['Caption', 'caption']];

const say = t => { $('#status').textContent = t; };
const get = (path, p) => fetch(path + '?' + new URLSearchParams(p)).then(r => r.json())
  .catch(() => ({error: 'Could not reach Framefy. Is it still running?'}));

function params() {
  const p = new URLSearchParams(state);
  p.set('hide', [...hidden].join(','));
  return p;
}
function refresh(preview) {
  mark();
  if (!state.id) return;
  const p = params();
  $('#download').href = '/poster?' + p;
  if (!preview) return;
  p.set('preview', 1);
  $('#preview').classList.add('busy');
  $('#preview').src = '/poster?' + p;
}
function mark() {
  for (const [b, isOn] of marks) { b.classList.toggle('on', isOn()); b.setAttribute('aria-pressed', isOn()); }
  if (!album) return;
  const c = [album.colors[0], state.fg || album.colors[1], ...album.colors.slice(2)];
  ['--bg', '--fg', '--c1', '--c2', '--c3'].forEach((v, i) => document.documentElement.style.setProperty(v, c[i]));
}
function button(text, isOn, click, cls, after = () => refresh(true)) {
  const b = el('button', text, cls);
  b.type = 'button';
  b.onclick = () => { click(); after(); };
  marks.push([b, isOn]);
  return b;
}
function group(label, wide, ...children) {
  const div = el('div', '', wide ? 'wide' : '');
  div.append(el('span', label), ...children);
  $('#opts').append(div);
  return div;
}

for (const [label, key, values, wide] of CHOICES)
  group(label, wide, ...values.map(([v, text]) => button(text, () => state[key] === v, () => { state[key] = v; })));
group('Show', true,
  ...SHOW.map(([text, part, cls]) => button(text, () => !hidden.has(part), () => { hidden.has(part) ? hidden.delete(part) : hidden.add(part); }, cls)),
  ...FLAGS.map(([text, key, cls]) => button(text, () => !!state[key], () => { state[key] = state[key] ? '' : '1'; }, cls)));
const swatches = {bg: group('Background', false), fg: group('Text colour', false)};
for (const [label, key] of TEXTS) {
  const input = el('input');
  input.setAttribute('aria-label', label);
  input.dataset.k = key;
  input.oninput = () => { clearTimeout(timer); timer = setTimeout(() => { state[key] = input.value.trim(); refresh(true); }, 500); };
  group(label, true, input);
}
const file = el('input'), remove = button('Use the album cover', () => !state.upload, () => { state.upload = ''; file.value = ''; load(); });
file.type = 'file';
file.accept = 'image/*';
file.setAttribute('aria-label', 'Own cover image');
file.onchange = async () => {
  if (!file.files[0]) return;
  say('Uploading…');
  const r = await fetch('/upload', {method: 'POST', body: file.files[0]}).then(r => r.json()).catch(() => ({error: 'Upload failed.'}));
  if (r.error) return say(r.error);
  state.upload = r.upload;
  load();
};
group('Own cover', true, file, el('br'), remove);

function paint() {   // colour swatches for the loaded cover
  for (const key of ['bg', 'fg']) {
    const div = swatches[key];
    while (div.children.length > 1) { const b = div.lastChild, i = marks.findIndex(m => m[0] === b); if (i >= 0) marks.splice(i, 1); b.remove(); }
    if (!album) continue;
    const after = key === 'bg' ? load : undefined;
    div.append(button('Auto', () => !state[key], () => { state[key] = ''; }, '', after));
    for (const c of album.swatches) {
      const b = button('', () => state[key] === c, () => { state[key] = c; }, 'sw', after);
      b.style.background = c;
      b.setAttribute('aria-label', c);
      div.append(b);
    }
    const pick = el('input');
    pick.type = 'color';
    pick.setAttribute('aria-label', 'Custom colour');
    pick.value = state[key] || album.colors[key === 'bg' ? 0 : 1];
    pick.onchange = () => { state[key] = pick.value; key === 'bg' ? load() : refresh(true); };
    div.append(pick);
  }
}

const picked = () => state.id && state.id !== 'jellyfin' ? state.id.split(',') : [];
function listPicked() {   // the selected albums, next to the search results
  const ids = picked();
  $('#picked').hidden = !many && ids.length < 2;   // a mosaic keeps its list in One mode, to remove albums from
  $('#picked ol').replaceChildren(...ids.map(id => {
    const li = el('li', names[id] || '…'), x = el('button', '×');
    x.type = 'button';
    x.setAttribute('aria-label', 'Remove ' + (names[id] || 'album'));
    x.onclick = () => choose(ids.filter(i => i !== id).join(','));
    li.append(x);
    return li;
  }));
}
function take(ids) {   // Multiple adds to the selection, One replaces it
  choose([...new Set([...(many ? picked() : []), ...ids])].join(','));
}

async function load() {
  const id = state.id;
  listPicked();
  if (!id) {   // nothing selected (any more): back to the empty page
    album = null;
    state.bg = state.fg = '';
    document.documentElement.removeAttribute('style');
    paint();
    say('');
    document.querySelectorAll('[data-k=title], [data-k=artist]').forEach(i => { i.placeholder = ''; });
    $('#preview').removeAttribute('src');
    $('#download').removeAttribute('href');
    $('.empty').hidden = false;
    $('h1').textContent = 'Framefy';
    $('h2').textContent = 'Posters from Spotify albums';
    return mark();
  }
  say('Loading…');
  const a = await get('/album', {id, upload: state.upload, bg: state.bg});
  if (id !== state.id) return;   // superseded by a later pick
  if (a.error) return say(a.error);
  album = a;
  picked().forEach((p, i) => { names[p] = a.albums[i]; });
  listPicked();
  $('h1').textContent = a.title;
  $('h2').textContent = a.artist;
  document.querySelector('[data-k=title]').placeholder = a.title;
  document.querySelector('[data-k=artist]').placeholder = a.artist;
  paint();
  refresh(true);
}
function choose(id) {
  state.id = id;
  if (!many) state.bg = state.fg = '';   // Multiple keeps the chosen colours while the selection changes
  document.body.classList.toggle('multi', id === 'jellyfin' || id.includes(','));
  location.hash = id;  // so a reload keeps the album
  listPicked();
  if (id) say('Loading…');
  clearTimeout(soon);
  soon = setTimeout(load, id.includes(',') ? 600 : 0);   // quick picks in a row make one mosaic, not one each
}
$('#preview').onload = e => { e.target.classList.remove('busy'); $('.empty').hidden = true; say(''); };
$('#preview').onerror = e => { e.target.classList.remove('busy'); say('Could not make the poster.'); };

async function find(more) {
  if (!more) { query = $('#q').value.trim(); offset = 0; $('#results').replaceChildren(); }
  if (!query) return;
  say('Searching…');
  $('#more').hidden = true;
  const r = await get('/search', {q: query, offset});
  if (r.error) return say(r.error);
  if (r.id) {   // one or several links
    if (many) $('#q').value = '';
    return take(r.id.split(','));
  }
  say(r.results.length || offset ? '' : 'No albums found.');
  for (const a of r.results) {
    const li = el('li'), b = el('button', a.title);
    b.type = 'button';
    b.append(el('small', ` ${a.artist} · ${a.year}`));
    b.onclick = () => { names[a.id] = `${a.title} — ${a.artist}`; take([a.id]); };
    li.append(b);
    $('#results').append(li);
  }
  offset += 10;
  $('#more').hidden = r.results.length < 10;
}
$('form').onsubmit = e => { e.preventDefault(); find(false); };
$('#more').onclick = () => find(true);
$('#jellyfin').onclick = () => choose('jellyfin');
$('#mode').append(...[['One', false], ['Multiple', true]].map(([text, v]) =>
  button(text, () => many === v, () => { many = v; }, '', () => { mark(); listPicked(); })));
mark();
if (location.hash) choose(location.hash.slice(1));
</script>
</body>
</html>
""".replace("__VERSION__", VERSION)


def main():
    p = argparse.ArgumentParser(description=__doc__, fromfile_prefix_chars="@")
    p.add_argument("album", nargs="*", help="Spotify album link, or text to search for; several links (or @FILE with "
                                            "one a line) make one mosaic poster; leave out to open the UI")
    p.add_argument("--jellyfin", action="store_true", help="mosaic poster of your Jellyfin favourite albums")
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("--size", type=str.upper, choices=list(SIZES), default="A4")
    p.add_argument("--orientation", type=str.lower, choices=ORIENTATIONS, default="portrait")
    p.add_argument("--format", type=str.lower, choices=FORMATS, default="png")
    p.add_argument("--layout", choices=LAYOUTS, default="standard", help="cover-right applies to landscape")
    p.add_argument("--cover", choices=list(FADES), default="fade", help="how the cover meets the background")
    p.add_argument("--cover-image", metavar="FILE", help="use your own image instead of the album cover")
    p.add_argument("--bg", metavar="HEX", help="background colour, e.g. #ecebe6 (default: from the cover)")
    p.add_argument("--text", dest="fg", metavar="HEX", help="text colour (default: from the cover)")
    p.add_argument("--font", choices=list(FONTS), default="montserrat")
    p.add_argument("--weight", choices=list(WEIGHTS), default="bold")
    p.add_argument("--hide", metavar="PARTS", help=f"comma-separated parts to leave out: {', '.join(PARTS)}, artists")
    p.add_argument("--artist-image", action="store_true", help="show the artist's profile picture next to their name")
    p.add_argument("--durations", action="store_true", help="show each track's length")
    p.add_argument("--clean-titles", dest="clean", action="store_true", help="drop (feat. ...) and remaster notes from track names")
    p.add_argument("--columns", type=int, choices=range(5), default=0, help="tracklist columns (default: automatic)")
    p.add_argument("--title", help="replace the album title")
    p.add_argument("--artist", help="replace the artist name")
    p.add_argument("--caption", help="small line at the bottom, e.g. the record label")
    p.add_argument("--bleed", type=int, default=0, metavar="MM", help="extra margin for print shops, 0-10 mm")
    p.add_argument("-o", "--output", help="output file (default: '<artist> - <album>.<format>')")
    args = p.parse_args()
    args.album = [a for a in args.album if a.strip()]  # blank lines of an @FILE
    if not args.album and not args.jellyfin:
        return serve()
    try:
        opts = options({k: "" if v in (None, False) else str(v) for k, v in vars(args).items()})
        cover = own_cover(args.cover_image) if args.cover_image else None
    except ValueError as e:
        p.error(str(e))
    try:
        found = [ALBUM_RE.search(a) for a in args.album]
        if args.jellyfin:
            src = "jellyfin"
        elif len(found) > 1:
            if not all(found):
                sys.exit("Several albums must all be Spotify album links.")
            src = tuple(dict.fromkeys(m.group(1) for m in found))
        elif found[0]:
            src = found[0].group(1)
        elif tok := token():
            src = pick(args.album[0], tok)
        else:
            sys.exit("Searching by name needs Spotify credentials in .env; pass an album link instead.")
        album, img = poster(src, args.size, args.orientation, args.artist_image, cover, **opts)
    except urllib.error.HTTPError as e:
        sys.exit(f"Spotify error {e.code}: {e.read().decode(errors='replace')[:300]}")
    except (ValueError, RuntimeError) as e:
        sys.exit(str(e))
    out = args.output or file_name(album, args.format)
    save(img, out, args.format)
    print(out)


if __name__ == "__main__":
    main()
