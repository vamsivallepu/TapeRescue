# TapeRescue

**Kiroween 2026 · Resurrection · Development brief for Kiro**

## Product

TapeRescue recovers vintage software from multiple imperfect recordings of the same data cassette. The target users are software preservation volunteers, small computer museums, and people digitizing their own unpublished programs. A Stardot member described making repeated transfers and manually switching between files to recover surviving blocks. TapeRescue automates that workflow and adds a way to reconstruct certain blocks that fail in every recording. [Stardot thread](https://stardot.org.uk/forums/viewtopic.php?t=26873)

**Pitch.** Two recordings fail to load an old program. TapeRescue shows which bytes survived in each, rebuilds the program, and produces a tape image you can run again.

**Scope.** Start with two PCM WAV captures of the same single, unprotected program recorded in the standard BBC Micro 1200-baud cassette format. Support 44.1 kHz, 16-bit mono first. Accept a single capture for inspection and as a benchmark baseline. Produce a new `.uef` tape image and a downloadable recovery report. Defer other formats, arbitrary waveform alignment, copy-protected loaders, resampling, and recovery of headers or missing byte positions. Keep the decoder boundary small enough to add formats later if needed.

## How recovery works

Each cassette block has a numbered header and a header CRC. A nonempty block also has data and a separate 16-bit data CRC. The header CRC checks the identification and layout fields. The data CRC checks the payload. For example, two reads of the same block might decode as `[10, 99, 30, 40]` and `[10, 20, 88, 40]`. If the only combination that matches the original data CRC is `[10, 20, 30, 40]`, the recorded evidence for byte two came from the second read and for byte three from the first. A CRC match is evidence, and the report must preserve that distinction. [BBC Micro User Guide](https://www.bbcmicrobot.com/docs/BBC_User_Guide.pdf)

### Decoder output

Start with MIT-licensed AtomTapeUtils at revision `98d1a8d1bed00aa22f1d72e3d5e91a254a69ec64` and retain its notices. `ScanTape -bbm` supports BBC Micro 1200 baud. Add a small machine-readable output mode at the block decoder boundary or wrap the decoder directly. The existing CLI can zero-pad partial data when assembling a file. Read the raw observations before file assembly. [Repository and license](https://github.com/joakimoh/AtomTapeUtils/tree/98d1a8d1bed00aa22f1d72e3d5e91a254a69ec64) · [1200-baud option](https://github.com/joakimoh/AtomTapeUtils/blob/98d1a8d1bed00aa22f1d72e3d5e91a254a69ec64/ScanTape/ArgParser.cpp#L29) · [Block decoder](https://github.com/joakimoh/AtomTapeUtils/blob/98d1a8d1bed00aa22f1d72e3d5e91a254a69ec64/shared/BlockDecoder.cpp#L136) · [File assembly](https://github.com/joakimoh/AtomTapeUtils/blob/98d1a8d1bed00aa22f1d72e3d5e91a254a69ec64/shared/FileDecoder.cpp#L204)

Emit a capture ID derived from the WAV digest, a block time or sample offset, raw header bytes and header fields, raw payload bytes, expected and actual payload lengths, original header/data CRC values when readable, and the validation outcome for each CRC. Preserve incomplete reads as incomplete. Distinguish empty payloads from truncated data. The recovery engine needs full, position-aligned payloads and the original observed CRC, including for reads whose payload CRC fails. Validate that the decoder can produce both on the fault-injected WAV before expanding the scope. If it cannot, expose the required observations at a lower layer and record which failure modes remain unsupported.

### Selection and reconstruction

1. Parse each recording independently. Identity candidates must have a valid header. Match observations for the same block using their block number and matching header fields, including cassette filename, load and execution addresses for that block, length, and flags. Use matching complete, CRC-valid blocks as anchors before pairing captures. Require at least one shared anchor for the MVP and reject conflicting intact payloads or trusted headers. Ask the user to select recordings of the same program version. A shared anchor gives evidence that the captures belong together.
2. Prefer a complete payload that passes its recorded data CRC. If two intact reads agree, retain both as evidence. If intact reads disagree, report a conflict instead of deciding by upload order. Treat duplicate WAV content as one capture in the report. Keep valid empty blocks where the file format permits them.
3. If neither copy passes its data CRC, try reconstruction only when both headers are valid and agree, both payloads are full and aligned, and both reads contain the same original data CRC value. Keep agreed bytes and enumerate distinct combinations of the recorded values at the positions that disagree. Cap at **64 candidates per block** for the MVP. If the full candidate space exceeds the cap, mark the block unresolved. Never pick the first CRC match or claim uniqueness from a partial search.
4. Compute CRC-16/XMODEM for each candidate using the BBC format parameters and compare it to the original captured data CRC. With exactly one matching candidate, label the block **Reconstructed · CRC match**. Record each differing offset, the value chosen, and the supporting capture. If there are no matches, multiple matches, unreadable CRCs, or missing data needed for the search, show the reason and leave the block unresolved.
5. Permit `.uef` export only when all blocks from zero through the validated final block are present, the sequence and headers are consistent, and each block is intact or successfully reconstructed. Retain the source bytes and CRC evidence. Validate the exported blocks with an independent reader and run the demo program in JSBeeb. Surface reconstructed blocks to the user before download. An incomplete session can still export its report.

The report should include the algorithm version and candidate cap, input digests, each block's status and source offsets, the recorded checksum and repair candidate count where relevant, reconstruction details, and the recovered payload's digest when complete. Stable evidence and payload results should survive reordering of the uploads. Original recordings stay unchanged.

## Implementation milestones

### 1. Prove the audio and tape-image path

- Set up the new project and Kiro Feature Spec. Build the pinned decoder locally. Establish an ordinary command-line path to decode one WAV and output structured block observations.
- Author an original BBC BASIC program called `STARMAP` that draws a star map. Make the tape fixture span eight genuine data blocks, numbered 0–7, with the program designed to run using the recovered block 6 (`0x0006`, the seventh block). Generate its clean BBC-format WAV and retain the known original payload or a digest per block. Verify there is an actual final-block marker and that the program runs in JSBeeb.
- Create two reproducible test WAVs from that clean fixture. Change two payload bit values in distinct bytes in block 6 of each capture, using disjoint positions across the two. Change the encoded byte frames before modulation, or replace their corresponding tone spans while keeping framing and bit timing valid. Preserve the original data CRC bytes in the damaged recordings. Verify the changes are visible at the WAV decoder output and that they cause both single reads to fail block 6.
- Wire an intact-block-based `.uef` export for the clean capture. Open the generated image in JSBeeb, select tape filing mode with `*TAPE`, and run `CHAIN "STARMAP"`. Use the emulator's local tape image picker for judge uploads. Avoid redistributing ROMs and keep the emulator out of the app bundle for this milestone. [JSBeeb emulator](https://bbc.xania.org/) · [UEF format registry](https://pronom.nationalarchives.gov.uk/fmt/792)

**Exit criterion.** Show the clean `WAV → block observations → UEF → running STARMAP` path. Show that both damaged WAVs expose the failed block's raw payload and original CRC. A byte-array-only test establishes the reconstruction logic separately from this milestone.

### 2. Build and verify the recovery engine

- Implement intact selection, bounded byte reconstruction, ambiguous and incomplete outcomes, deterministic provenance, and JSON export. Keep the merge logic independent of WAV decoding and the UI.
- Wire the two damaged WAVs through recovery. Verify the recovered program payload byte-for-byte against the clean reference and run the exported program in the emulator. Add the core regression and property tests below.
- Benchmark the same inputs using the best single capture and intact-block merge. Quadbike already offers experimental single-bit repair via `lsblocks.php +r`. Measure its result on the same fixture or document why the comparison could not run. Describe the project's contribution as **reconstruction across captures with byte provenance**. [Quadbike manual](https://beebstuff.uk/w/acorn/qbdocs/latest/)

### 3. Ship a minimal web app

- Suggested stack is a Python/FastAPI wrapper and recovery engine, native AtomTapeUtils binary, and a Vite/React TypeScript UI. Use a single deployable container if it simplifies building the native decoder and serving the app. Keep the CLI as a reproducible path for fixtures and verification. No database or account setup is needed for the MVP.
- Put two capture strips and the merged result on one screen. Use text and color for **Intact**, **CRC failed**, **Reconstructed**, and **Unresolved**. Selecting a block shows the two observations, the chosen values, and why they were selected. Include **Try sample**, **Download report**, **Download tape image**, and a clear link and instructions to run it in JSBeeb.
- Validate actual WAV encoding, bound file size and duration, enforce a decoder timeout, and clean up per-session temporary files. Use generated paths and invoke the native decoder with an argument list. Give clear messages for unsupported, incomplete, and incompatible inputs. Do not upload user captures to the emulator or embed third-party trackers. Bundle the project-authored demo so judges can try the full workflow without credentials.

### 4. Validate impact and prepare the submission

- Seek a permission-cleared pair of real archival captures early. If available, report blocks recovered by each baseline and by TapeRescue, include whether the program loads, and record any independent ground truth. Label generated fixtures clearly. Claim a real recovery only after testing it, and publish a capture only with the necessary rights.
- Keep the Kiro Feature Spec and implementation tasks in `.kiro/specs`. Add a Kiro hook that reruns recovery tests when the decoder adapter or merge logic changes. Record an actual example of a requirement, generated test, failure found, and resulting change for the write-up. [Kiro Specs](https://kiro.dev/docs/specs/) · [Kiro property-based testing](https://kiro.dev/docs/specs/correctness/) · [Kiro hooks](https://kiro.dev/docs/hooks/)
- Prepare a functional URL, public GitHub repo with the source, open-source license, `.kiro` at the root, third-party notices and disclosures, and a README with local commands and a judge walkthrough. Prepare the Kiro write-up and a public YouTube demo under three minutes. Record the demo with your own program, fixtures, and assets. [Kiroween 2026 rules](https://kiroween.devpost.com/rules)

## Test fixtures and acceptance checks

Use the following payloads for unit tests independently of the complete `STARMAP` WAV fixture. Create 256 bytes using `payload[i] = (17*i + 11*i*i + 42) mod 256`, with zero-based offsets. The initial data CRC is `0xAC93` with CRC-16/XMODEM. The standard text vector `123456789` yields `0x31C3`.

| Case | Input or procedure | Expected result |
| --- | --- | --- |
| Intact block selection | A has a valid block 4 and bad block 6. B has bad block 4 and valid block 6. | Blocks 4 and 6 are intact and sourced from A and B respectively. |
| Shared failure with a unique repair | Copy the payload twice. In A, XOR offsets 23 and 128 with `0x02` and `0x08`. In B, XOR offsets 75 and 241 with `0x01` and `0x10`. Retain `0xAC93` in both reads. | Both reads fail. One of 16 combinations passes and is byte-for-byte equal to the original. A supplies offsets 75 and 241. B supplies offsets 23 and 128. |
| Same error in both reads | Add an XOR of `0x04` at offset 7 of both copies in the preceding case. | Zero candidates match. Block stays unresolved. |
| CRC collision | Start from fresh copies. In A, XOR offset 20 with `0x01`. In B, XOR offsets 21 and 22 with `0x10` and `0x21`. Retain `0xAC93`. | Both reads fail. Two of eight candidates match. Block stays unresolved. |
| Search cap | Supply two full reads with seven differing bytes and the same recorded CRC. | 128 combinations exceed the cap. Block stays unresolved even when a match would exist. |
| Missing evidence | Supply a truncated read, missing CRC, incompatible headers, or conflicting intact reads. | The relevant block is rejected or marked unresolved. Missing bytes and checksums are never silently filled. |
| End-to-end sample | Process the two damaged `STARMAP` WAVs, export `.uef`, parse it, and run the resulting program. | Both single inputs and intact-only merge fall short. The full repair matches the known original program bytes and the star map draws. |

Property-test that input order leaves the payload and evidence unchanged, every reconstructed byte can be traced to an observed value at its offset, a unique candidate is never claimed for a skipped or incomplete search, and unresolved required blocks prevent complete export. Independently validate the CRCs read from the captured input when testing export. Recalculating a new CRC for the output only tests internal consistency.

## Demo sequence

1. Open on the failed block. “Neither recording can load this program. Both fail on block 6.”
2. Load the two WAVs. Show the same failed block in both strips and click it to reveal the byte disagreements and the original recorded CRC.
3. Recover the program. Show where the repaired bytes came from, the successful comparison with the known original, and the completed block strip.
4. Download the recovered `.uef` and run `STARMAP` in the emulator. Close with the real-world test result if available, plus a short explanation of what Kiro changed in the implementation.
