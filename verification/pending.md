# Pending Items

Manual or external steps that have not been performed. No result is claimed for any item until its evidence field is filled in with a link, file path, or log excerpt and its status is changed. Commands are run from the repository root with the virtual environment's `taperescue` (`.venv/bin/taperescue`) and the decoder built by `scripts/build_decoder.sh` at `build/decoder/ScanTape`.

## P1: Run the clean `.uef` in JSBeeb

- Status: pending
- Requirement: 22.2
- Step:
  1. Produce the image: `taperescue recover fixtures/starmap/clean.wav --uef clean.uef`. Optionally check it with `taperescue verify-uef clean.uef --reference fixtures/starmap/reference_payloads.json`.
  2. Open JSBeeb at <https://bbc.xania.org/> (the link the web UI shows), and load `clean.uef` with the emulator's local tape picker. The file stays on the local machine.
  3. At the BASIC prompt type `*TAPE`, then `CHAIN "STARMAP"`, and confirm that the program loads and the star map draws to completion.
  4. Record the JSBeeb URL used, the browser, the SHA-256 of `clean.uef`, and a screenshot or screen recording of the finished star map.
- Evidence:

## P2: Run the recovered `.uef` in JSBeeb

- Status: pending
- Requirement: 22.2
- Step:
  1. Produce the image from the two damaged fixtures: `taperescue demo`. It writes `demo-out/STARMAP.uef` and `demo-out/report.json` (`demo-out/` is git-ignored). The web app's **Try sample** then **Download tape image** gives the same image.
  2. Load `demo-out/STARMAP.uef` in JSBeeb at <https://bbc.xania.org/> with the local tape picker.
  3. Type `*TAPE`, then `CHAIN "STARMAP"`, and confirm that the star map draws to completion. STARMAP needs the bytes of block 6, the reconstructed block, to run to completion (Requirement 4.6).
  4. Record the JSBeeb URL used, the browser, the SHA-256 of `STARMAP.uef`, and a screenshot or screen recording of the finished star map.
- Evidence:

## P3: Test with real archival captures

- Status: pending
- Requirement: 22.2
- Step:
  1. Obtain one or two WAV captures of the same real BBC Micro cassette whose owner has given permission to process and report on them. Convert them to 44,100 Hz, 16-bit, mono PCM outside TapeRescue if needed (TapeRescue does not resample).
  2. Run `taperescue recover <capture_a.wav> [<capture_b.wav>] --report real-report.json --uef real.uef`.
  3. Record the source and permission of each capture, the capture SHA-256 values, the per-block statuses and reasons from the report, and whether export was permitted. Do not commit the captures unless the permission covers redistribution.
- Evidence:

## P4: Quadbike comparison (if not runnable locally)

- Status: pending
- Requirement: 15.3, 22.2
- Step: Run Quadbike `lsblocks.php +r` on Damaged_Fixture_A and Damaged_Fixture_B and record the recovered block count. If it cannot be run, state why.
- Why it has not run (task 17.1): `taperescue benchmark` recorded `quadbike: {"ran": false, "reason": "php is not on PATH; QUADBIKE_LSBLOCKS is not set; QUADBIKE_BIN is not set. ..."}` in `benchmarks/results.json`. On the development machine (Ubuntu 24.04) PHP is not installed (`command -v php` finds nothing; `apt-cache policy php-cli` shows `Installed: (none)`), and installing it needs `sudo`, which asks for a password, so it is a manual step. `lsblocks.php` also reads only CSW or TIBET files, not WAV (its usage line is `php -f lsblocks.php [options] <CSW/TIBET/TIBETZ file>`), so each fixture must first be transcribed by the Quadbike executable (`quadbike -t capture.tibet capture.wav`). Quadbike has no Linux binary; it must be built from source with `src/build.sh`, which needs the libsndfile development headers (`/usr/include/sndfile.h` is absent here), again a `sudo` install. Quadbike and its PHP scripts are GPL-2.0-or-later and are downloadable from <https://beebstuff.uk/f/acorn/quadbike/> (releases 2.0.1 to 2.0.5, scripts including `lsblocks-1.2.php.txt`); the site states it will go offline on or before 22 February 2027.
- To run it: install `php-cli` and `libsndfile1-dev`, build Quadbike from a release, then run `QUADBIKE_LSBLOCKS=/path/to/lsblocks.php QUADBIKE_BIN=/path/to/quadbike .venv/bin/taperescue benchmark`. The benchmark transcribes each damaged WAV to TIBET, runs `php -f lsblocks.php +r capture.tibet` with an argument list, and records the block rows and the `M: Metrics|Errors|E|Blocks|N` line from the actual output.
- Evidence:

## P5: Deployment to a functional URL

- Status: pending
- Requirement: 22.2
- Current state: the container image builds and runs locally (task 21.1: `docker build -t taperescue:local .`, then `docker run --rm -p 8000:8000 taperescue:local`, UI at `http://127.0.0.1:8000/`). It has not been pushed to any registry or deployed to any host.
- Step:
  1. Push the image to a registry and deploy it to a host that serves it over HTTPS.
  2. Open the public URL, click **Try sample**, and confirm that block 6 shows `Reconstructed · CRC match` and **Download tape image** returns `STARMAP.uef`.
  3. Record the public URL, the image digest deployed, the date, and a screenshot of the sample result.
- Evidence:

## P6: YouTube demo

- Status: pending
- Requirement: 22.2
- Step:
  1. Record the demo following the README judge walkthrough: block 6 failing in both recordings, the byte disagreements and the recorded CRC, the reconstruction with provenance, and the `.uef` download run in JSBeeb (P2).
  2. Publish it on YouTube and record the video URL and its publication date.
- Evidence:
