// TapeRescue main screen: upload one or two captures or run the sample, see
// each capture's strip, the merged strip and the selected block, and download
// the report and tape image (Requirements 11.6, 18.1-18.7).
import { useState, type FormEvent } from "react";
import {
  ApiError,
  createSession,
  deleteSession,
  runSample,
  type CaptureError,
  type Report,
  type SessionEnvelope,
} from "./api.ts";
import { BlockInspector } from "./BlockInspector.tsx";
import { CaptureStrip } from "./CaptureStrip.tsx";
import { safeCassetteName } from "./download.ts";
import { DownloadBar } from "./DownloadBar.tsx";
import { JsBeebHelp } from "./JsBeebHelp.tsx";
import { Heartbeat, LabHeader } from "./LabHeader.tsx";
import { MergedStrip } from "./MergedStrip.tsx";
import { Verdict } from "./Verdict.tsx";
import "./downloads.css";

export const REPO_URL = "https://github.com/vamsivallepu/TapeRescue";

/** First merged block that is not Intact, so the screen opens on the interesting block. */
export function defaultBlock(report: Report): number | null {
  const first = report.blocks.find((b) => b.status !== "Intact") ?? report.blocks[0];
  return first?.block_no ?? null;
}

/** Upload display names per capture ID; two uploads of the same file share one ID. */
export function displayNames(envelope: SessionEnvelope): Record<string, string> {
  const names: Record<string, string> = {};
  for (const c of envelope.captures) {
    if (c.capture_id === null) continue;
    const prev = names[c.capture_id];
    names[c.capture_id] = prev === undefined ? c.display_name : `${prev} / ${c.display_name}`;
  }
  return names;
}

/** Cassette name from a valid block 0 header, with the server's file name rule. */
export function cassetteName(envelope: SessionEnvelope): string | null {
  for (const cid of Object.keys(envelope.observations).sort()) {
    for (const block of envelope.observations[cid] ?? []) {
      if (block.header !== null && block.header.block_no === 0) {
        const name = safeCassetteName(block.header.filename);
        if (name !== null) return name;
      }
    }
  }
  return null;
}

type Busy = "upload" | "sample" | null;

export default function App() {
  const [files, setFiles] = useState<File[]>([]);
  const [envelope, setEnvelope] = useState<SessionEnvelope | null>(null);
  const [errors, setErrors] = useState<CaptureError[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const [selected, setSelected] = useState<number | null>(null);

  const tooMany = files.length > 2;

  async function run(kind: Exclude<Busy, null>, start: () => Promise<SessionEnvelope>) {
    setBusy(kind);
    setMessage(null);
    setErrors([]);
    const previous = envelope?.session_id;
    try {
      const next = await start();
      setEnvelope(next);
      setErrors(next.errors);
      setSelected(next.report ? defaultBlock(next.report) : null);
    } catch (e) {
      setEnvelope(null);
      setSelected(null);
      if (e instanceof ApiError && e.captureErrors.length > 0) setErrors(e.captureErrors);
      else setMessage(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
    // The old session is no longer shown; remove it from the server now
    // rather than waiting for the idle sweep. A failure here is harmless.
    if (previous !== undefined) deleteSession(previous).catch(() => undefined);
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (files.length < 1 || tooMany) return;
    void run("upload", () => createSession(files));
  }

  const report = envelope?.report ?? null;
  const names = envelope ? displayNames(envelope) : {};
  const program = envelope ? cassetteName(envelope) : null;

  const idle = envelope === null && busy === null && errors.length === 0 && message === null;

  return (
    <>
    <main className="app">
      <LabHeader busy={busy !== null} />

      <form className="upload" onSubmit={onSubmit} aria-label="Upload captures">
        <label htmlFor="captures-input">Captures (one or two WAV files)</label>
        <input
          id="captures-input"
          type="file"
          name="captures"
          accept=".wav,audio/wav,audio/x-wav,audio/wave"
          multiple
          onChange={(e) => setFiles(Array.from(e.currentTarget.files ?? []))}
        />
        <button type="submit" disabled={busy !== null || files.length < 1 || tooMany}>
          {busy === "upload" ? "Recovering…" : "Recover"}
        </button>
        <button
          type="button"
          className="btn-primary"
          disabled={busy !== null}
          onClick={() => void run("sample", runSample)}
        >
          {busy === "sample" ? "Running sample…" : "Try sample"}
        </button>
      </form>
      {tooMany && (
        <p className="errors" role="alert">
          Select one or two WAV captures ({files.length} selected).
        </p>
      )}
      {busy !== null && (
        <p role="status">
          <Heartbeat />
          Decoding and recovering. This can take a few seconds per capture.
        </p>
      )}
      {idle && (
        <p className="slab-empty">
          The slab is empty. Load one or two recordings of the same cassette, or press <strong>Try sample</strong>{" "}
          for two recordings of STARMAP that both die at block 6.
        </p>
      )}
      {message !== null && (
        <p className="errors" role="alert">
          {message}
        </p>
      )}

      <CaptureList envelope={envelope} errors={errors} />

      {envelope !== null && report !== null && (
        <>
          {report.inputs.map(({ capture_id }) => (
            <CaptureStrip
              key={capture_id}
              report={report}
              captureId={capture_id}
              title={names[capture_id]}
              selectedBlock={selected}
              onSelect={setSelected}
            />
          ))}
          <MergedStrip report={report} selectedBlock={selected} onSelect={setSelected} />
          <Verdict report={report} decision={envelope.export} displayNames={names} />
          <BlockInspector report={report} blockNo={selected} displayNames={names} />
          <DownloadBar
            key={envelope.session_id}
            sessionId={envelope.session_id}
            report={report}
            decision={envelope.export}
            tapeFilename={`${program ?? "tape"}.uef`}
          />
        </>
      )}

      <JsBeebHelp programName={program ?? undefined} />
    </main>
    <footer className="site-footer">
      <p>
        STARMAP and its recordings are generated fixtures made for this project; no real cassettes were harmed.
        TapeRescue bundles no BBC Micro ROMs and never sends your recordings to the emulator.{" "}
        <a href={REPO_URL} target="_blank" rel="noopener noreferrer">
          Source on GitHub
        </a>
        .
      </p>
    </footer>
    </>
  );
}

interface CaptureListProps {
  envelope: SessionEnvelope | null;
  errors: readonly CaptureError[];
}

/**
 * Each upload with its status and its error messages (Requirement 18.7).
 * Without an envelope (a 422), the errors carry the index and display name.
 */
function CaptureList({ envelope, errors }: CaptureListProps) {
  const session = errors.filter((e) => e.capture_index === null);
  const byIndex = (i: number) => errors.filter((e) => e.capture_index === i);
  const rows: { index: number; name: string; status: string | null }[] = envelope
    ? envelope.captures.map((c) => ({ index: c.index, name: c.display_name, status: c.status }))
    : Array.from(new Set(errors.flatMap((e) => (e.capture_index === null ? [] : [e.capture_index]))))
        .sort((a, b) => a - b)
        .map((index) => ({
          index,
          name: errors.find((e) => e.capture_index === index)?.display_name ?? `Capture ${index + 1}`,
          status: "error",
        }));
  if (rows.length === 0 && session.length === 0) return null;
  return (
    <section aria-label="Captures">
      {rows.length > 0 && (
        <ul className="captures-list" data-testid="captures">
          {rows.map((row) => (
            <li key={row.index}>
              <strong>{row.name}</strong>: {row.status === "ok" ? "decoded" : "rejected"}
              {byIndex(row.index).map((e, i) => (
                <p key={i} className="errors" role="alert">
                  {e.message}
                </p>
              ))}
            </li>
          ))}
        </ul>
      )}
      {session.map((e, i) => (
        <p key={i} className="errors" role="alert">
          {e.message}
        </p>
      ))}
    </section>
  );
}
