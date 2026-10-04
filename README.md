# Framefy

Makes a printable poster from a Spotify album.

<img src="examples/portrait.png" height="320"> <img src="examples/landscape.png" height="320">

## Setup

    pip install pillow

## Adding your Spotify API credentials

Copy the example file:

    cp .env.example .env

Open `.env` and put your Spotify client ID and client secret in place of the placeholders:

    SPOTIFY_CLIENT_ID=your_client_id
    SPOTIFY_CLIENT_SECRET=your_client_secret

`.env` is not tracked by git. Variables already exported in the shell take priority over the file.

## Use

    python framefy.py "polaris the guilt and the grief"
    python framefy.py https://open.spotify.com/album/30TlXptEYPQHt9ozcEeqBt --size A3 --orientation landscape --format pdf

Search text lists matching albums to browse and pick from; an album link skips the search.

| Option | Values | Default |
| --- | --- | --- |
| `--size` | `A4`, `A3` | `A4` |
| `--orientation` | `portrait`, `landscape` | `portrait` |
| `--format` | `png`, `pdf` | `png` |
| `--artist-image` | flag: adds the artist's profile picture next to their name | off |
| `-o` | output file | `<artist> - <album>.<format>` |
