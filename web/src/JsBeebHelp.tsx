// How to run the downloaded tape image in JSBeeb (Requirement 18.6).
//
// A plain outbound link: JSBeeb is not embedded, and TapeRescue never sends a
// capture or tape image to it (Requirement 19.2). The user loads the file from
// their own disk with the emulator's local tape picker.
import { useId } from "react";

export const JSBEEB_URL = "https://bbc.xania.org/";

export interface JsBeebHelpProps {
  /** Cassette file name to CHAIN; defaults to the sample program. */
  programName?: string;
}

export function JsBeebHelp({ programName = "STARMAP" }: JsBeebHelpProps) {
  const headingId = useId();
  return (
    <section className="jsbeeb" aria-labelledby={headingId}>
      <h2 id={headingId}>Run the tape image in JSBeeb</h2>
      <ol>
        <li>
          Download the tape image above. It is saved on your computer as a <code>.uef</code> file.
        </li>
        <li>
          Open{" "}
          <a href={JSBEEB_URL} target="_blank" rel="noopener noreferrer">
            JSBeeb
          </a>{" "}
          (opens in a new tab).
        </li>
        <li>
          In the emulator's tape menu, use the local tape picker to choose the downloaded <code>.uef</code> file.
          The file is read from your disk; TapeRescue does not send it to JSBeeb.
        </li>
        <li>
          Type <kbd>*TAPE</kbd> and press Return.
        </li>
        <li>
          Type <kbd>CHAIN "{programName}"</kbd> and press Return.
        </li>
      </ol>
    </section>
  );
}
