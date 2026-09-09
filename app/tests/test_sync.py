import json
import os
import time

import pytest
import responses
from django.urls import reverse

from youtubarr import tasks, utils
from youtubarr.models import Playlist, Snapshot, TrackItem

YT_PL = "https://www.googleapis.com/youtube/v3/playlists"
YT_ITEMS = "https://www.googleapis.com/youtube/v3/playlistItems"
MB = "https://musicbrainz.org/ws/2/artist/"
TOKEN_URL = "https://oauth2.googleapis.com/token"


@pytest.fixture(autouse=True)
def _no_mb_sleep(monkeypatch):
    monkeypatch.setattr(tasks.time, "sleep", lambda *a, **k: None)


@pytest.fixture
def _inline(monkeypatch):
    """Force the view's inline fallback so tests don't need a broker."""
    def boom(*a, **k):
        raise OSError("no broker")
    monkeypatch.setattr(tasks.sync_task, "delay", boom)


@pytest.mark.django_db
def test_playlists_page_has_sync_button(client):
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    html = client.get(reverse("playlists")).content.decode()
    assert 'hx-post="{}"'.format(reverse("sync-playlists")) in html
    assert 'hx-post="{}"'.format(reverse("sync-playlist", args=[pl.pk])) in html
    assert 'id="sync-status"' in html


@pytest.mark.django_db
@responses.activate
def test_sync_button_populates_items_and_snapshot(client, settings, _inline):
    settings.YOUTUBE_API_KEY = "TESTKEY"
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")

    responses.add(responses.GET, YT_PL,
                  json={"items": [{"snippet": {"title": "Road Trip", "channelTitle": "Mark"}}]}, status=200)
    responses.add(responses.GET, YT_ITEMS, json={"items": [
        {"snippet": {"title": "Fleetwood Mac - Dreams", "channelTitle": "Warner",
                     "publishedAt": "2024-01-01T00:00:00Z", "position": 0,
                     "resourceId": {"videoId": "vid1"}}}]}, status=200)
    responses.add(responses.GET, MB,
                  json={"artists": [{"id": "11111111-1111-1111-1111-111111111111"}]}, status=200)

    r = client.post(reverse("sync-playlists"), headers={"hx-request": "true"})
    body = r.content.decode()

    assert r.status_code == 200
    assert "Sync complete" in body
    assert TrackItem.objects.count() == 1
    pl.refresh_from_db()
    assert pl.title == "Road Trip"
    assert pl.last_synced is not None
    assert Snapshot.objects.latest("created_at").payload == [
        {"MusicBrainzId": "11111111-1111-1111-1111-111111111111"}]


@pytest.mark.django_db
@responses.activate
def test_private_playlist_error_is_shown_not_swallowed(client, settings, _inline):
    """The original bug: a private playlist returned 0 items with no error at all."""
    settings.YOUTUBE_API_KEY = "TESTKEY"
    settings.YOUTUBE_OAUTH_CLIENT_ID = None
    pl = Playlist.objects.create(playlist_id="PLprivateplaylist")

    responses.add(responses.GET, YT_PL, json={"error": {"code": 404,
        "message": "The playlist cannot be found.",
        "errors": [{"reason": "playlistNotFound"}]}}, status=404)

    body = client.post(reverse("sync-playlist", args=[pl.pk]), headers={"hx-request": "true"}).content.decode()
    assert "playlistNotFound" in body
    assert "private" in body.lower()


@pytest.mark.django_db
@responses.activate
def test_quota_error_is_shown(client, settings, _inline):
    settings.YOUTUBE_API_KEY = "TESTKEY"
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    responses.add(responses.GET, YT_PL, json={"items": []}, status=200)
    responses.add(responses.GET, YT_ITEMS, json={"error": {"code": 403,
        "message": "Quota exceeded.", "errors": [{"reason": "quotaExceeded"}]}}, status=403)

    body = client.post(reverse("sync-playlist", args=[pl.pk]), headers={"hx-request": "true"}).content.decode()
    assert "quotaExceeded" in body


@pytest.mark.django_db
def test_missing_api_key_is_reported(client, settings, _inline):
    settings.YOUTUBE_API_KEY = ""
    Playlist.objects.create(playlist_id="PLsomethinglong1")
    body = client.post(reverse("sync-playlists"), headers={"hx-request": "true"}).content.decode()
    assert "No YouTube API key configured" in body


@responses.activate
def test_expired_oauth_token_is_refreshed(tmp_path, settings, monkeypatch):
    """Gemini's version read the raw access_token, which dies after ~1 hour."""
    settings.YOUTUBE_OAUTH_CLIENT_ID = "cid"
    settings.YOUTUBE_OAUTH_CLIENT_SECRET = "secret"
    oauth_path = tmp_path / "oauth.json"
    oauth_path.write_text(json.dumps({
        "scope": "https://www.googleapis.com/auth/youtube", "token_type": "Bearer",
        "access_token": "STALE", "refresh_token": "REFRESH",
        "expires_at": int(time.time()) - 10, "expires_in": 3600,
        "refresh_token_expires_in": 604800,
    }))
    monkeypatch.setattr(utils, "OAUTH_PATH", str(oauth_path))
    responses.add(responses.POST, TOKEN_URL, json={
        "access_token": "FRESH", "expires_in": 3599,
        "scope": "https://www.googleapis.com/auth/youtube", "token_type": "Bearer"}, status=200)

    assert utils.get_oauth_bearer() == "Bearer FRESH"
    # the new token is persisted so other processes benefit too
    assert json.loads(oauth_path.read_text())["access_token"] == "FRESH"


def test_oauth_missing_file_gives_actionable_error(tmp_path, settings, monkeypatch):
    settings.YOUTUBE_OAUTH_CLIENT_ID = "cid"
    settings.YOUTUBE_OAUTH_CLIENT_SECRET = "secret"
    monkeypatch.setattr(utils, "OAUTH_PATH", str(tmp_path / "nope.json"))
    with pytest.raises(utils.YouTubeAuthError, match="ytmusicapi oauth"):
        utils.get_oauth_bearer()


@pytest.mark.django_db
def test_sync_without_oauth_still_works_for_public_playlists(settings, monkeypatch):
    """No OAuth configured must not break API-key-only syncing."""
    settings.YOUTUBE_OAUTH_CLIENT_ID = None
    settings.YOUTUBE_OAUTH_CLIENT_SECRET = None
    assert tasks._auth_headers() == {}


@pytest.mark.django_db
@responses.activate
def test_sync_works_without_htmx(client, settings, _inline):
    """If the htmx CDN is unreachable the button must still work as a plain form POST."""
    settings.YOUTUBE_API_KEY = "TESTKEY"
    Playlist.objects.create(playlist_id="PLsomethinglong1")
    responses.add(responses.GET, YT_PL,
                  json={"items": [{"snippet": {"title": "Road Trip", "channelTitle": "Mark"}}]}, status=200)
    responses.add(responses.GET, YT_ITEMS, json={"items": [
        {"snippet": {"title": "Fleetwood Mac - Dreams", "channelTitle": "Warner",
                     "publishedAt": "2024-01-01T00:00:00Z", "position": 0,
                     "resourceId": {"videoId": "vid1"}}}]}, status=200)
    responses.add(responses.GET, MB,
                  json={"artists": [{"id": "11111111-1111-1111-1111-111111111111"}]}, status=200)

    r = client.post(reverse("sync-playlists"))          # no HX-Request header
    assert r.status_code == 302 and r["Location"] == reverse("playlists")
    assert TrackItem.objects.count() == 1

    page = client.get(reverse("playlists")).content.decode()
    assert "Sync complete" in page


@pytest.mark.django_db
def test_sync_button_is_a_real_form(client):
    """The button posts to a real action so it degrades gracefully without JS."""
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    html = client.get(reverse("playlists")).content.decode()
    assert 'action="{}"'.format(reverse("sync-playlists")) in html
    assert 'action="{}"'.format(reverse("sync-playlist", args=[pl.pk])) in html
    assert "csrfmiddlewaretoken" in html


@pytest.mark.django_db
@responses.activate
def test_failed_sync_does_not_update_last_synced(client, settings, _inline):
    """
    The original bug: last_synced was stamped from the metadata call before any items
    were fetched, so a playlist that returned nothing still looked freshly synced.
    """
    settings.YOUTUBE_API_KEY = "TESTKEY"
    pl = Playlist.objects.create(playlist_id="PLprivateplaylist")
    responses.add(responses.GET, YT_PL,
                  json={"items": [{"snippet": {"title": "Private Mixtape", "channelTitle": "Mark"}}]}, status=200)
    responses.add(responses.GET, YT_ITEMS, json={"error": {"code": 403,
        "message": "Forbidden.", "errors": [{"reason": "forbidden"}]}}, status=403)

    client.post(reverse("sync-playlist", args=[pl.pk]), headers={"hx-request": "true"})

    pl.refresh_from_db()
    assert pl.last_synced is None, "a failed sync must not claim to have synced"
    assert pl.title == "Private Mixtape", "metadata we did retrieve is still saved"


@pytest.mark.django_db
@responses.activate
def test_successful_sync_does_update_last_synced(client, settings, _inline):
    settings.YOUTUBE_API_KEY = "TESTKEY"
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    responses.add(responses.GET, YT_PL,
                  json={"items": [{"snippet": {"title": "Road Trip", "channelTitle": "Mark"}}]}, status=200)
    responses.add(responses.GET, YT_ITEMS, json={"items": [
        {"snippet": {"title": "A - B", "channelTitle": "C", "position": 0,
                     "publishedAt": "2024-01-01T00:00:00Z",
                     "resourceId": {"videoId": "vid1"}}}]}, status=200)
    responses.add(responses.GET, MB, json={"artists": []}, status=200)

    client.post(reverse("sync-playlist", args=[pl.pk]), headers={"hx-request": "true"})
    pl.refresh_from_db()
    assert pl.last_synced is not None
