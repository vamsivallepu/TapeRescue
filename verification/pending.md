# Pending Items

Manual or external steps that have not been performed. No result is claimed for any item until its evidence field is filled in with a link, file path, or log excerpt and its status is changed.

## P1: Run the clean `.uef` in JSBeeb

- Status: pending
- Requirement: 22.2
- Step: Load the `.uef` exported from the Clean_Fixture into JSBeeb with the local tape picker, type `*TAPE`, then `CHAIN "STARMAP"`, and confirm the star map draws to completion.
- Evidence:

## P2: Run the recovered `.uef` in JSBeeb

- Status: pending
- Requirement: 22.2
- Step: Load the `.uef` recovered from Damaged_Fixture_A and Damaged_Fixture_B into JSBeeb with the local tape picker, type `*TAPE`, then `CHAIN "STARMAP"`, and confirm the star map draws to completion.
- Evidence:

## P3: Test with real archival captures

- Status: pending
- Requirement: 22.2
- Step: Run TapeRescue on permission-cleared real cassette captures and record the per-block results.
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
- Step: Deploy the TapeRescue container and record the public URL and a successful **Try sample** run.
- Evidence:

## P6: YouTube demo

- Status: pending
- Requirement: 22.2
- Step: Publish the demo video following the README judge walkthrough and record its URL.
- Evidence:
