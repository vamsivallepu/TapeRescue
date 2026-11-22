// Download controls (Requirements 11.6, 18.4, 18.5).
//
// - **Download report** is always enabled, including for refused sessions.
// - **Download tape image** is disabled while export is not permitted, and the
//   blocking block numbers and reasons are shown beside it.
// - When the image contains reconstructed blocks, a modal dialog lists them
//   with the report's label for each before the download starts.
//
// Reason text comes from the report (`export.messages` and each block's
// `label`), so the UI says what the CLI and report say.
import { useEffect, useId, useRef, useState } from "react";
import {
  ApiError,
  fetchReportFile,
  fetchTapeFile,
  type Blocker,
  type ExportDecision,
  type Report,
} from "./api.ts";
import { saveBlob } from "./download.ts";
import "./downloads.css";

export const REPORT_FILENAME = "taperescue-report.json";

export interface DownloadBarProps {
  sessionId: string;
  report: Report;
  /** Export decision from the session envelope; defaults to the report's own. */
  decision?: ExportDecision | null;
  /** File name for the tape image, for example `STARMAP.uef`. */
  tapeFilename?: string;
  /** Network and save hooks; the defaults use the API client and an `<a download>` click. */
  fetchReport?: (sessionId: string) => Promise<Blob>;
  fetchTape?: (sessionId: string) => Promise<Blob>;
  save?: (blob: Blob, filename: string) => void;
}

type Busy = "report" | "tape" | null;

function blockerMessage(report: Report, [, code]: Blocker): string {
  return report.export.messages[code] ?? code;
}

export function DownloadBar({
  sessionId,
  report,
  decision,
  tapeFilename = "tape.uef",
  fetchReport = fetchReportFile,
  fetchTape = fetchTapeFile,
  save = saveBlob,
}: DownloadBarProps) {
  const exportDecision: ExportDecision = decision ?? report.export;
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);
  // Blockers from a 409 on download, in case the decision changed on the server.
  const [refused, setRefused] = useState<Blocker[]>([]);
  const [confirming, setConfirming] = useState(false);
  const tapeButton = useRef<HTMLButtonElement>(null);
  const blockersId = useId();

  const blockers = refused.length > 0 ? refused : exportDecision.blockers;
  const permitted = exportDecision.permitted && refused.length === 0;
  const reconstructed = exportDecision.reconstructed_blocks;

  async function download(kind: "report" | "tape") {
    setBusy(kind);
    setError(null);
    try {
      const blob = await (kind === "report" ? fetchReport(sessionId) : fetchTape(sessionId));
      save(blob, kind === "report" ? REPORT_FILENAME : tapeFilename);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.blockers.length > 0) setRefused(e.blockers);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  function onTapeClick() {
    if (reconstructed.length > 0) setConfirming(true);
    else void download("tape");
  }

  function closeDialog(proceed: boolean) {
    setConfirming(false);
    tapeButton.current?.focus();
    if (proceed) void download("tape");
  }

  return (
    <section className="downloads" aria-label="Downloads">
      <div className="downloads-buttons">
        <button type="button" onClick={() => void download("report")} disabled={busy === "report"}>
          Download report
        </button>
        <button
          type="button"
          ref={tapeButton}
          onClick={onTapeClick}
          disabled={!permitted || busy === "tape"}
          aria-describedby={permitted ? undefined : blockersId}
        >
          Download tape image
        </button>
      </div>
      {!permitted && (
        <div id={blockersId} className="downloads-blockers" data-testid="export-blockers">
          <p>The tape image cannot be exported because these blocks are not recovered:</p>
          <ul>
            {blockers.map((b) => (
              <li key={`${b[0]}-${b[1]}`}>
                Block {b[0]}: {blockerMessage(report, b)} <code>{b[1]}</code>
              </li>
            ))}
          </ul>
        </div>
      )}
      {error !== null && (
        <p className="downloads-error" role="alert">
          {error}
        </p>
      )}
      {confirming && (
        <ReconstructedDialog report={report} blocks={reconstructed} onClose={closeDialog} />
      )}
    </section>
  );
}

interface DialogProps {
  report: Report;
  blocks: readonly number[];
  onClose: (proceed: boolean) => void;
}

/**
 * Native modal `<dialog>`: the browser traps focus and makes the page inert.
 * Where `showModal` is missing (jsdom), the dialog is opened with the `open`
 * attribute instead. Escape cancels in both cases.
 */
function ReconstructedDialog({ report, blocks, onClose }: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const confirm = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const descId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (dialog === null) return;
    if (typeof dialog.showModal === "function") {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
    confirm.current?.focus();
    return () => {
      if (typeof dialog.close === "function" && dialog.open) dialog.close();
    };
  }, []);

  return (
    <dialog
      ref={ref}
      className="downloads-dialog"
      aria-labelledby={titleId}
      aria-describedby={descId}
      onCancel={(e) => {
        // Escape: let React state close the dialog.
        e.preventDefault();
        onClose(false);
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          onClose(false);
        }
      }}
    >
      <h2 id={titleId}>This tape image contains reconstructed blocks</h2>
      <div id={descId}>
        <p>
          These blocks failed their data CRC in every capture. Their bytes were combined from the captures, and
          exactly one combination matched the recorded CRC:
        </p>
        <ul data-testid="reconstructed-blocks">
          {blocks.map((n) => {
            const block = report.blocks.find((b) => b.block_no === n);
            return (
              <li key={n}>
                Block {n}: {block?.status ?? "Reconstructed · CRC match"}
                {block?.label ? `. ${block.label}` : ""}
              </li>
            );
          })}
        </ul>
      </div>
      <div className="downloads-dialog-buttons">
        <button type="button" ref={confirm} onClick={() => onClose(true)}>
          Continue download
        </button>
        <button type="button" onClick={() => onClose(false)}>
          Cancel
        </button>
      </div>
    </dialog>
  );
}
