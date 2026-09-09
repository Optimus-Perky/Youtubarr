# 🎵 Youtubarr

Youtubarr bridges YouTube playlists into Lidarr. It syncs playlists (including
your "Liked Music"), tries to identify each track's artist against
MusicBrainz, and exposes the result as a Lidarr-compatible Import List feed
— so Lidarr can pull in and manage the artists your playlists reference.

## 🚀 Features

- Add YouTube playlists by ID, including private playlists and Liked Music.
- Fetches videos and resolves each track to a MusicBrainz artist ID, using a
  consensus check (not just "top hit") to avoid confidently wrong matches.
- Exposes a Lidarr-compatible Import List feed: `/api/v1/lidarr?token=YOUR_TOKEN`.
- Web UI for managing playlists and reviewing tracks that couldn't be resolved.
- Built with Django + Celery + Redis, all in one container behind nginx.

## 🛠 Requirements

- Docker & Docker Compose
- A YouTube Data API v3 key (free from Google Cloud)
- Optionally, OAuth2 credentials — only needed for private playlists or Liked Music

## ⚡ Quick start

```
cp .env.example .env
```

Edit `.env`:

- `SECRET_KEY` — any long random string.
- `LIDARR_TOKEN` — any long random string; this is what you'll put in Lidarr's
  import list URL, so anyone who has it can read your resolved artist list.
- `ALLOWED_HOSTS` — the hostname/IP you'll browse to, e.g. `192.168.1.50` or
  `localhost`. A CSRF error on the playlists page almost always means this is
  wrong.
- `YOUTUBE_API_KEY` — see [Google API setup](#-google-api-setup) below. Can be
  left blank and set later from the Settings page instead.
- `MB_USER_AGENT` — MusicBrainz requires a real contact identifier in this
  string, e.g. `YourApp/1.0 (you@example.com)`. Requests with a generic or
  missing one get rate-limited harder.

Then check whether `docker-compose.yml`'s networking matches your setup — see
[Networking](#-networking) below, since the default in this repo assumes a
VPN passthrough container. Once it does:

```
docker compose build
docker compose up -d
```

The web UI comes up on whatever port your `docker-compose.yml` publishes (see
below). Visit `/playlists` to add your first playlist.

## 🌐 Networking

This repo's `docker-compose.yml` is set up for a specific pattern: `youtubarr`
and `redis` run with `network_mode: "container:passthroughvpn"`, sharing the
network namespace of an existing VPN container (e.g. so Lidarr and other
`*arr` apps behind the same VPN container can reach Youtubarr on localhost).
If that's not your setup, **this will fail to start** with something like
`cannot join network of a non running container`.

If you don't have a shared VPN container, replace the `network_mode` lines
with a normal port mapping instead:

```yaml
services:
  youtubarr:
    build: .
    env_file: .env
    ports:
      - "8000:80"
    volumes:
      - ./data:/data
    depends_on:
      - redis
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    restart: unless-stopped
```

...and change `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` in `.env` from
`127.0.0.1:6379` to `redis:6379`, since without the shared namespace the two
containers reach each other by service name instead of localhost.

If you *are* using the VPN-passthrough pattern: since all outbound traffic
(YouTube API, MusicBrainz) leaves through the VPN tunnel too, a shared VPN
egress IP can eat into MusicBrainz's per-IP rate limit fast. The `netproxy`
service in `docker-compose.yml` is an optional way around that — a small
proxy on the plain Docker network that lets you route just those two outbound
destinations around the tunnel via `HTTP_PROXY`/`HTTPS_PROXY` in `.env`. It's
unused unless you set those variables, so it's safe to ignore if you don't
need it.

## 🔑 Google API setup

### Option A: API key only (public/unlisted playlists)

1. In [Google Cloud Console](https://console.cloud.google.com/), enable the
   **YouTube Data API v3**.
2. Create an API key (APIs & Services → Credentials → Create Credentials →
   API key).
3. Put it in `.env`:
   ```
   YOUTUBE_API_KEY=your_api_key_here
   ```

⚠️ An API key alone can only see public and unlisted playlists — private
playlists and "Liked Music" need Option B, and will silently return empty
results otherwise (no error).

### Option B: OAuth2 (private playlists and Liked Music)

1. In Google Cloud Console, create an **OAuth 2.0 Client ID** of type "TVs and
   Limited Input devices".
2. Under **Google Auth Platform → Audience**, add your own Google account as
   a test user (required while the app is unpublished).
3. Put the client ID/secret in `.env`:
   ```
   YOUTUBE_OAUTH_CLIENT_ID=your-client-id
   YOUTUBE_OAUTH_CLIENT_SECRET=your-client-secret
   ```
4. On any machine with Python, run:
   ```
   pip install ytmusicapi
   ytmusicapi oauth
   ```
   Follow the prompts, then copy the resulting `oauth.json` into the
   directory you mount to `/data` (i.e. next to `db.sqlite3`).

Youtubarr refreshes the access token itself as it expires (roughly hourly)
and writes the new one back to `oauth.json`, so this is a one-time setup.

## 🎵 Adding playlists

Go to `/playlists` in the web UI and paste a playlist ID — the part after
`list=` in a YouTube playlist URL, e.g. `PL1234abcd...`. Use `LM` for your
Liked Music playlist (requires OAuth2 — see above).

A first full sync can take 20–30 minutes for a large playlist: MusicBrainz
allows one lookup per second, and that's the floor for how fast unresolved
tracks can be identified. Progress shows in the UI while it runs.

## 🎼 Connecting to Lidarr

1. In Lidarr: **Settings → Import Lists → + → Custom List**.
2. Set the URL to:
   ```
   http://<youtubarr-host>:<port>/api/v1/lidarr?token=YOUR_TOKEN
   ```
   replacing `YOUR_TOKEN` with the `LIDARR_TOKEN` value from `.env`.
3. Save. Lidarr will now treat Youtubarr as a source of artists, pulled from
   whichever tracks it managed to resolve to a MusicBrainz ID.

Tracks Youtubarr couldn't confidently resolve are listed on the `/items` page
with a reason, rather than being guessed at — a wrong artist match is worse
than none, since Lidarr will go and fetch that artist's whole discography.

## 🧪 Running tests

```
cd app
pytest
```

The test suite mocks every external call (YouTube, MusicBrainz, OAuth), so it
needs no network access or credentials to run.
