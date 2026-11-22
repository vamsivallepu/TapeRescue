"""Tests for server/app.py (Requirements 3.1-3.5, 10.8, 17.1, 17.3-17.5, 19.1).

Tests that run the real decoder are skipped when ``build/decoder/ScanTape``
has not been built.
"""
from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.app import DEFAULT_DECODER, DEFAULT_FIXTURES, create_app, sweep_sessions
from server.sessions import IDLE_TIMEOUT_S
from taperescue.engine.report import dumps_report
from taperescue.uef.reader import read_uef

needs_decoder = pytest.mark.skipif(
    not DEFAULT_DECODER.is_file(),
    reason=f"decoder not built at {DEFAULT_DECODER}; run scripts/build_decoder.sh",
)

CSP = "default-src 'self'"
SID_UNKNOWN = "0" * 32


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def wav_bytes(rate: int = 44_100, bits: int = 16, channels: int = 1, frames: int = 100) -> bytes:
    block_align = channels * bits // 8
    data = b"\x00" * (frames * block_align)
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * block_align, block_align, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>TapeRescue</title>", encoding="utf-8")
    return d


@pytest.fixture
def client(tmp_path: Path, clock: FakeClock, static_dir: Path):
    app = create_app(
        decoder=DEFAULT_DECODER,
        fixtures=DEFAULT_FIXTURES,
        static=static_dir,
        session_parent=tmp_path,
        clock=clock,
    )
    with TestClient(app) as c:
        yield c


def upload(client: TestClient, *files: tuple[str, bytes]):
    return client.post(
        "/api/sessions",
        files=[("captures", (name, data, "audio/wav")) for name, data in files],
    )


def fixture_bytes(name: str) -> bytes:
    return (DEFAULT_FIXTURES / name).read_bytes()


def reference_program() -> bytes:
    ref = json.loads((DEFAULT_FIXTURES / "reference_payloads.json").read_text(encoding="utf-8"))
    return b"".join(bytes.fromhex(b["hex"]) for b in sorted(ref["blocks"], key=lambda b: b["block_no"]))


# --- no decoder needed ----------------------------------------------------------

@pytest.mark.parametrize("count", [0, 3])
def test_upload_count_outside_one_or_two_is_400(client: TestClient, count: int) -> None:
    files = [(f"c{i}.wav", wav_bytes()) for i in range(count)]
    if count == 0:
        r = client.post("/api/sessions", data={"other": "x"})
    else:
        r = upload(client, *files)
    assert r.status_code == 400
    assert r.json() == {"detail": "Upload one or two WAV captures"}
    assert client.app.state.store.session_ids() == []


@pytest.mark.parametrize(
    ("data", "code", "fragment"),
    [
        (wav_bytes(rate=48_000, channels=2), "unsupported_format", "48000 Hz"),
        (b"not a wav file at all", "not_riff_wave", None),
    ],
)
def test_rejected_capture_is_422_with_capture_index(
    client: TestClient, data: bytes, code: str, fragment: str | None
) -> None:
    r = upload(client, ("../../etc/side-a.wav", data))
    assert r.status_code == 422
    (error,) = r.json()["errors"]
    assert error["capture_index"] == 0
    assert error["code"] == code
    assert error["display_name"] == "../../etc/side-a.wav"
    if fragment:
        assert fragment in error["message"]
        assert "44100 Hz, 16-bit, mono PCM" in error["message"]
    # Nothing to show, so the session and its files are gone.
    assert client.app.state.store.session_ids() == []
    assert list(client.app.state.store.root.iterdir()) == []


def test_unknown_or_malformed_session_is_404(client: TestClient) -> None:
    for sid in (SID_UNKNOWN, "not-a-session", "A" * 32, "0" * 33):
        for path in ("", "/report.json", "/tape.uef"):
            r = client.get(f"/api/sessions/{sid}{path}")
            assert r.status_code == 404, (sid, path)
        assert client.delete(f"/api/sessions/{sid}").status_code == 404
    assert client.get(f"/api/sessions/{SID_UNKNOWN}").json() == {
        "detail": "Session not found or expired"
    }


def test_security_headers_on_api_and_static(client: TestClient) -> None:
    for r in (client.get(f"/api/sessions/{SID_UNKNOWN}"), client.get("/")):
        assert r.headers["content-security-policy"] == CSP
        assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/").text.startswith("<!doctype html>")
    assert client.get(f"/api/sessions/{SID_UNKNOWN}").headers["cache-control"] == "no-store"


def test_missing_decoder_is_503(tmp_path: Path, static_dir: Path) -> None:
    app = create_app(decoder=tmp_path / "missing", static=static_dir, session_parent=tmp_path)
    with TestClient(app) as c:
        r = upload(c, ("a.wav", wav_bytes()))
        assert r.status_code == 503
        assert c.app.state.store.session_ids() == []


# --- real decoder -----------------------------------------------------------------

@needs_decoder
def test_sample_recovers_and_tape_matches_reference(client: TestClient, tmp_path: Path) -> None:
    r = client.post("/api/sample")
    assert r.status_code == 200, r.text
    env = r.json()
    assert set(env) == {"session_id", "captures", "errors", "report", "observations", "export"}
    assert [c["status"] for c in env["captures"]] == ["ok", "ok"]
    assert [c["display_name"] for c in env["captures"]] == ["damaged_a.wav", "damaged_b.wav"]
    assert env["errors"] == []
    assert env["export"] == {"permitted": True, "blockers": [], "reconstructed_blocks": [6]}
    statuses = {b["block_no"]: b["status"] for b in env["report"]["blocks"]}
    assert statuses == {n: ("Reconstructed · CRC match" if n == 6 else "Intact") for n in range(8)}
    ids = sorted(c["capture_id"] for c in env["captures"])
    assert sorted(env["observations"]) == ids
    for blocks in env["observations"].values():
        assert [b["block_no"] for b in blocks] == list(range(8))

    sid = env["session_id"]
    assert client.get(f"/api/sessions/{sid}").json() == env

    report = client.get(f"/api/sessions/{sid}/report.json")
    assert report.status_code == 200
    assert "attachment" in report.headers["content-disposition"]
    assert report.text == dumps_report(env["report"]) + "\n"

    tape = client.get(f"/api/sessions/{sid}/tape.uef")
    assert tape.status_code == 200
    assert tape.headers["content-disposition"] == 'attachment; filename="STARMAP.uef"'
    out = tmp_path / "download.uef"
    out.write_bytes(tape.content)
    blocks = read_uef(out)
    assert all(b.header_crc_ok and b.data_crc_ok for b in blocks)
    program = b"".join(b.payload for b in blocks)
    assert program == reference_program()
    assert hashlib.sha256(program).hexdigest() == env["report"]["recovered_payload_sha256"]

    assert client.delete(f"/api/sessions/{sid}").status_code == 204
    assert client.get(f"/api/sessions/{sid}").status_code == 404
    assert client.app.state.store.session_ids() == []


@needs_decoder
def test_single_capture_refuses_tape_but_report_downloads(client: TestClient) -> None:
    r = upload(client, ("side-a.wav", fixture_bytes("damaged_a.wav")))
    assert r.status_code == 200, r.text
    env = r.json()
    assert env["export"]["permitted"] is False
    assert env["export"]["blockers"] == [[6, "single_read"]]
    sid = env["session_id"]
    tape = client.get(f"/api/sessions/{sid}/tape.uef")
    assert tape.status_code == 409
    assert tape.json()["blockers"] == [[6, "single_read"]]
    assert "single_read" in tape.json()["messages"]
    report = client.get(f"/api/sessions/{sid}/report.json")
    assert report.status_code == 200
    assert "recovered_payload_sha256" not in report.json()


@needs_decoder
def test_one_rejected_capture_keeps_the_valid_one(client: TestClient) -> None:
    r = upload(client, ("good.wav", fixture_bytes("clean.wav")), ("bad.wav", wav_bytes(bits=8)))
    assert r.status_code == 200, r.text
    env = r.json()
    assert [c["status"] for c in env["captures"]] == ["ok", "error"]
    assert env["captures"][1]["capture_id"] is None
    (error,) = env["errors"]
    assert (error["capture_index"], error["code"]) == (1, "unsupported_format")
    assert env["export"]["permitted"] is True
    assert len(env["report"]["inputs"]) == 1


@needs_decoder
def test_silent_capture_reports_no_blocks(client: TestClient) -> None:
    r = upload(client, ("silence.wav", wav_bytes(frames=44_100)))
    assert r.status_code == 422
    (error,) = r.json()["errors"]
    assert error["code"] == "no_blocks"
    assert error["message"] == "No BBC 1200-baud blocks were found in this capture"


@needs_decoder
def test_idle_session_is_swept_after_60_minutes(client: TestClient, clock: FakeClock) -> None:
    sid = client.post("/api/sample").json()["session_id"]
    directory = client.app.state.store.root / sid
    clock.now += IDLE_TIMEOUT_S - 1
    assert sweep_sessions(client.app) == []
    assert client.get(f"/api/sessions/{sid}").status_code == 200  # touches the session
    clock.now += IDLE_TIMEOUT_S
    assert sweep_sessions(client.app) == [sid]
    assert not directory.exists()
    assert client.get(f"/api/sessions/{sid}").status_code == 404
