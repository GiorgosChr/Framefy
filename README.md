# Framefy

Makes a printable poster from a Spotify album.

<img src="examples/portrait.png" height="320"> <img src="examples/landscape.png" height="320">

## Setup

    pip install pillow

## Use

    python framefy.py

This opens the UI in your browser: paste a Spotify album link, choose the options, download the poster. No Spotify API key is needed.

From the terminal instead:

    python framefy.py https://open.spotify.com/album/30TlXptEYPQHt9ozcEeqBt --size A3 --orientation landscape --format pdf

| Option | Values | Default |
| --- | --- | --- |
| `--size` | `A4`, `A3` | `A4` |
| `--orientation` | `portrait`, `landscape` | `portrait` |
| `--format` | `png`, `pdf` | `png` |
| `--artist-image` | flag: adds the artist's profile picture next to their name | off |
| `-o` | output file | `<artist> - <album>.<format>` |

## Searching by name (optional)

Searching albums by name, in the UI or with `python framefy.py "polaris the guilt and the grief"`, needs your Spotify API credentials. Copy the example file:

    cp .env.example .env

Open `.env` and add your client ID and client secret after the `=`:

    SPOTIFY_CLIENT_ID=
    SPOTIFY_CLIENT_SECRET=

`.env` is not tracked by git. Variables already exported in the shell take priority over the file.
