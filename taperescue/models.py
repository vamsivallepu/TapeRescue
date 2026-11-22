"""Shared, immutable data models used by every TapeRescue layer.

This module has no dependencies on the decoder, WAV handling, or web code.
All dataclasses are frozen. Payload bytes are stored exactly as observed:
an incomplete read is kept at its actual length and never padded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from taperescue.engine.reconstruct import ReconstructionOutcome

CrcOutcome = Literal["valid", "invalid", "unreadable"]

BlockStatus = Literal[
    "Intact",
    "CRC failed",
    "Reconstructed · CRC match",
    "Unresolved",
    "Conflict",
    "Missing",
]

UnresolvedReason = Literal[
    "no_match",
    "multiple_matches",
    "cap_exceeded",
    "crc_unreadable",
    "crc_mismatch_between_reads",
    "truncated_payload",
    "header_invalid",
    "header_disagreement",
    "single_read",
    "block_missing",
]

PayloadState = Literal["complete", "truncated", "empty"]

CRC_OUTCOMES: tuple[str, ...] = ("valid", "invalid", "unreadable")
RECOVERED_STATUSES: tuple[str, ...] = ("Intact", "Reconstructed · CRC match")


@dataclass(frozen=True)
class HeaderFields:
    """Parsed BBC cassette block header."""

    filename: str
    load_addr: int
    exec_addr: int
    block_no: int
    block_len: int
    flags: int
    next_addr: int

    def identity_key(self) -> tuple:
        """Fields matched across captures. ``next_addr`` is reported but not matched."""
        return (
            self.filename,
            self.load_addr,
            self.exec_addr,
            self.block_no,
            self.block_len,
            self.flags,
        )


@dataclass(frozen=True)
class CrcReading:
    """A header or data CRC as read from tape.

    ``recorded`` is ``None`` if and only if ``outcome == "unreadable"``.
    """

    outcome: CrcOutcome
    recorded: int | None

    def __post_init__(self) -> None:
        if self.outcome not in CRC_OUTCOMES:
            raise ValueError(f"unknown CRC outcome {self.outcome!r}")
        if self.outcome == "unreadable":
            if self.recorded is not None:
                raise ValueError("an unreadable CRC must not carry a recorded value")
        else:
            if self.recorded is None:
                raise ValueError(f"a CRC with outcome {self.outcome!r} needs a recorded value")
            if isinstance(self.recorded, bool) or not isinstance(self.recorded, int):
                raise ValueError("recorded CRC must be an int")
            if not 0 <= self.recorded <= 0xFFFF:
                raise ValueError(f"recorded CRC {self.recorded} is outside 0..0xFFFF")


@dataclass(frozen=True)
class BlockObservation:
    """One block as decoded from one capture, before any file assembly."""

    capture_id: str
    sample_offset: int
    header_raw: bytes
    header: HeaderFields | None  # None when the header CRC is not valid
    header_crc: CrcReading
    payload: bytes
    expected_len: int
    actual_len: int
    data_crc: CrcReading | None  # None for an Empty_Payload

    def __post_init__(self) -> None:
        if not isinstance(self.payload, bytes) or not isinstance(self.header_raw, bytes):
            raise ValueError("payload and header_raw must be bytes")
        if self.expected_len < 0 or self.actual_len < 0:
            raise ValueError("lengths must be non-negative")
        if len(self.payload) != self.actual_len:
            raise ValueError(
                f"payload holds {len(self.payload)} bytes but actual_len is {self.actual_len}"
            )
        if self.actual_len > self.expected_len:
            raise ValueError(
                f"actual_len {self.actual_len} exceeds expected_len {self.expected_len}"
            )

    @property
    def payload_state(self) -> PayloadState:
        """``empty`` for a zero-length block with a valid header, ``truncated`` when
        fewer bytes than expected were read, otherwise ``complete``."""
        if self.expected_len == 0 and self.header_crc.outcome == "valid":
            return "empty"
        if self.actual_len < self.expected_len:
            return "truncated"
        return "complete"

    @property
    def is_intact(self) -> bool:
        """Valid header CRC and either an empty payload, or a complete payload
        with a valid data CRC."""
        if self.header_crc.outcome != "valid":
            return False
        state = self.payload_state
        if state == "empty":
            return True
        return (
            state == "complete"
            and self.data_crc is not None
            and self.data_crc.outcome == "valid"
        )


@dataclass(frozen=True)
class CaptureObservations:
    """All block observations from one capture."""

    capture_id: str  # SHA-256 hex of the WAV bytes
    observations: tuple[BlockObservation, ...]

    def __post_init__(self) -> None:
        for obs in self.observations:
            if obs.capture_id != self.capture_id:
                raise ValueError(
                    f"observation capture_id {obs.capture_id!r} does not match "
                    f"capture {self.capture_id!r}"
                )


@dataclass(frozen=True)
class RecoverOptions:
    reconstruct: bool = True
    candidate_cap: int = 64


@dataclass(frozen=True)
class BlockResult:
    block_no: int
    status: BlockStatus
    reason: UnresolvedReason | None
    payload: bytes | None  # set only for Intact and Reconstructed
    supporting_capture_ids: tuple[str, ...]
    reads: tuple[BlockObservation, ...]
    conflict: dict | None  # {"kind": "header"|"payload", "capture_ids": [...]}
    reconstruction: ReconstructionOutcome | None

    @property
    def is_recovered(self) -> bool:
        return self.status in RECOVERED_STATUSES and self.payload is not None


@dataclass(frozen=True)
class RecoveryResult:
    captures: tuple[CaptureObservations, ...]  # sorted by capture_id, deduplicated
    duplicates_merged: tuple[str, ...]
    anchors: tuple[int, ...]
    final_block: int | None
    blocks: tuple[BlockResult, ...]  # sorted by block_no
    options: RecoverOptions

    def program_payload(self) -> bytes | None:
        """Concatenated payload of blocks 0..final_block, or ``None`` unless a final
        block is known and every one of those blocks is Intact or Reconstructed."""
        if self.final_block is None:
            return None
        by_no: dict[int, BlockResult] = {}
        for block in self.blocks:
            if block.block_no in by_no:
                return None
            by_no[block.block_no] = block
        parts: list[bytes] = []
        for n in range(self.final_block + 1):
            block = by_no.get(n)
            if block is None or not block.is_recovered:
                return None
            parts.append(block.payload)  # type: ignore[arg-type]
        return b"".join(parts)
