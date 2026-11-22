// The merged result strip (Requirement 18.1): one cell per block with the
// merged Block_Status as text and colour (Requirement 18.2).

import type { Report } from "./api.ts";
import { BlockStrip, type StripCell } from "./CaptureStrip.tsx";
import { blockStatusStyle } from "./status.ts";

export interface MergedStripProps {
  report: Report;
  title?: string;
  selectedBlock: number | null;
  onSelect: (blockNo: number) => void;
}

export function mergedCells(report: Report): StripCell[] {
  return report.blocks.map((block) => {
    const style = blockStatusStyle(block.status);
    // The accessible name carries the full status ("Reconstructed · CRC match"),
    // which contains the visible short label.
    return {
      blockNo: block.block_no,
      style,
      status: block.status,
      ariaLabel: `Block ${block.block_no}: ${block.status}`,
      detail: block.reason_message ?? block.label ?? block.status,
    };
  });
}

export function MergedStrip({ report, title = "Merged result", selectedBlock, onSelect }: MergedStripProps) {
  return (
    <BlockStrip
      title={title}
      className="strip--merged"
      cells={mergedCells(report)}
      selectedBlock={selectedBlock}
      onSelect={onSelect}
    />
  );
}
