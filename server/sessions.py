"""Server-side sessions: private temporary storage for uploaded captures.

Each session is a directory ``<root>/<uuid4 hex>/`` inside a private root
created with ``tempfile.mkdtemp`` (mode 0700). Uploaded captures are stored as
``capture_0.wav`` and ``capture_1.wav``. The filename the user supplied is kept
only as display text on the session record for the response envelope and is
never used to build a path (Requirement 17.2).

Uploads are streamed to disk with a running byte count and aborted, with the
partial file removed, as soon as they exceed 64 MiB (Requirement 3.3).

Every request should go through ``get`` (or ``touch``), which updates
``last_access``. ``sweep`` deletes sessions idle for 60 minutes and ``delete``
removes a session immediately (Requirement 17.3). The clock is injectable so
the sweep can be tested without waiting.

All public methods are safe to call from several threads; the session table is
guarded by one lock.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncIterable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from taperescue.wavcheck import MAX_BYTES

IDLE_TIMEOUT_S = 60 * 60
SWEEP_INTERVAL_S = 60
MAX_CAPTURES = 2
CAPTURE_NAMES = ("capture_0.wav", "capture_1.wav")
DEFAULT_CHUNK_SIZE = 1024 * 1024
MAX_DISPLAY_NAME = 255

SESSION_ID_RE = re.compile(r"[0-9a-f]{32}")

SessionErrorCode = Literal["invalid_session_id", "not_found", "invalid_capture_index", "too_large"]


class SessionError(Exception):
    """Base class for session failures. ``message`` is user-facing."""

    code: SessionErrorCode

    def __init__(self, code: SessionErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InvalidSessionId(SessionError):
    def __init__(self) -> None:
        super().__init__("invalid_session_id", "Session not found or expired")


class SessionNotFound(SessionError):
    def __init__(self) -> None:
        super().__init__("not_found", "Session not found or expired")


class InvalidCaptureIndex(SessionError):
    def __init__(self, index: object) -> None:
        super().__init__(
            "invalid_capture_index",
            f"Capture index must be 0 or 1, got {index!r}",
        )


class UploadTooLarge(SessionError):
    """An upload went over the size limit. The partial file has been removed."""

    def __init__(self, capture_index: int, limit: int) -> None:
        super().__init__(
            "too_large",
            f"Capture is larger than the 64 MiB limit ({limit} bytes)",
        )
        self.capture_index = capture_index
        self.limit = limit


class AsyncReadable(Protocol):
    async def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True)
class StoredCapture:
    index: int
    display_name: str
    path: Path
    size: int


@dataclass
class Session:
    session_id: str
    directory: Path
    created: float
    last_access: float
    captures: dict[int, StoredCapture] = field(default_factory=dict)
    # Uploads in progress. The sweep never removes a session that is being written.
    active_uploads: int = 0

    def stored_captures(self) -> list[StoredCapture]:
        return [self.captures[i] for i in sorted(self.captures)]


def is_valid_session_id(session_id: object) -> bool:
    return isinstance(session_id, str) and SESSION_ID_RE.fullmatch(session_id) is not None


def clean_display_name(name: object) -> str:
    """Display text for a user-supplied filename. Never used in a path."""
    text = name if isinstance(name, str) else ""
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:MAX_DISPLAY_NAME]


async def iter_upload(upload: AsyncReadable, chunk_size: int = DEFAULT_CHUNK_SIZE):
    """Yield chunks from an object with ``async read(n)``, such as FastAPI ``UploadFile``."""
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            return
        yield chunk


class _UploadWriter:
    """Writes one capture to ``<name>.part`` and renames it into place on success."""

    def __init__(self, final_path: Path, capture_index: int, limit: int) -> None:
        self.final_path = final_path
        self.part_path = final_path.with_name(final_path.name + ".part")
        self.capture_index = capture_index
        self.limit = limit
        self.size = 0
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.part_path, flags, 0o600)
        except FileNotFoundError:
            # The session directory was removed while the upload was starting.
            raise SessionNotFound() from None
        self._file = os.fdopen(fd, "wb")

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > self.limit:
            self.abort()
            raise UploadTooLarge(self.capture_index, self.limit)
        self._file.write(chunk)

    def commit(self) -> int:
        self._file.close()
        try:
            os.replace(self.part_path, self.final_path)
        except FileNotFoundError:
            # The session directory was removed while the upload was in progress.
            raise SessionNotFound() from None
        return self.size

    def abort(self) -> None:
        self._file.close()
        try:
            self.part_path.unlink()
        except FileNotFoundError:
            pass


class SessionStore:
    def __init__(
        self,
        parent: Path | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
        max_upload_bytes: int = MAX_BYTES,
    ) -> None:
        # mkdtemp creates the directory with mode 0700; chmod again in case of an odd umask.
        self.root = Path(
            tempfile.mkdtemp(prefix="taperescue-sessions-", dir=parent)
        ).resolve()
        os.chmod(self.root, 0o700)
        self._clock = clock
        self.idle_timeout_s = idle_timeout_s
        self.max_upload_bytes = max_upload_bytes
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    # Session lifecycle -------------------------------------------------

    def create(self) -> Session:
        session_id = uuid.uuid4().hex
        directory = self._session_dir(session_id)
        directory.mkdir(mode=0o700)
        now = self._clock()
        session = Session(session_id, directory, created=now, last_access=now)
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str, *, touch: bool = True) -> Session:
        """Return the session and, by default, record this request as its last access."""
        self._check_id(session_id)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFound()
            if touch:
                session.last_access = self._clock()
            return session

    def touch(self, session_id: str) -> None:
        self.get(session_id, touch=True)

    def delete(self, session_id: str) -> bool:
        """Remove the session and its files now. Returns False if it did not exist."""
        self._check_id(session_id)
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        self._remove_dir(session.directory)
        return True

    def sweep(self) -> list[str]:
        """Delete sessions idle for at least ``idle_timeout_s``. Returns their IDs."""
        now = self._clock()
        expired: list[Session] = []
        with self._lock:
            for session_id, session in list(self._sessions.items()):
                if session.active_uploads == 0 and now - session.last_access >= self.idle_timeout_s:
                    expired.append(self._sessions.pop(session_id))
        for session in expired:
            self._remove_dir(session.directory)
        return sorted(s.session_id for s in expired)

    async def run_sweeper(self, interval_s: float = SWEEP_INTERVAL_S) -> None:
        """Sweep forever every ``interval_s`` seconds. Cancel the task to stop it."""
        while True:
            await asyncio.sleep(interval_s)
            await asyncio.to_thread(self.sweep)

    def close(self) -> None:
        """Delete every session and the private root."""
        with self._lock:
            self._sessions.clear()
        shutil.rmtree(self.root, ignore_errors=True)

    def session_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._sessions)

    # Uploads -----------------------------------------------------------

    def capture_path(self, session_id: str, index: int) -> Path:
        self._check_index(index)
        return self._session_dir(session_id) / CAPTURE_NAMES[index]

    def save_upload_sync(
        self,
        session_id: str,
        index: int,
        chunks: Iterable[bytes],
        display_name: object = "",
    ) -> StoredCapture:
        writer, session = self._begin_upload(session_id, index)
        try:
            for chunk in chunks:
                writer.write(chunk)
            size = writer.commit()
        except BaseException:
            writer.abort()
            self._end_upload(session)
            raise
        return self._finish_upload(session, index, display_name, writer.final_path, size)

    async def save_upload(
        self,
        session_id: str,
        index: int,
        chunks: AsyncIterable[bytes] | Iterable[bytes],
        display_name: object = "",
    ) -> StoredCapture:
        """Stream ``chunks`` (async or sync iterable of bytes) to the capture file."""
        writer, session = self._begin_upload(session_id, index)
        try:
            if hasattr(chunks, "__aiter__"):
                async for chunk in chunks:  # type: ignore[union-attr]
                    writer.write(chunk)
            else:
                for chunk in chunks:  # type: ignore[union-attr]
                    writer.write(chunk)
            size = writer.commit()
        except BaseException:
            writer.abort()
            self._end_upload(session)
            raise
        return self._finish_upload(session, index, display_name, writer.final_path, size)

    # Internals ---------------------------------------------------------

    def _begin_upload(self, session_id: str, index: int) -> tuple[_UploadWriter, Session]:
        self._check_index(index)
        session = self.get(session_id)
        with self._lock:
            session.active_uploads += 1
        try:
            writer = _UploadWriter(
                self.capture_path(session_id, index), index, self.max_upload_bytes
            )
        except BaseException:
            self._end_upload(session)
            raise
        return writer, session

    def _end_upload(self, session: Session) -> None:
        with self._lock:
            session.active_uploads -= 1
            session.last_access = self._clock()

    def _finish_upload(
        self, session: Session, index: int, display_name: object, path: Path, size: int
    ) -> StoredCapture:
        stored = StoredCapture(index, clean_display_name(display_name), path, size)
        with self._lock:
            session.captures[index] = stored
            session.active_uploads -= 1
            session.last_access = self._clock()
            deleted = self._sessions.get(session.session_id) is not session
        if deleted:
            # The session was deleted while this upload was in progress.
            self._remove_dir(session.directory)
            raise SessionNotFound()
        return stored

    def _check_id(self, session_id: object) -> None:
        if not is_valid_session_id(session_id):
            raise InvalidSessionId()

    @staticmethod
    def _check_index(index: object) -> None:
        if type(index) is not int or not 0 <= index < MAX_CAPTURES:
            raise InvalidCaptureIndex(index)

    def _session_dir(self, session_id: str) -> Path:
        # Validate before joining, then confirm the result is a direct child of the root.
        self._check_id(session_id)
        directory = self.root / session_id
        if directory.parent != self.root:
            raise InvalidSessionId()
        return directory

    def _remove_dir(self, directory: Path) -> None:
        if directory.parent != self.root or not is_valid_session_id(directory.name):
            return
        shutil.rmtree(directory, ignore_errors=True)
