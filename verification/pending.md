# Pending Items

Manual or external steps that have not been performed. No result is claimed for any item until its evidence field is filled in with a link, file path, or log excerpt and its status is changed. Commands are run from the repository root with the virtual environment's `taperescue` (`.venv/bin/taperescue`) and the decoder built by `scripts/build_decoder.sh` at `build/decoder/ScanTape`.

## P1: Run the clean `.uef` in JSBeeb

- Status: done (covered by the P2 run; identical image bytes)
- Requirement: 22.2
- Step:
  1. Produce the image: `taperescue recover fixtures/starmap/clean.wav --uef clean.uef`. Optionally check it with `taperescue verify-uef clean.uef --reference fixtures/starmap/reference_payloads.json`.
  2. Open JSBeeb at <https://bbc.xania.org/> (the link the web UI shows), and load `clean.uef` with the emulator's local tape picker. The file stays on the local machine.
  3. At the BASIC prompt type `*TAPE`, then `CHAIN "STARMAP"`, and confirm that the program loads and the star map draws to completion.
  4. Record the JSBeeb URL used, the browser, the SHA-256 of `clean.uef`, and a screenshot or screen recording of the finished star map.
- Evidence: No separate run of `clean.uef` was made. The image exported from `clean.wav` and the image recovered from `damaged_a.wav` and `damaged_b.wav` are byte-identical: both have SHA-256 `e7041f167d9e33e192604e0eeca7f4400d4af3a98932bea43eb9d6c3de1a2045`, `cmp` found them identical in task 18 (`cmp /tmp/m2x/clean.uef /tmp/m2x/demo/STARMAP.uef`), and `sha256sum` gave the same digest for both in task 23.4. The P2 run therefore exercised the same bytes. See P2 and `verification/entries/26.1.md`.

## P2: Run the recovered `.uef` in JSBeeb

- Status: done (manual, user-run)
- Requirement: 22.2
- Step:
  1. Produce the image from the two damaged fixtures: `taperescue demo`. It writes `demo-out/STARMAP.uef` and `demo-out/report.json` (`demo-out/` is git-ignored). The web app's **Try sample** then **Download tape image** gives the same image.
  2. Load `demo-out/STARMAP.uef` in JSBeeb at <https://bbc.xania.org/> with the local tape picker.
  3. Type `*TAPE`, then `CHAIN "STARMAP"`, and confirm that the star map draws to completion. STARMAP needs the bytes of block 6, the reconstructed block, to run to completion (Requirement 4.6).
  4. Record the JSBeeb URL used, the browser, the SHA-256 of `STARMAP.uef`, and a screenshot or screen recording of the finished star map.
- Evidence: Screenshot posted by the user in the Kiro chat on 2026-11-23, saved in task 26.5 as [`verification/evidence/jsbeeb-starmap.png`](evidence/jsbeeb-starmap.png) (PNG, 1914 x 865, 225781 bytes, SHA-256 `007143985891615af51f15a4d9575663c53f3126cb04817ba2c62b156e42dee9`). It is an unmodified `cp` of the user's file `~/Pictures/Screenshots/Screenshot from 2026-11-23 10-44-58.png` (same SHA-256), taken at 10:44:58 +0530, about ten minutes after the modification time of `~/Downloads/STARMAP.uef` (10:34:26). The user was asked to start the container, click **Try sample**, click **Download tape image**, load the downloaded `STARMAP.uef` (the recovered image) in JSBeeb at <https://bbc.xania.org/> with the local tape picker, then type `*TAPE` and `CHAIN "STARMAP"`. The user then posted the screenshot with the words "Here is the screenshot".
  - What it shows: the JSBeeb UI (menus BBC B, Media, Reset, State, Debug, More; status bar "drive 0/2 Elite", "drive 1/3 empty", "cassette motor STARMAP"). On the emulated screen: the title "STARMAP" at top centre, "The northern winter sky" in cyan, "240 stars plotted" in blue/purple, the four constellations Plough, Cassiopeia, Cygnus and Orion with labels, cyan connecting lines and white "+" stars, a crescent moon top right, a band of small blue/purple dots across the middle, a short white diagonal line top left, white "+" stars across the sky, hills along the bottom, the compass letters W, N and E along the bottom edge, and the BASIC prompt `>` at the top left. No error message is visible.
  - Cross-check against `fixtures/starmap/STARMAP.bas`: every visible text string and drawn element matches the source line that produces it, and the star count 240 matches `PROCstars(240)` (line 100). The constellation labels and lines are drawn by `PROCconstellations` (lines 3000–3150, program offsets 1538–1771), which lies entirely in block 6, the reconstructed block. If the damaged value from `damaged_a.wav` at block 6 offset 17 or from `damaged_b.wav` at offset 201 had been loaded, BASIC would have stopped with an error before the constellations were complete. No mismatch was found. The table is in `verification/entries/26.1.md`.
  - File loaded (task 26.4, 2026-11-23): the screenshot does not show which `.uef` was loaded, but the user has since stated in the Kiro chat: "I loaded STARMAP.uef file only". On the user's machine, `sha256sum ~/Downloads/STARMAP.uef` gives `e7041f167d9e33e192604e0eeca7f4400d4af3a98932bea43eb9d6c3de1a2045`, and `stat` gives 2420 bytes, modified 2026-11-23 10:34:26 +0530. `taperescue verify-uef ~/Downloads/STARMAP.uef --reference fixtures/starmap/reference_payloads.json` reports `verified: 8 blocks match the reference` (block 6 `data_crc 0x8A59`, payload SHA-256 `4ac213a0…6e3a`). This digest is the recovered image's, which is identical to the clean image's, so the digest alone cannot tell the two apart. The file's name and location in `~/Downloads` are consistent with the web app's **Download tape image**, which names the file `STARMAP.uef`. That JSBeeb loaded this exact copy rests on the user's statement. The file was not copied into the repository. Details are in `verification/entries/26.4.md`.
  - Web UI steps (task 26.5, 2026-11-23, user-reported): the user stated in the Kiro chat: "I have opened the web UI and clicked on try sample. I selected the block 6 and after downloading the tape image only I got that starmap.uef file and that's how I got it and thats one that you have seen it in downloads folder." Recorded as reported by the user, in a real browser: the user opened the web UI, clicked **Try sample**, selected block 6, and clicked **Download tape image**, and the resulting file is `~/Downloads/STARMAP.uef` (SHA-256 `e7041f16…2045`, checked in task 26.4). This statement replaces the earlier reading of "only" in task 26.4. Screenshots of the TapeRescue UI were supplied afterwards (task 26.6, below); no recording was supplied.
  - Inference, not stated by the user: the reconstructed-block confirmation dialog was shown and **Continue download** was clicked. In `web/src/DownloadBar.tsx`, `onTapeClick` (lines 80–83) opens the dialog instead of downloading whenever `reconstructed_blocks` is non-empty, and the tape is fetched only from `closeDialog(true)` (lines 85–89), which only the **Continue download** button calls (lines 199–200). Escape and **Cancel** call `onClose(false)` (lines 171, 176, 202). `tapeUrl` is used only by `fetchTapeFile` (`web/src/api.ts` line 374). The `POST /api/sample` envelope has `"reconstructed_blocks": [6]` (`tests/server/test_app.py` line 164), so a tape download from the UI implies the dialog was confirmed.
  - Web UI screenshots (task 26.6, 2026-11-23): after the statement above, the user posted two screenshots of the web UI without comment. Saved unmodified with `cp` as [`verification/evidence/webui-sample-block6-strips.png`](evidence/webui-sample-block6-strips.png) (SHA-256 `c65ad8f59fb8813563f36a3398cab63ac580bbba3fd0948fef3e59d4ccf69a19`, from `~/Pictures/Screenshots/Screenshot from 2026-11-23 12-10-33.png`) and [`verification/evidence/webui-sample-block6-inspector.png`](evidence/webui-sample-block6-inspector.png) (SHA-256 `7f829f57d38e6eedee1a00dbe3ac9b4aa51e2d1402b20e2ca5588365747528a7`, from `Screenshot from 2026-11-23 12-10-45.png`). Both are PNG, 1904 x 932, written by gnome-screenshot. They were taken at 12:10:33 and 12:10:45 +0530, about 1.5 hours after `~/Downloads/STARMAP.uef` was modified (10:34:26), so they may come from a later **Try sample** session than the download.
    - Strips screenshot: the TapeRescue heading and intro, the **Choose files**, **Recover** (disabled) and **Try sample** controls, "damaged_a.wav: decoded" and "damaged_b.wav: decoded", the strips `damaged_a.wav 2e09d3ce` and `damaged_b.wav 83a0ab04` with cells 0–5 and 7 green **Intact** and cell 6 red **CRC failed**, the **Merged result** strip with cells 0–5 and 7 green **Intact** and cell 6 blue **Reconstructed**, block 6 outlined as selected in all three strips, and the top of the Block 6 inspector down to the reads table.
    - Inspector screenshot: the bottom of the merged strip, the Block 6 inspector (status `Reconstructed · CRC match`, decision "CRC match is evidence, not proof of the original bytes", recorded data CRC `0x8A59`, supplied by `damaged_a.wav, damaged_b.wav`, Candidates 16, Evaluated 16, CRC matches 1), the reads table, the table of differing offsets 17, 64, 143 and 201 with both recordings' bytes, the chosen byte and the supplying recording, the **Download report** and enabled **Download tape image** buttons, and the five steps under "Run the tape image in JSBeeb".
    - Cross-check: every value shown equals the block 6 entry of the sample report, which is identical between `POST /api/sample` and `taperescue recover` on the two damaged fixtures, and the chosen bytes equal the reference payload in `fixtures/starmap/reference_payloads.json`. The labels and colour families match `web/src/status.ts`. The rendered pixel values of the coloured cells differ from the CSS values (for example green `#00892d` against `#2e7d32`) while the greys match exactly; the cause was not determined. The table is in `verification/entries/26.6.md`.
  - Not shown and not stated: the confirmation dialog and its text, the download itself, whether **Download report** was used, the JSBeeb help panel, the browser name and version, and the tape load progress and block-by-block loading messages. The run also cannot distinguish the value at block 6 offset 64, which is the low byte of the number of line 3040.

## P3: Test with real archival captures

- Status: pending
- Requirement: 22.2
- Step:
  1. Obtain one or two WAV captures of the same real BBC Micro cassette whose owner has given permission to process and report on them. Convert them to 44,100 Hz, 16-bit, mono PCM outside TapeRescue if needed (TapeRescue does not resample).
  2. Run `taperescue recover <capture_a.wav> [<capture_b.wav>] --report real-report.json --uef real.uef`.
  3. Record the source and permission of each capture, the capture SHA-256 values, the per-block statuses and reasons from the report, and whether export was permitted. Do not commit the captures unless the permission covers redistribution.
- Evidence:

## P4: Quadbike comparison (if not runnable locally)

- Status: done (task 26.2, run locally)
- Requirement: 15.3, 22.2
- Step: Run Quadbike `lsblocks.php +r` on Damaged_Fixture_A and Damaged_Fixture_B and record the recovered block count. If it cannot be run, state why.
- History (task 17.1): the comparison first did not run because PHP and the libsndfile headers were not installed and installing them needed `sudo`. The user later installed `php-cli` (PHP 8.3.6) and `libsndfile1-dev` (1.2.2).
- To rerun it: build Quadbike as described in the README benchmark section, then run `QUADBIKE_BIN=build/quadbike/src205/quadbike-2.0.5-rc7/src/quadbike QUADBIKE_LSBLOCKS=build/quadbike/lsblocks.php .venv/bin/taperescue benchmark`. The benchmark transcribes each damaged WAV to TIBET with `quadbike -t capture.tibet <wav>`, then runs `php -f lsblocks.php +r capture.tibet`, both with argument lists.
- Evidence: `benchmarks/results.json` (`quadbike.runs`) and `benchmarks/results.md`, and `verification/entries/26.2.md`, which has the commands and raw output. Quadbike 2.0.5 (`quadbike-2.0.5-rc7.zip`, SHA-256 `1c6931bb05c7445c543fcd0d9eb1b3b0fda808b802af3ba37b5f5433be979920`, matching the published `sha256.txt`) was built with `./build.sh linux gcc release`. `lsblocks.php` v1.2 (`lsblocks-1.2.php.txt`, SHA-256 `954928818162ae961135b3f8d4e51cae3111d5972464c043425b0c00dc18ea3e`, matching the published value) ran under PHP 8.3.6. For each of `damaged_a.wav` and `damaged_b.wav`, lsblocks lists 8 blocks. Blocks 0–5 and 7 have no error code, and block 6 is marked `DC` (data CRC mismatch). The metrics line is `M: Metrics|Errors|1|Blocks|8`. The `+r` repair then prints `Block #6: bad data CRC: read 8a59, computed c6f8.` (damaged_a; `computed 66b5` for damaged_b) followed by `Bitflip failed.`, so Quadbike recovers 7 of 8 blocks from each capture. As a baseline, `clean.wav` gives 8 blocks with no errors and `M: Metrics|Errors|0|Blocks|8`.

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
