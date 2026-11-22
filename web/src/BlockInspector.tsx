// Details of the selected block (Requirement 18.3): each capture's read, the
// differing offsets with every capture's value, the recorded data CRC, the
// chosen values and the capture that supplied each, and the decision text.
//
// All explanatory text about the decision comes from the report (`label` and
// `reason_message`, built from STATUS_LABELS and REASON_MESSAGES in
// taperescue/engine/report.py), so the UI says exactly what the CLI says.
// Chosen values are shown only for `Reconstructed · CRC match` blocks.
import { useId } from "react";
import type { ReconstructionChoice, Report, ReportBlock, ReportRead } from "./api.ts";
import { shortId } from "./CaptureStrip.tsx";
import { blockStatusStyle } from "./status.ts";
import "./inspector.css";

export interface BlockInspectorProps {
  report: Report;
  /** Selected block number; `null` when nothing is selected. */
  blockNo: number | null;
  /** Upload display name per capture ID. Captures without one show a short ID prefix. */
  displayNames?: Readonly<Record<string, string>>;
}

const RECONSTRUCTED = "Reconstructed · CRC match";

/** A byte as `0x61 (97)`. */
export function formatByte(value: number): string {
  return `0x${value.toString(16).toUpperCase().padStart(2, "0")} (${value})`;
}

/** A 16-bit CRC as `0x8A59`. */
export function formatCrc(value: number): string {
  return `0x${value.toString(16).toUpperCase().padStart(4, "0")}`;
}

function formatObserved(value: number | number[] | undefined): string {
  if (value === undefined) return "Not observed";
  return Array.isArray(value) ? value.map(formatByte).join(", ") : formatByte(value);
}

function captureName(captureId: string, names: Readonly<Record<string, string>>): string {
  return names[captureId] ?? shortId(captureId);
}

export function BlockInspector({ report, blockNo, displayNames = {} }: BlockInspectorProps) {
  const headingId = useId();
  const block = blockNo === null ? undefined : report.blocks.find((b) => b.block_no === blockNo);
  return (
    <section className="inspector" aria-labelledby={headingId}>
      <h2 id={headingId} className="inspector-title">
        {block ? `Block ${block.block_no}` : "Block inspector"}
      </h2>
      {blockNo === null ? (
        <p className="inspector-empty">Select a block to see its details.</p>
      ) : block === undefined ? (
        <p className="inspector-empty">Block {blockNo} is not in the report.</p>
      ) : (
        <BlockDetails report={report} block={block} names={displayNames} />
      )}
    </section>
  );
}

interface DetailsProps {
  report: Report;
  block: ReportBlock;
  names: Readonly<Record<string, string>>;
}

function BlockDetails({ report, block, names }: DetailsProps) {
  const style = blockStatusStyle(block.status);
  const reconstructed = block.status === RECONSTRUCTED;
  const recovered = reconstructed || block.status === "Intact";
  const rec = block.reconstruction;
  const name = (cid: string) => captureName(cid, names);
  return (
    <>
      <dl className="inspector-decision">
        <dt>Status</dt>
        <dd>
          <span
            className="inspector-status"
            data-status={block.status}
            style={{ backgroundColor: style.colour, color: style.textColour }}
          >
            {block.status}
          </span>
        </dd>
        {block.label !== null && (
          <>
            <dt>Decision</dt>
            <dd data-testid="inspector-label">{block.label}</dd>
          </>
        )}
        {block.reason_message !== null && (
          <>
            <dt>Reason</dt>
            <dd data-testid="inspector-reason">{block.reason_message}</dd>
          </>
        )}
        <dt>Recorded data CRC</dt>
        <dd data-testid="inspector-crc">
          {block.recorded_data_crc === null ? "No single recorded value" : formatCrc(block.recorded_data_crc)}
        </dd>
        {recovered && (
          <>
            <dt>Supplied by</dt>
            <dd data-testid="inspector-sources">{block.supporting_capture_ids.map(name).join(", ")}</dd>
          </>
        )}
        {rec.attempted && (
          <>
            <dt>Candidates</dt>
            <dd data-testid="inspector-candidates">{rec.candidate_count}</dd>
            <dt>Evaluated</dt>
            <dd data-testid="inspector-evaluated">{rec.evaluated_count}</dd>
            <dt>CRC matches</dt>
            <dd data-testid="inspector-matches">{rec.match_count ?? "Not evaluated"}</dd>
          </>
        )}
        {block.conflict !== null && (
          <>
            <dt>Conflict</dt>
            <dd data-testid="inspector-conflict">
              {block.conflict.kind ?? "unknown"} between {block.conflict.capture_ids.map(name).join(", ")}
            </dd>
          </>
        )}
      </dl>
      <ReadsTable report={report} block={block} name={name} />
      {rec.choices.length > 0 && (
        <OffsetsTable
          report={report}
          block={block}
          choices={rec.choices}
          showChosen={reconstructed}
          name={name}
        />
      )}
    </>
  );
}

interface TableProps {
  report: Report;
  block: ReportBlock;
  name: (captureId: string) => string;
}

function crcText(outcome: string | null): string {
  return outcome ?? "None (empty payload)";
}

function ReadsTable({ report, block, name }: TableProps) {
  // One row per read; a capture with no read of this block gets one row saying so.
  const rows: { captureId: string; read: ReportRead | null }[] = [];
  for (const { capture_id } of report.inputs) {
    const reads = block.reads.filter((r) => r.capture_id === capture_id);
    if (reads.length === 0) rows.push({ captureId: capture_id, read: null });
    else for (const read of reads) rows.push({ captureId: capture_id, read });
  }
  return (
    <div className="inspector-scroll">
      <table className="inspector-table" data-testid="inspector-reads">
        <caption>Reads of block {block.block_no} by capture</caption>
        <thead>
          <tr>
            <th scope="col">Capture</th>
            <th scope="col">Sample offset</th>
            <th scope="col">Header CRC</th>
            <th scope="col">Data CRC</th>
            <th scope="col">Recorded data CRC</th>
            <th scope="col">Expected length</th>
            <th scope="col">Actual length</th>
            <th scope="col">Payload</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ captureId, read }, i) => (
            <tr key={`${captureId}-${read?.sample_offset ?? "none"}-${i}`}>
              <th scope="row" title={captureId}>
                {name(captureId)}
              </th>
              {read === null ? (
                <td colSpan={7}>No read of this block</td>
              ) : (
                <>
                  <td>{read.sample_offset}</td>
                  <td>{read.header_crc}</td>
                  <td>{crcText(read.data_crc)}</td>
                  <td>{read.recorded_data_crc === null ? "Not recorded" : formatCrc(read.recorded_data_crc)}</td>
                  <td>{read.expected_len}</td>
                  <td>{read.actual_len}</td>
                  <td>{read.payload_state}</td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface OffsetsProps extends TableProps {
  choices: readonly ReconstructionChoice[];
  /** True only for `Reconstructed · CRC match`; other blocks never show chosen bytes. */
  showChosen: boolean;
}

function OffsetsTable({ report, block, choices, showChosen, name }: OffsetsProps) {
  const captureIds = report.inputs.map((i) => i.capture_id);
  return (
    <div className="inspector-scroll">
      <table className="inspector-table" data-testid="inspector-offsets">
        <caption>Differing payload offsets in block {block.block_no}</caption>
        <thead>
          <tr>
            <th scope="col">Offset</th>
            {captureIds.map((cid) => (
              <th scope="col" key={cid} title={cid}>
                {name(cid)}
              </th>
            ))}
            {showChosen && (
              <>
                <th scope="col">Chosen</th>
                <th scope="col">Supplied by</th>
              </>
            )}
          </tr>
        </thead>
        <tbody>
          {choices.map((choice) => (
            <tr key={choice.offset}>
              <th scope="row">{choice.offset}</th>
              {captureIds.map((cid) => (
                <td key={cid}>{formatObserved(choice.observed[cid])}</td>
              ))}
              {showChosen && (
                <>
                  <td>{choice.chosen === null ? "None" : formatByte(choice.chosen)}</td>
                  <td>{choice.supporting_capture_ids.map(name).join(", ")}</td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
