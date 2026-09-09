import json
import logging
import os
import re
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

DATA_DIR = getattr(settings, "DATA_DIR", "/data")
OAUTH_PATH = os.path.join(DATA_DIR, "oauth.json")


class YouTubeAuthError(RuntimeError):
    """Raised when OAuth is not usable. Message is safe to show in the UI."""


def guess_artist_from_title(title: str, channel_title: str) -> str:
    """
    Heuristics:
    - 'Artist - Song' => take left half
    - Channel like 'Foo - Topic' => use 'Foo'
    - Otherwise: empty string (we'll skip for MB search)
    """
    if " - " in title:
        left = title.split(" - ", 1)[0].strip()
        # avoid generic prefixes like "Official Video"
        if len(left) >= 2:
            return left
    if " - Topic" in channel_title:
        return channel_title.replace(" - Topic", "").strip()
    return ""


def oauth_status() -> dict:
    """Describe the OAuth setup so the UI can explain what is missing."""
    return {
        "file_exists": os.path.exists(OAUTH_PATH),
        "path": OAUTH_PATH,
        "client_id_set": bool(settings.YOUTUBE_OAUTH_CLIENT_ID),
        "client_secret_set": bool(settings.YOUTUBE_OAUTH_CLIENT_SECRET),
    }


def _require_oauth():
    """Validate OAuth prerequisites, raising a user-readable error."""
    if not os.path.exists(OAUTH_PATH):
        raise YouTubeAuthError(
            f"No oauth.json found at {OAUTH_PATH}. Run 'ytmusicapi oauth' and place the "
            f"resulting file in the directory you mount to {DATA_DIR}."
        )
    if not settings.YOUTUBE_OAUTH_CLIENT_ID or not settings.YOUTUBE_OAUTH_CLIENT_SECRET:
        raise YouTubeAuthError(
            "YOUTUBE_OAUTH_CLIENT_ID / YOUTUBE_OAUTH_CLIENT_SECRET are not set in .env. "
            "They must match the OAuth client you used to create oauth.json."
        )


def _credentials():
    from ytmusicapi import OAuthCredentials

    return OAuthCredentials(
        client_id=settings.YOUTUBE_OAUTH_CLIENT_ID,
        client_secret=settings.YOUTUBE_OAUTH_CLIENT_SECRET,
    )


def get_ytmusic():
    """
    Return an authenticated YTMusic instance.

    Raises YouTubeAuthError with a message suitable for display in the web UI.
    """
    from ytmusicapi import YTMusic

    _require_oauth()
    try:
        return YTMusic(OAUTH_PATH, oauth_credentials=_credentials())
    except Exception as exc:
        raise YouTubeAuthError(f"Could not initialise YouTube Music client: {exc}") from exc


def get_oauth_bearer() -> str:
    """
    Return a *fresh* 'Bearer <access_token>' header value for the YouTube Data API.

    The access token in oauth.json expires roughly hourly. ytmusicapi's RefreshingToken
    renews it using the refresh token and writes the new value back to oauth.json, so
    this keeps working indefinitely without re-running 'ytmusicapi oauth'.
    """
    _require_oauth()

    try:
        from ytmusicapi.auth.oauth import RefreshingToken
        from ytmusicapi.auth.oauth.token import Token

        with open(OAUTH_PATH, encoding="utf-8") as fh:
            stored = json.load(fh)

        # Google's device flow adds keys (e.g. refresh_token_expires_in) that the
        # dataclass does not accept, so filter to the fields it knows about.
        kwargs = {k: stored[k] for k in Token.members() if k in stored}
        missing = {"access_token", "refresh_token"} - set(kwargs)
        if missing:
            raise YouTubeAuthError(
                f"{OAUTH_PATH} is missing {', '.join(sorted(missing))}. "
                "Re-run 'ytmusicapi oauth' to regenerate it."
            )

        token = RefreshingToken(
            credentials=_credentials(),
            _local_cache=Path(OAUTH_PATH),
            **kwargs,
        )
        # Attribute access triggers refresh-if-expiring and rewrites oauth.json.
        return token.as_auth()
    except YouTubeAuthError:
        raise
    except Exception as exc:
        raise YouTubeAuthError(
            f"Could not obtain a valid OAuth access token: {exc}. "
            "If this persists, re-run 'ytmusicapi oauth' to regenerate oauth.json."
        ) from exc


def fetch_liked_music():
    """Fetch every track from the YouTube Music 'Liked Music' playlist."""
    ytmusic = get_ytmusic()
    try:
        # limit=None retrieves the whole playlist; the default of 100 silently truncates.
        playlist = ytmusic.get_playlist("LM", limit=None)
    except Exception as exc:
        raise YouTubeAuthError(f"YouTube Music rejected the Liked Music request: {exc}") from exc

    items = []
    for track in playlist.get("tracks") or []:
        vid = track.get("videoId")
        if not vid:
            continue
        artists = [a.get("name", "") for a in (track.get("artists") or []) if a.get("name")]
        items.append({
            "video_id": vid,
            "title": track.get("title", ""),
            "artist": artists[0] if artists else "",
        })
    return items
