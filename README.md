# framefy

Makes a printable poster from a Spotify album.

<img src="examples/portrait.png" height="320"> <img src="examples/landscape.png" height="320">

## Setup

    pip install pillow
    export SPOTIFY_CLIENT_ID=...
    export SPOTIFY_CLIENT_SECRET=...

The two values come from an app at https://developer.spotify.com/dashboard.

## Use

    python framefy.py "periphery clear"
    python framefy.py https://open.spotify.com/album/5DeKC7Werv3iQRhWWANQFj --size A3 --orientation landscape --format pdf

Search text lists matching albums to browse and pick from; an album link skips the search.

| Option | Values | Default |
| --- | --- | --- |
| `--size` | `A4`, `A3` | `A4` |
| `--orientation` | `portrait`, `landscape` | `portrait` |
| `--format` | `png`, `pdf` | `png` |
| `-o` | output file | `<artist> - <album>.<format>` |
