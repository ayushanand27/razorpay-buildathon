"""
Covers groq_keys.py's rotation logic in isolation (no network, no
dependency on nlu.py/upsell_copy.py's own fallback behavior).
"""

from app import groq_keys


class _FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code


def test_no_keys_returns_none():
    assert groq_keys.post_with_rotation(lambda key: _FakeResp(200), "") is None


def test_single_key_success_no_rotation_needed():
    calls = []

    def post(key):
        calls.append(key)
        return _FakeResp(200)

    resp = groq_keys.post_with_rotation(post, "primary_key")
    assert resp.status_code == 200
    assert calls == ["primary_key"]


def test_rotates_to_backup_on_429(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY_2", "backup_key")
    calls = []

    def post(key):
        calls.append(key)
        return _FakeResp(429) if key == "primary_key" else _FakeResp(200)

    resp = groq_keys.post_with_rotation(post, "primary_key")
    assert resp.status_code == 200
    assert calls == ["primary_key", "backup_key"]


def test_does_not_rotate_on_non_429_error():
    """A 400 (bad request) isn't a rate-limit problem -- a different key
    wouldn't fix it, so only the primary should be tried."""
    calls = []

    def post(key):
        calls.append(key)
        return _FakeResp(400)

    resp = groq_keys.post_with_rotation(post, "primary_key")
    assert resp.status_code == 400
    assert calls == ["primary_key"]


def test_all_keys_exhausted_returns_last_429(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY_2", "backup_key")
    resp = groq_keys.post_with_rotation(lambda key: _FakeResp(429), "primary_key")
    assert resp.status_code == 429


def test_backup_keys_read_in_order(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY_2", "key2")
    monkeypatch.setenv("GROQ_API_KEY_3", "key3")
    assert groq_keys.backup_keys() == ["key2", "key3"]


def test_backup_keys_stop_at_first_gap(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY_2", "key2")
    monkeypatch.delenv("GROQ_API_KEY_3", raising=False)
    monkeypatch.setenv("GROQ_API_KEY_4", "key4")  # never reached -- gap at _3 stops the scan
    assert groq_keys.backup_keys() == ["key2"]
