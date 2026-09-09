from django.db import models
from django.utils import timezone
from django.core.validators import RegexValidator

class AppSettings(models.Model):
    """Singleton-style config."""
    youtube_api_key = models.CharField(max_length=256, blank=True, default="")
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    # A sync runs in the Celery worker, not in the browser, so the page needs to
    # be able to find it again after a reload or navigating away.
    sync_task_id = models.CharField(max_length=64, blank=True, default="")
    last_sync_summary = models.JSONField(null=True, blank=True)
    last_sync_finished_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce single row
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

YOUTUBE_PLAYLIST_ID_RE = RegexValidator(
    regex=r"^[A-Za-z0-9_-]{13,}$", message="Looks like an invalid playlist ID."
)

class Playlist(models.Model):
    playlist_id = models.CharField(max_length=64, unique=True, validators=[YOUTUBE_PLAYLIST_ID_RE])
    title = models.CharField(max_length=255, blank=True, default="")
    channel_title = models.CharField(max_length=255, blank=True, default="")
    enabled = models.BooleanField(default=True)
    last_synced = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.title or self.playlist_id

class Artist(models.Model):
    name = models.CharField(max_length=255, unique=True)
    mbid = models.CharField(max_length=36, blank=True, null=True)  # UUID
    # How this artist was identified, e.g. "musicbrainz recording consensus 8/8".
    resolved_from = models.CharField(max_length=200, blank=True, default="")

    def __str__(self):
        return f"{self.name} [{self.mbid or 'no-mbid'}]"

class TrackItem(models.Model):
    playlist = models.ForeignKey(Playlist, on_delete=models.CASCADE, related_name="items")
    video_id = models.CharField(max_length=32)
    title = models.CharField(max_length=512)
    channel_title = models.CharField(max_length=255, blank=True, default="")
    artist_name_guess = models.CharField(max_length=255, blank=True, default="")
    artist = models.ForeignKey(Artist, null=True, blank=True, on_delete=models.SET_NULL)
    blacklisted = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    position = models.IntegerField(default=0)

    # Real track length from YouTube's own contentDetails, not guessed - a
    # sanity check for a human eyeballing a match (see CLAUDE.md on why this
    # is deliberately NOT used to automate matching itself).
    duration_seconds = models.IntegerField(null=True, blank=True)

    # Set when a human edits the title/artist in the UI. Syncs then leave those
    # fields alone instead of overwriting the correction with YouTube's metadata.
    manually_edited = models.BooleanField(default=False)

    # Why the last artist lookup did or didn't produce a match, and when it ran.
    # Lets the UI explain unresolved tracks, and stops us re-querying MusicBrainz
    # for the same hopeless title on every single sync.
    resolution_note = models.CharField(max_length=255, blank=True, default="")
    resolution_attempted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("playlist", "video_id")

    @property
    def duration_display(self) -> str:
        """'6:28', '1:02:03', or '' when we don't have a duration yet."""
        if self.duration_seconds is None:
            return ""
        minutes, seconds = divmod(self.duration_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

class Snapshot(models.Model):
    """What we actually serve to Lidarr; newest wins."""
    created_at = models.DateTimeField(default=timezone.now)
    payload = models.JSONField()  # [{"MusicBrainzId": "..."}]
