"""The Graph backend must upload a local file before publishing its container."""

import requests
import pytest

from reelbot.publishers.base import PublishError
from reelbot.publishers.graph_backend import GraphPublisher


class Response:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code
        self.text = str(body)

    def json(self):
        return self.body


def setup_publisher(cfg):
    cfg.graph.ig_user_id = "17841471829225215"
    cfg.graph.access_token = "page-token-for-test"
    cfg.graph.poll_seconds = 0
    return GraphPublisher(cfg)


def test_resumable_upload_precedes_publication(cfg, tmp_path, monkeypatch):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video from youtube")
    publisher = setup_publisher(cfg)
    calls = []
    statuses = iter(["IN_PROGRESS", "FINISHED"])

    def get(url, *, params, timeout):
        calls.append(("get", url))
        if url.endswith("/17841471829225215"):
            return Response({"username": "the.tinhorn"})
        if url.endswith("/container-1"):
            return Response({"status_code": next(statuses)})
        if url.endswith("/media-1"):
            return Response({"permalink": "https://www.instagram.com/reel/example/"})
        raise AssertionError(url)

    def post(url, *, data, timeout, headers=None, allow_redirects=True):
        calls.append(("post", url))
        if url.endswith("/media"):
            assert data["media_type"] == "REELS"
            assert data["upload_type"] == "resumable"
            assert data["caption"] == "My YouTube title"
            assert "video_url" not in data
            return Response({
                "id": "container-1",
                "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/container-1",
            })
        if url.startswith("https://rupload.facebook.com/"):
            assert allow_redirects is False
            assert data.read() == video.read_bytes()
            assert headers["Authorization"] == "OAuth page-token-for-test"
            assert headers["offset"] == "0"
            assert headers["file_size"] == str(video.stat().st_size)
            return Response({"success": True})
        if url.endswith("/media_publish"):
            assert data["creation_id"] == "container-1"
            return Response({"id": "media-1"})
        raise AssertionError(url)

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)

    result = publisher.publish(video, "My YouTube title")

    assert result.media_id == "media-1"
    assert result.permalink == "https://www.instagram.com/reel/example/"
    assert result.raw == {"container_id": "container-1"}
    assert [kind for kind, _ in calls] == ["get", "post", "post", "get", "get", "post", "get"]


def test_upload_failure_never_calls_media_publish(cfg, tmp_path, monkeypatch):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video")
    publisher = setup_publisher(cfg)
    posts = []

    monkeypatch.setattr(requests, "get", lambda *a, **k: Response({"username": "the.tinhorn"}))

    def post(url, **kwargs):
        posts.append(url)
        if url.endswith("/media"):
            return Response({
                "id": "container-1",
                "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/container-1",
            })
        return Response({"error": "upload failed"}, status_code=500)

    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(PublishError, match="video upload failed"):
        publisher.publish(video, "Title")
    assert not any(url.endswith("/media_publish") for url in posts)


def test_rejects_untrusted_upload_uri(cfg, tmp_path, monkeypatch):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video")
    publisher = setup_publisher(cfg)
    posts = []

    monkeypatch.setattr(requests, "get", lambda *a, **k: Response({"username": "the.tinhorn"}))

    def post(url, **kwargs):
        posts.append(url)
        return Response({"id": "container-1", "uri": "https://example.com/collect"})

    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(PublishError, match="invalid resumable upload URI"):
        publisher.publish(video, "Title")
    assert len(posts) == 1


def test_empty_file_is_rejected_before_meta_call(cfg, tmp_path, monkeypatch):
    video = tmp_path / "empty.mp4"
    video.touch()
    publisher = setup_publisher(cfg)
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("unexpected API call"))

    with pytest.raises(PublishError, match="between 1 byte and 1 GB"):
        publisher.publish(video, "Title")


def test_processing_error_never_calls_media_publish(cfg, tmp_path, monkeypatch):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video")
    publisher = setup_publisher(cfg)
    posts = []

    def get(url, **kwargs):
        if url.endswith("/container-1"):
            return Response({"status_code": "ERROR", "status": "processing failed"})
        return Response({"username": "the.tinhorn"})

    def post(url, **kwargs):
        posts.append(url)
        if url.endswith("/media"):
            return Response({
                "id": "container-1",
                "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/container-1",
            })
        return Response({"success": True})

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(PublishError, match="processing failed"):
        publisher.publish(video, "Title")
    assert not any(url.endswith("/media_publish") for url in posts)


def test_missing_published_media_id_is_an_error(cfg, tmp_path, monkeypatch):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video")
    publisher = setup_publisher(cfg)

    def get(url, **kwargs):
        if url.endswith("/container-1"):
            return Response({"status_code": "FINISHED"})
        return Response({"username": "the.tinhorn"})

    def post(url, **kwargs):
        if url.endswith("/media"):
            return Response({
                "id": "container-1",
                "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/container-1",
            })
        if url.endswith("/media_publish"):
            return Response({})
        return Response({"success": True})

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(PublishError, match="did not return a media ID"):
        publisher.publish(video, "Title")


def test_graph_errors_do_not_expose_the_page_token(cfg, monkeypatch):
    publisher = setup_publisher(cfg)

    def get(*args, **kwargs):
        raise requests.ConnectionError("https://graph.facebook.com/?access_token=page-token-for-test")

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(PublishError) as failure:
        publisher.check()
    assert "page-token-for-test" not in str(failure.value)


def test_meta_error_body_redacts_the_page_token(cfg):
    publisher = setup_publisher(cfg)
    response = Response({"error": "page-token-for-test is invalid"}, status_code=400)
    with pytest.raises(PublishError) as failure:
        publisher._result(response, "account check")
    assert "page-token-for-test" not in str(failure.value)
