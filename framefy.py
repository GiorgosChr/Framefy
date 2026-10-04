#!/usr/bin/env python3
"""Make a poster from a Spotify album."""
import argparse
import base64
import calendar
import functools
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

API = "https://api.spotify.com/v1"
ALBUM_RE = re.compile(r"(?:open\.spotify\.com/(?:intl-\w+/)?album/|spotify:album:)([A-Za-z0-9]{22})")
FONT = str(Path(__file__).parent / "fonts" / "Montserrat.ttf")
DPI = 300
SIZES = {"A4": (2480, 3508), "A3": (3508, 4961)}  # short, long side in px at 300 dpi


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


def token():
    load_env()
    cid, secret = os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not (cid and secret):
        sys.exit("Copy .env.example to .env and fill in your Spotify credentials (see README).")
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    body = fetch("https://accounts.spotify.com/api/token",
                 {"Authorization": f"Basic {auth}"}, b"grant_type=client_credentials")
    return json.loads(body)["access_token"]


def api(path, tok):
    url = path if path.startswith("http") else API + path
    return json.loads(fetch(url, {"Authorization": f"Bearer {tok}"}))


def pick(query, tok):
    """Browse search results ten at a time and return the chosen album id."""
    offset = 0
    while True:
        q = urllib.parse.urlencode({"q": query, "type": "album", "limit": 10, "offset": offset})
        items = [a for a in api(f"/search?{q}", tok)["albums"]["items"] if a]
        if not items:
            if offset == 0:
                sys.exit("No albums found.")
            print("No more results.")
            offset -= 10
            continue
        for i, a in enumerate(items, 1):
            artists = ", ".join(x["name"] for x in a["artists"])
            print(f"{i:2}. {a['name']} — {artists} ({a['release_date'][:4]})")
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


def load_cover(url):
    # ponytail: undocumented hi-res variant of the 640px cover URL; falls back if Spotify drops it
    try:
        return Image.open(io.BytesIO(fetch(url.replace("ab67616d0000b273", "ab67616d000082c1"))))
    except OSError:
        return Image.open(io.BytesIO(fetch(url)))


def load_code(album_id):
    """Spotify scan code as a mask (white = ink), or None if it cannot be fetched."""
    url = f"https://scannables.scdn.co/uri/plain/png/000000/white/1280/spotify:album:{album_id}"
    try:
        return Image.open(io.BytesIO(fetch(url))).convert("L")
    except OSError:
        print("warning: could not fetch the Spotify code, leaving it out", file=sys.stderr)


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
    px = list(cover.convert("RGB").resize((64, 64)).getdata())
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
    return bg, fg, (cols[1:] + [fg] * 3)[:3]


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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("album", help="Spotify album link, or text to search for")
    p.add_argument("--size", type=str.upper, choices=list(SIZES), default="A4")
    p.add_argument("--orientation", type=str.lower, choices=["portrait", "landscape"], default="portrait")
    p.add_argument("--format", type=str.lower, choices=["png", "pdf"], default="png")
    p.add_argument("--artist-image", action="store_true", help="show the artist's profile picture next to their name")
    p.add_argument("-o", "--output", help="output file (default: '<artist> - <album>.<format>')")
    args = p.parse_args()
    try:
        tok = token()
        m = ALBUM_RE.search(args.album)
        album = load_album(m.group(1) if m else pick(args.album, tok), tok)
        artist_img = None
        if args.artist_image:
            images = api(f"/artists/{album['artist_id']}", tok)["images"]
            if images:
                artist_img = load_cover(images[0]["url"])
            else:
                print("warning: the artist has no profile picture", file=sys.stderr)
        img = render(album, load_cover(album["cover_url"]), load_code(album["id"]),
                     args.size, args.orientation, artist_img)
    except urllib.error.HTTPError as e:
        sys.exit(f"Spotify error {e.code}: {e.read().decode(errors='replace')[:300]}")
    name = re.sub(r'[\\/:*?"<>|]', "_", f"{album['artist']} - {album['title']}")
    out = args.output or f"{name}.{args.format}"
    save(img, out, args.format)
    print(out)


if __name__ == "__main__":
    main()
