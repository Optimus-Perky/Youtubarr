import logging
import re
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import (
    HttpResponse,
    HttpResponseForbidden,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import AppSettings, Artist, Playlist, Snapshot, TrackItem
from .tasks import lookup_mb_artist_name, resolve_mbids_for_items, resolve_selected, run_sync, sync_task
from .utils import YouTubeAuthError, oauth_status

logger = logging.getLogger(__name__)

# Not anchored: a MusicBrainz artist page URL (the natural thing to paste,
# e.g. https://musicbrainz.org/artist/561d854a-...) carries the UUID as a
# substring rather than the whole field, so this pulls it out of either form.
MBID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def settings_view(request):
    s = AppSettings.load()
    if request.method == "POST":
        s.youtube_api_key = request.POST.get("youtube_api_key", "").strip()
        s.save()
        messages.success(request, "YouTube API key updated.")
        return redirect("settings")
    return render(request, "settings.html", {
        "settings": s,
        "env_has_key": bool(settings.YOUTUBE_API_KEY),
        "lidarr_token": getattr(settings, "LIDARR_TOKEN", None),
        "oauth": oauth_status(),
    })


@require_http_methods(["GET", "POST"])
def playlists_view(request):
    if request.method == "POST":
        pid = (request.POST.get("playlist_id") or "").strip()
        if pid:
            Playlist.objects.get_or_create(playlist_id=pid)
            messages.success(request, f"Added {pid}")
        else:
            messages.error(request, "Playlist ID required.")
        return redirect("playlists")
    return render(request, "playlists.html", {
        "playlists": _playlists_qs(),
        "oauth": oauth_status(),
        "sync": _sync_panel_context(),
    })


# Maps a URL-safe sort key to the ORM field it actually orders by. Keeping
# this as an allow-list, rather than passing ?sort= straight into order_by(),
# stops a query string from ordering on an arbitrary/expensive field. Columns
# with no UI presence (blacklisted, published_at) aren't listed - there's no
# header to click for them, so they'd only be reachable by hand-editing the
# URL, and the natural fallback order below already covers "newest first".
ITEM_SORT_FIELDS = {
    "playlist": "playlist__title",
    "video": "video_id",
    "duration": "duration_seconds",
    "title": "title",
    "artist": "artist_name_guess",
    "mbid": "artist__mbid",
    "attempted": "resolution_attempted_at",
    "notes": "resolution_note",
}
DEFAULT_ORDER = ("-published_at", "-id")

# Query params a filter can appear under. Each maps to a lookup applied in
# items_view; kept as an explicit list (rather than trusting request.GET
# wholesale) so a stray query param can't be mistaken for a filter.
ITEM_FILTER_PARAMS = ["q_playlist", "q_title", "q_artist", "q_mbid", "q_notes"]


# Rendering 500 rows in one page - each with several inputs and two forms -
# made the DOM heavy enough that resizing the browser (which relayouts
# everything currently visible, repeatedly, for the duration of the drag)
# could visibly stall the page and, on modest hardware, the OS pointer along
# with it. Paginating cuts what's in the DOM at once by roughly 10x.
ITEMS_PAGE_SIZE = 50


def _query_state(request):
    """
    Everything about the current Items view worth carrying across a redirect
    or an action: the sort, the page, and every active filter. Centralised so
    a sort link, a page link, the filter form, and "Match selected" all agree
    on what "here" means instead of each reconstructing the query string by
    hand.
    """
    sort = request.GET.get("sort", "")
    if sort not in ITEM_SORT_FIELDS:
        sort = ""
    direction = request.GET.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"

    state = {"sort": sort, "dir": direction}
    for param in ITEM_FILTER_PARAMS:
        state[param] = request.GET.get(param, "").strip()
    state["page"] = request.GET.get("page", "").strip()
    return state


def _items_url(state):
    qs = urlencode({k: v for k, v in state.items() if v})
    url = reverse("items")
    return f"{url}?{qs}" if qs else url


def items_view(request):
    state = _query_state(request)
    sort, direction = state["sort"], state["dir"]

    qs = TrackItem.objects.select_related("playlist", "artist")
    if state["q_playlist"]:
        qs = qs.filter(playlist__playlist_id=state["q_playlist"])
    if state["q_title"]:
        qs = qs.filter(title__icontains=state["q_title"])
    if state["q_artist"]:
        qs = qs.filter(artist_name_guess__icontains=state["q_artist"])
    if state["q_mbid"] == "resolved":
        qs = qs.filter(artist__mbid__isnull=False).exclude(artist__mbid="")
    elif state["q_mbid"] == "unresolved":
        qs = qs.filter(Q(artist__isnull=True) | Q(artist__mbid__isnull=True) | Q(artist__mbid=""))
    if state["q_notes"]:
        qs = qs.filter(resolution_note__icontains=state["q_notes"])

    if sort:
        order_field = ITEM_SORT_FIELDS[sort]
        order = (f"-{order_field}" if direction == "desc" else order_field, "-id")
    else:
        order = DEFAULT_ORDER
    qs = qs.order_by(*order)

    paginator = Paginator(qs, ITEMS_PAGE_SIZE)
    try:
        page_number = int(state["page"] or 1)
    except ValueError:
        page_number = 1
    page_obj = paginator.get_page(page_number)
    # Reflect whatever page Paginator actually landed on (it clamps an
    # out-of-range request rather than erroring) back into state, so links
    # built below - and a "Match selected" redirect - point at the page the
    # user is actually looking at.
    state["page"] = str(page_obj.number)

    # What a sort link needs to append: filters, but not sort/dir (it's
    # setting those itself) and not page (changing sort should land on
    # page 1, same as changing a filter does).
    base_qs = urlencode({k: v for k, v in state.items() if v and k not in ("sort", "dir", "page")})
    # What a page link needs to append: sort, dir and filters, but not page
    # (it's setting that itself).
    nav_qs = urlencode({k: v for k, v in state.items() if v and k != "page"})

    return render(request, "items.html", {
        "items": page_obj,
        "page_obj": page_obj,
        "sort": sort,
        "dir": direction,
        "filters": state,
        "base_qs": base_qs,
        "nav_qs": nav_qs,
        "current_qs": urlencode({k: v for k, v in state.items() if v}),
        "playlists": Playlist.objects.all().order_by("title", "playlist_id"),
    })


# A selection is a deliberate, bounded action - unlike a full backlog resolve,
# there's no reason it should ever be huge. Caps how far the inline fallback
# will go when no Celery worker is up, so a big selection gets a clear error
# instead of quietly risking gunicorn's request timeout.
INLINE_MATCH_CAP = 50


@require_http_methods(["POST"])
def match_selected_view(request):
    # The form's action carries the sort and filters that were on screen when
    # "Match selected" was clicked (see items.html) - read them back so the
    # redirect lands on the same view instead of resetting to the default.
    back = _items_url(_query_state(request))

    item_ids = [int(v) for v in request.POST.getlist("item_id") if v.isdigit()]
    if not item_ids:
        messages.error(request, "No tracks selected.")
        return redirect(back)

    if _worker_available():
        resolve_selected.delay(item_ids)
        messages.success(
            request,
            f"Matching {len(item_ids)} selected track(s) in the background — "
            "reload this page in a bit to see the result.",
        )
        return redirect(back)

    if len(item_ids) > INLINE_MATCH_CAP:
        messages.error(
            request,
            f"No Celery worker is available, and {len(item_ids)} tracks is too many to "
            f"match inline (limit {INLINE_MATCH_CAP}) without risking a timeout. Select "
            "fewer, or try again once the worker is up.",
        )
        return redirect(back)

    result = resolve_mbids_for_items(item_ids)
    messages.success(
        request,
        f"Matched {result['resolved']} of {result['considered']} selected track(s); "
        f"{result['unresolved']} still unresolved.",
    )
    return redirect(back)


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #

def _sync_panel_context():
    """
    What to show in the sync panel on a fresh page load.

    A sync outlives the page that started it, so reattach to one still running,
    and otherwise show the result of the last one - without this you lose the
    outcome entirely by navigating away mid-sync.
    """
    s = AppSettings.load()
    if s.sync_task_id:
        return {"task_id": s.sync_task_id, "state": "PENDING",
                "message": "Sync in progress\u2026"}
    if s.last_sync_summary:
        return {"state": "SUCCESS", "result": s.last_sync_summary,
                "finished_at": s.last_sync_finished_at, "historic": True}
    return None


def _remember_task(task_id):
    s = AppSettings.load()
    s.sync_task_id = task_id or ""
    s.save(update_fields=["sync_task_id"])


def _remember_result(result):
    s = AppSettings.load()
    s.sync_task_id = ""
    s.last_sync_summary = result
    s.last_sync_finished_at = timezone.now()
    s.save(update_fields=["sync_task_id", "last_sync_summary", "last_sync_finished_at"])


def _playlists_qs():
    return Playlist.objects.all().order_by("-last_synced", "playlist_id")


def playlist_table(request):
    """HTMX: re-render just the playlist table after a sync."""
    return render(request, "partials/playlist_table.html", {"playlists": _playlists_qs()})


def _worker_available() -> bool:
    """
    True only if a Celery worker is actually listening.

    Without this check a queued task sits in Redis forever when the worker is down,
    and the UI spins with no explanation. If nothing answers, we run the sync inline
    instead so the button always does something.
    """
    try:
        from core.celery import app as celery_app
        return bool(celery_app.control.ping(timeout=1.0))
    except Exception as exc:
        logger.info("No Celery worker reachable (%s); running sync inline", exc)
        return False


def _start_sync(request, playlist_ids=None, label="All playlists"):
    """
    Kick off a sync.

    Prefers Celery so the request returns immediately; falls back to running inline
    when no worker is listening. Responds with an HTMX fragment when htmx made the
    request, and with a plain redirect + message otherwise, so the button still works
    if the htmx script did not load.
    """
    is_htmx = request.headers.get("HX-Request") == "true"
    worker = _worker_available()

    if worker:
        try:
            async_result = sync_task.delay(playlist_ids)
            _remember_task(async_result.id)
            if is_htmx:
                return render(request, "partials/sync_status.html", {
                    "task_id": async_result.id,
                    "state": "PENDING",
                    "message": f"Sync queued: {label}",
                    "label": label,
                })
            messages.success(
                request,
                f"Sync started in the background: {label}. Reload this page in a minute to see the result.",
            )
            return redirect("playlists")
        except Exception as exc:
            logger.warning("Celery dispatch failed (%s); running sync inline", exc)

    try:
        result = run_sync(playlist_ids=playlist_ids)
    except Exception as exc:
        logger.exception("Inline sync failed")
        if is_htmx:
            return render(request, "partials/sync_status.html", {"state": "FAILURE", "error": str(exc)})
        messages.error(request, f"Sync failed: {exc}")
        return redirect("playlists")

    _remember_result(result)
    if is_htmx:
        return render(request, "partials/sync_status.html", {
            "state": "SUCCESS", "result": result, "inline": True, "label": label,
        })

    if result["errors"]:
        for err in result["errors"]:
            messages.error(request, f"Sync problem — {err}")
    else:
        messages.success(
            request,
            f"Sync complete: {result['items']} tracks, "
            f"{result['snapshot_artists']} artists published to Lidarr.",
        )
    return redirect("playlists")


@require_http_methods(["POST"])
def sync_playlists_view(request):
    """Sync every enabled playlist."""
    return _start_sync(request, playlist_ids=None, label="All enabled playlists")


@require_http_methods(["POST"])
def sync_playlist_view(request, pk):
    """Sync a single playlist."""
    pl = get_object_or_404(Playlist, pk=pk)
    return _start_sync(request, playlist_ids=[pl.playlist_id], label=pl.title or pl.playlist_id)


def sync_status_view(request, task_id):
    """HTMX polling endpoint reporting on a running sync."""
    from celery.result import AsyncResult

    from core.celery import app as celery_app

    ctx = {"task_id": task_id}
    try:
        res = AsyncResult(task_id, app=celery_app)
        state = res.state
        ctx["state"] = state
        if state == "PROGRESS":
            ctx["message"] = (res.info or {}).get("message", "Working…")
        elif state == "SUCCESS":
            ctx["result"] = res.result
            _remember_result(res.result)
        elif state == "FAILURE":
            ctx["error"] = str(res.result)
            _remember_task(None)
        else:
            ctx["message"] = "Waiting for a worker to pick this up…"
    except Exception as exc:
        ctx["state"] = "FAILURE"
        ctx["error"] = f"Could not read task status: {exc}"
    return render(request, "partials/sync_status.html", ctx)


@require_http_methods(["POST"])
def add_liked_music(request):
    """Add the YouTube Music 'Liked Music' pseudo-playlist."""
    status = oauth_status()
    if not status["file_exists"]:
        messages.error(
            request,
            f"Liked Music needs OAuth. No oauth.json found at {status['path']} — "
            "run 'ytmusicapi oauth' and put the file in your mounted data directory.",
        )
    elif not (status["client_id_set"] and status["client_secret_set"]):
        messages.error(
            request,
            "Liked Music needs YOUTUBE_OAUTH_CLIENT_ID and YOUTUBE_OAUTH_CLIENT_SECRET set in .env.",
        )
    else:
        _, created = Playlist.objects.get_or_create(
            playlist_id="LM",
            defaults={"title": "Liked Music", "channel_title": "YouTube Music"},
        )
        messages.success(request, "Added Liked Music." if created else "Liked Music is already in the list.")
    return redirect("playlists")


# --------------------------------------------------------------------------- #
# HTMX item helpers
# --------------------------------------------------------------------------- #

def item_row(request, item_id, mbid_error=None):
    it = get_object_or_404(TrackItem.objects.select_related("playlist", "artist"), id=item_id)
    return render(request, "partials/item_row.html", {"it": it, "mbid_error": mbid_error})


@require_http_methods(["POST"])
def toggle_blacklist(request, item_id):
    it = get_object_or_404(TrackItem, id=item_id)
    # checkbox sends "on" when checked; missing when unchecked
    val = request.POST.get("blacklisted") == "on"
    if it.blacklisted != val:
        it.blacklisted = val
        it.save(update_fields=["blacklisted"])
    return item_row(request, item_id)


def _set_artist_by_mbid(it: TrackItem, mbid: str) -> None:
    """
    Point a track at a specific MusicBrainz artist, chosen by hand rather than
    guessed. Prefers an Artist row that already carries this mbid, so
    correcting the same artist twice does not create a duplicate; otherwise
    looks up the real name (falling back to whatever guess is on the track)
    rather than trusting arbitrary text as the artist's name.
    """
    artist = Artist.objects.filter(mbid=mbid).first()
    if not artist:
        name = lookup_mb_artist_name(mbid) or it.artist_name_guess or it.title
        artist, created = Artist.objects.get_or_create(
            name=name, defaults={"mbid": mbid, "resolved_from": "manually set"}
        )
        if not created and artist.mbid != mbid:
            artist.mbid = mbid
            artist.resolved_from = "manually set"
            artist.save(update_fields=["mbid", "resolved_from"])
    it.artist = artist
    # Keep the displayed guess in sync with what actually got linked.
    it.artist_name_guess = artist.name
    it.resolution_note = "manually set"
    it.resolution_attempted_at = timezone.now()


@require_http_methods(["POST"])
def edit_item(request, item_id):
    it = get_object_or_404(TrackItem, id=item_id)
    title = request.POST.get("title", it.title)
    artist_guess = request.POST.get("artist_name_guess", it.artist_name_guess)
    mbid = (request.POST.get("mbid") or "").strip()

    changed = []
    if title != it.title:
        it.title = title
        changed.append("title")
    if artist_guess != it.artist_name_guess:
        it.artist_name_guess = artist_guess
        changed.append("artist_name_guess")

    mbid_error = None
    current_mbid = it.artist.mbid if it.artist else ""
    if mbid != current_mbid:
        if not mbid:
            # Cleared by hand - unlink rather than leaving the old artist in
            # place, and clear resolution_attempted_at so it's immediately
            # eligible for another automatic pass instead of waiting out
            # RESOLUTION_RETRY_DAYS.
            it.artist = None
            it.resolution_note = ""
            it.resolution_attempted_at = None
            changed += ["artist", "resolution_note", "resolution_attempted_at"]
        else:
            match = MBID_RE.search(mbid)
            if not match:
                # This is an HTMX partial swap of just the row - Django's
                # messages framework has nowhere to render, so the error has
                # to travel back in the row itself or it's invisible and the
                # save just looks like it silently did nothing.
                mbid_error = "That doesn't look like a MusicBrainz artist ID (paste the ID or its musicbrainz.org artist page URL)."
            else:
                _set_artist_by_mbid(it, match.group(0).lower())
                changed += ["artist", "artist_name_guess", "resolution_note", "resolution_attempted_at"]

    if changed:
        # Remember this was corrected by hand so a later sync does not
        # overwrite it with YouTube's own metadata.
        it.manually_edited = True
        changed.append("manually_edited")
        it.save(update_fields=changed)
    return item_row(request, item_id, mbid_error=mbid_error)


@require_http_methods(["POST"])
def delete_item(request, item_id):
    it = get_object_or_404(TrackItem, id=item_id)
    it.delete()
    # HTMX: tell client to remove the row
    return HttpResponse(status=204, headers={"HX-Trigger": "item-deleted"})


def healthz(request):
    return HttpResponse("ok")


def lidarr_youtubarr_view(request):
    # token via ?token=... or X-Api-Key header
    token = request.GET.get("token") or request.headers.get("X-Api-Key")
    if not (settings.LIDARR_TOKEN and token == settings.LIDARR_TOKEN):
        return HttpResponseForbidden("missing/invalid token")
    snap = Snapshot.objects.order_by("-created_at").first()
    return JsonResponse(snap.payload if snap else [], safe=False)
