from datetime import datetime, timedelta, timezone

from conftest import make_video
from reelbot.config import Filters, Source
from reelbot.discover import (
    add_url,
    check_filters,
    discover_source,
    entry_to_video,
    needs_enrichment,
)
from reelbot.store import APPROVED, PENDING

SOURCE = Source(name="test", url="https://example.invalid/shorts")


def entry(**overrides):
    base = {
        "id": "vid1",
        "title": "Funny short",
        "duration": 30,
        "view_count": 5000,
        "channel": "Some Channel",
        "webpage_url": "https://www.youtube.com/shorts/vid1",
        "thumbnails": [{"url": "https://i.ytimg.com/vi/vid1/hq.jpg"}],
    }
    base.update(overrides)
    return base


def fake_extractor(entries):
    def extract(url, opts):
        if "playlist" in url or url == SOURCE.url:
            return {"entries": entries}
        for item in entries:
            if item.get("id") and item["id"] in url:
                return item
        return None

    return extract


def test_entry_to_video_maps_fields():
    video = entry_to_video(entry(), SOURCE)
    assert video.id == "vid1"
    assert video.url == "https://www.youtube.com/shorts/vid1"
    assert video.duration == 30 and video.view_count == 5000
    assert video.thumbnail_url.endswith("hq.jpg")
    assert video.source == "test" and video.status == PENDING


def test_entry_to_video_builds_a_url_from_a_bare_id():
    video = entry_to_video({"id": "xyz", "url": "xyz"}, SOURCE)
    assert video.url == "https://www.youtube.com/watch?v=xyz"


def test_upload_date_from_timestamp():
    ts = int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp())
    assert entry_to_video(entry(timestamp=ts), SOURCE).upload_date == "2026-03-01"


def test_filters():
    filters = Filters(max_duration=90, min_duration=5, min_views=1000)
    assert check_filters(make_video(duration=30, view_count=2000), filters) is None
    assert "max" in check_filters(make_video(duration=200), filters)
    assert "min" in check_filters(make_video(duration=2), filters)
    assert "views" in check_filters(make_video(view_count=10), filters)


def test_title_allow_and_deny():
    assert check_filters(make_video(title="cat video"), Filters(title_allow=["dog"]))
    assert check_filters(make_video(title="dog video"), Filters(title_allow=["dog"])) is None
    assert "denied" in check_filters(make_video(title="Buy now #ad"), Filters(title_deny=["#ad"]))


def test_age_filter():
    filters = Filters(published_within_days=7)
    old = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    fresh = datetime.now(timezone.utc).date().isoformat()
    assert "older than" in check_filters(make_video(upload_date=old), filters)
    assert check_filters(make_video(upload_date=fresh), filters) is None


def test_needs_enrichment():
    filters = Filters(max_duration=90, min_views=100, published_within_days=7)
    assert needs_enrichment(make_video(duration=0), filters) is True
    assert needs_enrichment(make_video(view_count=0), filters) is True
    assert needs_enrichment(make_video(upload_date=""), filters) is True
    assert needs_enrichment(
        make_video(duration=10, view_count=500, upload_date="2026-01-01"), filters
    ) is False


def test_discover_queues_pending_not_approved(cfg, store):
    added, skipped = discover_source(
        cfg, store, SOURCE, extractor=fake_extractor([entry(), entry(id="vid2")])
    )
    assert (added, skipped) == (2, 0)
    assert all(v.status == PENDING for v in store.list())


def test_discover_is_idempotent(cfg, store):
    extractor = fake_extractor([entry()])
    discover_source(cfg, store, SOURCE, extractor=extractor)
    added, skipped = discover_source(cfg, store, SOURCE, extractor=extractor)
    assert (added, skipped) == (0, 1)


def test_discover_applies_filters(cfg, store):
    cfg.filters.max_duration = 20
    added, skipped = discover_source(
        cfg, store, SOURCE, extractor=fake_extractor([entry(duration=60)])
    )
    assert (added, skipped) == (0, 1)
    assert store.list(status="all") == []


def test_discover_enriches_when_a_field_is_missing(cfg, store):
    cfg.filters.min_views = 100
    flat = {"id": "vid1", "title": "Funny short", "url": "vid1"}
    full = entry(view_count=999)
    calls = []

    def extractor(url, opts):
        calls.append(url)
        return {"entries": [flat]} if url == SOURCE.url else full

    added, _ = discover_source(cfg, store, SOURCE, extractor=extractor)
    assert added == 1
    assert len(calls) == 2  # flat listing, then the per-video lookup
    assert store.get("vid1").view_count == 999


def test_auto_approve_source_skips_review(cfg, store):
    source = Source(name="mine", url=SOURCE.url, auto_approve=True)
    discover_source(cfg, store, source, extractor=fake_extractor([entry()]))
    video = store.get("vid1")
    assert video.status == APPROVED and video.reviewed_by == "auto:mine"


def test_add_url_queues_pending_by_default(cfg, store):
    video = add_url(cfg, store, "https://youtu.be/vid1", extractor=fake_extractor([entry()]))
    assert video.status == PENDING
    approved = add_url(
        cfg, store, "https://youtu.be/vid2",
        approve=True, extractor=fake_extractor([entry(id="vid2")]),
    )
    assert approved.status == APPROVED


def test_filtered_videos_are_not_re_fetched_on_the_next_run(cfg, store):
    """A scheduled run must not re-enrich the same rejects every hour."""
    cfg.filters.min_views = 1000
    calls = []

    def extractor(url, opts):
        calls.append(url)
        if url == SOURCE.url:
            return {"entries": [{"id": "vid1", "title": "Unpopular", "url": "vid1"}]}
        return entry(view_count=5)          # below min_views

    discover_source(cfg, store, SOURCE, extractor=extractor)
    assert len(calls) == 2                  # listing + the per-video lookup
    assert store.is_filtered("vid1") is True

    calls.clear()
    added, skipped = discover_source(cfg, store, SOURCE, extractor=extractor)
    assert (added, skipped) == (0, 1)
    assert len(calls) == 1                  # listing only — no second lookup


def test_clear_filtered_gives_changed_filters_a_fresh_look(cfg, store):
    cfg.filters.max_duration = 10
    extractor = fake_extractor([entry(duration=30)])
    assert discover_source(cfg, store, SOURCE, extractor=extractor) == (0, 1)

    cfg.filters.max_duration = 90           # the user relaxes the filter
    assert discover_source(cfg, store, SOURCE, extractor=extractor) == (0, 1)  # still skipped
    assert store.clear_filtered() == 1
    assert discover_source(cfg, store, SOURCE, extractor=extractor) == (1, 0)


def test_changing_a_filter_re_checks_what_it_rejected(cfg, store):
    """Loosening a filter must take effect without anyone passing --refilter."""
    from reelbot.discover import discover_all

    cfg.sources = [SOURCE]
    cfg.filters.published_within_days = 30
    old = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y%m%d")
    extractor = fake_extractor([entry(upload_date=old)])

    assert discover_all(cfg, store, extractor=extractor) == (0, 1)
    assert store.is_filtered("vid1") is True

    cfg.filters.published_within_days = None          # "stop filtering by age"
    assert discover_all(cfg, store, extractor=extractor) == (1, 0)
    assert store.get("vid1") is not None


def test_unchanged_filters_keep_the_rejects_remembered(cfg, store):
    """The whole point of remembering them is not re-fetching every hour."""
    from reelbot.discover import discover_all

    cfg.sources = [SOURCE]
    cfg.filters.max_duration = 10
    extractor = fake_extractor([entry(duration=60)])

    assert discover_all(cfg, store, extractor=extractor) == (0, 1)
    assert discover_all(cfg, store, extractor=extractor) == (0, 1)
    assert store.is_filtered("vid1") is True


def test_fingerprint_tracks_every_filter_field(cfg, store):
    from reelbot.config import Filters
    from reelbot.discover import filters_fingerprint

    base = Filters()
    assert filters_fingerprint(base) == filters_fingerprint(Filters())
    for field, value in [
        ("max_duration", 60), ("min_duration", 1), ("min_views", 10),
        ("published_within_days", 7), ("title_allow", ["x"]), ("title_deny", ["y"]),
    ]:
        changed = Filters(**{**base.__dict__, field: value})
        assert filters_fingerprint(changed) != filters_fingerprint(base), field
