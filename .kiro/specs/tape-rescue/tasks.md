# Implementation Plan: TapeRescue

## Overview

The plan follows the four milestones in order. Milestone 1 builds the pinned, patched decoder, the CRC and WAV modules, the decoder adapter, the STARMAP fixture encoder, the damaged-fixture observation checks, and intact-block `.uef` export with an independent reader. Milestone 2 adds matching, bounded reconstruction, the report, the acceptance and property tests, end-to-end recovery, and the benchmark. Milestone 3 adds the FastAPI server, the React UI, and the container. Milestone 4 (local parts) adds the Kiro hook, notices, README, and the final pending-item list.

Implementation languages: Python 3.12 (`taperescue` package, server, tests with pytest and Hypothesis), C++ (decoder patch), TypeScript (Vite and React UI, tests with `vitest --run`).

**Verification log rule (applies to every task, including optional ones):** when a task is finished, write `verification/entries/<task-id>.md` with the commands run, the observed results, and any blockers. Each task writes only its own entry file, so tasks in the same wave never edit the same file. Checkpoints run `python scripts/collect_verification.py`, which rebuilds `VERIFICATION_LOG.md` from the entries plus `verification/pending.md`. Manual or external steps (JSBeeb runs, real archival captures, Quadbike when not runnable, deployment, YouTube demo) stay in `verification/pending.md` as Pending_Items with an evidence placeholder. No task may record a result for a step it did not perform.

## Tasks

### Milestone 1

- [x] 1. Set up the repository scaffold and verification log tooling
  - Create `pyproject.toml` for the `taperescue` package (Python 3.12) with a `taperescue` console entry point and dev dependencies `pytest` and `hypothesis`
  - Create empty package modules matching the design layout (`taperescue/`, `decoder_adapter/`, `engine/`, `uef/`, `fixtures/`) and `tests/` subdirectories (`properties/`, `engine/`, `decoder_adapter/`, `integration/`, `smoke/`)
  - Add the MIT `LICENSE` at the repository root
  - Write `scripts/collect_verification.py`, which concatenates `verification/entries/*.md` in task order and appends `verification/pending.md` into `VERIFICATION_LOG.md`
  - Create `verification/pending.md` with one Pending_Item per external step: clean `.uef` in JSBeeb, recovered `.uef` in JSBeeb, real archival captures, Quadbike comparison (if not runnable), deployment URL, YouTube demo. Each item has status `pending` and an empty evidence field
  - _Requirements: 21.1, 22.1, 22.2, 22.3_

- [x] 2. Vendor and build the pinned decoder
  - [x] 2.1 Vendor AtomTapeUtils and write the build script
    - Place the source at revision `98d1a8d1bed00aa22f1d72e3d5e91a254a69ec64` under `third_party/AtomTapeUtils/` with the upstream LICENSE and notices unchanged, and add a `REVISION` file
    - Write `scripts/build_decoder.sh`: fetch the revision only when the tree is absent, verify the revision, copy to `build/decoder-src/`, `git apply --check` then apply the patch, build with the upstream CMake files into `build/decoder/ScanTape`
    - Exit 2 with `failed to fetch AtomTapeUtils revision 98d1a8d1...` on fetch failure, and exit 3 with `patch does not apply to pinned revision` on patch failure
    - If the fetch fails in this environment, record it as a blocker in the task's verification entry and stop the decoder-dependent tasks
    - _Requirements: 1.1, 1.3, 1.4_

  - [x] 2.2 Write the JSON block-observation patch
    - Create `patches/atomtapeutils-json-observations.patch` against the pinned revision
    - Add `-json <path>` and `-capture-id <hex>` options to `ScanTape/ArgParser.cpp`, valid with `-bbm`
    - Add `shared/ObservationWriter.h/.cpp` with a hand-written JSON emitter for schema `taperescue.observations/1`
    - Hook `shared/BlockDecoder.cpp` after the header read and after the data and data CRC reads, before any `FileDecoder` assembly. Record the full payload on data CRC mismatch, the bytes read so far on early end of signal, and `outcome: "unreadable"` with no `recorded` value when CRC bytes cannot be read. Never zero-pad
    - Build with `scripts/build_decoder.sh` and run the binary once on a WAV to confirm the JSON is emitted
    - _Requirements: 1.2, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

  - [ ]* 2.3 Write smoke tests for the vendored decoder
    - `tests/smoke/test_decoder_vendor.py`: revision file matches, upstream LICENSE present and unchanged, `git apply --check` succeeds on a fresh copy, the build succeeds with network access disabled
    - _Requirements: 1.1, 1.2, 1.3_

- [x] 3. Implement the shared data models and CRC
  - [x] 3.1 Implement `taperescue/models.py`
    - Frozen dataclasses and literals from the design: `CrcOutcome`, `BlockStatus`, `UnresolvedReason`, `HeaderFields` with `identity_key()`, `CrcReading`, `BlockObservation` with `payload_state` and `is_intact`, `CaptureObservations`, `BlockResult`, `RecoveryResult` with `program_payload()`, and `RecoverOptions`
    - _Requirements: 2.2, 2.4, 2.6, 2.7_

  - [x] 3.2 Implement `taperescue/crc.py`
    - Table-driven `crc16_xmodem(data, init=0)` with polynomial 0x1021, no reflection, xorout 0
    - _Requirements: 6.1, 6.2_

  - [x] 3.3 Write the CRC vector unit tests
    - `tests/test_crc.py`: ASCII `123456789` gives `0x31C3`, and the Test_Payload `payload[i] = (17*i + 11*i*i + 42) mod 256` (256 bytes) gives `0xAC93`
    - Kept as a required test because the user request names these vectors
    - _Requirements: 6.1, 6.2_

  - [ ]* 3.4 Write property test for CRC
    - **Property 1: CRC matches the reference definition**
    - Compare against a bitwise reference and check the zero residue when the CRC is appended high byte first
    - **Validates: Requirements 6.1, 6.2, 6.3**

- [x] 4. Implement WAV validation
  - [x] 4.1 Implement `taperescue/wavcheck.py`
    - `WavInfo`, `WavRejected` with codes `not_riff_wave`, `unsupported_format`, `too_large`, `too_long`, `no_data`
    - Walk RIFF chunks directly; accept PCM (1) and `WAVE_FORMAT_EXTENSIBLE` with the PCM GUID; check size before parsing and duration from `data_bytes / (sample_rate * block_align)`
    - Rejection messages state the detected format and the supported format, or the limit exceeded
    - _Requirements: 3.1, 3.2, 3.3_

  - [ ]* 4.2 Write property test for WAV acceptance
    - **Property 3: WAV format acceptance**
    - **Validates: Requirements 3.1, 3.2, 3.3**

- [x] 5. Implement the decoder adapter
  - [x] 5.1 Implement `taperescue/decoder_adapter/schema.py`
    - `DecoderError` and `parse_observations(doc, expected_capture_id)` enforcing the schema rules in the design (capture ID, payload length equals `actual_len`, `actual_len <= expected_len`, `expected_len` equals `block_len` for valid headers, `recorded` present only when readable and in `0..0xFFFF`, valid data CRC only with a complete payload)
    - Raise `DecoderError(code="invalid_output")` naming the capture ID
    - _Requirements: 2.2, 2.4, 2.5, 2.6, 2.7, 2.8_

  - [x] 5.2 Implement `taperescue/decoder_adapter/run.py`
    - `capture_id_for(path)` returns the SHA-256 hex of the file bytes
    - `decode_capture` calls `subprocess.run([decoder, "-bbm", "-json", out, "-capture-id", cid, wav], shell=False, timeout=60, capture_output=True)` with `out` generated inside `workdir`
    - Map `TimeoutExpired` to `timeout`, non-zero exit to `decoder_failed`, and zero valid-header observations to `no_blocks` with the message `No BBC 1200-baud blocks were found in this capture`
    - _Requirements: 1.5, 3.4, 3.5_

  - [ ]* 5.3 Write property test for the observation schema
    - **Property 2: Observation schema round trip and rejection**
    - **Validates: Requirements 2.2, 2.4, 2.5, 2.6, 2.7, 2.8**

  - [ ]* 5.4 Write unit tests for the adapter process handling
    - `tests/decoder_adapter/test_run.py`: `subprocess.run` receives a list and `shell=False`; a fake decoder that sleeps triggers `timeout`; a fake decoder that emits no blocks triggers `no_blocks`
    - _Requirements: 1.5, 3.4, 3.5_

- [x] 6. Implement the fixture encoder and generate STARMAP fixtures
  - [x] 6.1 Implement the BASIC tokenizer
    - `taperescue/fixtures/tokenizer.py` with `tokenize` and `detokenize`, the line format `0x0D, hi, lo, len, body`, terminator `0x0D 0xFF`, the keyword token table for STARMAP's keywords, no tokenizing inside strings or after `REM`/`DATA`, and an error on line-number references
    - _Requirements: 4.2_

  - [ ]* 6.2 Write property test for the tokenizer
    - **Property 4: Tokenizer round trip**
    - **Validates: Requirements 4.2**

  - [x] 6.3 Implement the block builder
    - `taperescue/fixtures/block_builder.py` with `TapeBlock`, `ByteFrame`, `build_blocks`, and `block_frames`
    - 256-byte blocks except the last, the design's header layout, flag bit 7 on the last block only, header and data CRCs from `crc16_xmodem`
    - _Requirements: 4.3, 4.4, 4.5_

  - [ ]* 6.4 Write property test for the block builder
    - **Property 5: Block builder structure**
    - **Validates: Requirements 4.3, 4.4, 4.5**

  - [x] 6.5 Implement the FSK modulator
    - `taperescue/fixtures/fsk.py` with carrier, gap, and byte-frame segments; start bit, 8 data bits LSB first, stop bit; integer sample boundaries `floor(k*147/4)`; fixed int16 sine table; hand-written 44.1 kHz 16-bit mono WAV header; lead-in constants in one table
    - Import nothing from `taperescue.decoder_adapter`
    - _Requirements: 4.7, 4.8, 4.9_

  - [x] 6.6 Implement the damage injector
    - `taperescue/fixtures/damage.py` with `BitFlip` and `apply_damage`; change only payload frame values; reject non-payload targets, repeated offsets, and multi-bit flips
    - _Requirements: 5.1, 5.3, 5.4_

  - [ ]* 6.7 Write property test for damage injection
    - **Property 7: Damage injection is confined to chosen payload bits**
    - **Validates: Requirements 5.1, 5.2, 5.3, 5.4**

  - [x] 6.8 Write STARMAP and the fixture generator
    - Write the original `fixtures/starmap/STARMAP.bas` (MODE 1, seeded stars, constellation lines from `DATA`, title) with `DEF PROCconstellations` placed inside payload offsets 1536–1791 and called before `END`
    - Implement `taperescue/fixtures/starmap.py`: tokenize, pad with `REM` lines to exactly 8 blocks or raise, build `clean.wav`, `damaged_a.wav`, `damaged_b.wav` with the planned block 6 flips (A: 17 bit 2, 143 bit 5; B: 64 bit 0, 201 bit 6)
    - Check that A and B offsets are disjoint, that both damaged payloads fail the original CRC, and that exactly one of the 16 combinations matches; on collision pick the next offsets from a fixed seeded list and record the choice
    - Write `reference_payloads.json` and `damage.json` (offsets, bits, original and damaged values)
    - _Requirements: 4.1, 4.3, 4.6, 4.10, 5.2, 5.5_

  - [ ]* 6.9 Write property test for deterministic fixture generation
    - **Property 6: Fixture generation is deterministic**
    - **Validates: Requirements 4.9**

  - [ ]* 6.10 Write unit tests for STARMAP layout and encoder independence
    - `tests/test_starmap.py`: `DEF PROCconstellations` byte range lies in block 6; the `taperescue.fixtures` import graph excludes `taperescue.decoder_adapter` and no module references the decoder binary
    - _Requirements: 4.6, 4.8_

- [x] 7. Add the first CLI commands
  - Implement `taperescue/cli.py` with argparse and the `fixtures` and `decode` subcommands
  - Every failure prints `error: <reason>` to stderr and exits non-zero
  - Run `taperescue fixtures` to produce the tracked files under `fixtures/starmap/`
  - _Requirements: 16.1, 16.3_

- [x] 8. Check decoder observations on the generated fixtures
  - Write `tests/integration/test_fixture_decode.py`, skipped with a clear message when `build/decoder/ScanTape` is missing
  - Clean fixture: blocks 0–7 intact, payloads equal the Reference_Payloads, final flag on block 7
  - Each damaged fixture: block 6 complete, data CRC `invalid`, recorded data CRC equals the original, payload differs from the reference at exactly the recorded offsets
  - A WAV cut inside block 3 gives a truncated observation with no padding; a silent WAV gives `no_blocks`
  - If the decoder cannot expose the block 6 payload and recorded CRC, move the patch hook to the byte reader (update the patch from task 2.2) and record the failing observation fields and unsupported failure modes in the task's verification entry
  - _Requirements: 2.1, 2.3, 2.4, 2.9, 3.5, 4.11, 5.6, 5.7_

- [x] 9. Implement intact-block export and the independent UEF reader
  - [x] 9.1 Implement the engine skeleton with intact selection
    - `taperescue/engine/__init__.py` (`recover`, `RecoverOptions`) and `taperescue/engine/selection.py`
    - Normalise inputs: drop duplicate capture IDs into `duplicates_merged`, sort captures by ID and reads by `(block_no, sample_offset)`
    - Compute the block range from the lowest flagged final block; implement decision steps a (`Missing`), b (`header_invalid`), e (`Intact` with all supporting IDs, including empty payloads), and f (`CRC failed`, `single_read`)
    - _Requirements: 3.6, 8.1, 8.2, 8.3, 8.4, 9.11_

  - [x] 9.2 Implement the export gate
    - `taperescue/engine/gate.py` with `ExportDecision` and `export_decision`: permitted only with a valid final-flag block, every block `0..final` `Intact` or `Reconstructed · CRC match`, consistent filename and addresses, `block_no` equal to position, and flag bit 7 on the final block only; otherwise list blocker block numbers and reasons
    - _Requirements: 11.1, 11.2_

  - [x] 9.3 Implement the independent UEF reader
    - `taperescue/uef/reader.py` with `UefBlock` and `read_uef`: plain or gzip UEF, chunk walking, unknown chunks ignored, `0x2A` sync scan in `0x0100` chunks, its own bitwise CRC-16/XMODEM
    - Import nothing from `taperescue.crc` or `taperescue.uef.exporter`
    - _Requirements: 11.4_

  - [x] 9.4 Implement the UEF exporter and export verification
    - `taperescue/uef/exporter.py` with `export_uef` and `ExportRefused`: UEF 0.10 header, origin chunk, carrier chunks (5100 cycles before block 0, 1800 before others, trailing tone), `0x0100` blocks from the lowest-capture-ID valid-header `header_raw`, recorded header CRC, selected payload, and recorded data CRC (high byte first); never recompute CRCs
    - `taperescue/uef/verify.py` with `verify_export(path, result)`: CRCs in the file equal the recorded CRCs, both CRCs recompute from file bytes, and the concatenated payload equals the recovered program; on failure delete the output and raise `ExportVerificationFailed`
    - _Requirements: 11.3, 11.4_

  - [x] 9.5 Add the single-capture recover, export, and verify-uef CLI commands
    - Extend `taperescue/cli.py` with `recover A.wav [--uef out.uef]`, `export --observations a.json -o out.uef`, and `verify-uef out.uef --reference ...`; exit 4 when the gate refuses and print the blockers
    - _Requirements: 11.2, 16.1, 16.2, 16.3_

  - [x] 9.6 Write the clean-fixture UEF integration test
    - `tests/integration/test_clean_uef.py`: clean WAV → observations → recover → export → independent reader; the concatenated payload equals the Reference_Payloads and CRCs match the recorded values
    - _Requirements: 4.11, 11.4, 11.5_

- [x] 10. Checkpoint: Milestone 1 exit
  - Ensure all tests pass, ask the user if questions arise.
  - Run `python scripts/collect_verification.py` and confirm `VERIFICATION_LOG.md` contains an entry for every completed Milestone 1 task
  - Confirm the log records both exit checks with commands and results: the clean WAV → Block_Observations → `.uef` path, and block 6 raw payload plus recorded data CRC exposed for both damaged fixtures. If either is not met, record it as a blocker and do not start Milestone 2
  - _Requirements: 2.9, 22.1, 22.4_

### Milestone 2

- [x] 11. Implement capture matching and anchors
  - [x] 11.1 Implement `taperescue/engine/matching.py` and wire it into `recover`
    - Identity candidates are valid-header reads only; identity key excludes `next_addr`
    - Compute anchors before pairing other blocks; raise `PairingRejected("no_anchor")` with the design message when two captures share none
    - Add decision steps c (`Conflict`, `header_disagreement`, excluded from reconstruction) and d (`Conflict`, payload kind, both capture IDs) in `taperescue/engine/__init__.py`
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

  - [ ]* 11.2 Write property test for valid-header identity
    - **Property 8: Only valid headers supply identity or bytes**
    - **Validates: Requirements 7.1**

  - [ ]* 11.3 Write property test for header disagreement
    - **Property 9: Header disagreement blocks reconstruction**
    - **Validates: Requirements 7.2, 7.5**

  - [ ]* 11.4 Write property test for anchors
    - **Property 10: Pairing requires an anchor**
    - **Validates: Requirements 7.3, 7.4**

  - [ ]* 11.5 Write property test for intact selection
    - **Property 11: Intact selection**
    - **Validates: Requirements 7.6, 8.1, 8.2, 8.4**

  - [ ]* 11.6 Write property test for duplicate captures
    - **Property 19: Duplicate captures collapse**
    - **Validates: Requirements 3.6**

- [x] 12. Implement bounded reconstruction
  - [x] 12.1 Implement `taperescue/engine/reconstruct.py`
    - `OffsetChoice`, `ReconstructionOutcome`, `candidate_count` (exact product computed without building candidates), and `reconstruct(reads, recorded_crc, cap)`
    - Skip evaluation entirely above the cap (`cap_exceeded`, `evaluated_count = 0`); otherwise evaluate every candidate from `itertools.product` without stopping at the first match; set `no_match`, single match with chosen values and supporting capture IDs, or `multiple_matches` with the count
    - _Requirements: 9.2, 9.3, 9.4, 9.5, 9.6, 9.7_

  - [x] 12.2 Wire reconstruction into `recover`
    - Add decision steps g (intact-only mode: `CRC failed`, `attempted = false`), h (`single_read`), i (`truncated_payload`), j (`crc_unreadable`), k (`crc_mismatch_between_reads`), and l (bounded reconstruction) in `taperescue/engine/__init__.py`
    - Unresolved blocks carry no payload and no substitute CRC
    - _Requirements: 9.1, 9.8, 9.9, 9.10, 9.12, 14.2_

  - [x] 12.3 Write property test for the candidate cap
    - **Property 13: Candidate count and cap**
    - Required because Requirement 13.3 mandates this property test
    - **Validates: Requirements 9.2, 9.3, 12.4, 13.3**

  - [ ]* 12.4 Write property test against a brute-force model
    - **Property 14: Reconstruction agrees with a brute-force model**
    - Use the reader's independent bitwise CRC in the model
    - **Validates: Requirements 9.4, 9.5, 9.6, 9.7**

  - [x] 12.5 Write property test for byte provenance
    - **Property 15: Reconstructed bytes trace to observations**
    - Required because Requirement 13.2 mandates this property test
    - **Validates: Requirements 9.5, 10.4, 13.2**

  - [ ]* 12.6 Write property test for planted damage
    - **Property 16: Planted damage is recovered when unique**
    - **Validates: Requirements 9.4, 9.5, 12.1**

  - [x] 12.7 Write property test for missing evidence
    - **Property 17: Missing evidence never produces a repair**
    - Required because Requirement 13.3 mandates this property test
    - **Validates: Requirements 9.1, 9.8, 9.9, 9.10, 9.12, 12.5, 13.3**

  - [ ]* 12.8 Write property test for single-capture sessions
    - **Property 18: Single capture never reconstructs**
    - **Validates: Requirements 9.11, 16.2**

- [x] 13. Implement the recovery report
  - [x] 13.1 Implement `taperescue/engine/report.py`
    - `ALGORITHM_VERSION`, `build_report`, `dumps_report` (sorted keys, indent 2), `loads_report`, and one reason-message table shared by CLI, API, and UI
    - Include algorithm version, cap, input digests, duplicates, anchors, per-block status, reason, supporting IDs, sample offsets, recorded data CRC, candidate and match counts, offset choices with observed values, the CRC-match label for Reconstructed blocks, export decision, and `recovered_payload_sha256` only when all required blocks are recovered
    - No timestamps, paths, filenames, or upload order
    - _Requirements: 9.13, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6_

  - [x] 13.2 Write property test for order independence
    - **Property 12: Input order does not change results**
    - Required because Requirement 13.1 mandates this property test
    - **Validates: Requirements 8.3, 10.6, 13.1**

  - [ ]* 13.3 Write property test for report completeness
    - **Property 20: Report completeness**
    - **Validates: Requirements 10.1, 10.2, 10.3, 10.5**

- [x] 14. Write the export property tests
  - [x] 14.1 Write property test for the export gate
    - **Property 21: Export gate**
    - Required because Requirement 13.4 mandates this property test
    - **Validates: Requirements 11.1, 11.2, 13.4**

  - [x] 14.2 Write property test for UEF round trip
    - **Property 22: UEF export round trip with recorded CRCs**
    - Required because Requirement 13.5 mandates this property test
    - **Validates: Requirements 11.3, 11.4, 13.5**

- [x] 15. Write the byte-array acceptance tests
  - `tests/engine/test_acceptance.py` with the brief's fixed cases; required because Requirement 12 mandates them
  - Unique repair: 16 candidates, 1 match equal to the Test_Payload, offsets 75 and 241 from Capture A, 23 and 128 from Capture B
  - Shared error at offset 7: 0 matches, `no_match`
  - Collision (offset 20 vs 21, 22): 8 candidates, 2 matches, `multiple_matches`
  - Seven differing offsets: 128 candidates, `cap_exceeded`, 0 evaluated, even with a matching candidate present
  - Truncated read, unreadable CRC, incompatible headers, conflicting intact reads: correct status and reason, no filled-in bytes or CRCs in the report
  - Intact A block 4 / B block 6 sourcing; Reconstructed label differs from Intact
  - _Requirements: 8.5, 9.13, 12.1, 12.2, 12.3, 12.4, 12.5, 12.6_

- [x] 16. Add end-to-end recovery
  - [x] 16.1 Extend the CLI for two-capture recovery and the demo
    - `recover A.wav [B.wav] [--intact-only] [--report r.json] [--uef out.uef]` writes the report even for incomplete sessions and exports only when permitted
    - Hash each input before and after the run and fail if a digest changes; open inputs read-only
    - Add `taperescue demo`: decode both damaged fixtures, recover, export, verify against `reference_payloads.json`
    - _Requirements: 10.7, 10.8, 14.5, 16.1, 16.2, 16.3_

  - [x] 16.2 Write the end-to-end integration tests
    - `tests/integration/test_end_to_end.py` (skipped without the built decoder): each damaged fixture alone gives block 6 `CRC failed` and refused export; intact-only merge leaves block 6 unrecovered; full recovery gives block 6 `Reconstructed · CRC match` and all others `Intact`; the exported UEF payload read by the independent reader equals the reference byte for byte; input digests are unchanged; CLI failure paths exit non-zero with a reason
    - _Requirements: 10.7, 14.1, 14.2, 14.3, 14.4, 16.3_

- [x] 17. Implement the baseline benchmark
  - [x] 17.1 Implement `taperescue/benchmark.py` and the `benchmark` CLI command
    - Modes: best single capture (most `Intact` blocks, ties by capture ID), intact-only merge, full TapeRescue; report recovered block count and whether a complete program was produced
    - Run `php lsblocks.php +r <wav>` with an argument list when `QUADBIKE_LSBLOCKS` and `php` are available; otherwise record `{"ran": false, "reason": ...}` and update the Quadbike Pending_Item in `verification/pending.md` with the reason
    - Write `benchmarks/results.json` and `benchmarks/results.md`, both headed "Inputs: generated STARMAP fixtures"
    - _Requirements: 15.1, 15.2, 15.3, 15.4_

  - [ ]* 17.2 Write unit tests for the benchmark
    - `tests/test_benchmark.py`: mode counts on the fixtures; with Quadbike absent the result records `ran: false` with a reason
    - _Requirements: 15.1, 15.3, 15.4_

- [x] 18. Checkpoint: Milestone 2 exit
  - Ensure all tests pass, ask the user if questions arise.
  - Run `python scripts/collect_verification.py` and confirm every completed Milestone 2 task has an entry
  - Confirm the Quadbike comparison is either recorded with real output or listed as a Pending_Item with its reason
  - _Requirements: 15.3, 22.1_

### Milestone 3

- [x] 19. Implement the web API
  - [x] 19.1 Implement `server/sessions.py`
    - `SessionStore` with a private temp root (mode 0700), `uuid4().hex` session directories, stored names `capture_0.wav` and `capture_1.wav`, display names kept only in the envelope
    - Streamed uploads aborted above 64 MiB; `last_access` per request; sweep that deletes sessions idle for 60 minutes using an injectable clock; immediate delete on request
    - _Requirements: 3.3, 17.2, 17.3_

  - [ ]* 19.2 Write property test for session paths
    - **Property 23: Session paths ignore user filenames**
    - Also test the sweep with an injected clock
    - **Validates: Requirements 17.2, 17.3**

  - [x] 19.3 Implement `server/app.py`
    - Endpoints `POST /api/sessions`, `POST /api/sample`, `GET /api/sessions/{sid}`, `GET .../report.json`, `GET .../tape.uef` (409 with blockers), `DELETE /api/sessions/{sid}`, and static `/`
    - Validate captures with `wavcheck`, decode in a worker thread, return the envelope with report, observations, and export decision; map capture errors to 422 with capture index; 400 for zero or more than two files; 404 for unknown sessions
    - Start the sweep in the lifespan; set the CSP header `default-src 'self'`; never forward captures to external services
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 10.8, 17.1, 17.3, 17.4, 17.5, 19.1, 19.2_

  - [ ]* 19.4 Write API tests
    - `tests/integration/test_api.py` with `TestClient`: `/api/sample` returns a permitted export and `tape.uef` downloads; an unsupported WAV returns 422 with the capture index; refused export returns 409 with blockers; report downloads for an incomplete session
    - _Requirements: 10.8, 17.1, 17.4, 17.5_

- [x] 20. Implement the web UI
  - [x] 20.1 Scaffold the Vite React TypeScript app and API client
    - `web/` with Vite config, a placeholder `App.tsx`, and `web/src/api.ts` with typed envelope, report, and error models
    - No third-party scripts or fonts
    - _Requirements: 19.1_

  - [x] 20.2 Implement the capture and merged strips
    - `web/src/status.ts` with label and colour per Block_Status from the design table; `CaptureStrip.tsx` and `MergedStrip.tsx` with one cell per block showing number, text label, and colour, and selection on click
    - _Requirements: 18.1, 18.2_

  - [x] 20.3 Implement the block inspector
    - `web/src/BlockInspector.tsx`: per-capture table (sample offset, CRC outcomes, recorded data CRC in hex, lengths), differing offsets with each capture's value, chosen value and supporting capture, and the decision reason or Unresolved_Reason message
    - _Requirements: 18.3_

  - [x] 20.4 Implement downloads, JSBeeb help, and wire the app
    - `DownloadBar.tsx`: **Download report** always enabled; **Download tape image** disabled with blockers shown while export is not permitted; confirmation dialog listing reconstructed blocks before the download
    - `JsBeebHelp.tsx`: link to `https://bbc.xania.org/` and the steps to load the `.uef` with the local tape picker, `*TAPE`, and `CHAIN "STARMAP"`
    - `App.tsx`: upload of one or two files, **Try sample**, per-capture error messages, and the strips, inspector, and download bar wired to the API client
    - _Requirements: 11.6, 18.4, 18.5, 18.6, 18.7_

  - [ ]* 20.5 Write UI tests
    - `vitest --run` with React Testing Library: status label and colour per status, download disabled with blockers, reconstructed-block confirmation, JSBeeb instructions, per-capture errors, one strip per capture plus the merged strip
    - _Requirements: 11.6, 18.1, 18.2, 18.3, 18.4, 18.5, 18.6, 18.7_

- [ ] 21. Package the single container
  - [ ] 21.1 Write the `Dockerfile` and `.dockerignore`
    - Stages: `decoder` (debian bookworm-slim, g++, cmake, build script on the vendored source), `web` (node 20, `npm ci && npm run build`), runtime (python 3.12-slim, `taperescue` wheel, `ScanTape`, `web/dist`, the bundled fixture WAVs, `uvicorn server.app:app` as a non-root user)
    - Exclude ROM files and the JSBeeb emulator
    - _Requirements: 17.6, 19.3, 19.4_

  - [ ]* 21.2 Write container and bundle smoke tests
    - `tests/smoke/test_container.py`: the image lists no `*.rom` or `jsbeeb` paths; `web/dist/index.html` has no third-party script tags; bundled fixtures are only the project-authored STARMAP files
    - _Requirements: 19.1, 19.3, 19.4_

- [ ] 22. Checkpoint: Milestone 3 exit
  - Ensure all tests pass, ask the user if questions arise.
  - Run `python scripts/collect_verification.py` and confirm every completed Milestone 3 task has an entry; the deployment URL stays a Pending_Item
  - _Requirements: 22.1, 22.2_

### Milestone 4 (local parts)

- [ ] 23. Add the hook, notices, README, and final pending items
  - [ ] 23.1 Create the recovery test hook
    - `.kiro/hooks/recovery-tests.json`: `PostFileSave` with matcher `taperescue/(decoder_adapter|engine)/.*\.py$`, running `python -m pytest tests/engine tests/decoder_adapter -q`
    - _Requirements: 20.1, 20.2_

  - [ ] 23.2 Write `THIRD_PARTY_NOTICES.md`
    - AtomTapeUtils with license and revision, and every bundled Python and npm dependency with its license
    - _Requirements: 21.2_

  - [ ] 23.3 Write `README.md`
    - Local commands: build the decoder, generate fixtures, run tests, run `taperescue demo`, run the benchmark, start the web app and the container
    - Judge walkthrough: block 6 failing in both recordings, byte disagreements and the recorded CRC, reconstruction with provenance, `.uef` download for JSBeeb
    - Label STARMAP fixtures as generated; state that real-archive results are claimed only after testing with permission-cleared captures; describe every Pending_Item without evidence as pending
    - _Requirements: 21.3, 21.4, 21.5, 22.3_

  - [ ] 23.4 Finalise the verification pending items and traceability example
    - Update `verification/pending.md` so each external step (clean `.uef` in JSBeeb, recovered `.uef` in JSBeeb, real archival captures, Quadbike if not runnable, deployment URL, YouTube demo) has an evidence placeholder and no claimed result
    - Add one example linking a requirement, the generated test, a failure that test found, and the resulting code change, taken from the existing task entries; if no test has failed yet, record that as a blocker instead of inventing one
    - _Requirements: 20.3, 22.2, 22.3_

  - [ ]* 23.5 Write documentation smoke tests
    - `tests/smoke/test_docs.py`: README contains the command and walkthrough sections and the generated-fixture label; root LICENSE, notices, hook file, and `.kiro/specs/tape-rescue/` exist; every Pending_Item without evidence is described as pending in the README
    - _Requirements: 20.1, 20.2, 21.1, 21.2, 21.3, 21.4, 21.5, 22.3_

- [ ] 24. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.
  - Run `python scripts/collect_verification.py`; confirm every task has an entry and every external step is listed as a Pending_Item with an evidence placeholder
  - _Requirements: 22.1, 22.2, 22.3_

## Notes

- Sub-tasks marked with `*` are optional and can be skipped for a faster MVP.
- Some test sub-tasks are not optional because the requirements mandate them: the CRC vector tests (3.3, requested by the user), the property tests named in Requirement 13 (12.3, 12.5, 12.7, 13.2, 14.1, 14.2), the acceptance tests in Requirement 12 (15), and the Milestone 1 and end-to-end integration tests (8, 9.6, 16.2), which provide the evidence for the milestone exit checks.
- Integration tests skip with a clear message when `build/decoder/ScanTape` is missing. A skip is recorded as a blocker in the task's verification entry, not as a pass.
- Property tests use Hypothesis with at least 100 examples and a docstring tag `Feature: tape-rescue, Property N: <title>`.
- Per-task entry files under `verification/entries/` keep parallel tasks from editing the same file; `VERIFICATION_LOG.md` is regenerated at each checkpoint.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1"] },
    { "id": 1, "tasks": ["2.1", "3.1", "3.2", "4.1", "6.1", "9.3"] },
    { "id": 2, "tasks": ["2.2", "3.3", "3.4", "4.2", "5.1", "6.2", "6.3"] },
    { "id": 3, "tasks": ["2.3", "5.2", "5.3", "6.4", "6.5", "6.6"] },
    { "id": 4, "tasks": ["5.4", "6.7", "6.8"] },
    { "id": 5, "tasks": ["6.9", "6.10", "7", "9.1"] },
    { "id": 6, "tasks": ["8", "9.2"] },
    { "id": 7, "tasks": ["9.4"] },
    { "id": 8, "tasks": ["9.5"] },
    { "id": 9, "tasks": ["9.6"] },
    { "id": 10, "tasks": ["10"] },
    { "id": 11, "tasks": ["11.1", "12.1"] },
    { "id": 12, "tasks": ["11.2", "11.3", "11.4", "11.5", "11.6", "12.2"] },
    { "id": 13, "tasks": ["12.3", "12.4", "12.5", "12.6", "12.7", "12.8", "13.1"] },
    { "id": 14, "tasks": ["13.2", "13.3", "14.1", "14.2", "15", "16.1"] },
    { "id": 15, "tasks": ["16.2", "17.1"] },
    { "id": 16, "tasks": ["17.2"] },
    { "id": 17, "tasks": ["18"] },
    { "id": 18, "tasks": ["19.1", "20.1"] },
    { "id": 19, "tasks": ["19.2", "19.3", "20.2"] },
    { "id": 20, "tasks": ["19.4", "20.3"] },
    { "id": 21, "tasks": ["20.4"] },
    { "id": 22, "tasks": ["20.5", "21.1"] },
    { "id": 23, "tasks": ["21.2"] },
    { "id": 24, "tasks": ["22"] },
    { "id": 25, "tasks": ["23.1", "23.2", "23.3", "23.4"] },
    { "id": 26, "tasks": ["23.5"] },
    { "id": 27, "tasks": ["24"] }
  ]
}
```
