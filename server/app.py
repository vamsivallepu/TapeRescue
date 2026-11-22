"""TapeRescue web API (design section 11).

Endpoints:

* ``POST /api/sessions``: multipart upload of one or two files in the field
  ``captures``. Each capture is validated with ``wavcheck``, decoded with the
  patched decoder in a worker thread, and the decoded captures go through
  ``recover``, the export gate and ``build_report`` (Requirement 17.1).
* ``POST /api/sample``: the same pipeline on the bundled ``damaged_a.wav`` and
  ``damaged_b.wav`` (Requirement 17.5).
* ``GET /api/sessions/{sid}``: the stored response envelope.
* ``GET /api/sessions/{sid}/report.json``: the recovery report as a download,
  for complete and incomplete sessions alike (Requirements 10.8, 17.4).
* ``GET /api/sessions/{sid}/tape.uef``: the UEF image, written into the
  session directory and verified with the independent reader by
  ``export_uef``; 409 with the blockers when the gate refuses (Requirement 11).
* ``DELETE /api/sessions/{sid}``: removes the session's files now (17.3).
* ``/``: the built Web UI, when the static directory exists.

The envelope follows ``SessionEnvelope`` in ``web/src/api.ts``::

    {"session_id", "captures": [{"index", "display_name", "capture_id", "status"}],
     "errors": [{"capture_index", "display_name", "code", "message"}],
     "report", "observations": {capture_id: [block, ...]}, "export"}

Status codes:

* 200: at least one capture decoded and recovery ran. Rejected captures are
  listed in ``errors`` with ``status: "error"``; a session with one rejected
  and one valid capture returns the valid capture's single-capture report.
* 400: zero files, more than two files, or a malformed upload
  (``{"detail": "Upload one or two WAV captures"}``).
* 404: unknown, expired or malformed session ID.
* 409: ``tape.uef`` while export is not permitted
  (``{"detail", "blockers", "messages"}``).
* 413: the request body exceeds what two 64 MiB captures need.
* 422: no capture decoded, or two captures share no anchor block. The body is
  ``{"errors": [...]}`` with the capture index of each failure, or
  ``capture_index: null`` for the session-level ``no_anchor``. The session is
  deleted, since there is nothing to show or download.
* 503: the decoder or the sample captures are not installed on this server.

Uploads are stored under server-generated names by ``SessionStore``; the
user's filename is display text only (Requirement 17.2). Captures are never
sent anywhere: the only process that sees them is the local decoder
(Requirement 19.2). Every response carries ``Content-Security-Policy:
default-src 'self'`` and ``X-Content-Type-Options: nosniff`` (Requirement 19.1).

Configuration comes from the environment, with defaults inside the repository:
``TAPERESCUE_DECODER`` (``build/decoder/ScanTape``), ``TAPERESCUE_FIXTURES``
(``fixtures/starmap``) and ``TAPERESCUE_STATIC`` (``web/dist``, served only
if it contains ``index.html``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio.to_thread
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from server.sessions import (
    MAX_CAPTURES,
    SWEEP_INTERVAL_S,
    Session,
    SessionError,
    SessionStore,
    StoredCapture,
    UploadTooLarge,
    clean_display_name,
    iter_upload,
)
from taperescue.decoder_adapter import DecoderError, decode_capture
from taperescue.decoder_adapter.run import DEFAULT_TIMEOUT_S
from taperescue.decoder_adapter.schema import observations_to_doc
from taperescue.engine import PairingRejected, recover
from taperescue.engine.gate import ExportDecision, export_decision
from taperescue.engine.report import build_report, dumps_report, reason_message
from taperescue.models import CaptureObservations, RecoverOptions, RecoveryResult
from taperescue.uef.exporter import ExportRefused, export_uef
from taperescue.uef.verify import ExportVerificationFailed
from taperescue.wavcheck import MAX_BYTES, WavRejected, validate_capture

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DECODER_ENV = "TAPERESCUE_DECODER"
FIXTURES_ENV = "TAPERESCUE_FIXTURES"
STATIC_ENV = "TAPERESCUE_STATIC"
DEFAULT_DECODER = REPO_ROOT / "build" / "decoder" / "ScanTape"
DEFAULT_FIXTURES = REPO_ROOT / "fixtures" / "starmap"
DEFAULT_STATIC = REPO_ROOT / "web" / "dist"
SAMPLE_FILES = ("damaged_a.wav", "damaged_b.wav")

UPLOAD_FIELD = "captures"
# Two captures at the size limit plus room for multipart boundaries and headers.
MAX_BODY_BYTES = MAX_CAPTURES * MAX_BYTES + 1024 * 1024
MAX_FORM_FIELDS = 16

MSG_UPLOAD_COUNT = "Upload one or two WAV captures"
MSG_MALFORMED = "The upload is not a valid multipart form"
MSG_BODY_TOO_LARGE = "The upload is larger than two 64 MiB captures"
MSG_NOT_FOUND = "Session not found or expired"
MSG_EXPORT_REFUSED = "Export is not permitted for this session"
MSG_VERIFY_FAILED = "Exported image failed verification"
MSG_DECODER_MISSING = "The decoder is not available on this server"
MSG_SAMPLE_MISSING = "The sample captures are not available on this server"

REPORT_DOWNLOAD_NAME = "taperescue-report.json"
DEFAULT_TAPE_NAME = "tape"
SAFE_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,10}")

SECURITY_HEADERS = (
    (b"content-security-policy", b"default-src 'self'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


# --- middleware ---------------------------------------------------------------

class SecurityHeadersMiddleware:
    """Adds the CSP and nosniff headers to every HTTP response.

    API responses also get ``Cache-Control: no-store`` because they carry
    session data.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_api = scope.get("path", "").startswith("/api/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                skip = {name for name, _ in SECURITY_HEADERS}
                if is_api:
                    skip.add(b"cache-control")
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in skip]
                headers.extend(SECURITY_HEADERS)
                if is_api:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _BodyTooLarge(Exception):
    pass


def _limited_receive(receive: Receive, limit: int) -> Receive:
    """Wrap ``receive`` so a request body over ``limit`` bytes raises ``_BodyTooLarge``."""
    received = 0

    async def wrapped() -> Message:
        nonlocal received
        message = await receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > limit:
                raise _BodyTooLarge()
        return message

    return wrapped


# --- session results ------------------------------------------------------------

@dataclass
class SessionData:
    """What a finished session needs for its GET and download endpoints."""

    envelope: dict[str, Any]
    result: RecoveryResult
    decision: ExportDecision
    report_text: str
    tape_name: str


@dataclass(frozen=True)
class AppConfig:
    decoder: Path
    fixtures: Path
    static: Path | None
    session_parent: Path | None
    clock: Callable[[], float] | None
    timeout_s: float
    sweep_interval_s: float


class _Rejected(Exception):
    """The pipeline ended without a report; respond with ``status`` and ``body``."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(status)
        self.status = status
        self.body = body


def _capture_error(index: int | None, display_name: str | None, code: str, message: str) -> dict:
    return {"capture_index": index, "display_name": display_name, "code": code, "message": message}


def _observation_blocks(capture: CaptureObservations) -> list[dict]:
    blocks = observations_to_doc(capture)["blocks"]
    for doc, obs in zip(blocks, capture.observations):
        doc["block_no"] = None if obs.header is None else obs.header.block_no
    return blocks


def _export_envelope(decision: ExportDecision) -> dict:
    return {
        "permitted": decision.permitted,
        "blockers": [[n, code] for n, code in sorted(decision.blockers)],
        "reconstructed_blocks": sorted(decision.reconstructed_blocks),
    }


def _tape_name(result: RecoveryResult) -> str:
    """Download name from the cassette filename of block 0, if it is a safe name."""
    for block in result.blocks:
        if block.block_no != 0:
            continue
        for read in block.reads:
            if read.header_crc.outcome == "valid" and read.header is not None:
                name = read.header.filename
                if SAFE_NAME_RE.fullmatch(name):
                    return name
    return DEFAULT_TAPE_NAME


def _check_decoder(decoder: Path) -> None:
    if not decoder.is_file() or not os.access(decoder, os.X_OK):
        log.error("decoder not found or not executable at %s", decoder)
        raise _Rejected(503, {"detail": MSG_DECODER_MISSING})


def _run_pipeline(
    config: AppConfig,
    session: Session,
    stored: list[StoredCapture],
    upload_errors: list[tuple[int, dict]],
) -> SessionData:
    """Validate, decode and recover. Runs in a worker thread.

    ``upload_errors`` holds ``(index, error)`` for uploads that never reached
    disk (too large). Raises ``_Rejected`` when no report can be produced.
    """
    _check_decoder(config.decoder)
    summaries: dict[int, dict] = {}
    errors: dict[int, dict] = dict(upload_errors)
    decoded: list[CaptureObservations] = []

    for index, error in upload_errors:
        summaries[index] = {
            "index": index,
            "display_name": error["display_name"],
            "capture_id": None,
            "status": "error",
        }

    for cap in stored:
        summary = {
            "index": cap.index,
            "display_name": cap.display_name,
            "capture_id": None,
            "status": "error",
        }
        summaries[cap.index] = summary
        try:
            validate_capture(cap.path)
        except WavRejected as exc:
            errors[cap.index] = _capture_error(cap.index, cap.display_name, exc.code, exc.message)
            continue
        try:
            observations = decode_capture(
                cap.path, decoder=config.decoder, workdir=session.directory,
                timeout_s=config.timeout_s,
            )
        except DecoderError as exc:
            summary["capture_id"] = exc.capture_id
            errors[cap.index] = _capture_error(cap.index, cap.display_name, exc.code, exc.message)
            continue
        summary["capture_id"] = observations.capture_id
        summary["status"] = "ok"
        decoded.append(observations)

    error_list = [errors[i] for i in sorted(errors)]
    if not decoded:
        raise _Rejected(422, {"errors": error_list})
    try:
        result = recover(decoded, RecoverOptions())
    except PairingRejected as exc:
        error_list.append(_capture_error(None, None, exc.code, exc.message))
        raise _Rejected(422, {"errors": error_list}) from exc

    decision = export_decision(result)
    report = build_report(result, decision)
    envelope = {
        "session_id": session.session_id,
        "captures": [summaries[i] for i in sorted(summaries)],
        "errors": error_list,
        "report": report,
        "observations": {
            cap.capture_id: _observation_blocks(cap)
            for cap in sorted(result.captures, key=lambda c: c.capture_id)
        },
        "export": _export_envelope(decision),
    }
    return SessionData(
        envelope=envelope,
        result=result,
        decision=decision,
        report_text=dumps_report(report) + "\n",
        tape_name=_tape_name(result),
    )


def _copy_sample(store: SessionStore, session_id: str, fixtures: Path) -> list[StoredCapture]:
    """Copy the bundled damaged fixtures into the session under generated names."""
    stored = []
    for index, name in enumerate(SAMPLE_FILES):
        with open(fixtures / name, "rb") as fh:
            chunks = iter(lambda: fh.read(1 << 20), b"")
            stored.append(store.save_upload_sync(session_id, index, chunks, name))
    return stored


# --- application ------------------------------------------------------------------

def sweep_sessions(app: FastAPI) -> list[str]:
    """Delete idle sessions now and forget their results. Returns the removed IDs."""
    expired = app.state.store.sweep()
    for sid in expired:
        app.state.results.pop(sid, None)
    return expired


async def _sweep_loop(app: FastAPI, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            expired = await anyio.to_thread.run_sync(sweep_sessions, app)
        except Exception:  # keep sweeping; one failure must not stop cleanup
            log.exception("session sweep failed")
            continue
        if expired:
            log.info("swept %d idle sessions", len(expired))


def create_app(
    *,
    decoder: Path | None = None,
    fixtures: Path | None = None,
    static: Path | None = None,
    session_parent: Path | None = None,
    clock: Callable[[], float] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    sweep_interval_s: float = SWEEP_INTERVAL_S,
) -> FastAPI:
    static_dir = static if static is not None else _env_path(STATIC_ENV, DEFAULT_STATIC)
    config = AppConfig(
        decoder=(decoder or _env_path(DECODER_ENV, DEFAULT_DECODER)).resolve(),
        fixtures=(fixtures or _env_path(FIXTURES_ENV, DEFAULT_FIXTURES)).resolve(),
        static=static_dir if (static_dir / "index.html").is_file() else None,
        session_parent=session_parent,
        clock=clock,
        timeout_s=timeout_s,
        sweep_interval_s=sweep_interval_s,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        kwargs = {} if config.clock is None else {"clock": config.clock}
        store = SessionStore(config.session_parent, **kwargs)
        app.state.store = store
        app.state.results = {}
        sweeper = asyncio.create_task(_sweep_loop(app, config.sweep_interval_s))
        try:
            yield
        finally:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper
            app.state.results.clear()
            store.close()

    app = FastAPI(title="TapeRescue", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.config = config
    app.add_middleware(SecurityHeadersMiddleware)

    def store_of(request: Request) -> SessionStore:
        return request.app.state.store

    def lookup(request: Request, session_id: str) -> tuple[Session, SessionData]:
        """The session (touched, so the request counts as access) and its results."""
        try:
            session = store_of(request).get(session_id)
        except SessionError:
            request.app.state.results.pop(session_id, None)
            raise HTTPException(404, MSG_NOT_FOUND) from None
        data = request.app.state.results.get(session_id)
        if data is None:
            raise HTTPException(404, MSG_NOT_FOUND)
        return session, data

    async def finish(
        request: Request,
        session: Session,
        stored: list[StoredCapture],
        upload_errors: list[tuple[int, dict]],
    ) -> JSONResponse:
        store = store_of(request)
        try:
            data = await anyio.to_thread.run_sync(
                _run_pipeline, config, session, stored, upload_errors
            )
        except _Rejected as exc:
            store.delete(session.session_id)
            return JSONResponse(exc.body, status_code=exc.status)
        except BaseException:
            store.delete(session.session_id)
            raise
        try:
            store.touch(session.session_id)
        except SessionError:
            # Deleted while the pipeline ran.
            raise HTTPException(404, MSG_NOT_FOUND) from None
        request.app.state.results[session.session_id] = data
        return JSONResponse(data.envelope)

    @app.post("/api/sessions")
    async def create_session(request: Request) -> Response:
        length = request.headers.get("content-length")
        if length is not None and length.isdigit() and int(length) > MAX_BODY_BYTES:
            raise HTTPException(413, MSG_BODY_TOO_LARGE)
        limited = Request(request.scope, _limited_receive(request.receive, MAX_BODY_BYTES))
        try:
            form = await limited.form(max_files=MAX_CAPTURES, max_fields=MAX_FORM_FIELDS)
        except _BodyTooLarge:
            raise HTTPException(413, MSG_BODY_TOO_LARGE) from None
        except StarletteHTTPException as exc:
            # Starlette turns multipart parser errors into a 400 with its own text.
            detail = str(exc.detail)
            message = MSG_UPLOAD_COUNT if detail.startswith("Too many files") else MSG_MALFORMED
            raise HTTPException(400, message) from None
        try:
            uploads = form.getlist(UPLOAD_FIELD)
            if not 1 <= len(uploads) <= MAX_CAPTURES or not all(
                isinstance(u, UploadFile) for u in uploads
            ):
                raise HTTPException(400, MSG_UPLOAD_COUNT)
            store = store_of(request)
            session = store.create()
            stored: list[StoredCapture] = []
            upload_errors: list[tuple[int, dict]] = []
            try:
                for index, upload in enumerate(uploads):
                    try:
                        stored.append(await store.save_upload(
                            session.session_id, index, iter_upload(upload), upload.filename
                        ))
                    except UploadTooLarge as exc:
                        name = clean_display_name(upload.filename)
                        upload_errors.append(
                            (index, _capture_error(index, name, "too_large", exc.message))
                        )
            except BaseException:
                store.delete(session.session_id)
                raise
        finally:
            await form.close()
        return await finish(request, session, stored, upload_errors)

    @app.post("/api/sample")
    async def run_sample(request: Request) -> Response:
        if not all((config.fixtures / name).is_file() for name in SAMPLE_FILES):
            log.error("sample captures missing under %s", config.fixtures)
            raise HTTPException(503, MSG_SAMPLE_MISSING)
        store = store_of(request)
        session = store.create()
        try:
            stored = await anyio.to_thread.run_sync(
                _copy_sample, store, session.session_id, config.fixtures
            )
        except BaseException:
            store.delete(session.session_id)
            raise
        return await finish(request, session, stored, [])

    @app.get("/api/sessions/{session_id}")
    async def get_session(request: Request, session_id: str) -> Response:
        _, data = lookup(request, session_id)
        return JSONResponse(data.envelope)

    @app.get("/api/sessions/{session_id}/report.json")
    async def get_report(request: Request, session_id: str) -> Response:
        _, data = lookup(request, session_id)
        return Response(
            data.report_text.encode("utf-8"),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{REPORT_DOWNLOAD_NAME}"'},
        )

    def refused(decision_blockers) -> JSONResponse:
        blockers = sorted(decision_blockers)
        return JSONResponse(
            {
                "detail": MSG_EXPORT_REFUSED,
                "blockers": [[n, code] for n, code in blockers],
                "messages": {code: reason_message(code) for code in sorted({c for _, c in blockers})},
            },
            status_code=409,
        )

    @app.get("/api/sessions/{session_id}/tape.uef")
    async def get_tape(request: Request, session_id: str) -> Response:
        session, data = lookup(request, session_id)
        if not data.decision.permitted:
            return refused(data.decision.blockers)
        out = session.directory / "tape.uef"

        def export() -> bytes:
            export_uef(data.result, data.decision, out)
            return out.read_bytes()

        try:
            image = await anyio.to_thread.run_sync(export)
        except ExportRefused as exc:
            return refused(exc.blockers)
        except ExportVerificationFailed as exc:
            log.error("session %s: %s", session_id, exc)
            raise HTTPException(500, MSG_VERIFY_FAILED) from None
        except FileNotFoundError:
            # The session directory was removed during the export.
            raise HTTPException(404, MSG_NOT_FOUND) from None
        return Response(
            image,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{data.tape_name}.uef"'},
        )

    @app.delete("/api/sessions/{session_id}", status_code=204)
    async def delete_session(request: Request, session_id: str) -> Response:
        request.app.state.results.pop(session_id, None)
        try:
            removed = await anyio.to_thread.run_sync(store_of(request).delete, session_id)
        except SessionError:
            removed = False
        if not removed:
            raise HTTPException(404, MSG_NOT_FOUND)
        return Response(status_code=204)

    if config.static is not None:
        app.mount("/", StaticFiles(directory=config.static, html=True), name="web")

    return app


app = create_app()
