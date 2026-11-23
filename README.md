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
504 passed, 1 warning in 39.42s
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
QUADBIKE_BIN=build/quadbike/src205/quadbike-2.0.5-rc7/src/quadbike \
QUADBIKE_LSBLOCKS=build/quadbike/lsblocks.php \
.venv/bin/taperescue benchmark
```

```
Inputs: generated STARMAP fixtures
Best single capture          damaged_a.wav                  recovered 7 of 8, complete program: no
Intact-block merge           damaged_a.wav, damaged_b.wav   recovered 7 of 8, complete program: no
TapeRescue reconstruction    damaged_a.wav, damaged_b.wav   recovered 8 of 8, complete program: yes, matches reference: yes
Quadbike lsblocks.php +r     damaged_a.wav: 8 block rows, 7 without errors, 7 recovered after bit-flip repair
Quadbike lsblocks.php +r     damaged_b.wav: 8 block rows, 7 without errors, 7 recovered after bit-flip repair
wrote benchmarks/results.json
wrote benchmarks/results.md
```

This rewrites the tracked `benchmarks/results.json` and `benchmarks/results.md`; the rerun produced byte-identical files. Use `--out DIR` to write elsewhere.

On the generated fixtures, the best single capture and the intact-block merge each recover 7 of 8 blocks, and TapeRescue recovers 8 of 8 with a payload equal to the reference. Quadbike 2.0.5 with `lsblocks.php` v1.2 `+r` recovers 7 of 8 blocks from each damaged capture. Block 6 fails its data CRC in both, and the single-bit-flip repair fails because each capture has two bit errors in that block. Quadbike works on one capture at a time, so it cannot combine the two.

The Quadbike rows depend on a local Quadbike build, which TapeRescue does not ship. `lsblocks.php` reads CSW or TIBET files, not WAV, so the benchmark first transcribes each WAV with the Quadbike executable. To build it, install `php-cli` and `libsndfile1-dev`, download `quadbike-2.0.5-rc7.zip` and `lsblocks-1.2.php.txt` from <https://beebstuff.uk/f/acorn/quadbike/> into `build/quadbike/` (saving the script as `lsblocks.php`), unzip the release into `build/quadbike/src205/`, and run `./build.sh linux gcc release` in its `src/` directory. Without `QUADBIKE_BIN` and `QUADBIKE_LSBLOCKS`, the benchmark records `"ran": false` with the reason, and a run with the default `--out` replaces the tracked Quadbike rows with that reason. The commands and raw output are in `verification/entries/26.2.md`.

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

### 9. Deploy to AWS

The public instance runs the same container on one Amazon Lightsail container service, which serves both the UI and the API over HTTPS:

<https://taperescue.9twzsb9wks5a8.us-east-2.cs.amazonlightsail.com/>

```sh
scripts/deploy_aws_lightsail.sh --build
```

The script uses the AWS profile `personal` only, in `us-east-2`. It creates the service `taperescue` (Micro power: 0.25 vCPU, 1 GB RAM, one node, USD 10 per month) if it does not exist, pushes the local image, deploys it with a health check on `/`, and waits until the deployment is active. It needs the AWS CLI v2, Docker and the [`lightsailctl`](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-install-software.html) plugin. The service stays at one node because sessions and their temporary files live inside the container.

To delete the service and stop its charges:

```sh
aws lightsail delete-container-service --service-name taperescue --profile personal --region us-east-2
```

## Judge walkthrough

This follows the demo sequence on the bundled sample. It uses the web app from step 7 or 8, or the public instance from step 9.

The web UI has been checked by component tests (`web/src/*.test.ts(x)`) and by `curl` against the API. On 2026-11-23 the user reported running part of this sequence in a real browser: they opened the web UI, clicked **Try sample**, selected block 6, and clicked **Download tape image**, which saved `~/Downloads/STARMAP.uef`. The user later posted two screenshots of the web UI after **Try sample** with block 6 selected, saved as [`verification/evidence/webui-sample-block6-strips.png`](verification/evidence/webui-sample-block6-strips.png) (SHA-256 `c65ad8f5…9a19`, taken 12:10:33 +0530) and [`verification/evidence/webui-sample-block6-inspector.png`](verification/evidence/webui-sample-block6-inspector.png) (SHA-256 `7f829f57…28a7`, taken 12:10:45 +0530). They were taken about 1.5 hours after `STARMAP.uef` was saved (10:34:26 +0530), so they may come from a later **Try sample** session. Every value they show matches the sample report (`verification/entries/26.6.md`). They confirm in a real browser the strips in step 1, the inspector in steps 2 and 3, and the **Download report** and enabled **Download tape image** buttons and JSBeeb instructions in steps 4 and 5. They do not show the download dialog or a download. That part of step 4, and the disabled-button case, come from the code and tests only. Step 5 has been run by the user in JSBeeb (see below).

1. **Block 6 fails in both recordings.** Click **Try sample**. The server runs the generated recordings `damaged_a.wav` and `damaged_b.wav` through recovery. The page shows one strip per recording and a merged result strip, one cell per block, each with a text label and a colour. In both recording strips, block 6 is labelled **CRC failed** and blocks 0–5 and 7 are **Intact**. Each recording was damaged by flipping one bit in each of two bytes of block 6 before modulation (offsets 17 and 143 in `damaged_a.wav`, 64 and 201 in `damaged_b.wav`; see `fixtures/starmap/damage.json`). Neither recording can load the program on its own. The strips screenshot shows this: the strips are headed `damaged_a.wav 2e09d3ce` and `damaged_b.wav 83a0ab04` (the first 8 hex digits of each recording's SHA-256), with block 6 a red **CRC failed** cell and the other cells green **Intact**.
2. **Byte disagreements and the recorded CRC.** Click block 6. The block inspector shows each recording's read of the block (header CRC valid, data CRC invalid, 256 of 256 bytes), the recorded data CRC `0x8A59`, which both recordings carry, and the four offsets where the reads disagree:

   | Offset | `damaged_a.wav` | `damaged_b.wav` | Chosen | Supplied by |
   | --- | --- | --- | --- | --- |
   | 17 | `0x65` | `0x61` | `0x61` | `damaged_b.wav` |
   | 64 | `0xE0` | `0xE1` | `0xE0` | `damaged_a.wav` |
   | 143 | `0x14` | `0x34` | `0x34` | `damaged_b.wav` |
   | 201 | `0x69` | `0x29` | `0x69` | `damaged_a.wav` |

   Both screenshots show the reads table (sample offset 1116172, header CRC valid, data CRC invalid, recorded data CRC `0x8A59`, expected and actual length 256, payload complete, for each recording). The inspector screenshot shows this offsets table with the same values, each byte written in hex and decimal, for example `0x65 (101)`.
3. **Reconstruction with provenance.** In the merged strip, block 6 is **Reconstructed** and the other blocks are **Intact**. The four disagreeing offsets give 2⁴ = 16 candidate payloads. All 16 were checked against `0x8A59` and exactly one matched, so the block is labelled `Reconstructed · CRC match` with the note "CRC match is evidence, not proof of the original bytes". Each chosen byte is the value from the recording that was not damaged at that offset. The recovered program's SHA-256 is `4ac213a0e014229eb195bd7380962b9f4528147838ec9f1f7cb00be165e06e3a`, equal to the reference in `fixtures/starmap/reference_payloads.json`. The strips screenshot shows the merged strip with block 6 as a blue **Reconstructed** cell and the other cells **Intact**. Both screenshots show the inspector with the status `Reconstructed · CRC match`, the decision "CRC match is evidence, not proof of the original bytes", supplied by `damaged_a.wav, damaged_b.wav`, and Candidates 16, Evaluated 16, CRC matches 1.
4. **Download the report and the tape image.** **Download report** saves the JSON report (inputs and their SHA-256, per-block status, sources, sample offsets, recorded CRCs, candidate counts and per-offset choices). **Download tape image** first opens a dialog listing block 6 as reconstructed; **Continue download** saves `STARMAP.uef`. When export is not permitted (for example with one recording), the button is disabled and the blocking blocks and reasons are shown, while the report can still be downloaded. The inspector screenshot shows both buttons, with **Download tape image** enabled, and below them the five JSBeeb steps. It does not show the dialog, a download, the disabled state or a blocker list; those are covered by `web/src/downloads.test.tsx`.
5. **Run it in JSBeeb.** Open <https://bbc.xania.org/>, load `STARMAP.uef` with the emulator's local tape picker (the file stays on your machine), type `*TAPE`, press Return, then type `CHAIN "STARMAP"` and press Return. STARMAP needs the bytes of block 6 to run to completion. On 2026-11-23 the user ran this step and posted a screenshot of JSBeeb (items P1 and P2), saved as [`verification/evidence/jsbeeb-starmap.png`](verification/evidence/jsbeeb-starmap.png) (SHA-256 `0071439858…dee9`). It shows the finished star map: the title, "240 stars plotted", the four labelled constellations drawn by `PROCconstellations` (which is in block 6), the moon, the hills and the compass letters, and the BASIC `>` prompt with no error message. The screenshot does not show which `.uef` file was loaded or the tape loading messages. The user has since stated "I loaded STARMAP.uef file only". The file `~/Downloads/STARMAP.uef` on the user's machine (2420 bytes, modified 2026-11-23 10:34:26 +0530) has SHA-256 `e7041f167d9e33e192604e0eeca7f4400d4af3a98932bea43eb9d6c3de1a2045`, the recovered image's digest, and `taperescue verify-uef` matches all 8 blocks to the reference. That image is byte-identical to the image exported from the clean recording, so the digest cannot tell the two apart. That JSBeeb loaded this exact copy rests on the user's statement. The user reports that the file came from **Try sample**, selecting block 6, and **Download tape image** in the web UI. Because the sample has a reconstructed block, the UI fetches the tape only after **Continue download** in the confirmation dialog (`web/src/DownloadBar.tsx` lines 80–89 and 199), so the dialog was presumably confirmed; the user did not say so. The screenshot shows that the loaded program ran to completion. The CRC match itself is still evidence, not proof of the original bytes. The cross-check against `STARMAP.bas` is in `verification/entries/26.1.md`.

Without a browser, `.venv/bin/taperescue demo` (step 5) prints the same sequence.

## Status and pending items

The automated checks above pass. The external and manual steps are tracked with their steps and evidence fields in [`verification/pending.md`](verification/pending.md). P1 and P2 have user-supplied evidence, and P4 and P5 were run in this project. P3 and P6 have not been performed, and no result is claimed for them.

| Item | Step | Status |
| --- | --- | --- |
| P1 | Run the `.uef` exported from the clean fixture in JSBeeb | Done, covered by the P2 run: the clean and recovered images are byte-identical |
| P2 | Run the `.uef` recovered from the two damaged fixtures in JSBeeb | Done (manual, user-run): screenshot of the finished star map with no error. The user states the file loaded was `STARMAP.uef`; the copy in `~/Downloads` has the recovered image's SHA-256 `e7041f16…2045`. The user reports getting it in the web UI via **Try sample**, block 6, and **Download tape image**. Screenshots: `verification/evidence/jsbeeb-starmap.png`; web UI strips and block 6 inspector after **Try sample**: `verification/evidence/webui-sample-block6-strips.png`, `verification/evidence/webui-sample-block6-inspector.png` |
| P3 | Test with permission-cleared real archival captures | Pending |
| P4 | Quadbike `lsblocks.php +r` comparison | Done: Quadbike 2.0.5 and lsblocks v1.2 built and run locally; 7 of 8 blocks per damaged capture, block 6 not repaired |
| P5 | Deploy the container to a functional public URL | Done: <https://taperescue.9twzsb9wks5a8.us-east-2.cs.amazonlightsail.com/> on Amazon Lightsail. Checked with `curl` on 2026-11-23: **Try sample**, the tape image (`e7041f16…2045`, passes `verify-uef`), the report, and an upload of both damaged WAVs. No browser screenshot of the public URL yet |
| P6 | Publish the YouTube demo | Pending |

## Project files

- [`VERIFICATION_LOG.md`](VERIFICATION_LOG.md): commands run, results and blockers for every task, generated from `verification/entries/` by `scripts/collect_verification.py`.
- [`.kiro/specs/tape-rescue/`](.kiro/specs/tape-rescue/): the Kiro Feature Spec (requirements, design, tasks).
- `.kiro/hooks/recovery-tests.json`: Kiro hook that reruns the recovery tests when a file under `taperescue/decoder_adapter/` or `taperescue/engine/` is saved.
- [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md): AtomTapeUtils (MIT, revision `98d1a8d1bed00aa22f1d72e3d5e91a254a69ec64`), gzstream, and the Python and npm dependencies with their licenses.
- [`TapeRescue-Kiro-Brief.md`](TapeRescue-Kiro-Brief.md): the original development brief.

## License

TapeRescue is released under the MIT License; see [`LICENSE`](LICENSE). The vendored AtomTapeUtils source in `third_party/AtomTapeUtils/` keeps its upstream MIT license file unchanged. It includes gzstream, which is LGPL-2.1-or-later and is statically linked into `ScanTape`; [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) describes how its terms are met.
