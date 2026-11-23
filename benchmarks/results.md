# Inputs: generated STARMAP fixtures

Baseline benchmark for TapeRescue. The inputs are the generated STARMAP fixtures, not real archival captures:

- `damaged_a.wav` sha256 `2e09d3ce8d3bd4b042958c15dfb9d2d5a08774cf28ecbcff49e3bf06339d4c0b`
- `damaged_b.wav` sha256 `83a0ab0428d8299299f810f8ceb1df25c5781b7f8b8e29b34cb34794c735d0a0`

Engine `taperescue-merge/1.0.0`, candidate cap 64, decoder AtomTapeUtils ScanTape 98d1a8d1bed00aa22f1d72e3d5e91a254a69ec64 with patches/atomtapeutils-json-observations.patch. Reference program sha256 `4ac213a0e014229eb195bd7380962b9f4528147838ec9f1f7cb00be165e06e3a`.

| Mode | Captures | Recovered blocks | Complete program | Matches reference | Unrecovered blocks |
| --- | --- | --- | --- | --- | --- |
| Best single capture | damaged_a.wav | 7 of 8 | no | n/a | 6 (CRC failed, single_read) |
| Intact-block merge | damaged_a.wav, damaged_b.wav | 7 of 8 | no | n/a | 6 (CRC failed) |
| TapeRescue reconstruction | damaged_a.wav, damaged_b.wav | 8 of 8 | yes | yes | none |

## Quadbike `lsblocks.php +r`

Run with Quadbike 2.0.5, lsblocks.php v1.2. Each WAV is transcribed to TIBET by Quadbike, then listed by `lsblocks.php +r`. The table and the metrics line are printed before the `+r` bit-flip repair runs.

| Capture | Ran | Block rows | Rows without errors | Bit-flip repair | Recovered after repair | Metrics line |
| --- | --- | --- | --- | --- | --- | --- |
| damaged_a.wav | yes (exit 0) | 8 | 7 | block 6 failed | 7 of 8 | Errors 1, Blocks 8 |
| damaged_b.wav | yes (exit 0) | 8 | 7 | block 6 failed | 7 of 8 | Errors 1, Blocks 8 |

## Contribution

TapeRescue's contribution is reconstruction across captures with byte provenance. A block that fails its data CRC in every capture is rebuilt only from bytes observed at each offset, only when exactly one combination matches the recorded CRC, and the report names the capture that supplied each chosen byte. A CRC match is evidence, not proof of the original bytes.
