# TapeRescue

TapeRescue recovers a BBC Micro program from one or two imperfect WAV recordings of the same data cassette. It decodes each recording into per-block observations, takes every block from a recording where it passes its data CRC, and rebuilds a block that fails in every recording when exactly one combination of the observed byte values matches the CRC recorded on the tape. It writes a `.uef` tape image and a JSON recovery report that names the recording each byte came from.

The project's contribution is **reconstruction across captures with byte provenance**. A CRC match is evidence, not proof of the original bytes, and the report and UI label reconstructed blocks that way.

MVP scope: PCM WAV at 44,100 Hz, 16-bit, mono; one unprotected program in the standard BBC Micro 1200-baud cassette format. Other tape formats, resampling, copy-protected loaders, and recovery of headers or missing bytes are out of scope.

**Fixtures are generated.** The STARMAP recordings in `fixtures/starmap/` were produced by the project's own Python encoder (`taperescue/fixtures/`) from the project-authored BBC BASIC program `fixtures/starmap/STARMAP.bas`. They are not real cassette captures. Results on real archival tapes will be claimed only after testing with permission-cleared captures, which has not been done yet (pending item P3).

## Prerequisites

Versions used to check the commands below (Ubuntu 24.04.5 LTS):

| Tool | Version used | Needed for |
| --- | --- | --- |
| Python | 3.12.3 (with the `venv` module) | CLI, engine, server, tests (`requires-python >= 3.12`) |
| g++ | 13.3.0 | Decoder build |
| CMake | 3.28.3 | Decoder build |
| GNU Make | 4.3 | Decoder build |
| git | 2.43.0 | Decoder build (`git apply` of the patch, revision check) |
| zlib development headers | `zlib1g-dev` 1:1.3.dfsg-3.1ubuntu2.3 | Decoder build |
| Node.js / npm | 22.19.0 / 10.9.3 | Web UI (`web/package.json` requires Node `^22.12.0 \|\| >=24.0.0`, for Vite 8) |
| Docker | 29.1.3 | Container image (optional) |

The decoder source (AtomTapeUtils at revision `98d1a8d1bed00aa22f1d72e3d5e91a254a69ec64`) is vendored in `third_party/AtomTapeUtils/`, so the decoder build needs no network access. `pip install` and `npm ci` download packages.

## Local commands

Run everything from the repository root. Each command below was run from a fresh copy of the working tree with an empty environment (`env -i`, `PATH` set to the system directories plus Node's `bin` directory). The output shown is abridged.

### 1. Python environment

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,server]'
```

The `dev` extra installs pytest, Hypothesis and httpx (used by the server tests); the `server` extra installs FastAPI, uvicorn and python-multipart.

### 2. Build the decoder

```sh
scripts/build_decoder.sh
```

```
build_decoder: verified vendored source is revision 98d1a8d1bed00aa22f1d72e3d5e91a254a69ec64 (tree faf78c9b7ed3cf4a1cd2c2aaabc68716e8ec9f86)
build_decoder: applied …/patches/atomtapeutils-json-observations.patch
build_decoder: configuring with cmake version 3.28.3
build_decoder: built …/build/decoder/ScanTape
```

The script copies the vendored source to `build/decoder-src/`, applies `patches/atomtapeutils-json-observations.patch` (which adds the JSON block-observation output), and builds `build/decoder/ScanTape`. The vendored tree itself is not modified. `scripts/build_decoder.sh --help` lists the options and exit codes.

### 3. Generate the fixtures

```sh
.venv/bin/taperescue fixtures
```

```
wrote STARMAP fixtures to fixtures/starmap
program: 2025 bytes in 8 blocks
damage plan: planned
77fef8d5333430eb2afe524158a53f40a8d4e7c7897cf1c2337e6066f6e7d117  clean.wav
2e09d3ce8d3bd4b042958c15dfb9d2d5a08774cf28ecbcff49e3bf06339d4c0b  damaged_a.wav
83a0ab0428d8299299f810f8ceb1df25c5781b7f8b8e29b34cb34794c735d0a0  damaged_b.wav
```

By default this overwrites the tracked files in `fixtures/starmap/` (`clean.wav`, `damaged_a.wav`, `damaged_b.wav`, `damage.json`, `reference_payloads.json`, `STARMAP.bas`). The encoder is deterministic: regenerating them, in place and with `--out /tmp/fx`, gave files byte-identical to the tracked ones (same SHA-256 for all six), so the working tree does not change. To leave the tracked files alone, pass `--out DIR`, for example `.venv/bin/taperescue fixtures --out /tmp/fx`.

`damage.json` records the damaged block 6 offsets and byte values. `reference_payloads.json` holds the original payload and SHA-256 of every block.

### 4. Run the tests

```sh
.venv/bin/python -m pytest -q -rs
```

```
502 passed, 1 warning in 30.86s
```

The decoder-dependent tests need `build/decoder/ScanTape` from step 2. The warning is Starlette's deprecation notice about `httpx` in its test client. The property tests (Hypothesis) are in `tests/properties/` and run with fixed seeds.

```sh
cd web
npm ci
npm test
npm run build
```

```
 Test Files  5 passed (5)
      Tests  27 passed (27)
…
dist/index.html                   0.39 kB │ gzip:  0.27 kB
dist/assets/index-DRWZK5A5.css    3.15 kB │ gzip:  1.07 kB
dist/assets/index-kEOgb0gC.js   237.44 kB │ gzip: 73.99 kB
✓ built in 218ms
```

### 5. End-to-end recovery with one command

```sh
.venv/bin/taperescue demo
```

```
== 1. each capture alone
damaged_a.wav: block 6 CRC failed  (single_read); 7 of 8 blocks Intact; export refused (block 6: single_read)
damaged_b.wav: block 6 CRC failed  (single_read); 7 of 8 blocks Intact; export refused (block 6: single_read)
== 2. both captures, intact-block selection only
block 6: CRC failed (not recovered); export refused (block 6: crc_failed)
== 3. both captures, full recovery
anchors: 0, 1, 2, 3, 4, 5, 7
…
block   6  Reconstructed · CRC match
           candidates 16, evaluated 16, matches 1
           offset  17: damaged_a.wav=0x65 damaged_b.wav=0x61 -> chosen 0x61 from damaged_b.wav
           offset  64: damaged_a.wav=0xE0 damaged_b.wav=0xE1 -> chosen 0xE0 from damaged_a.wav
           offset 143: damaged_a.wav=0x14 damaged_b.wav=0x34 -> chosen 0x34 from damaged_b.wav
           offset 201: damaged_a.wav=0x69 damaged_b.wav=0x29 -> chosen 0x69 from damaged_a.wav
           note: CRC match is evidence, not proof of the original bytes
block   7  Intact
recovered payload sha256: 4ac213a0e014229eb195bd7380962b9f4528147838ec9f1f7cb00be165e06e3a
export: permitted
…
== 4. verify demo-out/STARMAP.uef with the independent UEF reader against reference_payloads.json
block   6  256 bytes  header_crc 0x2349  data_crc 0x8A59  sha256 9e1c72f9…5b7f  ok
…
verified: 8 blocks match the reference
demo complete: block 6 reconstructed from damaged_a.wav, damaged_b.wav; image demo-out/STARMAP.uef, report demo-out/report.json
```

The demo writes `demo-out/STARMAP.uef` and `demo-out/report.json` (`demo-out/` is git-ignored). The recovered image has SHA-256 `e7041f167d9e33e192604e0eeca7f4400d4af3a98932bea43eb9d6c3de1a2045`, the same as the image exported from `clean.wav` alone.

The individual steps are also available (`.venv/bin/taperescue --help` lists all commands). The CLI creates missing output directories, such as `out/` below:

```sh
.venv/bin/taperescue decode fixtures/starmap/damaged_a.wav -o out/damaged_a.json
.venv/bin/taperescue recover fixtures/starmap/damaged_a.wav fixtures/starmap/damaged_b.wav --report out/report.json --uef out/STARMAP.uef
.venv/bin/taperescue verify-uef out/STARMAP.uef --reference fixtures/starmap/reference_payloads.json
```

`recover` with one capture writes the report but refuses the tape image and exits with status 4 (`error: export refused: block 6: single_read` for `damaged_a.wav`).

### 6. Benchmark

```sh
.venv/bin/taperescue benchmark
```

```
Inputs: generated STARMAP fixtures
Best single capture          damaged_a.wav                  recovered 7 of 8, complete program: no
Intact-block merge           damaged_a.wav, damaged_b.wav   recovered 7 of 8, complete program: no
TapeRescue reconstruction    damaged_a.wav, damaged_b.wav   recovered 8 of 8, complete program: yes, matches reference: yes
Quadbike lsblocks.php +r: not run (php is not on PATH; QUADBIKE_LSBLOCKS is not set; QUADBIKE_BIN is not set. …)
wrote benchmarks/results.json
wrote benchmarks/results.md
```

This rewrites the tracked `benchmarks/results.json` and `benchmarks/results.md`; the rerun produced byte-identical files. Use `--out DIR` to write elsewhere.

On the generated fixtures, the best single capture and the intact-block merge each recover 7 of 8 blocks, and TapeRescue recovers 8 of 8 with a payload equal to the reference. The Quadbike `lsblocks.php +r` comparison has not run: PHP and the libsndfile headers are not installed on the development machine, and `lsblocks.php` reads CSW or TIBET files, not WAV, so a Quadbike build is also needed to transcribe the fixtures first. No Quadbike result is claimed. The setup to run it is in pending item P4.

### 7. Web app (local)

Build the UI (step 4, `npm run build` in `web/`) and the decoder (step 2), then from the repository root:

```sh
.venv/bin/uvicorn server.app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/>. No environment variables are needed when running from the repository root. The server reads `TAPERESCUE_DECODER` (default `build/decoder/ScanTape`), `TAPERESCUE_FIXTURES` (default `fixtures/starmap`) and `TAPERESCUE_STATIC` (default `web/dist`, served only if it contains `index.html`).

### 8. Container

```sh
docker build -t taperescue .
docker run --rm -p 8000:8000 taperescue
```

Open <http://127.0.0.1:8000/>. The image builds the patched decoder and the UI itself, runs `uvicorn server.app:app` as user `10001`, and bundles only the three generated STARMAP WAVs. It contains no BBC Micro ROMs and no emulator. Checked with `curl`: `GET /` returned 200, `POST /api/sample` returned 200 with block 6 `Reconstructed · CRC match`, and the downloaded tape image passed `taperescue verify-uef` against the reference.

## Judge walkthrough

This follows the demo sequence on the bundled sample. It uses the web app from step 7 or 8.

The web UI has been checked by component tests (`web/src/*.test.ts(x)`) and by `curl` against the API. No one has clicked through it in a real browser yet, so the steps below describe what the code and tests show, not an observed session.

1. **Block 6 fails in both recordings.** Click **Try sample**. The server runs the generated recordings `damaged_a.wav` and `damaged_b.wav` through recovery. The page shows one strip per recording and a merged result strip, one cell per block, each with a text label and a colour. In both recording strips, block 6 is labelled **CRC failed** and blocks 0–5 and 7 are **Intact**. Each recording was damaged by flipping one bit in each of two bytes of block 6 before modulation (offsets 17 and 143 in `damaged_a.wav`, 64 and 201 in `damaged_b.wav`; see `fixtures/starmap/damage.json`). Neither recording can load the program on its own.
2. **Byte disagreements and the recorded CRC.** Click block 6. The block inspector shows each recording's read of the block (header CRC valid, data CRC invalid, 256 of 256 bytes), the recorded data CRC `0x8A59`, which both recordings carry, and the four offsets where the reads disagree:

   | Offset | `damaged_a.wav` | `damaged_b.wav` | Chosen | Supplied by |
   | --- | --- | --- | --- | --- |
   | 17 | `0x65` | `0x61` | `0x61` | `damaged_b.wav` |
   | 64 | `0xE0` | `0xE1` | `0xE0` | `damaged_a.wav` |
   | 143 | `0x14` | `0x34` | `0x34` | `damaged_b.wav` |
   | 201 | `0x69` | `0x29` | `0x69` | `damaged_a.wav` |

3. **Reconstruction with provenance.** In the merged strip, block 6 is **Reconstructed** and the other blocks are **Intact**. The four disagreeing offsets give 2⁴ = 16 candidate payloads. All 16 were checked against `0x8A59` and exactly one matched, so the block is labelled `Reconstructed · CRC match` with the note "CRC match is evidence, not proof of the original bytes". Each chosen byte is the value from the recording that was not damaged at that offset. The recovered program's SHA-256 is `4ac213a0e014229eb195bd7380962b9f4528147838ec9f1f7cb00be165e06e3a`, equal to the reference in `fixtures/starmap/reference_payloads.json`.
4. **Download the report and the tape image.** **Download report** saves the JSON report (inputs and their SHA-256, per-block status, sources, sample offsets, recorded CRCs, candidate counts and per-offset choices). **Download tape image** first opens a dialog listing block 6 as reconstructed; **Continue download** saves `STARMAP.uef`. When export is not permitted (for example with one recording), the button is disabled and the blocking blocks and reasons are shown, while the report can still be downloaded.
5. **Run it in JSBeeb.** Open <https://bbc.xania.org/>, load `STARMAP.uef` with the emulator's local tape picker (the file stays on your machine), type `*TAPE`, press Return, then type `CHAIN "STARMAP"` and press Return. STARMAP needs the bytes of block 6 to run to completion. **This has not been verified yet**: no one has loaded the clean or the recovered image into JSBeeb (pending items P1 and P2). The recovered image is byte-identical to the image exported from the clean recording, and both pass the independent UEF reader check, but whether the star map draws has not been observed.

Without a browser, `.venv/bin/taperescue demo` (step 5) prints the same sequence.

## Status and pending items

The automated checks above pass. The following external or manual steps have not been performed, and no result is claimed for any of them. Each is tracked with its steps and an empty evidence field in [`verification/pending.md`](verification/pending.md).

| Item | Step | Status |
| --- | --- | --- |
| P1 | Run the `.uef` exported from the clean fixture in JSBeeb | Pending |
| P2 | Run the `.uef` recovered from the two damaged fixtures in JSBeeb | Pending |
| P3 | Test with permission-cleared real archival captures | Pending |
| P4 | Quadbike `lsblocks.php +r` comparison (not runnable locally; reason above) | Pending |
| P5 | Deploy the container to a functional public URL (it has only been run locally) | Pending |
| P6 | Publish the YouTube demo | Pending |

## Project files

- [`VERIFICATION_LOG.md`](VERIFICATION_LOG.md): commands run, results and blockers for every task, generated from `verification/entries/` by `scripts/collect_verification.py`.
- [`.kiro/specs/tape-rescue/`](.kiro/specs/tape-rescue/): the Kiro Feature Spec (requirements, design, tasks).
- `.kiro/hooks/recovery-tests.json`: Kiro hook that reruns the recovery tests when a file under `taperescue/decoder_adapter/` or `taperescue/engine/` is saved.
- [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md): AtomTapeUtils (MIT, revision `98d1a8d1bed00aa22f1d72e3d5e91a254a69ec64`), gzstream, and the Python and npm dependencies with their licenses.
- [`TapeRescue-Kiro-Brief.md`](TapeRescue-Kiro-Brief.md): the original development brief.

## License

TapeRescue is released under the MIT License; see [`LICENSE`](LICENSE). The vendored AtomTapeUtils source in `third_party/AtomTapeUtils/` keeps its upstream MIT license file unchanged. It includes gzstream, which is LGPL-2.1-or-later and is statically linked into `ScanTape`; [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) describes how its terms are met.
