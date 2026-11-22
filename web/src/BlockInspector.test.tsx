import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { Report, ReportBlock } from "./api.ts";
import { BlockInspector, formatByte, formatCrc } from "./BlockInspector.tsx";
// Written by `taperescue recover` on the damaged STARMAP fixtures (both, and A alone).
import fullJson from "./test-fixtures/report.json";
import singleJson from "./test-fixtures/report-single.json";

const full = fullJson as unknown as Report;
const single = singleJson as unknown as Report;
// Capture IDs sort as damaged_a.wav (2e09…) then damaged_b.wav (83a0…).
const [idA, idB] = full.inputs.map((i) => i.capture_id) as [string, string];
const names = { [idA]: "damaged_a.wav", [idB]: "damaged_b.wav" };

afterEach(cleanup);

function table(caption: RegExp): HTMLTableElement {
  return screen.getByRole("table", { name: caption }) as HTMLTableElement;
}

/** Text of every cell (th and td) per body row. */
function bodyRows(t: HTMLTableElement): string[][] {
  return Array.from(t.tBodies[0]!.rows).map((row) => Array.from(row.cells).map((c) => c.textContent ?? ""));
}

function headers(t: HTMLTableElement): string[] {
  return Array.from(t.tHead!.rows[0]!.cells).map((c) => c.textContent ?? "");
}

describe("formatting", () => {
  it("shows bytes in hex and decimal and CRCs as 4-digit hex", () => {
    expect(formatByte(97)).toBe("0x61 (97)");
    expect(formatByte(5)).toBe("0x05 (5)");
    expect(formatCrc(35417)).toBe("0x8A59");
    expect(formatCrc(0x31)).toBe("0x0031");
  });
});

describe("BlockInspector, two damaged captures", () => {
  it("shows block 6 reads, recorded CRC, differing offsets, chosen values and evidence label", () => {
    render(<BlockInspector report={full} blockNo={6} displayNames={names} />);
    const block = full.blocks[6]!;
    expect(screen.getByRole("heading", { name: "Block 6" })).toBeTruthy();
    expect(screen.getByTestId("inspector-crc").textContent).toBe("0x8A59");
    // The evidence statement is the report's own label text.
    expect(screen.getByTestId("inspector-label").textContent).toBe(block.label);
    expect(block.label).toBe("CRC match is evidence, not proof of the original bytes");
    expect(screen.getByTestId("inspector-candidates").textContent).toBe("16");
    expect(screen.getByTestId("inspector-evaluated").textContent).toBe("16");
    expect(screen.getByTestId("inspector-matches").textContent).toBe("1");

    const reads = table(/Reads of block 6/);
    expect(within(reads).getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "Capture",
      "Sample offset",
      "Header CRC",
      "Data CRC",
      "Recorded data CRC",
      "Expected length",
      "Actual length",
      "Payload",
    ]);
    expect(bodyRows(reads)).toEqual([
      ["damaged_a.wav", "1116172", "valid", "invalid", "0x8A59", "256", "256", "complete"],
      ["damaged_b.wav", "1116172", "valid", "invalid", "0x8A59", "256", "256", "complete"],
    ]);
    expect(within(reads).getAllByRole("rowheader").map((h) => h.getAttribute("scope"))).toEqual(["row", "row"]);

    const offsets = table(/Differing payload offsets in block 6/);
    expect(headers(offsets)).toEqual(["Offset", "damaged_a.wav", "damaged_b.wav", "Chosen", "Supplied by"]);
    expect(bodyRows(offsets)).toEqual([
      ["17", "0x65 (101)", "0x61 (97)", "0x61 (97)", "damaged_b.wav"],
      ["64", "0xE0 (224)", "0xE1 (225)", "0xE0 (224)", "damaged_a.wav"],
      ["143", "0x14 (20)", "0x34 (52)", "0x34 (52)", "damaged_b.wav"],
      ["201", "0x69 (105)", "0x29 (41)", "0x69 (105)", "damaged_a.wav"],
    ]);
  });

  it("shows which captures supplied intact block 0, with no offsets table", () => {
    render(<BlockInspector report={full} blockNo={0} displayNames={names} />);
    expect(screen.getByTestId("inspector-sources").textContent).toBe("damaged_a.wav, damaged_b.wav");
    expect(screen.getByTestId("inspector-label").textContent).toBe(full.blocks[0]!.label);
    expect(screen.getByTestId("inspector-crc").textContent).toBe(formatCrc(full.blocks[0]!.recorded_data_crc!));
    expect(bodyRows(table(/Reads of block 0/)).map((r) => r[3])).toEqual(["valid", "valid"]);
    expect(screen.queryByRole("table", { name: /Differing payload offsets/ })).toBeNull();
    expect(screen.queryByTestId("inspector-candidates")).toBeNull();
  });

  it("falls back to a short capture ID prefix without display names", () => {
    render(<BlockInspector report={full} blockNo={6} />);
    expect(bodyRows(table(/Reads of block 6/)).map((r) => r[0])).toEqual([idA.slice(0, 8), idB.slice(0, 8)]);
  });

  it("prompts for a selection when no block is selected", () => {
    render(<BlockInspector report={full} blockNo={null} />);
    expect(screen.getByText("Select a block to see its details.")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });
});

describe("BlockInspector, unrecovered blocks", () => {
  it("shows CRC failed and the single_read reason for block 6 of one capture", () => {
    render(<BlockInspector report={single} blockNo={6} displayNames={names} />);
    const block = single.blocks[6]!;
    expect(screen.getByText("CRC failed", { selector: ".inspector-status" })).toBeTruthy();
    expect(screen.getByTestId("inspector-reason").textContent).toBe(block.reason_message);
    expect(screen.getByTestId("inspector-crc").textContent).toBe("0x8A59");
    expect(screen.queryByTestId("inspector-sources")).toBeNull();
    expect(screen.queryByRole("table", { name: /Differing payload offsets/ })).toBeNull();
    // Capture B is not in this report's inputs, so the reads table has A only.
    expect(bodyRows(table(/Reads of block 6/))).toEqual([
      ["damaged_a.wav", "1116172", "valid", "invalid", "0x8A59", "256", "256", "complete"],
    ]);
  });

  it("never shows chosen values for an Unresolved block, even if the data carries them", () => {
    // Block 6 of the two-capture report recast as Unresolved, using a reason
    // message taken from a real report. The chosen values are deliberately left
    // in to check that the inspector ignores them.
    const message = single.export.messages.single_read!;
    const unresolved: ReportBlock = {
      ...full.blocks[6]!,
      status: "Unresolved",
      reason: "single_read",
      reason_message: message,
      label: "Not recovered",
      payload_sha256: null,
      reconstruction: { ...full.blocks[6]!.reconstruction, match_count: 2 },
    };
    const report: Report = { ...full, blocks: full.blocks.map((b) => (b.block_no === 6 ? unresolved : b)) };
    render(<BlockInspector report={report} blockNo={6} displayNames={names} />);
    expect(screen.getByTestId("inspector-reason").textContent).toBe(message);
    expect(screen.queryByTestId("inspector-sources")).toBeNull();
    const offsets = table(/Differing payload offsets in block 6/);
    expect(headers(offsets)).toEqual(["Offset", "damaged_a.wav", "damaged_b.wav"]);
    expect(bodyRows(offsets)[0]).toEqual(["17", "0x65 (101)", "0x61 (97)"]);
    expect(screen.queryByText("Chosen")).toBeNull();
    expect(screen.getByTestId("inspector-matches").textContent).toBe("2");
  });
});
