import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Report } from "./api.ts";
import { DownloadBar, REPORT_FILENAME } from "./DownloadBar.tsx";
import { JSBEEB_URL, JsBeebHelp } from "./JsBeebHelp.tsx";
// Written by `taperescue recover` on the damaged STARMAP fixtures (both, and A alone).
import fullJson from "./test-fixtures/report.json";
import singleJson from "./test-fixtures/report-single.json";

const full = fullJson as unknown as Report;
const single = singleJson as unknown as Report;

afterEach(cleanup);

function setup(report: Report) {
  const reportBlob = new Blob(["{}"], { type: "application/json" });
  const tapeBlob = new Blob(["UEF File!"], { type: "application/octet-stream" });
  const fetchReport = vi.fn(async () => reportBlob);
  const fetchTape = vi.fn(async () => tapeBlob);
  const save = vi.fn();
  render(
    <DownloadBar
      sessionId="abc"
      report={report}
      tapeFilename="STARMAP.uef"
      fetchReport={fetchReport}
      fetchTape={fetchTape}
      save={save}
    />,
  );
  return { reportBlob, tapeBlob, fetchReport, fetchTape, save };
}

const button = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;

describe("DownloadBar, export refused (damaged A alone)", () => {
  it("disables the tape image, lists the blockers, and still downloads the report", async () => {
    const { fetchReport, fetchTape, save, reportBlob } = setup(single);
    const tape = button("Download tape image");
    expect(tape.disabled).toBe(true);
    const blockers = screen.getByTestId("export-blockers");
    expect(tape.getAttribute("aria-describedby")).toBe(blockers.id);
    const items = within(blockers).getAllByRole("listitem").map((li) => li.textContent);
    expect(items).toEqual([`Block 6: ${single.export.messages.single_read} single_read`]);

    fireEvent.click(tape);
    expect(fetchTape).not.toHaveBeenCalled();

    const rep = button("Download report");
    expect(rep.disabled).toBe(false);
    fireEvent.click(rep);
    await waitFor(() => expect(save).toHaveBeenCalledWith(reportBlob, REPORT_FILENAME));
    expect(fetchReport).toHaveBeenCalledWith("abc");
  });
});

describe("DownloadBar, export permitted with reconstructed block 6", () => {
  it("asks for confirmation listing the reconstructed blocks and the evidence label first", async () => {
    const { fetchTape, save, tapeBlob } = setup(full);
    expect(screen.queryByTestId("export-blockers")).toBeNull();
    const tape = button("Download tape image");
    expect(tape.disabled).toBe(false);

    fireEvent.click(tape);
    const dialog = screen.getByRole("dialog", { name: "This tape image contains reconstructed blocks" });
    const items = within(within(dialog).getByTestId("reconstructed-blocks"))
      .getAllByRole("listitem")
      .map((li) => li.textContent);
    expect(items).toEqual([
      "Block 6: Reconstructed · CRC match. CRC match is evidence, not proof of the original bytes",
    ]);
    // Nothing is fetched until the user confirms; focus starts on the dialog's button.
    expect(fetchTape).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(within(dialog).getByRole("button", { name: "Continue download" }));

    // Cancel closes without downloading and returns focus to the trigger.
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(tape);
    expect(fetchTape).not.toHaveBeenCalled();

    fireEvent.click(tape);
    fireEvent.click(screen.getByRole("button", { name: "Continue download" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith(tapeBlob, "STARMAP.uef"));
    expect(fetchTape).toHaveBeenCalledWith("abc");
  });
});

describe("JsBeebHelp", () => {
  it("links to JSBeeb in a new tab and gives the tape picker, *TAPE and CHAIN steps", () => {
    render(<JsBeebHelp />);
    const link = screen.getByRole("link", { name: "JSBeeb" }) as HTMLAnchorElement;
    expect(link.href).toBe(JSBEEB_URL);
    expect(link.target).toBe("_blank");
    expect(link.rel).toBe("noopener noreferrer");
    const steps = screen.getAllByRole("listitem").map((li) => li.textContent ?? "");
    expect(steps.some((s) => /local tape picker/.test(s) && s.includes(".uef"))).toBe(true);
    expect(steps.some((s) => s.includes("*TAPE"))).toBe(true);
    expect(steps.some((s) => s.includes('CHAIN "STARMAP"'))).toBe(true);
    expect(document.querySelector("iframe")).toBeNull();
  });
});
