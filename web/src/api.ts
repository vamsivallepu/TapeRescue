// Typed client for the TapeRescue web API (design.md, section 11).
//
// The report types mirror `build_report` in `taperescue/engine/report.py`
// (schema `taperescue.report/1`) field for field. The envelope and error
// types follow the response envelope and error handling sections of the
// design. All requests go to the same origin under /api; nothing is sent to
// any other host (Requirement 19.2).

// --- shared vocabulary (taperescue/models.py) --------------------------------

export type BlockStatus =
  | "Intact"
  | "CRC failed"
  | "Reconstructed · CRC match"
  | "Unresolved"
  | "Conflict"
  | "Missing";

export const BLOCK_STATUSES: readonly BlockStatus[] = [
  "Intact",
  "CRC failed",
  "Reconstructed · CRC match",
  "Unresolved",
  "Conflict",
  "Missing",
];

export type CrcOutcome = "valid" | "invalid" | "unreadable";

export type PayloadState = "complete" | "truncated" | "empty";

/** Engine `UnresolvedReason` codes. */
export type UnresolvedReason =
  | "no_match"
  | "multiple_matches"
  | "cap_exceeded"
  | "crc_unreadable"
  | "crc_mismatch_between_reads"
  | "truncated_payload"
  | "header_invalid"
  | "header_disagreement"
  | "single_read"
  | "block_missing";

/** Export-gate codes (`GATE_REASONS` in taperescue/engine/gate.py) that are not engine reasons. */
export type GateReason =
  | "duplicate_block"
  | "crc_failed"
  | "payload_conflict"
  | "unresolved"
  | "payload_missing"
  | "filename_mismatch"
  | "address_mismatch"
  | "block_no_mismatch"
  | "unexpected_final_flag"
  | "final_flag_missing"
  | "block_len_invalid"
  | "payload_length_mismatch";

/** Every key of `REASON_MESSAGES` in report.py. */
export type ReasonCode = UnresolvedReason | GateReason;

// --- recovery report (taperescue.report/1) ------------------------------------

export const REPORT_SCHEMA = "taperescue.report/1";

export interface ReportInput {
  capture_id: string;
  sha256: string;
  observation_count: number;
}

export interface ReportRead {
  capture_id: string;
  sample_offset: number;
  header_crc: CrcOutcome;
  /** `null` for an empty payload, which has no data CRC. */
  data_crc: CrcOutcome | null;
  /** `null` unless the header CRC is valid and a data CRC value was recorded. */
  recorded_data_crc: number | null;
  expected_len: number;
  actual_len: number;
  payload_state: PayloadState;
  /** `null` when the header CRC is not valid; such reads never supply bytes. */
  payload_sha256: string | null;
}

/**
 * Observed value per capture at one offset. A capture with several reads that
 * differ at this offset maps to the sorted list of its values.
 */
export type ObservedValues = Record<string, number | number[]>;

export interface ReconstructionChoice {
  offset: number;
  observed: ObservedValues;
  /** `null` unless the block was reconstructed. */
  chosen: number | null;
  supporting_capture_ids: string[];
}

export interface ReconstructionEntry {
  attempted: boolean;
  /** All four counts are `null` when `attempted` is false. */
  candidate_count: number | null;
  /** 0 when the candidate count exceeded the cap. */
  evaluated_count: number | null;
  /** `null` when the cap was exceeded (no candidate was evaluated). */
  match_count: number | null;
  choices: ReconstructionChoice[];
}

export interface ConflictEntry {
  kind: "header" | "payload" | null;
  capture_ids: string[];
}

export interface ReportBlock {
  block_no: number;
  status: BlockStatus;
  /** Engine reason as set by `recover`; may be `null` for unrecovered blocks in intact-only mode. */
  reason: UnresolvedReason | null;
  /** Message for `reason`, or for the gate fallback code when `reason` is `null`. */
  reason_message: string | null;
  label: string | null;
  supporting_capture_ids: string[];
  reads: ReportRead[];
  recorded_data_crc: number | null;
  reconstruction: ReconstructionEntry;
  conflict: ConflictEntry | null;
  /** Set only for `Intact` and `Reconstructed · CRC match` blocks. */
  payload_sha256: string | null;
}

/** `[block_no, reason code]`, sorted by block number then code. */
export type Blocker = [number, ReasonCode];

/** The export decision as the API returns it in the envelope. */
export interface ExportDecision {
  permitted: boolean;
  blockers: Blocker[];
  reconstructed_blocks: number[];
}

/** The export decision inside the report, which also carries the message for each blocker code. */
export interface ReportExport extends ExportDecision {
  messages: Partial<Record<ReasonCode, string>>;
}

export interface Report {
  schema: typeof REPORT_SCHEMA;
  algorithm_version: string;
  candidate_cap: number;
  reconstruction_enabled: boolean;
  inputs: ReportInput[];
  duplicates_merged: string[];
  anchors: number[];
  final_block: number | null;
  blocks: ReportBlock[];
  export: ReportExport;
  /** Present only when every block 0..final_block is recovered. */
  recovered_payload_sha256?: string;
}

// --- observations (taperescue.observations/1 blocks) ----------------------------

export interface HeaderFields {
  filename: string;
  load_addr: number;
  exec_addr: number;
  block_no: number;
  block_len: number;
  flags: number;
  next_addr: number;
}

export interface CrcReading {
  outcome: CrcOutcome;
  /** Absent when `outcome` is `unreadable`. */
  recorded?: number;
}

/** One block observation, as written by `observations_to_doc` in decoder_adapter/schema.py. */
export interface ObservationBlock {
  sample_offset: number;
  /** Lowercase hex. */
  header_raw: string;
  header: HeaderFields | null;
  header_crc: CrcReading;
  /** Lowercase hex of the bytes the decoder read; never padded. */
  payload: string;
  expected_len: number;
  actual_len: number;
  data_crc: CrcReading | null;
  /** Header block number, `null` when the header CRC is not valid. */
  block_no?: number | null;
}

// --- session envelope and errors -------------------------------------------------

/** `WavRejected` codes from taperescue/wavcheck.py. */
export type WavRejectCode =
  | "not_riff_wave"
  | "unsupported_format"
  | "too_large"
  | "too_long"
  | "no_data";

/** `DecoderError` codes from taperescue/decoder_adapter/schema.py. */
export type DecoderErrorCode = "timeout" | "decoder_failed" | "invalid_output" | "no_blocks";

/** `PairingRejected` code from the engine; applies to the session, not one capture. */
export type PairingErrorCode = "no_anchor";

export type CaptureErrorCode = WavRejectCode | DecoderErrorCode | PairingErrorCode;

export interface CaptureError {
  /** Index of the upload (0 or 1); `null` for a session-level error such as `no_anchor`. */
  capture_index: number | null;
  display_name: string | null;
  code: CaptureErrorCode;
  message: string;
}

export interface CaptureSummary {
  index: number;
  /** The user's filename, display text only; never used as a path. */
  display_name: string;
  /** SHA-256 hex of the WAV bytes. */
  capture_id: string | null;
  status: "ok" | "error";
}

export interface SessionEnvelope {
  session_id: string;
  captures: CaptureSummary[];
  errors: CaptureError[];
  /** `null` when no capture could be decoded. */
  report: Report | null;
  /** Observations keyed by capture ID. */
  observations: Record<string, ObservationBlock[]>;
  export: ExportDecision | null;
}

/** Body of a 422 response. */
export interface CaptureErrorsBody {
  errors: CaptureError[];
}

/** Body of a 409 response from `tape.uef`. */
export interface ExportRefusedBody {
  blockers: Blocker[];
  messages?: Partial<Record<ReasonCode, string>>;
}

/** A non-2xx API response. */
export class ApiError extends Error {
  readonly status: number;
  /** Per-capture errors from a 422 response. */
  readonly captureErrors: CaptureError[];
  /** Export blockers from a 409 response. */
  readonly blockers: Blocker[];

  constructor(
    status: number,
    message: string,
    captureErrors: CaptureError[] = [],
    blockers: Blocker[] = [],
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.captureErrors = captureErrors;
    this.blockers = blockers;
  }
}

const FALLBACK_MESSAGES: Record<number, string> = {
  400: "Upload one or two WAV captures",
  404: "Session not found or expired",
  409: "Export is not permitted for this session",
  422: "One or more captures were rejected",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Builds an `ApiError` from a non-2xx response. Reads `errors` (422),
 * `blockers` (409), and a message from `message` or FastAPI's `detail`
 * when it is a string. A body that is not JSON falls back to a fixed message.
 */
export async function readApiError(response: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  let message: string | undefined;
  let captureErrors: CaptureError[] = [];
  let blockers: Blocker[] = [];
  if (isRecord(body)) {
    if (typeof body.message === "string") message = body.message;
    else if (typeof body.detail === "string") message = body.detail;
    if (Array.isArray(body.errors)) captureErrors = body.errors as CaptureError[];
    if (Array.isArray(body.blockers)) blockers = body.blockers as Blocker[];
  }
  if (message === undefined && captureErrors.length > 0) {
    message = captureErrors.map((e) => e.message).join("; ");
  }
  message ??= FALLBACK_MESSAGES[response.status] ?? `Request failed with status ${response.status}`;
  return new ApiError(response.status, message, captureErrors, blockers);
}

// --- endpoints ---------------------------------------------------------------------

const API = "/api";

export function sessionUrl(sessionId: string): string {
  return `${API}/sessions/${encodeURIComponent(sessionId)}`;
}

export function reportUrl(sessionId: string): string {
  return `${sessionUrl(sessionId)}/report.json`;
}

export function tapeUrl(sessionId: string): string {
  return `${sessionUrl(sessionId)}/tape.uef`;
}

async function request(input: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(input, { credentials: "same-origin", ...init });
  if (!response.ok) throw await readApiError(response);
  return response;
}

async function requestJson<T>(input: string, init?: RequestInit): Promise<T> {
  const response = await request(input, {
    ...init,
    headers: { Accept: "application/json", ...init?.headers },
  });
  return (await response.json()) as T;
}

/** `POST /api/sessions` with one or two files in the multipart field `captures`. */
export function createSession(files: readonly File[]): Promise<SessionEnvelope> {
  if (files.length < 1 || files.length > 2) {
    return Promise.reject(new ApiError(400, FALLBACK_MESSAGES[400]!));
  }
  const form = new FormData();
  for (const file of files) form.append("captures", file, file.name);
  return requestJson<SessionEnvelope>(`${API}/sessions`, { method: "POST", body: form });
}

/** `POST /api/sample`: runs the bundled damaged STARMAP captures. */
export function runSample(): Promise<SessionEnvelope> {
  return requestJson<SessionEnvelope>(`${API}/sample`, { method: "POST" });
}

/** `GET /api/sessions/{sid}`. */
export function getSession(sessionId: string): Promise<SessionEnvelope> {
  return requestJson<SessionEnvelope>(sessionUrl(sessionId));
}

/** `GET /api/sessions/{sid}/report.json` as a file for download. */
export async function fetchReportFile(sessionId: string): Promise<Blob> {
  return (await request(reportUrl(sessionId))).blob();
}

/** `GET /api/sessions/{sid}/tape.uef`; rejects with `ApiError` (status 409, `blockers`) when export is refused. */
export async function fetchTapeFile(sessionId: string): Promise<Blob> {
  return (await request(tapeUrl(sessionId))).blob();
}

/** `DELETE /api/sessions/{sid}`. */
export async function deleteSession(sessionId: string): Promise<void> {
  await request(sessionUrl(sessionId), { method: "DELETE" });
}
