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


# --------------------------------------------------------------------------- #
# Metadata refresh: rows created by earlier, unauthenticated syncs
# --------------------------------------------------------------------------- #

def _yt_item(title, owner, vid="vid1", pos=0):
    return {"snippet": {"title": title, "videoOwnerChannelTitle": owner,
                        "channelTitle": "Mark",  # playlist owner - must NOT be used
                        "publishedAt": "2024-01-01T00:00:00Z", "position": pos,
                        "resourceId": {"videoId": vid}}}


@pytest.mark.django_db
@responses.activate
def test_stale_private_video_rows_are_refreshed(client, settings, _inline):
    """
    The original code only ever updated `position` for existing rows, so tracks
    stored as 'Private video' by an unauthenticated sync stayed that way forever.
    """
    settings.YOUTUBE_API_KEY = "TESTKEY"
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    stale = TrackItem.objects.create(playlist=pl, video_id="vid1", title="Private video",
                                     channel_title="Mark", artist_name_guess="", position=0)

    responses.add(responses.GET, YT_PL,
                  json={"items": [{"snippet": {"title": "Old School 1", "channelTitle": "Mark"}}]}, status=200)
    responses.add(responses.GET, YT_ITEMS,
                  json={"items": [_yt_item("Pjanoo", "Music Library Uploads", "vid1", 3)]}, status=200)
    responses.add(responses.GET, MB, json={"recordings": []}, status=200)

    client.post(reverse("sync-playlist", args=[pl.pk]), headers={"hx-request": "true"})

    stale.refresh_from_db()
    assert stale.title == "Pjanoo", "placeholder title must be replaced once we can see the real one"
    assert stale.channel_title == "Music Library Uploads"
    assert stale.position == 3


@pytest.mark.django_db
@responses.activate
def test_uses_video_owner_not_playlist_owner(client, settings, _inline):
    """channelTitle is the playlist's owner; the '- Topic' heuristic needs the uploader."""
    settings.YOUTUBE_API_KEY = "TESTKEY"
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    responses.add(responses.GET, YT_PL, json={"items": []}, status=200)
    responses.add(responses.GET, YT_ITEMS,
                  json={"items": [_yt_item("Teardrop", "Massive Attack - Topic", "vidA")]}, status=200)
    responses.add(responses.GET, MB, json={"artists": [{"id": "mb-0000"}]}, status=200)

    client.post(reverse("sync-playlist", args=[pl.pk]), headers={"hx-request": "true"})

    ti = TrackItem.objects.get(video_id="vidA")
    assert ti.channel_title == "Massive Attack - Topic"
    assert ti.artist_name_guess == "Massive Attack", "Topic channel should name the artist"


@pytest.mark.django_db
@responses.activate
def test_hand_edited_rows_are_not_overwritten(client, settings, _inline):
    settings.YOUTUBE_API_KEY = "TESTKEY"
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    edited = TrackItem.objects.create(playlist=pl, video_id="vid1", title="Robert Miles - Children",
                                      artist_name_guess="Robert Miles", manually_edited=True)

    responses.add(responses.GET, YT_PL, json={"items": []}, status=200)
    responses.add(responses.GET, YT_ITEMS,
                  json={"items": [_yt_item("Children", "Music Library Uploads", "vid1", 7)]}, status=200)
    responses.add(responses.GET, MB, json={"artists": [{"id": "mb-1111"}]}, status=200)

    client.post(reverse("sync-playlist", args=[pl.pk]), headers={"hx-request": "true"})

    edited.refresh_from_db()
    assert edited.title == "Robert Miles - Children", "a manual correction must survive a sync"
    assert edited.artist_name_guess == "Robert Miles"
    assert edited.position == 7, "but machine fields still update"


@pytest.mark.django_db
@responses.activate
def test_real_title_is_not_replaced_by_a_placeholder(client, settings, _inline):
    """If a video later becomes private, keep the good title we already have."""
    settings.YOUTUBE_API_KEY = "TESTKEY"
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    good = TrackItem.objects.create(playlist=pl, video_id="vid1", title="Pjanoo",
                                    channel_title="Music Library Uploads")

    responses.add(responses.GET, YT_PL, json={"items": []}, status=200)
    responses.add(responses.GET, YT_ITEMS,
                  json={"items": [_yt_item("Private video", "", "vid1")]}, status=200)
    responses.add(responses.GET, MB, json={"recordings": []}, status=200)

    client.post(reverse("sync-playlist", args=[pl.pk]), headers={"hx-request": "true"})

    good.refresh_from_db()
    assert good.title == "Pjanoo"


@pytest.mark.django_db
def test_editing_an_item_marks_it_as_hand_edited(client):
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    ti = TrackItem.objects.create(playlist=pl, video_id="v1", title="Children", artist_name_guess="")

    client.post(reverse("edit-item", args=[ti.id]),
                {"title": "Children", "artist_name_guess": "Robert Miles"})

    ti.refresh_from_db()
    assert ti.artist_name_guess == "Robert Miles"
    assert ti.manually_edited is True


@pytest.mark.django_db
@responses.activate
def test_manually_entering_an_mbid_resolves_the_track(client):
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    ti = TrackItem.objects.create(playlist=pl, video_id="v1", title="Children", artist_name_guess="")
    mbid = "561d854a-6a28-4aa7-8c99-323e6ce46c2a"
    responses.add(responses.GET, f"{MB}{mbid}", json={"name": "Robert Miles"})

    client.post(reverse("edit-item", args=[ti.id]), {"title": ti.title, "mbid": mbid})

    ti.refresh_from_db()
    assert ti.artist.mbid == mbid
    assert ti.artist.name == "Robert Miles"
    assert ti.resolution_note == "manually set"
    assert ti.manually_edited is True


@pytest.mark.django_db
@responses.activate
def test_pasting_a_musicbrainz_url_extracts_the_mbid(client):
    """Copying from musicbrainz.org gives you the artist page URL, not the
    bare UUID - that has to work too, not just a hand-typed ID."""
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    ti = TrackItem.objects.create(playlist=pl, video_id="v1", title="Children", artist_name_guess="")
    mbid = "561d854a-6a28-4aa7-8c99-323e6ce46c2a"
    responses.add(responses.GET, f"{MB}{mbid}", json={"name": "Robert Miles"})

    resp = client.post(reverse("edit-item", args=[ti.id]),
                        {"title": ti.title, "mbid": f"https://musicbrainz.org/artist/{mbid}"})

    assert resp.status_code == 200
    ti.refresh_from_db()
    assert ti.artist.mbid == mbid


@pytest.mark.django_db
def test_malformed_mbid_is_rejected_with_a_visible_error(client):
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    ti = TrackItem.objects.create(playlist=pl, video_id="v1", title="Children", artist_name_guess="")

    resp = client.post(reverse("edit-item", args=[ti.id]), {"title": ti.title, "mbid": "not-a-real-mbid"})

    # The response IS the swapped row (HTMX partial) - Django's messages
    # framework has nowhere to render in that swap, so the error has to be
    # in this HTML or it's invisible to whoever just typed it.
    assert b"doesn" in resp.content and b"look like a MusicBrainz" in resp.content
    ti.refresh_from_db()
    assert ti.artist is None
    assert ti.manually_edited is False


# --------------------------------------------------------------------------- #
# Items page: sorting and matching a selection
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_items_page_sorts_by_the_requested_column(client):
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    TrackItem.objects.create(playlist=pl, video_id="v1", title="Zebra")
    TrackItem.objects.create(playlist=pl, video_id="v2", title="Apple")

    resp = client.get(reverse("items"), {"sort": "title", "dir": "asc"})

    titles = [it.title for it in resp.context["items"]]
    assert titles == ["Apple", "Zebra"]


@pytest.mark.django_db
def test_unknown_sort_key_falls_back_to_default_instead_of_crashing(client):
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    TrackItem.objects.create(playlist=pl, video_id="v1", title="Track")

    resp = client.get(reverse("items"), {"sort": "'; drop table--"})

    assert resp.status_code == 200
    # No such column - falls back to the natural (unsortable-from-the-UI)
    # newest-first order rather than trusting the query string.
    assert resp.context["sort"] == ""


@pytest.mark.django_db
def test_playlist_column_shows_the_friendly_title_not_the_raw_id(client):
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1", title="Car Music")
    TrackItem.objects.create(playlist=pl, video_id="v1", title="Track")

    resp = client.get(reverse("items"))

    # The raw id still shows up as the filter dropdown's option value - the
    # point is the *cell* shows the friendly name, not that the id vanishes
    # from the page entirely.
    assert b"<td class=\"small\">Car Music</td>" in resp.content


@pytest.mark.django_db
def test_title_filter_narrows_the_list(client):
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    TrackItem.objects.create(playlist=pl, video_id="v1", title="Sandstorm")
    TrackItem.objects.create(playlist=pl, video_id="v2", title="Nightcall")

    resp = client.get(reverse("items"), {"q_title": "sand"})

    titles = [it.title for it in resp.context["items"]]
    assert titles == ["Sandstorm"]


@pytest.mark.django_db
def test_mbid_filter_selects_only_unresolved_tracks(client):
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    from youtubarr.models import Artist
    resolved = TrackItem.objects.create(playlist=pl, video_id="v1", title="Resolved")
    resolved.artist = Artist.objects.create(name="Someone", mbid="561d854a-6a28-4aa7-8c99-323e6ce46c2a")
    resolved.save()
    TrackItem.objects.create(playlist=pl, video_id="v2", title="Unresolved")

    resp = client.get(reverse("items"), {"q_mbid": "unresolved"})

    titles = [it.title for it in resp.context["items"]]
    assert titles == ["Unresolved"]


@pytest.mark.django_db
def test_notes_filter_matches_resolution_note(client):
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    TrackItem.objects.create(playlist=pl, video_id="v1", title="A", resolution_note="ambiguous: 3 artists")
    TrackItem.objects.create(playlist=pl, video_id="v2", title="B", resolution_note="manually set")

    resp = client.get(reverse("items"), {"q_notes": "ambiguous"})

    titles = [it.title for it in resp.context["items"]]
    assert titles == ["A"]


@pytest.mark.django_db
@responses.activate
def test_matching_a_selection_only_touches_the_selected_rows(client, monkeypatch):
    """No worker, small selection - runs inline and leaves everything else alone."""
    monkeypatch.setattr("youtubarr.views._worker_available", lambda: False)
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    picked = TrackItem.objects.create(playlist=pl, video_id="v1", title="Sandstorm", artist_name_guess="Darude")
    ignored = TrackItem.objects.create(playlist=pl, video_id="v2", title="Some Other Song", artist_name_guess="")
    responses.add(responses.GET, MB, json={"artists": [{"id": "c8b03190-306c-4120-bb0b-6f2ebfc06ea9", "name": "Darude"}]})

    client.post(reverse("match-selected"), {"item_id": [picked.id]})

    picked.refresh_from_db()
    ignored.refresh_from_db()
    assert picked.artist.name == "Darude"
    assert ignored.artist is None
    assert ignored.resolution_attempted_at is None


@pytest.mark.django_db
def test_matching_with_nothing_selected_is_a_no_op(client):
    resp = client.post(reverse("match-selected"), {})
    assert resp.status_code == 302  # redirects back to items, doesn't 500


@pytest.mark.django_db
def test_large_selection_is_refused_inline_without_a_worker(client, monkeypatch):
    monkeypatch.setattr("youtubarr.views._worker_available", lambda: False)
    from youtubarr.views import INLINE_MATCH_CAP

    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    ids = [
        TrackItem.objects.create(playlist=pl, video_id=f"v{i}", title=f"Song {i}").id
        for i in range(INLINE_MATCH_CAP + 1)
    ]

    client.post(reverse("match-selected"), {"item_id": ids})

    # Nothing should have been attempted - refused up front, not partially run.
    assert TrackItem.objects.filter(resolution_attempted_at__isnull=False).count() == 0


@pytest.mark.django_db
def test_matching_a_selection_prefers_the_background_worker(client, monkeypatch):
    captured = {}
    monkeypatch.setattr("youtubarr.views._worker_available", lambda: True)
    monkeypatch.setattr(tasks.resolve_selected, "delay", lambda ids: captured.setdefault("ids", ids))

    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    ti = TrackItem.objects.create(playlist=pl, video_id="v1", title="Track")

    client.post(reverse("match-selected"), {"item_id": [ti.id]})

    assert captured["ids"] == [ti.id]
    # Dispatched to the worker, not run inline - nothing attempted synchronously.
    ti.refresh_from_db()
    assert ti.resolution_attempted_at is None


@pytest.mark.django_db
def test_matching_a_selection_preserves_the_current_sort(client, monkeypatch):
    """Regression: the redirect after matching used to drop back to the
    default sort instead of wherever the user actually was."""
    monkeypatch.setattr("youtubarr.views._worker_available", lambda: True)
    monkeypatch.setattr(tasks.resolve_selected, "delay", lambda ids: None)

    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    ti = TrackItem.objects.create(playlist=pl, video_id="v1", title="Track")

    resp = client.post(f"{reverse('match-selected')}?sort=mbid&dir=asc", {"item_id": [ti.id]})

    assert resp.status_code == 302
    assert resp.url == f"{reverse('items')}?sort=mbid&dir=asc"


# --------------------------------------------------------------------------- #
# A sync outlives the page that started it
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_running_sync_is_reattached_after_navigating_away(client, monkeypatch):
    """Leaving the page and coming back must find the sync still in progress."""
    from youtubarr.models import AppSettings

    class FakeResult:
        id = "task-abc-123"
    monkeypatch.setattr(tasks.sync_task, "delay", lambda *a, **k: FakeResult())
    monkeypatch.setattr("youtubarr.views._worker_available", lambda: True)

    client.post(reverse("sync-playlists"), headers={"hx-request": "true"})
    assert AppSettings.load().sync_task_id == "task-abc-123"

    # ...user wanders off to Items and comes back
    page = client.get(reverse("playlists")).content.decode()
    assert reverse("sync-status", args=["task-abc-123"]) in page
    assert "Sync in progress" in page


@pytest.mark.django_db
@responses.activate
def test_finished_sync_result_survives_a_reload(client, settings, _inline):
    """Otherwise a sync that finishes while you're elsewhere is invisible."""
    from youtubarr.models import AppSettings

    settings.YOUTUBE_API_KEY = "TESTKEY"
    Playlist.objects.create(playlist_id="PLsomethinglong1")
    responses.add(responses.GET, YT_PL, json={"items": []}, status=200)
    responses.add(responses.GET, YT_ITEMS, json={"items": []}, status=200)

    client.post(reverse("sync-playlists"), headers={"hx-request": "true"})

    s = AppSettings.load()
    assert s.sync_task_id == ""
    assert s.last_sync_summary is not None

    page = client.get(reverse("playlists")).content.decode()
    assert "last run" in page


@pytest.mark.django_db
def test_polling_to_completion_clears_the_running_task(client, monkeypatch):
    from youtubarr.models import AppSettings

    s = AppSettings.load()
    s.sync_task_id = "task-abc-123"
    s.save()

    class Done:
        state = "SUCCESS"
        result = {"items": 5, "playlists": [], "errors": [],
                  "artists_resolved": 2, "snapshot_artists": 2, "unresolved": 0}
    monkeypatch.setattr("celery.result.AsyncResult", lambda *a, **k: Done())

    body = client.get(reverse("sync-status", args=["task-abc-123"])).content.decode()
    assert "Sync complete" in body
    assert AppSettings.load().sync_task_id == "", "a finished task must stop being polled"
