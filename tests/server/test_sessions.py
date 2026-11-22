"""Tests for server/sessions.py (Requirements 3.3, 17.2, 17.3)."""

from __future__ import annotations

import asyncio
import io
import stat
import threading

import pytest

from server.sessions import (
    CAPTURE_NAMES,
    IDLE_TIMEOUT_S,
    InvalidCaptureIndex,
    InvalidSessionId,
    SessionNotFound,
    SessionStore,
    UploadTooLarge,
    iter_upload,
)
from taperescue.wavcheck import MAX_BYTES


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class AsyncUpload:
    """Minimal stand-in for an object with ``async read(n)``, like FastAPI's UploadFile."""

    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    async def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path, clock):
    s = SessionStore(tmp_path, clock=clock)
    yield s
    s.close()


def _files(path):
    return sorted(p.name for p in path.iterdir())


def test_root_is_private_and_fresh(store, tmp_path):
    assert store.root.parent == tmp_path.resolve()
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert list(store.root.iterdir()) == []


def test_default_root_is_private():
    s = SessionStore()
    try:
        assert stat.S_IMODE(s.root.stat().st_mode) == 0o700
    finally:
        s.close()
    assert not s.root.exists()


def test_session_ids_and_directories_are_generated(store):
    a, b = store.create(), store.create()
    assert a.session_id != b.session_id
    for session in (a, b):
        assert len(session.session_id) == 32
        assert int(session.session_id, 16) >= 0
        assert session.directory == store.root / session.session_id
        assert stat.S_IMODE(session.directory.stat().st_mode) == 0o700


def test_upload_uses_generated_names_and_keeps_display_name_out_of_paths(store):
    session = store.create()
    hostile = "../../etc/passwd\x00evil.wav"
    first = store.save_upload_sync(session.session_id, 0, [b"RIFF", b"data"], hostile)
    second = asyncio.run(
        store.save_upload(session.session_id, 1, iter_upload(AsyncUpload(b"x" * 10), 3), "side-b.wav")
    )

    assert first.path == session.directory / "capture_0.wav"
    assert second.path == session.directory / "capture_1.wav"
    assert _files(session.directory) == list(CAPTURE_NAMES)
    assert first.path.read_bytes() == b"RIFFdata" and first.size == 8
    assert second.path.read_bytes() == b"x" * 10 and second.size == 10
    # Display name is kept as text only (control characters dropped).
    assert first.display_name == "../../etc/passwdevil.wav"
    assert second.display_name == "side-b.wav"
    assert "passwd" not in str(first.path)
    assert [c.index for c in store.get(session.session_id).stored_captures()] == [0, 1]


def test_upload_accepts_a_plain_iterable_in_async_mode(store):
    session = store.create()
    stored = asyncio.run(store.save_upload(session.session_id, 0, [b"ab", b"cd"]))
    assert stored.path.read_bytes() == b"abcd"


@pytest.mark.parametrize("index", [-1, 2, "0", True, 0.0])
def test_capture_index_must_be_0_or_1(store, index):
    session = store.create()
    with pytest.raises(InvalidCaptureIndex):
        store.save_upload_sync(session.session_id, index, [b"x"])
    assert _files(session.directory) == []


def test_upload_at_limit_is_kept(tmp_path, clock):
    s = SessionStore(tmp_path, clock=clock, max_upload_bytes=10)
    try:
        session = s.create()
        stored = s.save_upload_sync(session.session_id, 0, [b"12345", b"67890"])
        assert stored.size == 10
    finally:
        s.close()


def test_too_large_upload_is_aborted_and_partial_file_removed(tmp_path, clock):
    s = SessionStore(tmp_path, clock=clock, max_upload_bytes=10)
    consumed = []

    def chunks():
        for c in (b"12345", b"67890", b"!", b"never read"):
            consumed.append(c)
            yield c

    try:
        session = s.create()
        with pytest.raises(UploadTooLarge) as info:
            s.save_upload_sync(session.session_id, 1, chunks(), "big.wav")
        assert info.value.code == "too_large"
        assert info.value.capture_index == 1
        assert "64 MiB" in info.value.message
        # Aborted on the chunk that crossed the limit; nothing else was read.
        assert consumed == [b"12345", b"67890", b"!"]
        assert _files(session.directory) == []
        assert s.get(session.session_id).captures == {}

        with pytest.raises(UploadTooLarge):
            asyncio.run(
                s.save_upload(session.session_id, 0, iter_upload(AsyncUpload(b"y" * 11), 4))
            )
        assert _files(session.directory) == []
    finally:
        s.close()


def test_default_limit_is_64_mib(store):
    assert store.max_upload_bytes == MAX_BYTES == 64 * 1024 * 1024


def test_get_updates_last_access(store, clock):
    session = store.create()
    clock.advance(120)
    store.get(session.session_id)
    assert session.last_access == clock.now
    clock.advance(30)
    store.get(session.session_id, touch=False)
    assert session.last_access == clock.now - 30


def test_sweep_deletes_only_idle_sessions(store, clock):
    idle = store.create()
    store.save_upload_sync(idle.session_id, 0, [b"data"])
    busy = store.create()

    clock.advance(IDLE_TIMEOUT_S - 1)
    store.touch(busy.session_id)
    assert store.sweep() == []          # idle is at 59:59
    clock.advance(1)
    assert store.sweep() == [idle.session_id]   # idle reached 60:00; busy was used 1 s ago

    assert not idle.directory.exists()
    assert busy.directory.exists()
    with pytest.raises(SessionNotFound):
        store.get(idle.session_id)
    assert store.session_ids() == [busy.session_id]

    clock.advance(IDLE_TIMEOUT_S)
    assert store.sweep() == [busy.session_id]
    assert list(store.root.iterdir()) == []


def test_sweep_skips_a_session_with_an_upload_in_progress(store, clock):
    session = store.create()
    seen = []

    def chunks():
        yield b"a"
        clock.advance(IDLE_TIMEOUT_S * 2)
        seen.append(store.sweep())
        yield b"b"

    store.save_upload_sync(session.session_id, 0, chunks())
    assert seen == [[]]
    assert (session.directory / "capture_0.wav").read_bytes() == b"ab"


def test_run_sweeper_removes_idle_sessions(store, clock):
    session = store.create()
    clock.advance(IDLE_TIMEOUT_S)

    async def run():
        task = asyncio.create_task(store.run_sweeper(interval_s=0.01))
        for _ in range(200):
            if not session.directory.exists():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert not session.directory.exists()


def test_delete_on_request(store):
    session = store.create()
    store.save_upload_sync(session.session_id, 0, [b"data"])
    other = store.create()

    assert store.delete(session.session_id) is True
    assert not session.directory.exists()
    assert other.directory.exists()
    with pytest.raises(SessionNotFound):
        store.get(session.session_id)
    assert store.delete(session.session_id) is False


def test_upload_to_deleted_session_is_rejected(store):
    session = store.create()
    store.delete(session.session_id)
    with pytest.raises(SessionNotFound):
        store.save_upload_sync(session.session_id, 0, [b"x"])
    assert not session.directory.exists()


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "../" + "a" * 29,
        "..",
        "A" * 32,                    # uppercase hex
        "a" * 31,
        "a" * 33,
        "g" * 32,
        "a" * 31 + "/",
        "/" + "a" * 31,
        "a" * 32 + "\n",
        "a" * 16 + "\x00" + "a" * 15,
        "１" * 32,                   # non-ASCII digits
        None,
        123,
    ],
)
def test_invalid_session_ids_are_rejected_before_any_path_use(store, bad):
    store.create()
    before = _files(store.root)
    for call in (
        lambda: store.get(bad),
        lambda: store.delete(bad),
        lambda: store.capture_path(bad, 0),
        lambda: store.save_upload_sync(bad, 0, [b"x"]),
    ):
        with pytest.raises(InvalidSessionId) as info:
            call()
        assert info.value.code == "invalid_session_id"
    assert _files(store.root) == before


def test_unknown_but_valid_session_id_is_not_found(store):
    with pytest.raises(SessionNotFound):
        store.get("0" * 32)


def test_concurrent_creates_and_deletes(store):
    ids: list[str] = []
    lock = threading.Lock()

    def worker():
        for _ in range(20):
            s = store.create()
            store.save_upload_sync(s.session_id, 0, [b"x"])
            with lock:
                ids.append(s.session_id)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 160
    assert store.session_ids() == sorted(ids)

    threads = [threading.Thread(target=store.delete, args=(sid,)) for sid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.session_ids() == []
    assert list(store.root.iterdir()) == []
