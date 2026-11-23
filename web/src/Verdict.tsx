// One-line verdict under the merged strip. The tone is themed; every fact
// (block numbers, capture names, blocker reasons) comes from the report and
// export decision, and the evidence caveat stays in place.
import type { ExportDecision, Report } from "./api.ts";
import { shortId } from "./CaptureStrip.tsx";

export interface VerdictProps {
  report: Report;
  decision?: ExportDecision | null;
  displayNames?: Readonly<Record<string, string>>;
}

function list(items: readonly string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

export function Verdict({ report, decision, displayNames = {} }: VerdictProps) {
  const exp = decision ?? report.export;
  const name = (cid: string) => displayNames[cid] ?? shortId(cid);

  if (!exp.permitted) {
    const items = exp.blockers.map(([n, code]) => `block ${n} (${report.export.messages[code] ?? code})`);
    return (
      <p className="verdict verdict--dead" data-testid="verdict">
        <strong>Still dead.</strong> The tape image cannot be exported yet: {list(items)}.
      </p>
    );
  }

  const raised = report.blocks.filter((b) => exp.reconstructed_blocks.includes(b.block_no));
  if (raised.length === 0) {
    return (
      <p className="verdict verdict--alive" data-testid="verdict">
        <strong>Every block survived.</strong> Nothing needed raising; each block passed its recorded CRC.
      </p>
    );
  }
  const sources = Array.from(new Set(raised.flatMap((b) => b.supporting_capture_ids))).map(name);
  const blocks = list(raised.map((b) => `block ${b.block_no}`));
  return (
    <p className="verdict verdict--alive" data-testid="verdict">
      <strong>It's alive!</strong> {blocks.charAt(0).toUpperCase() + blocks.slice(1)}{" "}
      {raised.length === 1 ? "was" : "were"} stitched back together from {list(sources)}, and exactly one
      combination matched the recorded CRC. A CRC match is evidence, not proof.
    </p>
  );
}
