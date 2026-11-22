# Requirements Document

## Introduction

TapeRescue recovers vintage software from multiple imperfect recordings of the same BBC Micro data cassette. The MVP accepts one or two PCM WAV captures (44.1 kHz, 16-bit, mono) of a single unprotected program in the standard BBC Micro 1200-baud cassette format. TapeRescue decodes each capture into per-block observations, selects intact blocks, reconstructs blocks that fail in every capture when exactly one combination of observed byte values matches the original recorded data CRC, and produces a `.uef` tape image plus a recovery report with byte-level provenance.

The work is delivered in four milestones, implemented in order:

1. Milestone 1: vendored and patched AtomTapeUtils decoder with JSON block observations, a project-owned Python fixture encoder, the `STARMAP` clean and damaged WAV fixtures, and intact-block `.uef` export.
2. Milestone 2: recovery engine, byte-array acceptance tests, property tests, end-to-end recovery, and baseline benchmark.
3. Milestone 3: minimal web application (FastAPI backend, Vite/React TypeScript UI) in a single deployable container, with the CLI retained.
4. Milestone 4 (local parts): Kiro hook that reruns recovery tests, README with judge walkthrough, open-source license, and third-party notices. External steps (JSBeeb runs, real archival captures, Quadbike comparison when not runnable, deployment, YouTube demo) are recorded as pending items with evidence placeholders.

Out of scope for the MVP: other tape formats, arbitrary waveform alignment, copy-protected loaders, resampling, recovery of headers, and recovery of missing byte positions.

## Glossary

- **TapeRescue**: The complete system described in this document, including CLI, recovery engine, web application, fixtures, and documentation.
- **Capture**: One PCM WAV file submitted as a recording of a cassette.
- **Capture_ID**: An identifier for a Capture derived from the SHA-256 digest of the WAV file bytes.
- **Decoder**: The AtomTapeUtils `ScanTape` program vendored at revision `98d1a8d1bed00aa22f1d72e3d5e91a254a69ec64` under `third_party/`, built locally with the project's tracked patch, run in BBC Micro 1200-baud mode (`-bbm`).
- **Decoder_Patch**: The tracked patch file that adds a JSON block-observation output mode to the Decoder at the block decoder boundary, before file assembly.
- **Decoder_Adapter**: The Python module that invokes the Decoder as a subprocess with an argument list and parses its JSON output into Block_Observations.
- **Block_Observation**: One decoded block from one Capture, containing Capture_ID, block sample offset or time, raw header bytes, parsed header fields, raw payload bytes, expected payload length, actual payload length, recorded header CRC, recorded data CRC, header CRC outcome, and data CRC outcome.
- **Header_Fields**: The parsed BBC cassette block header fields: filename, load address, execution address, block number, block length, block flags, and next-file address.
- **CRC_Outcome**: One of `valid`, `invalid`, or `unreadable` for a header CRC or data CRC.
- **Complete_Payload**: A payload whose actual length equals the expected length from a header with a valid header CRC.
- **Truncated_Payload**: A payload whose actual length is less than the expected length.
- **Empty_Payload**: A payload whose expected length is zero according to a header with a valid header CRC.
- **Intact_Block**: A Block_Observation with a valid header CRC, a Complete_Payload or Empty_Payload, and a valid data CRC (or no data CRC for an Empty_Payload).
- **Anchor**: A block number for which two Captures each have an Intact_Block with equal Header_Fields and equal payload bytes.
- **CRC_Calculator**: The function computing CRC-16/XMODEM (polynomial 0x1021, initial value 0x0000, no input or output reflection, final XOR 0x0000) as used by the BBC Micro cassette format.
- **Recovery_Engine**: The Python library that matches, selects, and reconstructs blocks from Block_Observations, independent of WAV decoding and of the user interface.
- **Candidate**: One byte sequence formed by keeping bytes on which all reads agree and choosing, at each differing offset, one of the distinct observed values.
- **Candidate_Cap**: The maximum number of Candidates evaluated for one block, fixed at 64 for the MVP.
- **Block_Status**: One of `Intact`, `CRC failed`, `Reconstructed · CRC match`, `Unresolved`, `Conflict`, or `Missing`.
- **Unresolved_Reason**: A machine-readable code and human-readable message explaining why a block is not Intact or Reconstructed, drawn from: `no_match`, `multiple_matches`, `cap_exceeded`, `crc_unreadable`, `crc_mismatch_between_reads`, `truncated_payload`, `header_invalid`, `header_disagreement`, `single_read`, `block_missing`.
- **Recovery_Report**: The JSON document produced by the Recovery_Engine describing inputs, per-block results, evidence, and the recovered payload digest.
- **UEF_Exporter**: The component that writes a `.uef` tape image from recovered blocks.
- **UEF_Reader**: A UEF parser that is implemented independently of the UEF_Exporter and is used to validate exported images.
- **Fixture_Encoder**: The project-owned Python encoder, independent of the Decoder, consisting of the BASIC_Tokenizer, Block_Builder, Damage_Injector, and FSK_Modulator.
- **BASIC_Tokenizer**: The Fixture_Encoder component that converts BBC BASIC source text into the tokenized BBC BASIC program format.
- **Block_Builder**: The Fixture_Encoder component that splits a program into BBC cassette blocks with headers, header CRC, data CRC, and the final-block flag.
- **Damage_Injector**: The Fixture_Encoder component that flips selected payload bits in encoded byte frames before modulation while preserving the recorded CRC bytes.
- **FSK_Modulator**: The Fixture_Encoder component that modulates byte frames into a 1200-baud BBC cassette FSK signal written as a 44.1 kHz, 16-bit, mono PCM WAV file.
- **STARMAP**: The project-authored BBC BASIC program that draws a star map and serves as the demo and end-to-end fixture.
- **Clean_Fixture**: The undamaged STARMAP WAV produced by the Fixture_Encoder.
- **Damaged_Fixture_A** and **Damaged_Fixture_B**: The two STARMAP WAVs produced by the Damage_Injector, each with bit flips in two distinct bytes of block 6, at positions disjoint between the two fixtures.
- **Reference_Payloads**: The known original payload bytes and SHA-256 digest for each STARMAP block, retained alongside the fixtures.
- **Test_Payload**: The 256-byte byte array defined by `payload[i] = (17*i + 11*i*i + 42) mod 256` for zero-based `i`.
- **CLI**: The TapeRescue command-line interface for decoding, recovering, exporting, and generating fixtures.
- **API_Server**: The FastAPI backend of the web application.
- **Web_UI**: The Vite/React TypeScript frontend of the web application.
- **Session**: A server-side unit of work holding the Captures uploaded together, their temporary files, and the resulting Recovery_Report.
- **Benchmark_Runner**: The component that compares best single Capture, intact-block merge, TapeRescue reconstruction, and the Quadbike `lsblocks.php +r` repair on the same inputs.
- **Recovery_Test_Hook**: The Kiro hook stored under `.kiro/hooks/` that reruns the recovery test suite when Decoder_Adapter or Recovery_Engine source files change.
- **Verification_Log**: The tracked file in the repository that records, for each task, the commands run, their results, and any blockers.
- **Pending_Item**: A Verification_Log entry for a manual or external step that has not been performed, with an evidence placeholder and no claimed result.

## Requirements

### Requirement 1: Vendored decoder and reproducible build

**User Story:** As a developer, I want the decoder vendored at a pinned revision with its notices and a tracked patch, so that decoding builds reproducibly and respects the upstream license.

#### Acceptance Criteria

1. THE TapeRescue repository SHALL contain the AtomTapeUtils source at revision `98d1a8d1bed00aa22f1d72e3d5e91a254a69ec64` under `third_party/` with the upstream LICENSE file and copyright notices unchanged.
2. THE TapeRescue repository SHALL store the Decoder_Patch as a separate tracked patch file that applies cleanly to the pinned revision.
3. WHEN the vendored source and Decoder_Patch are present locally, THE TapeRescue build script SHALL build the Decoder without network access.
4. IF fetching the pinned AtomTapeUtils revision fails, THEN THE TapeRescue build script SHALL exit with a non-zero status and an error message naming the revision, and THE Verification_Log SHALL record the failure as a blocker.
5. THE Decoder_Adapter SHALL invoke the Decoder using an argument list with generated file paths and without a shell.

### Requirement 2: Structured block observations

**User Story:** As a recovery engine developer, I want raw position-aligned block observations including failed reads, so that reconstruction can use payloads and original CRCs from reads that fail their data CRC.

#### Acceptance Criteria

1. WHEN the Decoder runs in JSON observation mode on a Capture, THE Decoder SHALL emit one Block_Observation per detected block, taken at the block decoder boundary before file assembly.
2. THE Block_Observation SHALL include the Capture_ID, the block sample offset, the raw header bytes, the Header_Fields, the raw payload bytes, the expected payload length, the actual payload length, the recorded header CRC, the recorded data CRC, the header CRC_Outcome, and the data CRC_Outcome.
3. WHEN a payload fails its data CRC, THE Decoder SHALL emit the full observed payload bytes and the recorded data CRC value for that block.
4. WHEN a payload ends before the expected length, THE Decoder SHALL emit only the bytes actually read and mark the Block_Observation as a Truncated_Payload.
5. THE Decoder SHALL emit payload bytes without zero padding.
6. WHEN a block header declares a zero length, THE Decoder SHALL mark the Block_Observation as an Empty_Payload, distinct from a Truncated_Payload.
7. IF the recorded header CRC or data CRC bytes cannot be read, THEN THE Decoder SHALL set the corresponding CRC_Outcome to `unreadable` and omit the CRC value.
8. WHEN the Decoder_Adapter receives Decoder output, THE Decoder_Adapter SHALL validate the output against the Block_Observation schema and reject output that fails validation with an error identifying the Capture_ID.
9. IF the Decoder cannot produce the full payload and recorded data CRC for block 6 of Damaged_Fixture_A or Damaged_Fixture_B, THEN THE Verification_Log SHALL record the failing observation fields and the unsupported failure modes before Milestone 2 work begins.

### Requirement 3: Capture input validation

**User Story:** As a user, I want clear messages for unsupported or unusable recordings, so that I know what to change.

#### Acceptance Criteria

1. WHEN a Capture is submitted, THE TapeRescue SHALL verify from the file contents that the Capture is a RIFF/WAVE PCM file with a 44,100 Hz sample rate, 16-bit samples, and one channel.
2. IF a Capture is not a PCM WAV file with 44,100 Hz, 16-bit, mono format, THEN THE TapeRescue SHALL reject the Capture with a message stating the detected format and the supported format.
3. IF a Capture exceeds 64 MiB in size or 10 minutes in duration, THEN THE TapeRescue SHALL reject the Capture with a message stating the limit exceeded.
4. IF the Decoder runs longer than 60 seconds on one Capture, THEN THE TapeRescue SHALL terminate the Decoder process and report a timeout for that Capture.
5. IF the Decoder finds no valid block header in a Capture, THEN THE TapeRescue SHALL report that no BBC 1200-baud blocks were found in that Capture.
6. WHEN two submitted Captures have identical SHA-256 digests, THE TapeRescue SHALL treat them as one Capture and state in the Recovery_Report that a duplicate was merged.

### Requirement 4: STARMAP program and clean fixture

**User Story:** As a judge, I want a project-authored demo program and clean recording, so that the full workflow can be tried without third-party assets.

#### Acceptance Criteria

1. THE TapeRescue repository SHALL contain the STARMAP program as original BBC BASIC source that draws a star map.
2. WHEN the Fixture_Encoder generates the Clean_Fixture, THE BASIC_Tokenizer SHALL produce a tokenized BBC BASIC program from the STARMAP source.
3. WHEN the Fixture_Encoder generates the Clean_Fixture, THE Block_Builder SHALL produce exactly eight data blocks numbered 0 through 7 with cassette filename `STARMAP`.
4. THE Block_Builder SHALL compute each header CRC and data CRC with the CRC_Calculator.
5. THE Block_Builder SHALL set the final-block flag on block 7 only.
6. THE STARMAP program SHALL require the bytes of block 6 to run to completion.
7. THE FSK_Modulator SHALL write the Clean_Fixture as a 1200-baud BBC cassette signal in a 44,100 Hz, 16-bit, mono PCM WAV file.
8. THE Fixture_Encoder SHALL be implemented without importing or invoking the Decoder.
9. WHEN the Fixture_Encoder generates fixtures with the same inputs twice, THE Fixture_Encoder SHALL produce byte-identical WAV files.
10. THE TapeRescue repository SHALL retain the Reference_Payloads for every STARMAP block.
11. WHEN the Decoder processes the Clean_Fixture, THE Decoder SHALL report blocks 0 through 7 as Intact_Blocks whose payloads equal the Reference_Payloads and whose block 7 carries the final-block flag.

### Requirement 5: Damaged fixtures

**User Story:** As a developer, I want two reproducible damaged recordings that each fail block 6 at disjoint positions, so that cross-capture reconstruction can be demonstrated and tested end to end.

#### Acceptance Criteria

1. WHEN the Fixture_Encoder generates Damaged_Fixture_A and Damaged_Fixture_B, THE Damage_Injector SHALL flip exactly one payload bit in each of two distinct bytes of block 6 per fixture, in the encoded byte frames before modulation.
2. THE Damage_Injector SHALL use byte offsets in Damaged_Fixture_A that are disjoint from the byte offsets in Damaged_Fixture_B.
3. THE Damage_Injector SHALL preserve the original recorded data CRC bytes and the header bytes of block 6 in both damaged fixtures.
4. THE Damage_Injector SHALL keep start bits, stop bits, and bit timing unchanged.
5. THE TapeRescue repository SHALL record the damaged offsets, original byte values, and damaged byte values for each damaged fixture.
6. WHEN the Decoder processes Damaged_Fixture_A or Damaged_Fixture_B, THE Decoder SHALL report block 6 with data CRC_Outcome `invalid`, a Complete_Payload, and the original recorded data CRC.
7. WHEN the Decoder processes Damaged_Fixture_A or Damaged_Fixture_B, THE Decoder SHALL report payload bytes for block 6 that differ from the Reference_Payloads at exactly the recorded damaged offsets.

### Requirement 6: CRC calculation

**User Story:** As a developer, I want a verified CRC implementation, so that intact checks and reconstruction use the BBC cassette checksum correctly.

#### Acceptance Criteria

1. WHEN the CRC_Calculator processes the ASCII bytes `123456789`, THE CRC_Calculator SHALL return `0x31C3`.
2. WHEN the CRC_Calculator processes the Test_Payload, THE CRC_Calculator SHALL return `0xAC93`.
3. WHEN the CRC_Calculator processes a STARMAP block payload from the Reference_Payloads, THE CRC_Calculator SHALL return the data CRC that the Block_Builder wrote for that block.

### Requirement 7: Capture matching and anchors

**User Story:** As a preservation volunteer, I want TapeRescue to confirm that recordings belong to the same program, so that bytes from different programs are not combined.

#### Acceptance Criteria

1. THE Recovery_Engine SHALL use only Block_Observations with a valid header CRC as identity candidates.
2. THE Recovery_Engine SHALL match Block_Observations across Captures by block number together with equal filename, load address, execution address, block length, and block flags.
3. WHEN two Captures are submitted, THE Recovery_Engine SHALL identify Anchors before pairing any other blocks.
4. IF two Captures share no Anchor, THEN THE Recovery_Engine SHALL reject the pairing and report that the Captures cannot be shown to belong to the same program, asking the user to select recordings of the same program version.
5. IF two Block_Observations with the same block number both have valid header CRCs and differ in any matched Header_Field, THEN THE Recovery_Engine SHALL report a header conflict for that block and exclude the block from reconstruction.
6. IF two Intact_Blocks with the same block number and matching Header_Fields have different payload bytes, THEN THE Recovery_Engine SHALL set the Block_Status to `Conflict` and report both Capture_IDs.

### Requirement 8: Intact block selection

**User Story:** As a preservation volunteer, I want each block taken from a recording where it passes its checksum, so that the recovered program uses verified data first.

#### Acceptance Criteria

1. WHEN at least one Block_Observation for a block is an Intact_Block, THE Recovery_Engine SHALL select that Intact_Block's payload and set the Block_Status to `Intact`.
2. WHEN more than one Capture supplies an Intact_Block for the same block with equal payloads, THE Recovery_Engine SHALL record every supporting Capture_ID as evidence.
3. THE Recovery_Engine SHALL select intact payloads independently of upload order.
4. WHEN a valid Empty_Payload block is observed, THE Recovery_Engine SHALL retain the Empty_Payload block with Block_Status `Intact`.
5. WHEN Capture A has an Intact_Block 4 and a failed block 6, and Capture B has a failed block 4 and an Intact_Block 6, THE Recovery_Engine SHALL report blocks 4 and 6 as `Intact`, sourced from Capture A and Capture B respectively.

### Requirement 9: Bounded byte reconstruction

**User Story:** As a preservation volunteer, I want blocks that fail in every recording rebuilt from the recorded bytes only when the checksum identifies a single combination, so that repairs are evidence-based.

#### Acceptance Criteria

1. WHEN no Intact_Block exists for a block, THE Recovery_Engine SHALL attempt reconstruction only if at least two reads have valid and equal headers, Complete_Payloads of equal length, and equal readable recorded data CRC values.
2. WHILE reconstructing a block, THE Recovery_Engine SHALL keep the bytes on which all reads agree and form Candidates from the distinct observed values at each differing offset.
3. WHEN the number of Candidates for a block exceeds the Candidate_Cap of 64, THE Recovery_Engine SHALL set the Block_Status to `Unresolved` with Unresolved_Reason `cap_exceeded` without evaluating any Candidate.
4. WHILE the number of Candidates is at most the Candidate_Cap, THE Recovery_Engine SHALL evaluate every Candidate with the CRC_Calculator against the recorded data CRC.
5. WHEN exactly one Candidate matches the recorded data CRC, THE Recovery_Engine SHALL set the Block_Status to `Reconstructed · CRC match` and record, for each differing offset, the chosen value and the Capture_IDs that observed the chosen value.
6. WHEN zero Candidates match the recorded data CRC, THE Recovery_Engine SHALL set the Block_Status to `Unresolved` with Unresolved_Reason `no_match`.
7. WHEN more than one Candidate matches the recorded data CRC, THE Recovery_Engine SHALL set the Block_Status to `Unresolved` with Unresolved_Reason `multiple_matches` and record the number of matching Candidates.
8. IF any read for the block has an unreadable recorded data CRC, THEN THE Recovery_Engine SHALL set the Block_Status to `Unresolved` with Unresolved_Reason `crc_unreadable`.
9. IF the reads for the block carry different recorded data CRC values, THEN THE Recovery_Engine SHALL set the Block_Status to `Unresolved` with Unresolved_Reason `crc_mismatch_between_reads`.
10. IF any read needed for the block is a Truncated_Payload, THEN THE Recovery_Engine SHALL set the Block_Status to `Unresolved` with Unresolved_Reason `truncated_payload`.
11. WHEN only one Capture is submitted and a block fails its data CRC, THE Recovery_Engine SHALL set the Block_Status to `CRC failed` with Unresolved_Reason `single_read`.
12. IF a byte or checksum needed for a block was not observed in any Capture, THEN THE Recovery_Engine SHALL set the Block_Status to `Unresolved` and leave the missing byte or checksum absent from the recovered block.
13. THE Recovery_Engine SHALL report a Reconstructed block as evidence from a CRC match and label the result as a CRC match, distinct from an Intact_Block.

### Requirement 10: Recovery report and provenance

**User Story:** As a museum archivist, I want a downloadable report with checksums, sources, and digests, so that the recovery can be audited later.

#### Acceptance Criteria

1. THE Recovery_Report SHALL include the algorithm version, the Candidate_Cap, and the SHA-256 digest of every input Capture.
2. THE Recovery_Report SHALL include, for every block, the Block_Status, the Unresolved_Reason when present, the supporting Capture_IDs, and the source sample offsets.
3. WHERE a block has a data CRC, THE Recovery_Report SHALL include the recorded data CRC and, for blocks where reconstruction was attempted, the Candidate count and matching Candidate count.
4. WHEN a block is Reconstructed, THE Recovery_Report SHALL list each differing offset with the observed value from each Capture, the chosen value, and the supporting Capture_IDs.
5. WHEN all blocks from 0 through the final block are `Intact` or `Reconstructed · CRC match`, THE Recovery_Report SHALL include the SHA-256 digest of the recovered program payload.
6. WHEN the same Captures are submitted in a different order, THE Recovery_Engine SHALL produce a Recovery_Report with identical evidence, Block_Status values, and payload results, ordered by block number and Capture_ID.
7. THE TapeRescue SHALL leave the bytes of every input Capture file unchanged.
8. WHEN a Session is incomplete, THE TapeRescue SHALL allow the Recovery_Report to be exported.

### Requirement 11: UEF export

**User Story:** As a user, I want a runnable tape image only when the program is fully recovered, so that I do not load a corrupted program by mistake.

#### Acceptance Criteria

1. WHEN every block from 0 through the block carrying the final-block flag is present with Block_Status `Intact` or `Reconstructed · CRC match` and consistent sequence and Header_Fields, THE UEF_Exporter SHALL permit `.uef` export.
2. IF any required block is `Unresolved`, `CRC failed`, `Conflict`, or `Missing`, or no block with the final-block flag has a valid header, THEN THE UEF_Exporter SHALL refuse `.uef` export and list the blocking block numbers and reasons.
3. THE UEF_Exporter SHALL write each block using the source header bytes and recorded data CRC bytes observed in the Captures.
4. WHEN the UEF_Exporter writes a `.uef` file, THE UEF_Reader SHALL parse the file and confirm that every block's header CRC and data CRC match the CRC values recorded in the input Captures and that the concatenated payload equals the recovered program payload.
5. WHEN the Clean_Fixture is processed alone, THE UEF_Exporter SHALL export a `.uef` file whose concatenated payload equals the Reference_Payloads.
6. WHEN a `.uef` export contains Reconstructed blocks, THE Web_UI SHALL list the Reconstructed block numbers to the user before the download starts.

### Requirement 12: Byte-array acceptance tests

**User Story:** As a developer, I want the brief's fixture cases as automated tests, so that the reconstruction logic is verified independently of audio decoding.

#### Acceptance Criteria

1. WHEN Capture A holds the Test_Payload with offsets 23 and 128 XORed by `0x02` and `0x08`, and Capture B holds the Test_Payload with offsets 75 and 241 XORed by `0x01` and `0x10`, both recording CRC `0xAC93`, THE Recovery_Engine SHALL report both reads as failed, evaluate 16 Candidates, find exactly one match equal to the Test_Payload, and attribute offsets 75 and 241 to Capture A and offsets 23 and 128 to Capture B.
2. WHEN the preceding case additionally XORs offset 7 of both copies with `0x04`, THE Recovery_Engine SHALL find zero matching Candidates and set the Block_Status to `Unresolved` with Unresolved_Reason `no_match`.
3. WHEN Capture A holds the Test_Payload with offset 20 XORed by `0x01`, and Capture B holds the Test_Payload with offsets 21 and 22 XORed by `0x10` and `0x21`, both recording CRC `0xAC93`, THE Recovery_Engine SHALL report both reads as failed, evaluate 8 Candidates, find 2 matches, and set the Block_Status to `Unresolved` with Unresolved_Reason `multiple_matches`.
4. WHEN two Complete_Payloads with the same recorded CRC differ at seven offsets, THE Recovery_Engine SHALL compute 128 Candidates, set the Block_Status to `Unresolved` with Unresolved_Reason `cap_exceeded`, and evaluate no Candidate, including when a matching Candidate exists.
5. WHEN a block is supplied with a Truncated_Payload, an unreadable data CRC, incompatible Header_Fields, or conflicting Intact_Blocks, THE Recovery_Engine SHALL reject or mark the block `Unresolved` or `Conflict` with the corresponding reason, and THE Recovery_Report SHALL contain no filled-in bytes or checksums for that block.
6. THE TapeRescue test suite SHALL include an automated test for the intact block selection case in Requirement 8 criterion 5.

### Requirement 13: Property-based tests

**User Story:** As a developer, I want property tests on the recovery engine, so that ordering, provenance, and export rules hold across generated inputs.

#### Acceptance Criteria

1. THE Recovery_Engine property tests SHALL verify that permuting the order of input Captures leaves the recovered payload, Block_Status values, and evidence unchanged.
2. THE Recovery_Engine property tests SHALL verify that every byte of a Reconstructed block equals a value observed at the same offset in at least one input Capture.
3. THE Recovery_Engine property tests SHALL verify that no block is reported as `Reconstructed · CRC match` when the Candidate search was skipped, exceeded the Candidate_Cap, or used a Truncated_Payload.
4. THE Recovery_Engine property tests SHALL verify that any `Unresolved`, `CRC failed`, `Conflict`, or `Missing` required block causes the UEF_Exporter to refuse export.
5. THE UEF_Exporter property tests SHALL validate exported CRCs against the CRC values read from the input Captures, in addition to recomputing CRCs from the output.

### Requirement 14: End-to-end recovery of STARMAP

**User Story:** As a judge, I want to see two failing recordings turned into the original program, so that the core claim is demonstrated on real decoder output.

#### Acceptance Criteria

1. WHEN Damaged_Fixture_A alone or Damaged_Fixture_B alone is processed, THE TapeRescue SHALL report block 6 as `CRC failed` and refuse `.uef` export.
2. WHEN Damaged_Fixture_A and Damaged_Fixture_B are processed with intact-block selection only, THE TapeRescue SHALL report block 6 as not recovered.
3. WHEN Damaged_Fixture_A and Damaged_Fixture_B are processed with reconstruction enabled, THE Recovery_Engine SHALL report block 6 as `Reconstructed · CRC match` and all other blocks as `Intact`.
4. WHEN the end-to-end recovery completes, THE TapeRescue SHALL export a `.uef` file whose payload, as parsed by the UEF_Reader, equals the Reference_Payloads byte for byte.
5. THE CLI SHALL run the end-to-end recovery with a single documented command.

### Requirement 15: Baseline benchmark

**User Story:** As a judge, I want a comparison with existing approaches on the same inputs, so that the contribution is measured, not asserted.

#### Acceptance Criteria

1. WHEN the Benchmark_Runner runs on Damaged_Fixture_A and Damaged_Fixture_B, THE Benchmark_Runner SHALL report the number of recovered blocks and whether a complete program was produced for the best single Capture, the intact-block merge, and TapeRescue reconstruction.
2. WHERE Quadbike is available locally, THE Benchmark_Runner SHALL run `lsblocks.php +r` on the same fixtures and report the recovered block count.
3. IF Quadbike cannot be run, THEN THE Verification_Log SHALL record a Pending_Item stating why the Quadbike comparison did not run.
4. THE Benchmark_Runner SHALL write its results to a tracked file that labels the inputs as generated fixtures.

### Requirement 16: Command-line interface

**User Story:** As a developer, I want a CLI for every core step, so that fixtures and verification stay reproducible without the web app.

#### Acceptance Criteria

1. THE CLI SHALL provide commands to generate fixtures, decode one Capture into Block_Observations as JSON, run recovery on one or two Captures, write the Recovery_Report, and export a `.uef` file.
2. WHEN one Capture is supplied to the CLI recovery command, THE CLI SHALL produce a Recovery_Report for inspection and benchmarking.
3. IF a CLI command fails, THEN THE CLI SHALL exit with a non-zero status and print the error reason.

### Requirement 17: Web API

**User Story:** As a user, I want to upload recordings in a browser, so that I can recover a program without installing tools.

#### Acceptance Criteria

1. WHEN the API_Server receives one or two Captures, THE API_Server SHALL create a Session, validate each Capture per Requirement 3, run the Decoder_Adapter and Recovery_Engine, and return the Recovery_Report.
2. THE API_Server SHALL store Session files under server-generated paths, independent of user-supplied filenames.
3. WHEN a Session is deleted or 60 minutes pass after the Session's last request, THE API_Server SHALL delete the Session's temporary files.
4. THE API_Server SHALL provide endpoints to download the Recovery_Report and, when permitted by Requirement 11, the `.uef` file.
5. THE API_Server SHALL provide a sample endpoint that runs the bundled Damaged_Fixture_A and Damaged_Fixture_B through recovery.
6. THE API_Server and Web_UI SHALL be buildable and runnable as a single container image that includes the built Decoder.

### Requirement 18: Web user interface

**User Story:** As a user, I want to see both recordings and the merged result on one screen, so that I understand which bytes came from where.

#### Acceptance Criteria

1. THE Web_UI SHALL display one capture strip per Capture and one merged result strip on a single screen, with one cell per block.
2. THE Web_UI SHALL indicate each block's Block_Status with both a text label and a color for `Intact`, `CRC failed`, `Reconstructed`, and `Unresolved`.
3. WHEN the user selects a block, THE Web_UI SHALL show the Block_Observation from each Capture, the differing offsets with observed values, the recorded data CRC, the chosen values, and the reason for the selection or the Unresolved_Reason.
4. THE Web_UI SHALL provide **Try sample**, **Download report**, and **Download tape image** controls.
5. WHILE `.uef` export is not permitted, THE Web_UI SHALL disable **Download tape image** and display the blocking block numbers and reasons.
6. THE Web_UI SHALL display a link to JSBeeb and instructions to load the downloaded `.uef` with the emulator's local tape picker, select `*TAPE`, and run `CHAIN "STARMAP"`.
7. WHEN the API_Server returns a validation, timeout, or incompatibility error, THE Web_UI SHALL display the error message for the affected Capture.

### Requirement 19: Privacy and safe operation

**User Story:** As a user, I want my recordings kept private, so that unpublished programs are not exposed.

#### Acceptance Criteria

1. THE Web_UI SHALL load no third-party tracking scripts.
2. THE TapeRescue SHALL keep user Captures on the API_Server and transmit user Captures to no emulator or third-party service.
3. THE TapeRescue container image SHALL exclude BBC Micro ROM files and the JSBeeb emulator.
4. THE TapeRescue repository SHALL bundle only project-authored demo fixtures.

### Requirement 20: Kiro hook and spec artifacts

**User Story:** As a developer, I want recovery tests to rerun automatically when core logic changes, so that regressions are caught during development.

#### Acceptance Criteria

1. THE TapeRescue repository SHALL contain the Kiro Feature Spec for this feature under `.kiro/specs/tape-rescue/`.
2. WHEN a file in the Decoder_Adapter or Recovery_Engine source directories is saved, THE Recovery_Test_Hook SHALL run the recovery test suite.
3. THE Verification_Log SHALL record at least one example linking a requirement, the generated test, a failure found by that test, and the resulting code change.

### Requirement 21: Documentation, license, and notices

**User Story:** As a judge, I want local commands, a walkthrough, and clear licensing, so that I can run and evaluate the project.

#### Acceptance Criteria

1. THE TapeRescue repository SHALL contain an open-source LICENSE file at the root.
2. THE TapeRescue repository SHALL contain a third-party notices file listing AtomTapeUtils with its license and revision, and every bundled third-party dependency with its license.
3. THE TapeRescue README SHALL document local commands to build the Decoder, generate fixtures, run tests, run the CLI end-to-end recovery, run the benchmark, and start the web application.
4. THE TapeRescue README SHALL include a judge walkthrough following the demo sequence: failed block 6 in both recordings, byte disagreements and recorded CRC, reconstruction with provenance, and `.uef` download for JSBeeb.
5. THE TapeRescue README SHALL label STARMAP fixtures as generated and state that real-archive results are claimed only after testing with permission-cleared captures.

### Requirement 22: Verification log and pending external items

**User Story:** As a project owner, I want every task's evidence and every outstanding external step recorded, so that no result is claimed without proof.

#### Acceptance Criteria

1. WHEN a task is completed, THE Verification_Log SHALL record the commands run, the results observed, and any blockers for that task.
2. THE Verification_Log SHALL contain a Pending_Item with an evidence placeholder for each external step not yet performed: running the clean `.uef` in JSBeeb, running the recovered `.uef` in JSBeeb, testing with real archival captures, the Quadbike comparison when not runnable, deployment to a functional URL, and the YouTube demo.
3. WHILE a Pending_Item has no recorded evidence, THE TapeRescue documentation SHALL describe the corresponding step as pending.
4. WHEN Milestone 2 work begins, THE Verification_Log SHALL already record the Milestone 1 exit checks: the clean WAV to Block_Observations to `.uef` path, and block 6 raw payload and recorded data CRC exposed for both damaged fixtures.
