// Saving a fetched file to disk.
//
// The API helpers return a Blob; this turns it into a `blob:` object URL and
// clicks a temporary `<a download>`. Under the CSP `default-src 'self'` this
// needs no inline script and no `data:` URL: a download through an anchor is
// a navigation, which the fetch directives in `default-src` do not govern.
// Nothing leaves the browser; the object URL refers to memory in this page.

/**
 * Server rule for the tape file name (server/app.py): the cassette name from
 * block 0's valid header when it matches `[A-Za-z0-9_-]{1,10}`, otherwise `tape`.
 */
const CASSETTE_NAME = /^[A-Za-z0-9_-]{1,10}$/;

export function safeCassetteName(name: string | null | undefined): string | null {
  return name !== null && name !== undefined && CASSETTE_NAME.test(name) ? name : null;
}

export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.rel = "noopener";
  a.hidden = true;
  document.body.appendChild(a);
  try {
    a.click();
  } finally {
    a.remove();
    // Some browsers start reading the URL after click() returns, so revoke later.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}
