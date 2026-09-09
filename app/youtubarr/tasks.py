import logging
import time

import requests
from celery import shared_task
from dateutil import parser as dateparser
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import AppSettings, Artist, Playlist, Snapshot, TrackItem
from .utils import (
    YouTubeAuthError,
    fetch_liked_music,
    get_oauth_bearer,
    guess_artist_from_title,
)

logger = logging.getLogger(__name__)

YT_API_ITEMS = "https://www.googleapis.com/youtube/v3/playlistItems"
YT_API_PLAYLISTS = "https://www.googleapis.com/youtube/v3/playlists"
MB_API = "https://musicbrainz.org/ws/2/artist/"
MB_HEADERS = {"User-Agent": settings.MB_USER_AGENT}


class PlaylistSyncError(RuntimeError):
    """A sync failure with a message that is safe to show in the UI."""


def _get_api_key():
    s = AppSettings.load()
    return s.youtube_api_key or settings.YOUTUBE_API_KEY


def _auth_headers():
    """
    Bearer header for the YouTube Data API, refreshed on demand.

    Private playlists are invisible to an API-key-only request, so this is what makes
    them sync. Returns {} when OAuth is not set up, which still allows public and
    unlisted playlists to sync with just the API key.
    """
    try:
        return {"Authorization": get_oauth_bearer()}
    except YouTubeAuthError as exc:
        logger.info("OAuth not available, falling back to API key only: %s", exc)
        return {}


def _api_error_message(response, playlist_id, authed):
    """Turn a Google API error response into something a human can act on."""
    detail = ""
    try:
        payload = response.json()
        err = payload.get("error", {})
        detail = err.get("message", "")
        reasons = [e.get("reason", "") for e in err.get("errors", []) if e.get("reason")]
        if reasons:
            detail = f"{detail} ({', '.join(reasons)})" if detail else ", ".join(reasons)
    except Exception:
        detail = (response.text or "")[:300]

    msg = f"YouTube API returned HTTP {response.status_code} for playlist {playlist_id}"
    if detail:
        msg = f"{msg}: {detail}"

    if response.status_code == 404 and not authed:
        msg += (
            " — this playlist is not visible with an API key alone. If it is private, "
            "set up OAuth (oauth.json + client id/secret) so Youtubarr can authenticate as you."
        )
    elif response.status_code in (401, 403) and authed:
        msg += (
            " — the OAuth token was rejected. Check that the Google account that created "
            "oauth.json owns this playlist, and that the YouTube Data API v3 is enabled."
        )
    return msg


def _upsert_liked_music(playlist: Playlist) -> int:
    items = fetch_liked_music()
    count = 0
    for it in items:
        with transaction.atomic():
            ti, created = TrackItem.objects.get_or_create(
                playlist=playlist,
                video_id=it["video_id"],
                defaults=dict(
                    title=it["title"],
                    artist_name_guess=it["artist"],
                    channel_title="YouTube Music",
                    position=0,  # LM doesn't have stable positions
                    published_at=None,
                ),
            )
            if not created:
                ti.title = it["title"]
                ti.artist_name_guess = it["artist"]
                ti.save(update_fields=["title", "artist_name_guess"])
        count += 1

    playlist.title = "Liked Music"
    playlist.channel_title = "YouTube Music"
    playlist.last_synced = timezone.now()
    playlist.save(update_fields=["title", "channel_title", "last_synced"])
    return count


def fetch_playlist_items(playlist: Playlist) -> int:
    """
    Sync one playlist. Returns the number of items seen.

    Raises PlaylistSyncError / YouTubeAuthError with a readable message instead of
    silently returning 0, so failures are visible in the UI.
    """
    if playlist.playlist_id == "LM":
        return _upsert_liked_music(playlist)

    api_key = _get_api_key()
    if not api_key:
        raise PlaylistSyncError(
            "No YouTube API key configured. Set YOUTUBE_API_KEY in .env or enter one on the Settings page."
        )

    headers = _auth_headers()
    authed = bool(headers)

    # --- Fetch playlist metadata ---
    meta_params = {"part": "snippet", "id": playlist.playlist_id, "key": api_key}
    try:
        rmeta = requests.get(YT_API_PLAYLISTS, params=meta_params, headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise PlaylistSyncError(f"Could not reach the YouTube API: {exc}") from exc

    if rmeta.status_code == 200:
        meta_items = rmeta.json().get("items", [])
        if meta_items:
            sn = meta_items[0].get("snippet", {})
            playlist.title = sn.get("title", playlist.title)
            playlist.channel_title = sn.get("channelTitle", playlist.channel_title)
            playlist.save(update_fields=["title", "channel_title"])
    elif rmeta.status_code in (401, 403, 404):
        raise PlaylistSyncError(_api_error_message(rmeta, playlist.playlist_id, authed))

    # --- Fetch playlist items ---
    params = {
        "part": "snippet,contentDetails",
        "playlistId": playlist.playlist_id,
        "maxResults": settings.YOUTUBE_QUOTA_SAFE_PAGE_SIZE,
        "key": api_key,
    }
    count = 0
    while True:
        try:
            r = requests.get(YT_API_ITEMS, params=params, headers=headers, timeout=30)
        except requests.RequestException as exc:
            raise PlaylistSyncError(f"Could not reach the YouTube API: {exc}") from exc

        if r.status_code != 200:
            raise PlaylistSyncError(_api_error_message(r, playlist.playlist_id, authed))

        data = r.json()
        for it in data.get("items", []):
            sn = it.get("snippet", {})
            vd = sn.get("resourceId", {}).get("videoId")
            if not vd:
                continue
            title = sn.get("title", "")
            ch = sn.get("channelTitle", "")
            published = sn.get("publishedAt")
            artist_guess = guess_artist_from_title(title, ch)

            with transaction.atomic():
                ti, created = TrackItem.objects.get_or_create(
                    playlist=playlist,
                    video_id=vd,
                    defaults=dict(
                        title=title,
                        channel_title=ch,
                        position=sn.get("position", 0),
                        published_at=dateparser.parse(published) if published else None,
                        artist_name_guess=artist_guess,
                    ),
                )
                if not created:
                    # only update "machine" fields that should always be current
                    ti.position = sn.get("position", ti.position)
                    ti.save(update_fields=["position"])
            count += 1

        token = data.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token

    playlist.last_synced = timezone.now()
    playlist.save(update_fields=["last_synced"])
    return count


def search_mb_artist_mbid(name: str) -> str | None:
    if not name:
        return None
    params = {"query": f'artist:"{name}"', "fmt": "json"}
    try:
        r = requests.get(MB_API, params=params, headers=MB_HEADERS, timeout=30)
    except requests.RequestException as exc:
        logger.warning("MusicBrainz lookup failed for %r: %s", name, exc)
        return None
    if r.status_code == 200:
        arts = r.json().get("artists") or []
        if arts:
            return arts[0]["id"]
    return None


def resolve_mbids(progress=None) -> int:
    """Resolve MusicBrainz IDs for artists we haven't looked up yet."""
    names = list(
        TrackItem.objects
        .filter(blacklisted=False, artist__isnull=True)
        .exclude(artist_name_guess="")
        .values_list("artist_name_guess", flat=True)
        .distinct()
    )
    total = len(names)
    resolved = 0
    for i, name in enumerate(names, 1):
        if progress:
            progress(f"Looking up artist {i} of {total} on MusicBrainz")
        mbid = search_mb_artist_mbid(name)
        time.sleep(1.05)  # MusicBrainz asks for max 1 request/second
        art, _ = Artist.objects.get_or_create(name=name)
        if mbid and not art.mbid:
            art.mbid = mbid
            art.save(update_fields=["mbid"])
            resolved += 1

    # Link TrackItems that now have an Artist row
    for ti in TrackItem.objects.filter(artist__isnull=True).exclude(artist_name_guess=""):
        try:
            ti.artist = Artist.objects.get(name=ti.artist_name_guess)
            ti.save(update_fields=["artist"])
        except Artist.DoesNotExist:
            pass
    return resolved


def make_snapshot() -> int:
    """Build the payload Lidarr reads. Returns the number of artists in it."""
    mbids = (
        Artist.objects.exclude(mbid__isnull=True)
        .exclude(mbid__exact="")
        .filter(trackitem__blacklisted=False)
        .values_list("mbid", flat=True)
        .distinct()
    )
    payload = [{"MusicBrainzId": mbid} for mbid in mbids]
    Snapshot.objects.create(payload=payload)
    logger.info("Snapshot created with %d artists", len(payload))
    return len(payload)


def sync_playlists(playlist_ids=None, progress=None) -> dict:
    """
    Fetch items for the given playlists (or all enabled ones). Returns a summary.

    Per-playlist failures are collected rather than aborting the run, so one bad
    playlist does not stop the rest.
    """
    def note(msg):
        logger.info("[sync] %s", msg)
        if progress:
            progress(msg)

    if playlist_ids:
        qs = Playlist.objects.filter(playlist_id__in=playlist_ids)
    else:
        qs = Playlist.objects.filter(enabled=True)
    playlists = list(qs)

    results, errors, total_items = [], [], 0
    for i, pl in enumerate(playlists, 1):
        label = pl.title or pl.playlist_id
        note(f"Syncing playlist {i} of {len(playlists)}: {label}")
        try:
            n = fetch_playlist_items(pl)
            total_items += n
            results.append({"playlist": label, "items": n, "ok": True})
        except (PlaylistSyncError, YouTubeAuthError) as exc:
            logger.warning("Sync failed for %s: %s", pl.playlist_id, exc)
            errors.append(f"{label}: {exc}")
            results.append({"playlist": label, "items": 0, "ok": False, "error": str(exc)})
        except Exception as exc:
            logger.exception("Unexpected error syncing %s", pl.playlist_id)
            errors.append(f"{label}: unexpected error: {exc}")
            results.append({"playlist": label, "items": 0, "ok": False, "error": str(exc)})

    return {"playlists": results, "items": total_items, "errors": errors}


def run_sync(playlist_ids=None, progress=None) -> dict:
    """
    Full pipeline: fetch playlists, resolve artists, publish a Lidarr snapshot.
    Runs synchronously; safe to call from a Celery task or inline from a view.
    """
    summary = sync_playlists(playlist_ids=playlist_ids, progress=progress)

    if progress:
        progress("Resolving artists on MusicBrainz")
    summary["artists_resolved"] = resolve_mbids(progress=progress)

    if progress:
        progress("Building Lidarr snapshot")
    summary["snapshot_artists"] = make_snapshot()

    return summary


# --------------------------------------------------------------------------- #
# Celery tasks
# --------------------------------------------------------------------------- #

@shared_task(bind=True)
def sync_task(self, playlist_ids=None):
    def progress(msg):
        self.update_state(state="PROGRESS", meta={"message": msg})
    return run_sync(playlist_ids=playlist_ids, progress=progress)


@shared_task
def refresh_playlists():
    return sync_playlists()["items"]


@shared_task
def resolve_missing_mbids():
    return resolve_mbids()


@shared_task
def build_snapshot():
    return make_snapshot()


@shared_task
def refresh_all_and_snapshot():
    """Scheduled full refresh. Runs the whole pipeline in order."""
    return run_sync()
