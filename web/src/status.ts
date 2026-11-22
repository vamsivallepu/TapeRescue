// Text label and colour for each block status (design.md section 12, "Status
// display"; Requirement 18.2). Every cell shows the label as text, so colour
// is never the only signal. `textColour` is the text drawn on `colour`; each
// pair has a contrast ratio of at least 4.5:1 (checked in status.test.ts).

import type { BlockStatus, ReportBlock, ReportRead } from "./api.ts";

export interface StatusStyle {
  /** Short text shown in the cell. */
  label: string;
  /** Cell background. */
  colour: string;
  /** Cell text colour on `colour`. */
  textColour: string;
}

const WHITE = "#ffffff";
const DARK = "#1a1a1a";

const GREEN = "#2e7d32";
const RED = "#c62828";
const BLUE = "#1565c0";
const AMBER = "#ef6c00";
const PURPLE = "#6a1b9a";
const GREY = "#616161";

/** Merged-result status, from the design table. */
export const BLOCK_STATUS_STYLES: Readonly<Record<BlockStatus, StatusStyle>> = {
  Intact: { label: "Intact", colour: GREEN, textColour: WHITE },
  "CRC failed": { label: "CRC failed", colour: RED, textColour: WHITE },
  "Reconstructed · CRC match": { label: "Reconstructed", colour: BLUE, textColour: WHITE },
  // White on amber is about 3:1, below the 4.5:1 needed for small text.
  Unresolved: { label: "Unresolved", colour: AMBER, textColour: DARK },
  Conflict: { label: "Conflict", colour: PURPLE, textColour: WHITE },
  Missing: { label: "Missing", colour: GREY, textColour: WHITE },
};

export function blockStatusStyle(status: BlockStatus): StatusStyle {
  return BLOCK_STATUS_STYLES[status];
}

/**
 * What one capture's reads of a block show, before merging.
 *
 * - `Intact`: header CRC valid, and the payload is empty or complete with a valid data CRC.
 * - `CRC failed`: header CRC valid, payload complete, data CRC invalid.
 * - `CRC unreadable`: header CRC valid, payload complete, data CRC could not be read.
 * - `Truncated`: header CRC valid, fewer payload bytes than the header declares.
 * - `Header failed`: header CRC invalid or unreadable; the read supplies no bytes.
 * - `Missing`: the capture has no read placed at this block number.
 */
export type CaptureReadStatus =
  | "Intact"
  | "CRC failed"
  | "CRC unreadable"
  | "Truncated"
  | "Header failed"
  | "Missing";

/**
 * Per-capture colours reuse the design tokens: green for a good read, red for
 * a read whose bytes fail a CRC check, amber for a read with missing evidence
 * (the merged result for such a block is Unresolved), grey for no read.
 */
export const CAPTURE_READ_STYLES: Readonly<Record<CaptureReadStatus, StatusStyle>> = {
  Intact: { label: "Intact", colour: GREEN, textColour: WHITE },
  "CRC failed": { label: "CRC failed", colour: RED, textColour: WHITE },
  "Header failed": { label: "Header failed", colour: RED, textColour: WHITE },
  "CRC unreadable": { label: "CRC unreadable", colour: AMBER, textColour: DARK },
  Truncated: { label: "Truncated", colour: AMBER, textColour: DARK },
  Missing: { label: "Missing", colour: GREY, textColour: WHITE },
};

/** Best first: when a capture read a block more than once, the best read is shown. */
const READ_RANK: readonly CaptureReadStatus[] = [
  "Intact",
  "CRC failed",
  "CRC unreadable",
  "Truncated",
  "Header failed",
];

export function readStatus(read: ReportRead): Exclude<CaptureReadStatus, "Missing"> {
  if (read.header_crc !== "valid") return "Header failed";
  if (read.payload_state === "empty") return "Intact";
  if (read.payload_state === "truncated") return "Truncated";
  if (read.data_crc === "valid") return "Intact";
  if (read.data_crc === "invalid") return "CRC failed";
  return "CRC unreadable";
}

export interface CaptureCell {
  status: CaptureReadStatus;
  /** Number of this capture's reads placed at the block (0 when Missing). */
  readCount: number;
}

/** Status of one capture's reads of `block`, taken from the report's per-block reads. */
export function captureReadStatus(block: ReportBlock, captureId: string): CaptureCell {
  const reads = block.reads.filter((r) => r.capture_id === captureId);
  if (reads.length === 0) return { status: "Missing", readCount: 0 };
  const best = reads
    .map(readStatus)
    .reduce((a, b) => (READ_RANK.indexOf(b) < READ_RANK.indexOf(a) ? b : a));
  return { status: best, readCount: reads.length };
}
