from datetime import datetime, timedelta, timezone

from conftest import make_video
from reelbot.store import APPROVED, DOWNLOADED, FAILED, PENDING, POSTED, REJECTED


def test_add_and_get(store):
    assert store.add_candidate(make_video("aaa")) is True
    assert store.add_candidate(make_video("aaa")) is False  # dedupe
    video = store.get("aaa")
    assert video.status == PENDING and video.title == "A test short"


def test_approve_records_the_reviewer(store):
    store.add_candidate(make_video("bbb"))
    assert store.approve("bbb", by="mel", caption="hi") is True
    video = store.get("bbb")
    assert video.status == APPROVED
    assert video.reviewed_by == "mel" and video.reviewed_at
    assert video.caption == "hi"


def test_reject_then_approve_is_allowed(store):
    store.add_candidate(make_video("ccc"))
    assert store.reject("ccc") is True
    assert store.get("ccc").status == REJECTED
    assert store.approve("ccc") is True


def test_posted_videos_cannot_be_re_reviewed(store):
    store.add_candidate(make_video("ddd"))
    store.set_status("ddd", POSTED)
    assert store.approve("ddd") is False
    assert store.reject("ddd") is False


def test_downloaded_is_not_rejectable(store):
    store.add_candidate(make_video("eee"))
    store.set_status("eee", DOWNLOADED)
    assert store.reject("eee") is False


def test_counts_and_listing(store):
    store.add_candidate(make_video("f1"))
    store.add_candidate(make_video("f2"))
    store.approve("f2")
    counts = store.counts()
    assert counts[PENDING] == 1 and counts[APPROVED] == 1
    assert {v.id for v in store.list(status=[PENDING, APPROVED])} == {"f1", "f2"}
    assert len(store.list(limit=1)) == 1


def test_post_budget_helpers(store):
    now = datetime.now(timezone.utc)
    store.add_candidate(make_video("g1"))
    store.set_status("g1", POSTED, posted_at=now.isoformat(timespec="seconds"))
    store.add_candidate(make_video("g2"))
    store.set_status(
        "g2", POSTED, posted_at=(now - timedelta(days=3)).isoformat(timespec="seconds")
    )

    assert store.posts_today(now) == 1          # the 3-day-old post does not count
    last = store.last_post_time()
    assert last is not None and (now - last) < timedelta(minutes=1)


def test_events_are_logged(store):
    store.add_candidate(make_video("h1"))
    store.set_status("h1", FAILED, error="boom")
    kinds = [e["kind"] for e in store.recent_events(10)]
    assert "discovered" in kinds and "status:failed" in kinds


def test_unknown_status_rejected(store):
    store.add_candidate(make_video("i1"))
    try:
        store.set_status("i1", "banana")
    except ValueError as exc:
        assert "banana" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_store_is_usable_from_several_threads(store):
    """The review UI serves requests on worker threads (see ThreadingHTTPServer)."""
    import threading

    errors = []

    def worker(n):
        try:
            store.add_candidate(make_video(f"t{n}"))
            store.approve(f"t{n}", by=f"thread-{n}")
            assert store.get(f"t{n}").status == APPROVED
        except Exception as exc:  # pragma: no cover - only on a regression
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert store.counts()[APPROVED] == 8
