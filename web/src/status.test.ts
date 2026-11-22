// @vitest-environment node
import { describe, expect, it } from "vitest";
import { BLOCK_STATUSES, type BlockStatus, type ReportBlock, type ReportRead } from "./api.ts";
import {
  BLOCK_STATUS_STYLES,
  CAPTURE_READ_STYLES,
  captureReadStatus,
  readStatus,
} from "./status.ts";

// The design's status display table (design.md section 12).
const DESIGN_TABLE: Record<BlockStatus, [string, string]> = {
  Intact: ["Intact", "#2e7d32"],
  "CRC failed": ["CRC failed", "#c62828"],
  "Reconstructed · CRC match": ["Reconstructed", "#1565c0"],
  Unresolved: ["Unresolved", "#ef6c00"],
  Conflict: ["Conflict", "#6a1b9a"],
  Missing: ["Missing", "#616161"],
};

// WCAG 2.x relative luminance and contrast ratio.
function luminance(hex: string): number {
  const channel = (i: number) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
}
function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

describe("status styles", () => {
  it("match the design table label and colour for every Block_Status", () => {
    for (const status of BLOCK_STATUSES) {
      const [label, colour] = DESIGN_TABLE[status];
      expect(BLOCK_STATUS_STYLES[status].label).toBe(label);
      expect(BLOCK_STATUS_STYLES[status].colour).toBe(colour);
    }
  });

  it("give text at least 4.5:1 contrast on every cell colour", () => {
    for (const style of [...Object.values(BLOCK_STATUS_STYLES), ...Object.values(CAPTURE_READ_STYLES)]) {
      expect(contrast(style.textColour, style.colour), style.label).toBeGreaterThanOrEqual(4.5);
    }
  });
});

const read = (over: Partial<ReportRead>): ReportRead => ({
  capture_id: "a",
  sample_offset: 0,
  header_crc: "valid",
  data_crc: "valid",
  recorded_data_crc: 1,
  expected_len: 256,
  actual_len: 256,
  payload_state: "complete",
  payload_sha256: "x",
  ...over,
});

describe("per-capture read status", () => {
  it("classifies one read", () => {
    expect(readStatus(read({}))).toBe("Intact");
    expect(readStatus(read({ data_crc: "invalid" }))).toBe("CRC failed");
    expect(readStatus(read({ data_crc: "unreadable", recorded_data_crc: null }))).toBe("CRC unreadable");
    expect(readStatus(read({ payload_state: "truncated", actual_len: 100, data_crc: "unreadable" }))).toBe(
      "Truncated",
    );
    expect(readStatus(read({ header_crc: "invalid", payload_sha256: null }))).toBe("Header failed");
    expect(
      readStatus(read({ payload_state: "empty", expected_len: 0, actual_len: 0, data_crc: null })),
    ).toBe("Intact");
  });

  it("uses only the capture's own reads, shows the best one, and reports Missing", () => {
    const block = {
      block_no: 3,
      reads: [
        read({ capture_id: "a", data_crc: "invalid", sample_offset: 10 }),
        read({ capture_id: "a", sample_offset: 900 }),
        read({ capture_id: "b", payload_state: "truncated", actual_len: 4 }),
      ],
    } as unknown as ReportBlock;
    expect(captureReadStatus(block, "a")).toEqual({ status: "Intact", readCount: 2 });
    expect(captureReadStatus(block, "b")).toEqual({ status: "Truncated", readCount: 1 });
    expect(captureReadStatus(block, "c")).toEqual({ status: "Missing", readCount: 0 });
  });
});
