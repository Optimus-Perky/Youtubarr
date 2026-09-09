"""
Artist resolution tests.

The fixtures below are trimmed from real MusicBrainz responses for tracks in
Mark's playlists, including the two that previously produced confident wrong
answers. They are the reason the rule is "exact title + consensus" rather than
"top hit" or "closest duration".
"""
import pytest
import responses

from youtubarr import tasks
from youtubarr.models import Artist, Playlist, TrackItem
from youtubarr.tasks import search_mb_artist_by_recording

MB_REC = "https://musicbrainz.org/ws/2/recording/"
MB_ART = "https://musicbrainz.org/ws/2/artist/"

PRYDZ = "35dac7d2-0b1f-470f-9a5a-c53c8821f6d6"
SHAPESHIFTERS = "f43b00ef-66cd-4c60-9c81-2c55236b5006"
ATB = "22a096ef-c70d-4d70-ae19-4fc2412d4986"
PETE_TONG = "b9585df2-bb26-4523-83fa-53d18b754d5b"


def rec(title, artist_name, artist_id, length=None):
    return {"title": title, "length": length,
            "artist-credit": [{"artist": {"id": artist_id, "name": artist_name}}]}


# Every candidate agrees, though not one length matches the 183s video.
PJANOO = [rec("Pjanoo", "Eric Prydz", PRYDZ, n) for n in
          (154000, 298000, None, 368000, 166000, 359000, 274000, 116000)]

# Consistent artist, mixed apostrophes and credit spellings.
LOLAS_THEME = [rec("Lola's Theme", "The Shapeshifters", SHAPESHIFTERS, 212000),
               rec("Lola’s Theme", "The Shapeshifters", SHAPESHIFTERS, 244000),
               rec("Lolas Theme", "Shapeshifters", SHAPESHIFTERS, 205000)]

# Nothing is actually titled "Children" - these are different songs.
CHILDREN = [rec("Children's Children", "Agent Blue", "a1"),
            rec("Children Children", "Deh Dog Himself", "a2"),
            rec("Children, Children", "Ginger Williams", "a3"),
            rec("Children, Children", "Sammy Davis Jr.", "a4"),
            rec("Children Children", "Big Youth", "a5")]

# Exact title, but seven different artists - genuinely ambiguous.
RIGHT_HERE = [rec("Right Here Right Now", "Ed Alleyne-Johnson", "b1", 180000),
              rec("Right Here, Right Now", "Jesus Jones", "b2", 190000),
              rec("Right Here, Right Now", "People Playing Music", "b3", 240000),
              rec("Right Here, Right Now", "Jesus Jones", "b2", 189000),
              rec("Right Here Right Now", "Raffaëla", "b4", 232000),
              rec("Right Here, Right Now", "Michael Monroe", "b5", 210000),
              rec("Right Here, Right Now", "Precious Metal", "b6", 175000),
              rec("Right Here Right Now", "J.T. Donaldson", "b7", 327000)]

NINE_PM = [rec("9pm (Till I Come)", "ATB", ATB, None),
           rec("9PM (Till I Come)", "ATB", ATB, 160000),
           rec("9PM (Till I Come)", "Pete Tong", PETE_TONG, 156000),
           rec("9PM (Till I Come)", "ATB", ATB, 298000)]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(tasks.time, "sleep", lambda *a, **k: None)


def mb_returns(recordings):
    responses.add(responses.GET, MB_REC, json={"recordings": recordings}, status=200)


@responses.activate
def test_unanimous_candidates_resolve_even_when_no_length_matches():
    mb_returns(PJANOO)
    name, mbid, note = search_mb_artist_by_recording("Pjanoo")
    assert (name, mbid) == ("Eric Prydz", PRYDZ)
    assert "8/8" in note


@responses.activate
def test_apostrophe_and_credit_variants_still_agree():
    mb_returns(LOLAS_THEME)
    name, mbid, _ = search_mb_artist_by_recording("Lola's Theme")
    assert (name, mbid) == ("The Shapeshifters", SHAPESHIFTERS)


@responses.activate
def test_majority_wins_over_a_single_remixer():
    mb_returns(NINE_PM)
    name, mbid, note = search_mb_artist_by_recording("9PM (Till I Come)")
    assert (name, mbid) == ("ATB", ATB)
    assert "3/4" in note


@responses.activate
def test_similar_but_different_titles_are_rejected():
    """'Children' must not match 'Children's Children'."""
    mb_returns(CHILDREN)
    name, mbid, note = search_mb_artist_by_recording("Children")
    assert name is None and mbid is None
    assert "no exact title match" in note


@responses.activate
def test_scattered_artists_are_rejected_rather_than_guessed():
    """The old code would have confidently returned the wrong artist here."""
    mb_returns(RIGHT_HERE)
    name, mbid, note = search_mb_artist_by_recording("Right Here, Right Now")
    assert name is None and mbid is None
    assert "ambiguous" in note


@responses.activate
def test_upload_noise_is_stripped_before_searching():
    mb_returns(PJANOO)
    search_mb_artist_by_recording("Pjanoo (Official Video) [HQ]")
    from urllib.parse import unquote
    assert 'recording:"Pjanoo"' in unquote(responses.calls[0].request.url)


@pytest.mark.django_db
@responses.activate
def test_failed_lookup_leaves_no_empty_artist_row_and_explains_itself():
    """
    The original bug: a miss created an Artist with no MBID, the track was linked
    to it, and it was never retried again.
    """
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    ti = TrackItem.objects.create(playlist=pl, video_id="v1", title="Children", artist_name_guess="")
    mb_returns(CHILDREN)

    stats = tasks.resolve_mbids()

    ti.refresh_from_db()
    assert stats == {"resolved": 0, "unresolved": 1, "considered": 1}
    assert ti.artist is None, "must not be linked to an artist we could not identify"
    assert Artist.objects.count() == 0, "must not create an empty Artist row"
    assert "no exact title match" in ti.resolution_note
    assert ti.resolution_attempted_at is not None


@pytest.mark.django_db
@responses.activate
def test_successful_lookup_links_artist_and_records_provenance():
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    ti = TrackItem.objects.create(playlist=pl, video_id="v1", title="Pjanoo", artist_name_guess="")
    mb_returns(PJANOO)

    stats = tasks.resolve_mbids()

    ti.refresh_from_db()
    assert stats["resolved"] == 1
    assert ti.artist.name == "Eric Prydz"
    assert ti.artist.mbid == PRYDZ
    assert "consensus" in ti.artist.resolved_from


@pytest.mark.django_db
@responses.activate
def test_named_artist_takes_priority_over_title_search():
    pl = Playlist.objects.create(playlist_id="PLsomethinglong1")
    ti = TrackItem.objects.create(playlist=pl, video_id="v1",
                                  title="Blue Monday", artist_name_guess="New Order")
    responses.add(responses.GET, MB_ART,
                  json={"artists": [{"id": "no-mbid-0000-0000-0000-000000000000"}]}, status=200)

    tasks.resolve_mbids()

    ti.refresh_from_db()
    assert ti.artist.name == "New Order"
    assert ti.artist.resolved_from == "MusicBrainz artist search"
