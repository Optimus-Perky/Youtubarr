import logging
import re
import time
from datetime import timedelta

import requests
from celery import shared_task
from dateutil import parser as dateparser
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import AppSettings, Artist, Playlist, Snapshot, TrackItem
from .utils import (
    YouTubeAuthError,
    clean_track_title,
    fetch_liked_music,
    get_oauth_bearer,
    guess_artist_from_title,
    is_placeholder_title,
    normalize_title,
)

logger = logging.getLogger(__name__)

YT_API_ITEMS = "https://www.googleapis.com/youtube/v3/playlistItems"
YT_API_PLAYLISTS = "https://www.googleapis.com/youtube/v3/playlists"
YT_API_VIDEOS = "https://www.googleapis.com/youtube/v3/videos"
MB_API = "https://musicbrainz.org/ws/2/artist/"
MB_RECORDING_API = "https://musicbrainz.org/ws/2/recording/"

_ISO8601_DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _parse_iso8601_duration(value: str) -> int | None:
    """'PT6M28S' -> 388. YouTube always returns this format for
    contentDetails.duration; None if it doesn't match at all."""
    match = _ISO8601_DURATION_RE.fullmatch(value or "")
    if not match:
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def fetch_video_durations(video_ids: list[str], api_key: str, headers: dict) -> dict[str, int]:
    """
    Real track length in seconds for each video id, straight from YouTube
    rather than guessed - the same number that shows on the video/watch page,
    useful for a human eyeballing whether an automated match is even
    plausible (see CLAUDE.md on why duration is deliberately NOT used to
    automate matching - it's still a good sanity check for a person).

    Batched 50 at a time (videos.list's max and 1 quota unit regardless of
    how many ids), so this is cheap even for a full library. Purely
    informational - a failed lookup just leaves the column blank rather than
    raising, since it should never block a sync.
    """
    out: dict[str, int] = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        params = {"part": "contentDetails", "id": ",".join(chunk), "key": api_key}
        try:
            r = requests.get(YT_API_VIDEOS, params=params, headers=headers, timeout=30)
        except requests.RequestException as exc:
            logger.warning("Could not fetch video durations: %s", exc)
            continue
        if r.status_code != 200:
            logger.warning("videos.list returned HTTP %d fetching durations", r.status_code)
            continue
        for item in r.json().get("items", []):
            vid = item.get("id")
            duration = _parse_iso8601_duration(item.get("contentDetails", {}).get("duration"))
            if vid and duration is not None:
                out[vid] = duration
    return out

# How long before we re-ask MusicBrainz about a track we could not identify.
RESOLUTION_RETRY_DAYS = 30

# A recording-title search is only trusted when this share of the exact-title
# candidates agree on the same artist, over at least this many candidates.
MB_MIN_CONSENSUS_SHARE = 0.6
MB_MIN_CONSENSUS_VOTES = 2
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


def _refresh_existing_item(ti: TrackItem, title: str, channel: str, position: int, duration_seconds=None) -> None:
    """
    Bring an existing row up to date with what YouTube now returns.

    The original code only ever updated ``position``, so rows created while the
    sync was unauthenticated kept their placeholder "Private video" titles forever
    and could never be identified. Anything the user has edited by hand is left alone.
    """
    changed = []
    if ti.position != position:
        ti.position = position
        changed.append("position")
    # Not a correction a human would make by hand, unlike title/channel below -
    # always safe to fill in or fix regardless of manually_edited.
    if duration_seconds is not None and ti.duration_seconds != duration_seconds:
        ti.duration_seconds = duration_seconds
        changed.append("duration_seconds")

    if not ti.manually_edited:
        # Don't trade a real title for a placeholder, but do replace a placeholder.
        if title and title != ti.title and not (is_placeholder_title(title) and not is_placeholder_title(ti.title)):
            ti.title = title
            changed.append("title")
        if channel and channel != ti.channel_title:
            ti.channel_title = channel
            changed.append("channel_title")
        if "title" in changed or "channel_title" in changed:
            guess = guess_artist_from_title(ti.title, ti.channel_title)
            if guess != ti.artist_name_guess:
                ti.artist_name_guess = guess
                changed.append("artist_name_guess")
            # Metadata moved, so any previous verdict is stale - allow a retry.
            ti.resolution_attempted_at = None
            changed.append("resolution_attempted_at")

    if changed:
        ti.save(update_fields=changed)


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
        page_items = data.get("items", [])
        video_ids = [
            it.get("snippet", {}).get("resourceId", {}).get("videoId")
            for it in page_items
        ]
        durations = fetch_video_durations([v for v in video_ids if v], api_key, headers)

        for it in page_items:
            sn = it.get("snippet", {})
            vd = sn.get("resourceId", {}).get("videoId")
            if not vd:
                continue
            title = sn.get("title", "")
            # channelTitle here is the PLAYLIST owner (i.e. you). The uploader,
            # which is what might name the artist, is videoOwnerChannelTitle.
            ch = sn.get("videoOwnerChannelTitle") or ""
            published = sn.get("publishedAt")
            position = sn.get("position", 0)
            duration_seconds = durations.get(vd)

            with transaction.atomic():
                ti, created = TrackItem.objects.get_or_create(
                    playlist=playlist,
                    video_id=vd,
                    defaults=dict(
                        title=title,
                        channel_title=ch,
                        position=position,
                        published_at=dateparser.parse(published) if published else None,
                        artist_name_guess=guess_artist_from_title(title, ch),
                        duration_seconds=duration_seconds,
                    ),
                )
                if not created:
                    _refresh_existing_item(ti, title, ch, position, duration_seconds)
            count += 1

        token = data.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token

    playlist.last_synced = timezone.now()
    playlist.save(update_fields=["last_synced"])
    return count


def lookup_mb_artist_name(mbid: str) -> str | None:
    """Fetch an artist's canonical name for a MBID, so a manually-entered ID in
    the UI can be stored under its real name rather than whatever guess was
    on the track."""
    try:
        r = requests.get(f"{MB_API}{mbid}", params={"fmt": "json"}, headers=MB_HEADERS, timeout=30)
    except requests.RequestException as exc:
        logger.warning("MusicBrainz artist lookup failed for %r: %s", mbid, exc)
        return None
    if r.status_code == 200:
        return r.json().get("name")
    return None


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



def search_mb_artist_by_recording(title: str):
    """
    Identify an artist from a song title alone, by asking MusicBrainz for the
    recording and seeing whether its candidates agree.

    Many tracks (personal uploads especially) carry no artist anywhere in their
    YouTube metadata, so the title is all we have. A plain title search is not
    trustworthy on its own - "Children" returns eight unrelated artists - so we
    keep only candidates whose title matches exactly and require most of them to
    name the same artist. Duration is deliberately NOT used: uploads are usually
    edits or remasters whose length does not match the MusicBrainz recording.

    Returns (name, mbid, note). name/mbid are None when nothing is trustworthy;
    note always explains the outcome.
    """
    cleaned = clean_track_title(title)
    target = normalize_title(cleaned)
    if not target:
        return None, None, "no usable title"

    params = {"query": f'recording:"{cleaned}"', "fmt": "json", "limit": 25}
    try:
        r = requests.get(MB_RECORDING_API, params=params, headers=MB_HEADERS, timeout=30)
    except requests.RequestException as exc:
        return None, None, f"MusicBrainz unreachable: {exc}"
    if r.status_code != 200:
        return None, None, f"MusicBrainz returned HTTP {r.status_code}"

    recordings = r.json().get("recordings") or []
    exact = [rec for rec in recordings if normalize_title(rec.get("title") or "") == target]
    if not exact:
        return None, None, f"no exact title match ({len(recordings)} candidates)"

    votes = {}
    for rec in exact:
        credit = (rec.get("artist-credit") or [{}])[0].get("artist") or {}
        mbid, name = credit.get("id"), credit.get("name")
        if not mbid:
            continue
        entry = votes.setdefault(mbid, {"name": name, "count": 0})
        entry["count"] += 1

    if not votes:
        return None, None, "candidates had no artist credit"

    total = sum(v["count"] for v in votes.values())
    best_mbid, best = max(votes.items(), key=lambda kv: kv[1]["count"])
    share = best["count"] / total

    if best["count"] < MB_MIN_CONSENSUS_VOTES or share < MB_MIN_CONSENSUS_SHARE:
        others = len(votes)
        return None, None, (
            f"ambiguous: {others} artists for this title, "
            f"best is {best['name']} with only {best['count']}/{total}"
        )

    return best["name"], best_mbid, f"MusicBrainz recording consensus {best['count']}/{total}"


def _link_artist(ti: TrackItem, name: str, mbid: str, note: str) -> None:
    artist, _ = Artist.objects.get_or_create(name=name)
    if mbid and artist.mbid != mbid:
        artist.mbid = mbid
        artist.resolved_from = note
        artist.save(update_fields=["mbid", "resolved_from"])
    ti.artist = artist
    ti.resolution_note = note
    ti.resolution_attempted_at = timezone.now()
    ti.save(update_fields=["artist", "resolution_note", "resolution_attempted_at"])


def _resolve_items(items: list[TrackItem], progress=None) -> dict:
    """
    The actual matching loop, shared by a full backlog pass and a hand-picked
    selection from the Items page.

    Two routes, in order of trust:
      1. the artist name parsed out of the title/uploader, looked up directly
      2. failing that, the song title resolved by recording-search consensus

    Unlike the original, a failed lookup no longer creates an empty Artist row that
    the track gets permanently attached to - tracks simply stay unresolved and are
    retried later, with a note saying why they failed.
    """
    total = len(items)
    resolved = unresolved = 0
    by_name, by_title = {}, {}

    for i, ti in enumerate(items, 1):
        if progress and (i == 1 or i % 25 == 0 or i == total):
            progress(f"Identifying artists: {i} of {total}")

        name = mbid = None
        note = ""

        # 1) we think we know the artist's name already
        guess = (ti.artist_name_guess or "").strip()
        if guess:
            if guess not in by_name:
                by_name[guess] = search_mb_artist_mbid(guess)
                time.sleep(1.05)  # MusicBrainz asks for max 1 request/second
            if by_name[guess]:
                name, mbid, note = guess, by_name[guess], "MusicBrainz artist search"

        # 2) otherwise ask what recording this title is
        if not mbid and ti.title and not is_placeholder_title(ti.title):
            key = normalize_title(clean_track_title(ti.title))
            if key not in by_title:
                by_title[key] = search_mb_artist_by_recording(ti.title)
                time.sleep(1.05)
            name, mbid, note = by_title[key]

        if mbid and name:
            _link_artist(ti, name, mbid, note)
            resolved += 1
        else:
            unresolved += 1
            ti.resolution_note = note or "could not identify an artist"
            ti.resolution_attempted_at = timezone.now()
            ti.save(update_fields=["resolution_note", "resolution_attempted_at"])

    return {"resolved": resolved, "unresolved": unresolved, "considered": total}


def _pending_qs():
    return (
        TrackItem.objects.filter(blacklisted=False)
        .filter(Q(artist__isnull=True) | Q(artist__mbid__isnull=True) | Q(artist__mbid=""))
    )


def resolve_mbids(progress=None, force=False) -> dict:
    """Give every track an artist with a MusicBrainz ID, so Lidarr has something
    to act on. See _resolve_items for how a single track is actually matched."""
    pending = _pending_qs().order_by("id")
    if not force:
        cutoff = timezone.now() - timedelta(days=RESOLUTION_RETRY_DAYS)
        pending = pending.filter(
            Q(resolution_attempted_at__isnull=True) | Q(resolution_attempted_at__lt=cutoff)
        )
    return _resolve_items(list(pending), progress=progress)


def resolve_mbids_for_items(item_ids, progress=None) -> dict:
    """
    Force-match the tracks hand-picked on the Items page - regardless of
    RESOLUTION_RETRY_DAYS, and regardless of whether a row already has an
    artist. Picking specific rows and clicking "Match selected" is itself
    the override: unlike the backlog pass, this doesn't skip already-linked
    tracks, so it doubles as a way to re-run a match you suspect is wrong.
    A re-run that doesn't find anything conclusive leaves the existing link
    alone (see _resolve_items) rather than clearing it.
    """
    items = list(
        TrackItem.objects.filter(blacklisted=False, id__in=item_ids).order_by("id")
    )
    return _resolve_items(items, progress=progress)


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
    stats = resolve_mbids(progress=progress)
    summary["artists_resolved"] = stats["resolved"]
    summary["unresolved"] = stats["unresolved"]

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
def resolve_missing_mbids(force=False):
    return resolve_mbids(force=force)


@shared_task(bind=True)
def resolve_selected(self, item_ids):
    def progress(msg):
        self.update_state(state="PROGRESS", meta={"message": msg})
    return resolve_mbids_for_items(item_ids, progress=progress)


@shared_task
def build_snapshot():
    return make_snapshot()


@shared_task
def refresh_all_and_snapshot():
    """Scheduled full refresh. Runs the whole pipeline in order."""
    return run_sync()
