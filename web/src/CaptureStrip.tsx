// One strip per capture (Requirement 18.1): one cell per block in the report,
// showing what that capture's own reads of the block look like before merging
// (Requirement 18.2). `BlockStrip` is the shared cell row, also used by MergedStrip.

import { useId } from "react";
import type { Report } from "./api.ts";
import { CAPTURE_READ_STYLES, captureReadStatus, type StatusStyle } from "./status.ts";
import "./strips.css";

export interface StripCell {
  blockNo: number;
  style: StatusStyle;
  /** Status key, exposed as `data-status` for styling hooks and tests. */
  status: string;
  /** Accessible name; must contain the visible block number and label. */
  ariaLabel: string;
  /** Extra detail shown as a tooltip. */
  detail?: string;
}

export interface BlockStripProps {
  title: string;
  /** Optional second line under the title, for example a shortened capture ID. */
  subtitle?: string;
  cells: readonly StripCell[];
  selectedBlock: number | null;
  onSelect: (blockNo: number) => void;
  /** Extra class on the section, for example "strip--merged". */
  className?: string;
}

export function BlockStrip({
  title,
  subtitle,
  cells,
  selectedBlock,
  onSelect,
  className,
}: BlockStripProps) {
  const headingId = useId();
  return (
    <section className={className ? `strip ${className}` : "strip"} aria-labelledby={headingId}>
      <h2 id={headingId} className="strip-title">
        {title}
        {subtitle !== undefined && <span className="strip-subtitle">{subtitle}</span>}
      </h2>
      <ol className="strip-cells" role="list">
        {cells.map((cell) => {
          const selected = cell.blockNo === selectedBlock;
          return (
            <li key={cell.blockNo}>
              <button
                type="button"
                className={selected ? "strip-cell strip-cell--selected" : "strip-cell"}
                data-status={cell.status}
                aria-pressed={selected}
                aria-label={cell.ariaLabel}
                title={cell.detail ?? cell.ariaLabel}
                // Set through the CSSOM by React, so the CSP `default-src 'self'` does not block it.
                style={{ backgroundColor: cell.style.colour, color: cell.style.textColour }}
                onClick={() => onSelect(cell.blockNo)}
              >
                <span className="strip-cell-no">{cell.blockNo}</span>
                <span className="strip-cell-label">{cell.style.label}</span>
              </button>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

export interface CaptureStripProps {
  report: Report;
  captureId: string;
  /** Display name of the upload, for example "side-a.wav". Defaults to the shortened capture ID. */
  title?: string;
  selectedBlock: number | null;
  onSelect: (blockNo: number) => void;
}

export function shortId(captureId: string): string {
  return captureId.slice(0, 8);
}

export function captureCells(report: Report, captureId: string): StripCell[] {
  return report.blocks.map((block) => {
    const { status, readCount } = captureReadStatus(block, captureId);
    const style = CAPTURE_READ_STYLES[status];
    const reads = readCount > 1 ? ` (${readCount} reads)` : "";
    return {
      blockNo: block.block_no,
      style,
      status,
      ariaLabel: `Block ${block.block_no}: ${style.label}${reads}`,
    };
  });
}

export function CaptureStrip({ report, captureId, title, selectedBlock, onSelect }: CaptureStripProps) {
  const short = shortId(captureId);
  return (
    <BlockStrip
      title={title ?? `Capture ${short}`}
      subtitle={title !== undefined ? short : undefined}
      cells={captureCells(report, captureId)}
      selectedBlock={selectedBlock}
      onSelect={onSelect}
    />
  );
}
