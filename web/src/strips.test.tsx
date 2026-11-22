import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BLOCK_STATUSES, type Report } from "./api.ts";
import { CaptureStrip } from "./CaptureStrip.tsx";
import { MergedStrip } from "./MergedStrip.tsx";
import { BLOCK_STATUS_STYLES, CAPTURE_READ_STYLES } from "./status.ts";
// Written by `taperescue recover` on the damaged STARMAP fixtures (both, and A alone).
import fullJson from "./test-fixtures/report.json";
import singleJson from "./test-fixtures/report-single.json";

const full = fullJson as unknown as Report;
const single = singleJson as unknown as Report;
const [idA, idB] = full.inputs.map((i) => i.capture_id) as [string, string];

afterEach(cleanup);

// jsdom normalises inline colours to rgb().
function rgb(hex: string): string {
  const n = (i: number) => parseInt(hex.slice(i, i + 2), 16);
  return `rgb(${n(1)}, ${n(3)}, ${n(5)})`;
}

function cells(name: string): HTMLButtonElement[] {
  const strip = screen.getByRole("region", { name: new RegExp(name) });
  return within(strip).getAllByRole("button") as HTMLButtonElement[];
}

describe("MergedStrip", () => {
  it("shows one cell per block with number, text label and colour", () => {
    render(<MergedStrip report={full} selectedBlock={null} onSelect={() => {}} />);
    const buttons = cells("Merged result");
    expect(buttons.map((b) => Number(b.querySelector(".strip-cell-no")!.textContent))).toEqual(
      full.blocks.map((b) => b.block_no),
    );
    const block6 = screen.getByRole("button", { name: "Block 6: Reconstructed · CRC match" });
    expect(block6.textContent).toBe("6Reconstructed");
    expect(block6.style.backgroundColor).toBe(rgb("#1565c0"));
    const block4 = screen.getByRole("button", { name: "Block 4: Intact" });
    expect(block4.style.backgroundColor).toBe(rgb("#2e7d32"));
  });

  it("shows CRC failed for block 6 of a single damaged capture", () => {
    render(<MergedStrip report={single} selectedBlock={null} onSelect={() => {}} />);
    const block6 = screen.getByRole("button", { name: "Block 6: CRC failed" });
    expect(block6.textContent).toContain("CRC failed");
    expect(block6.style.backgroundColor).toBe(rgb("#c62828"));
    expect(block6.title).toBe(single.blocks[6]!.reason_message);
  });

  it("renders the label and colour of every Block_Status", () => {
    const report = {
      ...full,
      blocks: BLOCK_STATUSES.map((status, i) => ({ ...full.blocks[0]!, block_no: i, status })),
    } as Report;
    render(<MergedStrip report={report} selectedBlock={null} onSelect={() => {}} />);
    BLOCK_STATUSES.forEach((status, i) => {
      const cell = screen.getByRole("button", { name: `Block ${i}: ${status}` });
      const style = BLOCK_STATUS_STYLES[status];
      expect(cell.textContent).toBe(`${i}${style.label}`);
      expect(cell.style.backgroundColor).toBe(rgb(style.colour));
      expect(cell.style.color).toBe(rgb(style.textColour));
    });
  });
});

describe("CaptureStrip", () => {
  it("shows each capture's own read status per block", () => {
    render(
      <>
        <CaptureStrip report={full} captureId={idA} title="side-a.wav" selectedBlock={null} onSelect={() => {}} />
        <CaptureStrip report={full} captureId={idB} title="side-b.wav" selectedBlock={null} onSelect={() => {}} />
      </>,
    );
    for (const name of ["side-a.wav", "side-b.wav"]) {
      const buttons = cells(name);
      expect(buttons).toHaveLength(full.blocks.length);
      buttons.forEach((b, n) => {
        const status = n === 6 ? "CRC failed" : "Intact";
        expect(b.getAttribute("data-status")).toBe(status);
        expect(b.textContent).toBe(`${n}${status}`);
        expect(b.style.backgroundColor).toBe(rgb(CAPTURE_READ_STYLES[status].colour));
      });
    }
  });

  it("marks blocks with no read from the capture as Missing", () => {
    render(<CaptureStrip report={single} captureId={idB} selectedBlock={null} onSelect={() => {}} />);
    for (const b of cells(`Capture ${idB.slice(0, 8)}`)) {
      expect(b.getAttribute("data-status")).toBe("Missing");
      expect(b.style.backgroundColor).toBe(rgb("#616161"));
    }
  });
});

describe("selection", () => {
  it("reports the clicked block and marks only that cell pressed in every strip", () => {
    const spy = vi.fn();
    function Harness() {
      const [selected, setSelected] = useState<number | null>(null);
      const select = (n: number) => {
        spy(n);
        setSelected(n);
      };
      return (
        <>
          <CaptureStrip report={full} captureId={idA} title="side-a.wav" selectedBlock={selected} onSelect={select} />
          <MergedStrip report={full} selectedBlock={selected} onSelect={select} />
        </>
      );
    }
    render(<Harness />);
    const merged = cells("Merged result");
    expect(merged.every((b) => b.getAttribute("aria-pressed") === "false")).toBe(true);

    fireEvent.click(merged[6]!);
    expect(spy).toHaveBeenLastCalledWith(6);
    for (const strip of [cells("Merged result"), cells("side-a.wav")]) {
      expect(strip.filter((b) => b.getAttribute("aria-pressed") === "true")).toEqual([strip[6]]);
      expect(strip.every((b) => b.type === "button")).toBe(true);
    }

    fireEvent.click(cells("side-a.wav")[2]!);
    expect(spy).toHaveBeenLastCalledWith(2);
    expect(cells("Merged result")[2]!.getAttribute("aria-pressed")).toBe("true");
    expect(cells("Merged result")[6]!.getAttribute("aria-pressed")).toBe("false");
  });
});
