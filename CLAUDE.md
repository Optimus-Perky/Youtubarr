# Youtubarr — working notes

Bridges YouTube playlists into Lidarr. Django + Celery + Redis in one container
(supervisord runs gunicorn, celery worker, celery beat and nginx). Lidarr reads
`/api/v1/lidarr?token=…`, which serves the newest `Snapshot` — a list of
MusicBrainz artist IDs.

The pipeline: **fetch playlist items → identify each track's artist → publish
their MBIDs as a snapshot.** The middle step is the hard one and most of these
notes concern it.

## Layout

    app/core/          settings, urls, celery app
    app/youtubarr/     models, views, tasks, utils, templates
    app/tests/         pytest suite (34 tests, all passing)

`tasks.py` holds the pipeline: `fetch_playlist_items` → `resolve_mbids` →
`make_snapshot`, wrapped by `run_sync`. Views dispatch `sync_task` to Celery and
fall back to running inline if no worker answers.

Run the tests with `cd app && pytest`. They mock every external call — no
network, no credentials needed.

## Deployment (Unraid, host CoruscantUR)

Built and run from `/mnt/user/appdata/YouTubarr`, **not** from the git clone.
Files are copied across from `Z:\YouTubarr\Git` on the Windows PC.

Both containers use `network_mode: container:passthroughvpn`, so all outbound
traffic leaves via the VPN. Consequences:

- **No `ports:` section** — Docker forbids publishing ports in container network
  mode. Publish the web port on `passthroughvpn` itself.
- **Redis is `127.0.0.1:6379`**, not `redis:6379`; both containers share one
  localhost. `CELERY_BROKER_URL` in `.env` must match.
- `passthroughvpn` must be running first, and if it restarts, restart these two.

### Never overwrite these

`.env` and `data/` (holding `db.sqlite3` and `oauth.json`) exist **only** in
appdata — they're gitignored, so copying the repo over the top destroys them.
This has bitten twice. `docker-compose.yml` was a third casualty until its VPN
config was committed to the repo.

## Things that cost us a lot of time

**CRLF line endings.** Cloning on Windows gave `entrypoint.sh` a `#!/bin/sh\r`
shebang, and the container failed with `exec /entrypoint.sh: no such file or
directory` — the file exists; the interpreter path is corrupt. `.gitattributes`
now pins `.sh`, `.conf`, `Dockerfile` etc. to LF. If it recurs on an existing
checkout: `git rm --cached -r . && git reset --hard`.

**`snippet.channelTitle` is the playlist's owner, not the uploader.** The
uploader is `videoOwnerChannelTitle`. The original code used the former, so the
"— Topic" artist heuristic could never fire — it was comparing the account
owner's name against "- Topic" on every track.

**Private playlists need an OAuth bearer header.** An API key alone returns
empty results with no error. `utils.get_oauth_bearer()` builds a ytmusicapi
`RefreshingToken`, so the hourly access-token expiry is handled and the new
token is written back to `oauth.json`. Reading `access_token` straight from the
file works for about an hour and then silently stops — don't.

**ytmusicapi OAuth is broken upstream.** `search`, `get_playlist` and therefore
Liked Music (`LM`) all return HTTP 400 from YouTube Music's internal API, while
the same token works fine against the *Data* API. Not a config problem; browser
auth is the only known workaround. See sigma67/ytmusicapi#676.

**Many tracks carry no artist at all.** A large share are personal uploads on a
channel like "Music Library Uploads", titled with just the song name — `Pjanoo`,
`Children`, `9PM (Till I Come)`. There is no artist anywhere in the metadata, so
title parsing cannot work and the artist has to be inferred.

## Why artist matching works the way it does

`search_mb_artist_by_recording()` searches MusicBrainz for the *recording*, keeps
only candidates whose normalised title matches exactly, and requires a majority
(≥60%, ≥2 votes) to agree on one artist. No agreement means no guess.

Two rejected alternatives, both tested against real data:

- **Top hit.** MusicBrainz scores nearly everything 100, so the score carries no
  confidence at all. "Children" returns eight different artists, all scoring 100,
  none of them Robert Miles.
- **Duration matching.** Tempting and wrong. Uploads are edits and remasters
  whose lengths don't match MusicBrainz recordings. For `Pjanoo` (183s video) not
  one of eight candidates was within 8s — yet all eight were Eric Prydz, the
  correct answer. Meanwhile it confidently matched `Right Here, Right Now` to
  "People Playing Music" at 240s. It rejects right answers and accepts wrong ones.

Consensus gets Pjanoo, Lola's Theme and 9PM right, and correctly refuses
`Children` and `Right Here, Right Now`. **A wrong artist is worse than none** —
Lidarr will go and fetch that discography. Unresolved tracks are surfaced with a
reason on the Items page for manual correction.

`test_artist_resolution.py` encodes these cases with fixtures trimmed from the
real responses. If you change the thresholds, those tests are the spec.

## Invariants worth preserving

- `last_synced` is stamped **only after items are actually fetched**. The
  original stamped it during the metadata call, so playlists that fetched nothing
  still looked freshly synced — which is what hid the whole bug for so long.
- **Failed lookups must not create an empty `Artist` row.** The original did, then
  linked tracks to it, and the resolver skipped anything already linked — so a
  single failure was permanent. Tracks now stay unlinked and are retried after
  `RESOLUTION_RETRY_DAYS`.
- **Syncs refresh existing rows**, not just `position`. Rows created while
  unauthenticated held placeholder "Private video" titles that could never
  self-heal, because only `position` was ever updated.
- **`manually_edited` protects human corrections** from being overwritten.
- **Errors reach the UI.** The original swallowed non-200s with a bare `break`
  and returned 0, making failure indistinguishable from an empty playlist. Keep
  raising `PlaylistSyncError` with a message worth reading.
- **supervisord forwards child logs to stdout** (`/dev/fd/1`, `maxbytes=0`).
  Without it, gunicorn and celery output vanishes into temp files and
  `docker compose logs` looks idle even mid-sync.

## Known rough edges

- The **inline fallback** (used when no Celery worker answers) will start a
  25-minute job inside a web request and get killed at the gunicorn timeout with
  no record. It should refuse work that size and report the worker as down.
- **Rebuilding mid-sync** kills the task silently; the page then polls a task id
  that no longer exists. The task should notice and say so.
- A first full resolve takes **20–30 minutes** for ~1400 tracks — MusicBrainz
  allows one request per second and that's the floor.

## Handy commands

    docker compose exec youtubarr celery -A core inspect active     # is a sync running?
    docker compose logs -f youtubarr                                # works properly now
    docker compose exec youtubarr python manage.py shell -c "..."   # poke the DB

Progress of a running resolve:

    docker compose exec youtubarr python manage.py shell -c "
    from youtubarr.models import TrackItem, Artist
    print(TrackItem.objects.filter(resolution_attempted_at__isnull=False).count(),
          'of', TrackItem.objects.count(), 'processed')
    print('artists:', Artist.objects.exclude(mbid__isnull=True).exclude(mbid='').count())"
