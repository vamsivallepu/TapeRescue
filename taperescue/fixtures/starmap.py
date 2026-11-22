"""STARMAP fixture generator.

:func:`generate` turns ``fixtures/starmap/STARMAP.bas`` into the files the
rest of the project tests against:

* ``clean.wav``: the tokenized program as eight 1200-baud cassette blocks;
* ``damaged_a.wav`` and ``damaged_b.wav``: the same recording with two
  single-bit payload flips in block 6, at offsets that differ between the two
  fixtures. The recorded data CRC is the original one;
* ``reference_payloads.json``: per-block payload hex, SHA-256, header fields
  and recorded CRCs, the whole-program SHA-256, and the WAV digests;
* ``damage.json``: the flips with original and damaged byte values, and the
  result of the candidate check below.

Layout rules (Requirements 4.3 and 4.6):

* the tokenized program is 1793 to 2048 bytes, so it fills exactly eight
  blocks and block 7 holds real program bytes;
* the line ``DEF PROCconstellations`` and every line through its
  ``ENDPROC`` lie inside payload offsets 1536 to 1791, which is block 6. The
  main program calls the procedure before ``END``, so it cannot run to the end
  without block 6.

If the source does not meet these rules on its own, ``REM`` lines are added:
before the ``DEF`` line to move it up to offset 1536, and after the last line
to reach 1793 bytes. Anything else (too long, procedure too large) raises
:class:`StarmapLayoutError`. The padding used is recorded in
``reference_payloads.json``; the checked-in source needs none.

Damage plan (Requirements 5.2 and 5.5): A flips offset 17 bit 2 and offset
143 bit 5; B flips offset 64 bit 0 and offset 201 bit 6. The plan is accepted
only if the A and B offsets are disjoint, both damaged payloads fail the
original data CRC, and exactly one of the 16 combinations of original and
damaged values at the four offsets matches it (that one is the original
payload). Otherwise the next plan from a fixed, seeded fallback list is tried
and the choice is recorded in ``damage.json``.

This module must not import ``taperescue.decoder_adapter`` or run the decoder
(Requirement 4.8).
"""
from __future__ import annotations

import hashlib
import itertools
import json
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from taperescue.crc import crc16_xmodem
from taperescue.fixtures.block_builder import BLOCK_SIZE, TapeBlock, block_frames, build_blocks
from taperescue.fixtures.damage import BitFlip, apply_damage
from taperescue.fixtures.fsk import BBC_TIMING, TapeTiming, modulate, tape_stream
from taperescue.fixtures.tokenizer import detokenize, tokenize

__all__ = [
    "BLOCK_COUNT",
    "DAMAGED_BLOCK",
    "DEFAULT_SOURCE",
    "FILENAME",
    "PLANNED_A",
    "PLANNED_B",
    "DamagePlan",
    "GeneratedFixtures",
    "StarmapLayoutError",
    "StarmapProgram",
    "build_program",
    "check_plan",
    "damaged_payload",
    "fallback_plans",
    "generate",
    "program_lines",
    "select_damage",
]

FILENAME = "STARMAP"
BLOCK_COUNT = 8
DAMAGED_BLOCK = 6
PROC_LINE = "DEF PROCconstellations"
PROC_FIRST = DAMAGED_BLOCK * BLOCK_SIZE            # 1536
PROC_LAST = PROC_FIRST + BLOCK_SIZE - 1             # 1791
MIN_LENGTH = (BLOCK_COUNT - 1) * BLOCK_SIZE + 1     # 1793
MAX_LENGTH = BLOCK_COUNT * BLOCK_SIZE               # 2048
MIN_REM_LINE = 5                                    # 0x0D, number, length, REM token
MAX_REM_LINE = 255

DEFAULT_SOURCE = Path(__file__).resolve().parents[2] / "fixtures" / "starmap" / "STARMAP.bas"

PLANNED_A: tuple[tuple[int, int], ...] = ((17, 2), (143, 5))
PLANNED_B: tuple[tuple[int, int], ...] = ((64, 0), (201, 6))
FALLBACK_SEED = 0x5747
FALLBACK_COUNT = 64
_LINE_NUMBER_RE = re.compile(r"\s*(\d+)")


class StarmapLayoutError(ValueError):
    """The STARMAP program cannot be laid out as eight blocks with PROCconstellations in block 6."""


@dataclass(frozen=True)
class ProgramLine:
    number: int
    start: int   # offset of the 0x0D byte
    end: int     # offset just past the line
    text: str    # detokenized body


def program_lines(program: bytes) -> list[ProgramLine]:
    """Offsets and detokenized text of every line in a tokenized program."""
    texts = detokenize(program).splitlines()
    lines = []
    i = 0
    for text in texts:
        number = (program[i + 1] << 8) | program[i + 2]
        length = program[i + 3]
        body = text[len(str(number)):].removeprefix(" ")
        lines.append(ProgramLine(number, i, i + length, body))
        i += length
    return lines


def _proc_range(lines: Sequence[ProgramLine]) -> tuple[int, int]:
    """Index of the DEF PROCconstellations line and of its ENDPROC line."""
    defs = [k for k, line in enumerate(lines) if line.text.strip() == PROC_LINE]
    if len(defs) != 1:
        raise StarmapLayoutError(f"expected one {PROC_LINE!r} line, found {len(defs)}")
    first = defs[0]
    for k in range(first + 1, len(lines)):
        if lines[k].text.strip() == "ENDPROC":
            return first, k
    raise StarmapLayoutError(f"{PROC_LINE!r} has no ENDPROC line")


def _check_call_before_end(lines: Sequence[ProgramLine], def_index: int) -> None:
    """The main program must call PROCconstellations on a line before its first END."""
    end_index = next((k for k, line in enumerate(lines) if line.text.strip() == "END"), None)
    if end_index is None or end_index > def_index:
        raise StarmapLayoutError("the main program must end with an END line before the procedures")
    calls = [k for k in range(end_index)
             if re.search(r"\bPROCconstellations\b", lines[k].text)]
    if not calls:
        raise StarmapLayoutError("the main program does not call PROCconstellations before END")


def _rem_lengths(gap: int) -> list[int]:
    """Split ``gap`` bytes (at least MIN_REM_LINE) into REM line lengths of 5 to 255."""
    count = -(-gap // MAX_REM_LINE)
    base, extra = divmod(gap, count)
    return [base + (1 if k < extra else 0) for k in range(count)]


def _rem_source(numbers: Sequence[int], lengths: Sequence[int]) -> list[str]:
    return [f"{n} REM" + "-" * (length - MIN_REM_LINE) for n, length in zip(numbers, lengths)]


def _insert_padding(source_lines: list[str], before_number: int | None, after_number: int,
                    lengths: Sequence[int]) -> tuple[list[str], list[dict]]:
    numbers = [after_number + 1 + k for k in range(len(lengths))]
    if before_number is not None and numbers and numbers[-1] >= before_number:
        raise StarmapLayoutError(
            f"no free line numbers between {after_number} and {before_number} for padding"
        )
    if numbers and numbers[-1] > 32767:
        raise StarmapLayoutError("no free line numbers after the last line for padding")
    pad = _rem_source(numbers, lengths)
    index = next(
        (k for k, raw in enumerate(source_lines)
         if (m := _LINE_NUMBER_RE.match(raw)) and int(m.group(1)) > after_number),
        len(source_lines),
    )
    records = [{"line": n, "length": length} for n, length in zip(numbers, lengths)]
    return source_lines[:index] + pad + source_lines[index:], records


@dataclass(frozen=True)
class StarmapProgram:
    source: str                      # source text actually tokenized, padding included
    program: bytes
    blocks: tuple[TapeBlock, ...]
    proc_start: int                  # offset of the DEF PROCconstellations line
    proc_end: int                    # offset just past its ENDPROC line
    padding: tuple[dict, ...]        # REM lines added: {"line", "length", "where"}


def build_program(source: str) -> StarmapProgram:
    """Tokenize STARMAP, pad with REM lines if needed, and split it into eight blocks."""
    source_lines = source.splitlines()
    padding: list[dict] = []

    program = tokenize(source)
    lines = program_lines(program)
    first, last = _proc_range(lines)
    gap = PROC_FIRST - lines[first].start
    if gap > 0:
        lengths = _rem_lengths(max(gap, MIN_REM_LINE))
        before = lines[first].number
        after = lines[first - 1].number if first > 0 else -1
        source_lines, records = _insert_padding(source_lines, before, after, lengths)
        padding += [dict(r, where="before_proc") for r in records]
        program = tokenize("\n".join(source_lines) + "\n")
        lines = program_lines(program)
        first, last = _proc_range(lines)

    if len(program) < MIN_LENGTH:
        lengths = _rem_lengths(max(MIN_LENGTH - len(program), MIN_REM_LINE))
        source_lines, records = _insert_padding(source_lines, None, lines[-1].number, lengths)
        padding += [dict(r, where="end") for r in records]
        program = tokenize("\n".join(source_lines) + "\n")
        lines = program_lines(program)
        first, last = _proc_range(lines)

    _check_call_before_end(lines, first)
    proc_start, proc_end = lines[first].start, lines[last].end
    if proc_start < PROC_FIRST or proc_end - 1 > PROC_LAST:
        raise StarmapLayoutError(
            f"{PROC_LINE} occupies offsets {proc_start}..{proc_end - 1}, "
            f"which is not inside block {DAMAGED_BLOCK} ({PROC_FIRST}..{PROC_LAST})"
        )
    if not MIN_LENGTH <= len(program) <= MAX_LENGTH:
        raise StarmapLayoutError(
            f"tokenized program is {len(program)} bytes; {BLOCK_COUNT} blocks need "
            f"{MIN_LENGTH} to {MAX_LENGTH}"
        )
    blocks = build_blocks(program, filename=FILENAME)
    if len(blocks) != BLOCK_COUNT:
        raise StarmapLayoutError(f"program splits into {len(blocks)} blocks, expected {BLOCK_COUNT}")
    padded_source = "\n".join(source_lines) + "\n"
    return StarmapProgram(padded_source, program, tuple(blocks), proc_start, proc_end,
                          tuple(padding))


# --- damage plan -----------------------------------------------------------

@dataclass(frozen=True)
class DamagePlan:
    a: tuple[tuple[int, int], ...]   # (offset, bit) pairs for fixture A
    b: tuple[tuple[int, int], ...]
    source: str                      # "planned" or "fallback"
    fallback_index: int | None


def fallback_plans(payload_len: int, seed: int = FALLBACK_SEED,
                   count: int = FALLBACK_COUNT) -> list[DamagePlan]:
    """Fixed list of alternative plans: four distinct offsets and four bits per plan."""
    rng = random.Random(seed)
    plans = []
    for index in range(count):
        offsets = sorted(rng.sample(range(payload_len), 4))
        rng.shuffle(offsets)
        bits = [rng.randrange(8) for _ in range(4)]
        a = tuple(sorted(zip(offsets[:2], bits[:2])))
        b = tuple(sorted(zip(offsets[2:], bits[2:])))
        plans.append(DamagePlan(a, b, "fallback", index))
    return plans


def damaged_payload(payload: bytes, flips: Sequence[tuple[int, int]]) -> bytes:
    out = bytearray(payload)
    for offset, bit in flips:
        out[offset] ^= 1 << bit
    return bytes(out)


def check_plan(payload: bytes, crc: int, plan: DamagePlan) -> list[str]:
    """Return the reasons ``plan`` is unusable for ``payload`` (empty when it is usable)."""
    problems = []
    offsets_a = [o for o, _ in plan.a]
    offsets_b = [o for o, _ in plan.b]
    all_offsets = offsets_a + offsets_b
    if any(not 0 <= o < len(payload) for o in all_offsets):
        return ["offset outside the payload"]
    if len(set(offsets_a)) != len(offsets_a) or len(set(offsets_b)) != len(offsets_b):
        problems.append("an offset is flipped twice within one fixture")
    if set(offsets_a) & set(offsets_b):
        problems.append("A and B offsets overlap")
    if problems:
        return problems
    for name, flips in (("A", plan.a), ("B", plan.b)):
        if crc16_xmodem(damaged_payload(payload, flips)) == crc:
            problems.append(f"damaged payload {name} still matches the data CRC")
    matches = _matching_combinations(payload, crc, plan.a + plan.b)
    if matches != 1:
        problems.append(f"{matches} of 16 combinations match the data CRC, expected 1")
    return problems


def _matching_combinations(payload: bytes, crc: int, flips: Sequence[tuple[int, int]]) -> int:
    matches = 0
    for choice in itertools.product((False, True), repeat=len(flips)):
        candidate = damaged_payload(payload, [f for f, use in zip(flips, choice) if use])
        if crc16_xmodem(candidate) == crc:
            matches += 1
    return matches


def select_damage(payload: bytes, crc: int) -> tuple[DamagePlan, list[dict]]:
    """Return the first usable plan (planned, then fallbacks) and the rejected plans."""
    rejected: list[dict] = []
    candidates = [DamagePlan(PLANNED_A, PLANNED_B, "planned", None)]
    candidates += fallback_plans(len(payload))
    for plan in candidates:
        problems = check_plan(payload, crc, plan)
        if not problems:
            return plan, rejected
        rejected.append({"source": plan.source, "fallback_index": plan.fallback_index,
                         "a": [list(f) for f in plan.a], "b": [list(f) for f in plan.b],
                         "problems": problems})
    raise StarmapLayoutError("no usable damage plan in the planned or fallback lists")


# --- output ----------------------------------------------------------------

@dataclass(frozen=True)
class GeneratedFixtures:
    out_dir: Path
    program: StarmapProgram
    plan: DamagePlan
    wav_sha256: dict[str, str]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="ascii")


def _flip_records(original: bytes, flips: Sequence[tuple[int, int]]) -> list[dict]:
    return [{"offset": o, "bit": b, "mask": 1 << b,
             "original": original[o], "damaged": original[o] ^ (1 << b)} for o, b in flips]


def _wav(blocks: Sequence[TapeBlock], timing: TapeTiming,
         flips: Sequence[tuple[int, int]] = ()) -> bytes:
    frame_lists = []
    for block in blocks:
        frames = block_frames(block)
        if block.block_no == DAMAGED_BLOCK and flips:
            frames = apply_damage(frames, DAMAGED_BLOCK,
                                  [BitFlip(DAMAGED_BLOCK, o, b) for o, b in flips])
        frame_lists.append(frames)
    return modulate(tape_stream(frame_lists, timing))


def generate(out_dir: Path | str, source: Path | str | None = None,
             timing: TapeTiming = BBC_TIMING) -> GeneratedFixtures:
    """Write the STARMAP fixtures into ``out_dir`` and return what was written.

    ``source`` defaults to ``fixtures/starmap/STARMAP.bas`` in the repository.
    The source file is copied into ``out_dir`` unless it is already there.
    Output depends only on the source text and ``timing``.
    """
    out = Path(out_dir)
    src_path = Path(source) if source is not None else DEFAULT_SOURCE
    text = src_path.read_text(encoding="ascii")
    starmap = build_program(text)
    blocks = starmap.blocks
    target = blocks[DAMAGED_BLOCK]
    assert target.data_crc is not None
    plan, rejected = select_damage(target.payload, target.data_crc)

    out.mkdir(parents=True, exist_ok=True)
    bas_out = out / "STARMAP.bas"
    if not (bas_out.exists() and bas_out.resolve() == src_path.resolve()):
        bas_out.write_text(text, encoding="ascii")

    wavs = {
        "clean.wav": _wav(blocks, timing),
        "damaged_a.wav": _wav(blocks, timing, plan.a),
        "damaged_b.wav": _wav(blocks, timing, plan.b),
    }
    for name, data in wavs.items():
        (out / name).write_bytes(data)
    digests = {name: _sha256(data) for name, data in wavs.items()}

    _write_json(out / "reference_payloads.json", {
        "filename": FILENAME,
        "load_addr": blocks[0].load_addr,
        "exec_addr": blocks[0].exec_addr,
        "program_length": len(starmap.program),
        "program_sha256": _sha256(starmap.program),
        "proc_constellations": {"first_offset": starmap.proc_start,
                                "last_offset": starmap.proc_end - 1,
                                "block_no": DAMAGED_BLOCK},
        "padding_lines": list(starmap.padding),
        "wav_sha256": digests,
        "blocks": [{
            "block_no": b.block_no,
            "block_len": b.block_len,
            "flags": b.flags,
            "next_addr": b.next_addr,
            "header_raw": b.header_raw.hex(),
            "header_crc": b.header_crc,
            "data_crc": b.data_crc,
            "hex": b.payload.hex(),
            "sha256": _sha256(b.payload),
        } for b in blocks],
    })

    payload = target.payload
    _write_json(out / "damage.json", {
        "block_no": DAMAGED_BLOCK,
        "data_crc": target.data_crc,
        "a": _flip_records(payload, plan.a),
        "b": _flip_records(payload, plan.b),
        "damaged_data_crc": {"a": crc16_xmodem(damaged_payload(payload, plan.a)),
                             "b": crc16_xmodem(damaged_payload(payload, plan.b))},
        "plan": {"source": plan.source, "fallback_index": plan.fallback_index,
                 "fallback_seed": FALLBACK_SEED, "rejected": rejected},
        "candidate_check": {"candidate_count": 2 ** (len(plan.a) + len(plan.b)),
                            "match_count": _matching_combinations(
                                payload, target.data_crc, plan.a + plan.b)},
        "wav_sha256": {"damaged_a.wav": digests["damaged_a.wav"],
                       "damaged_b.wav": digests["damaged_b.wav"]},
    })
    return GeneratedFixtures(out, starmap, plan, digests)
