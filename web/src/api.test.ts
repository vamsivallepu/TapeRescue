// @vitest-environment node
import { describe, expect, it } from "vitest";
import {
  ApiError,
  BLOCK_STATUSES,
  REPORT_SCHEMA,
  readApiError,
  reportUrl,
  sessionUrl,
  tapeUrl,
  type ReconstructionChoice,
  type ReconstructionEntry,
  type Report,
  type ReportBlock,
  type ReportExport,
  type ReportInput,
  type ReportRead,
} from "./api.ts";
// Written by `taperescue recover` on the damaged STARMAP fixtures (both, and A alone).
import fullReport from "./test-fixtures/report.json";
import singleReport from "./test-fixtures/report-single.json";

// One entry per interface field. `Record<keyof T, true>` fails to compile if a
// field is missing here or if a key here is not a field of T.
const REPORT_KEYS: Record<keyof Report, true> = {
  schema: true,
  algorithm_version: true,
  candidate_cap: true,
  reconstruction_enabled: true,
  inputs: true,
  duplicates_merged: true,
  anchors: true,
  final_block: true,
  blocks: true,
  export: true,
  recovered_payload_sha256: true,
};
const INPUT_KEYS: Record<keyof ReportInput, true> = {
  capture_id: true,
  sha256: true,
  observation_count: true,
};
const BLOCK_KEYS: Record<keyof ReportBlock, true> = {
  block_no: true,
  status: true,
  reason: true,
  reason_message: true,
  label: true,
  supporting_capture_ids: true,
  reads: true,
  recorded_data_crc: true,
  reconstruction: true,
  conflict: true,
  payload_sha256: true,
};
const READ_KEYS: Record<keyof ReportRead, true> = {
  capture_id: true,
  sample_offset: true,
  header_crc: true,
  data_crc: true,
  recorded_data_crc: true,
  expected_len: true,
  actual_len: true,
  payload_state: true,
  payload_sha256: true,
};
const RECONSTRUCTION_KEYS: Record<keyof ReconstructionEntry, true> = {
  attempted: true,
  candidate_count: true,
  evaluated_count: true,
  match_count: true,
  choices: true,
};
const CHOICE_KEYS: Record<keyof ReconstructionChoice, true> = {
  offset: true,
  observed: true,
  chosen: true,
  supporting_capture_ids: true,
};
const EXPORT_KEYS: Record<keyof ReportExport, true> = {
  permitted: true,
  blockers: true,
  messages: true,
  reconstructed_blocks: true,
};

const sorted = (o: object) => Object.keys(o).sort();

describe("report types match taperescue.report/1 output", () => {
  for (const [name, doc] of [
    ["full recovery", fullReport],
    ["single capture", singleReport],
  ] as const) {
    it(`has exactly the typed fields (${name})`, () => {
      const report = doc as unknown as Report;
      expect(report.schema).toBe(REPORT_SCHEMA);
      const expectedTop = sorted(REPORT_KEYS).filter(
        (k) => k !== "recovered_payload_sha256" || "recovered_payload_sha256" in report,
      );
      expect(sorted(report)).toEqual(expectedTop);
      expect(sorted(report.export)).toEqual(sorted(EXPORT_KEYS));
      for (const input of report.inputs) expect(sorted(input)).toEqual(sorted(INPUT_KEYS));
      for (const block of report.blocks) {
        expect(sorted(block)).toEqual(sorted(BLOCK_KEYS));
        expect(BLOCK_STATUSES).toContain(block.status);
        expect(sorted(block.reconstruction)).toEqual(sorted(RECONSTRUCTION_KEYS));
        for (const read of block.reads) expect(sorted(read)).toEqual(sorted(READ_KEYS));
        for (const choice of block.reconstruction.choices) {
          expect(sorted(choice)).toEqual(sorted(CHOICE_KEYS));
        }
      }
    });
  }

  it("covers a reconstructed block and a refused export", () => {
    const full = fullReport as unknown as Report;
    const single = singleReport as unknown as Report;
    const block6 = full.blocks.find((b) => b.block_no === 6)!;
    expect(block6.status).toBe("Reconstructed · CRC match");
    expect(block6.reconstruction.choices.length).toBeGreaterThan(0);
    expect(full.export.permitted).toBe(true);
    expect(full.recovered_payload_sha256).toMatch(/^[0-9a-f]{64}$/);

    expect(single.export.permitted).toBe(false);
    expect(single.export.blockers).toContainEqual([6, "single_read"]);
    expect(single.export.messages.single_read).toEqual(expect.any(String));
    expect("recovered_payload_sha256" in single).toBe(false);
  });
});

describe("readApiError", () => {
  const json = (status: number, body: unknown) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });

  it("reads per-capture errors from a 422 body", async () => {
    const errors = [
      {
        capture_index: 1,
        display_name: "side-b.wav",
        code: "unsupported_format",
        message: "Detected 48000 Hz; TapeRescue supports 44100 Hz, 16-bit, mono PCM",
      },
    ];
    const err = await readApiError(json(422, { errors }));
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(422);
    expect(err.captureErrors).toEqual(errors);
    expect(err.message).toBe(errors[0]!.message);
  });

  it("reads blockers from a 409 body", async () => {
    const err = await readApiError(json(409, { blockers: [[6, "single_read"]] }));
    expect(err.status).toBe(409);
    expect(err.blockers).toEqual([[6, "single_read"]]);
  });

  it("uses FastAPI's string detail, or a fixed message for a non-JSON body", async () => {
    expect((await readApiError(json(404, { detail: "Session not found or expired" }))).message).toBe(
      "Session not found or expired",
    );
    const plain = await readApiError(new Response("oops", { status: 400 }));
    expect(plain.message).toBe("Upload one or two WAV captures");
    expect(plain.captureErrors).toEqual([]);
  });
});

describe("endpoint URLs", () => {
  it("stay on the same origin under /api and encode the session ID", () => {
    expect(sessionUrl("9c1e")).toBe("/api/sessions/9c1e");
    expect(reportUrl("9c1e")).toBe("/api/sessions/9c1e/report.json");
    expect(tapeUrl("9c1e")).toBe("/api/sessions/9c1e/tape.uef");
    expect(sessionUrl("../x")).toBe("/api/sessions/..%2Fx");
  });
});
