# Third-party notices

TapeRescue's own code is released under the MIT License (see `LICENSE`, Copyright (c) 2026 V Vamsi Krishna). This file lists the third-party software that TapeRescue vendors in this repository or ships in its container image, with each component's license.

The lists below were collected from the built image `taperescue:local` (built from the repository `Dockerfile`), from `web/node_modules`, and from the vendored source tree. The commands are recorded in `verification/entries/23.2.md`. If the pinned versions in the `Dockerfile`, `pyproject.toml`, or `web/package-lock.json` change, regenerate these lists.

Components appear in three groups:

1. Vendored in this repository and compiled into the `ScanTape` decoder.
2. Distributed in the container image: Python packages, system libraries, and the JavaScript in the built web UI.
3. Used only for development, tests, or build stages, and not distributed.

## 1. Decoder: AtomTapeUtils and gzstream

### AtomTapeUtils

| Field | Value |
| --- | --- |
| Upstream | https://github.com/joakimoh/AtomTapeUtils |
| Revision | `98d1a8d1bed00aa22f1d72e3d5e91a254a69ec64` (git tree `faf78c9b7ed3cf4a1cd2c2aaabc68716e8ec9f86`) |
| Location | `third_party/AtomTapeUtils/` (unmodified upstream files plus a `REVISION` file) |
| License | MIT |
| License file | `third_party/AtomTapeUtils/LICENSE.txt`, SHA-256 `002c2696d92b5c8cf956c11072baa58eaf9f6ade995c031ea635c6a1ee342ad1` |
| Used as | The `ScanTape` decoder binary (`build/decoder/ScanTape` locally, `/app/bin/ScanTape` in the image) |

The upstream license file is named `LICENSE.txt`. It contains the standard MIT text, but its copyright line is the unfilled template placeholder `Copyright (c) [year] [fullname]`. TapeRescue keeps the file byte for byte and does not fill in a name or year. The author of the repository is the GitHub user `joakimoh`. Apart from gzstream (below), no vendored file carries its own copyright or license header.

**Modifications.** The vendored tree is not edited. TapeRescue ships its changes as a separate patch, `patches/atomtapeutils-json-observations.patch` (SHA-256 `ba59709a2510553bddfbec07fd33603338af4de7f607d6606e165f829763c628`). `scripts/build_decoder.sh` applies the patch to a copy in `build/decoder-src/` before compiling. The patch adds a JSON block-observation output mode (`-json <file>`, `-capture-id <id>`) and changes these files:

- `ScanTape/ArgParser.cpp`, `ScanTape/ArgParser.h`, `ScanTape/ScanTape.cpp`: the new command-line options and the observation output.
- `shared/BlockDecoder.cpp`, `shared/BlockDecoder.h`: emit one observation per block at the block decoder boundary.
- `shared/ObservationWriter.cpp`, `shared/ObservationWriter.h` (new files): the JSON writer.
- `shared/CMakeLists.txt`: adds the new source file to the build.
- `shared/WavCycleDecoder.cpp`: a behavior change to decoding. `countHalfCycles` now returns false when the samples end before a full bit duration. Without this change, a byte cut off by the end of a capture decodes with its missing bits set to `1`.

The patched files are distributed under the same MIT license as the upstream project. The `ScanTape` binary in the container image is built from the vendored source with this patch applied.

### gzstream (inside AtomTapeUtils)

| Field | Value |
| --- | --- |
| Location | `third_party/AtomTapeUtils/gzstream/gzstream.h`, `gzstream.cpp` (unmodified) |
| Copyright | Copyright (C) 2001 Deepak Bandyopadhyay, Lutz Kettner |
| License | GNU Lesser General Public License, version 2.1 or (at your option) any later version (LGPL-2.1-or-later) |
| Used as | A static library (`libgzstream.a`) linked into `ScanTape` |

gzstream wraps zlib in C++ iostream classes. The upstream CMake file builds it as a static library, and `ScanTape` links it. The binary contains the gzstream code; it is not loaded as a shared library at run time. TapeRescue does not modify gzstream, and the decoder patch does not touch the `gzstream/` directory.

Because `ScanTape` is a work that uses gzstream and is statically linked to it, LGPL-2.1 section 6 applies to anyone who distributes the `ScanTape` binary, including in the container image. TapeRescue meets those terms as follows:

- **Notice.** This file states that `ScanTape` contains gzstream and that gzstream is covered by the LGPL. The license headers in `gzstream.h` and `gzstream.cpp` are kept unchanged.
- **Library source.** The complete, unmodified gzstream source is in this repository at `third_party/AtomTapeUtils/gzstream/`.
- **Relinking.** The complete source of the rest of `ScanTape` (the vendored AtomTapeUtils tree and `patches/atomtapeutils-json-observations.patch`) and the build script `scripts/build_decoder.sh` are in this repository. A recipient can therefore change gzstream and rebuild and relink `ScanTape` (`scripts/build_decoder.sh`, or the `decoder` stage of the `Dockerfile`).
- **Source for the image.** The container image holds only the compiled binary. The corresponding source for the image is this repository at the same commit as the image was built from: https://github.com/vamsivallepu/TapeRescue.
- **License text.** The full LGPL-2.1 text is published at https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html. The upstream gzstream files point to it but do not include a copy, and neither this repository nor the image currently ships one.

## 2. Distributed in the container image

The runtime image is based on `python:3.12-slim-bookworm` and contains the `taperescue` wheel (MIT), the `server/` source (MIT), the patched `ScanTape` binary (section 1), the built web UI in `/app/web/dist/`, and the three generated STARMAP fixture WAVs. The fixtures are project-authored, generated by TapeRescue's own encoder, and covered by the TapeRescue MIT license.

### Python packages

These are the packages installed in the image's `site-packages`, read from `importlib.metadata` inside `taperescue:local`. `pip install "taperescue[server]"` plus the version pins in the `Dockerfile` installs them. Each package's license file is installed in its `*.dist-info/licenses/` directory (for `pip`, in `pip-25.0.1.dist-info/`).

| Package | Version | License | Copyright (from the license file) | Pulled in by |
| --- | --- | --- | --- | --- |
| fastapi | 0.165.0 | MIT | Copyright (c) 2018 Sebastián Ramírez | `taperescue[server]` |
| starlette | 1.10.0 | BSD-3-Clause | Copyright © 2018, Encode OSS Ltd | fastapi |
| uvicorn | 0.54.0 | BSD-3-Clause | Copyright © 2017-present, Encode OSS Ltd | `taperescue[server]` |
| python-multipart | 0.0.34 | Apache-2.0 | (Apache License 2.0 text, no separate copyright line) | `taperescue[server]` |
| anyio | 4.16.0 | MIT | Copyright (c) 2018 Alex Grönholm | fastapi, starlette, httpx |
| pydantic | 2.14.4 | MIT | Copyright (c) 2017 to present Pydantic Services Inc. and individual contributors | fastapi |
| pydantic_core | 2.50.5 | MIT (see note) | Copyright (c) 2022 Samuel Colvin | pydantic |
| annotated-types | 0.8.0 | MIT | Copyright (c) 2022 the contributors | pydantic |
| typing-inspection | 0.4.5 | MIT | Copyright (c) Pydantic Services Inc. 2025 to present | fastapi, pydantic |
| typing_extensions | 4.16.0 | PSF-2.0 | Python Software Foundation License | anyio, fastapi, pydantic, starlette, and others |
| annotated-doc | 0.0.7 | MIT | Copyright (c) 2025 Sebastián Ramírez | fastapi |
| opentelemetry-api | 1.45.2 | Apache-2.0 | (Apache License 2.0 text, no separate copyright line) | fastapi |
| idna | 3.20 | BSD-3-Clause | Copyright (c) 2013-2026, Kim Davies and contributors | anyio, httpx |
| click | 8.5.1 | BSD-3-Clause | Copyright 2014 Pallets | uvicorn |
| h11 | 0.16.0 | MIT | Copyright (c) 2016 Nathaniel J. Smith and other contributors | uvicorn, httpcore |
| httpx | 0.28.1 | BSD-3-Clause | Copyright © 2019, Encode OSS Ltd | `taperescue[server]` (see note) |
| httpcore | 1.0.9 | BSD-3-Clause | Copyright © 2020, Encode OSS Ltd | httpx |
| certifi | 2026.11.20 | MPL-2.0 | (Mozilla Public License 2.0 text) | httpx, httpcore |
| pip | 25.0.1 | MIT | Copyright (c) 2008-present The pip developers | the `python:3.12-slim-bookworm` base image |

Notes:

- **pydantic_core** is a compiled Rust extension. Its wheel includes a CycloneDX SBOM (`pydantic_core-2.50.5.dist-info/sboms/pydantic-core.cyclonedx.json`) that lists 101 Rust crates compiled into it. Their licenses are permissive: mostly `MIT OR Apache-2.0`, plus `MIT`, `Unicode-3.0`, `Unicode-DFS-2016`, `Zlib`, `Unlicense OR MIT`, `BSD-2-Clause OR Apache-2.0 OR MIT`, and `Apache-2.0 WITH LLVM-exception`. One crate is offered as `MIT OR Apache-2.0 OR LGPL-2.1-or-later`, which can be used under MIT. The SBOM in the image is the authoritative per-crate list.
- **httpx** (with httpcore and certifi) is installed because the `server` extra in `pyproject.toml` lists it. Only the server tests use it, through FastAPI's `TestClient`. The server code does not import it.
- **certifi** is MPL-2.0. It is distributed unmodified, and its source is available from https://github.com/certifi/python-certifi.

### System libraries and runtime

| Component | Version in the image | License | Source of the component |
| --- | --- | --- | --- |
| CPython | 3.12.16 | PSF-2.0 (`/usr/local/lib/python3.12/LICENSE.txt`) | `python:3.12-slim-bookworm` base image |
| zlib (`zlib1g`) | 1:1.2.13.dfsg-1 | Zlib (`/usr/share/doc/zlib1g/copyright`) | Debian bookworm package, installed by the `Dockerfile`. `ScanTape` links `libz.so.1` dynamically |
| GNU C++ runtime (`libstdc++6`, `libgcc-s1`) | 12.2.0-14+deb12u1 | GPL-3.0-or-later with the GCC Runtime Library Exception 3.1 (`/usr/share/doc/libstdc++6/copyright`) | Debian bookworm package. `ScanTape` links these dynamically |
| GNU C library (`libc6`) | 2.36-9+deb12u14 | LGPL-2.1-or-later and others (`/usr/share/doc/libc6/copyright`) | `python:3.12-slim-bookworm` base image |

The base image also contains the other packages of Debian 12 (bookworm) `slim`. Their licenses are in `/usr/share/doc/*/copyright` inside the image, and their source is available from Debian.

### JavaScript in the web UI bundle

Only the production dependencies of `web/package.json` reach the shipped bundle in `web/dist/`. `npm ls --omit=dev --all` lists exactly three packages, and their code is present in `web/dist/assets/index-*.js`. The minifier removes license comments from the bundle, so this file is where their notices are reproduced.

| Package | Version | License | Copyright |
| --- | --- | --- | --- |
| react | 19.3.1 | MIT | Copyright (c) Meta Platforms, Inc. and affiliates. |
| react-dom | 19.3.1 | MIT | Copyright (c) Meta Platforms, Inc. and affiliates. |
| scheduler | 0.28.0 | MIT | Copyright (c) Meta Platforms, Inc. and affiliates. |

### MIT License text

These components are distributed under the MIT License: react, react-dom, scheduler, fastapi, anyio, pydantic, pydantic_core, annotated-types, typing-inspection, annotated-doc, h11, pip, and AtomTapeUtils. The license text is as follows, with the copyright line given in the tables above:

```
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The full texts of the BSD-3-Clause, Apache-2.0, MPL-2.0, and PSF-2.0 licenses, with their copyright lines, are installed with each Python package in `site-packages/*.dist-info/licenses/` in the image.

## 3. Development, test, and build tools (not distributed)

These tools are used to develop, test, or build TapeRescue. None of them is copied into the runtime image or the `web/dist` bundle.

### Python (local `.venv`, `dev` extra)

| Package | Version | License |
| --- | --- | --- |
| pytest | 9.2.0 | MIT |
| hypothesis | 6.170.17 | MPL-2.0 |
| pluggy | 1.7.0 | MIT |
| iniconfig | 2.3.1 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| sortedcontainers | 2.4.0 | Apache-2.0 |
| Pygments | 2.21.0 | BSD-2-Clause |

### npm (`devDependencies` in `web/package.json`)

These are the direct development dependencies. With their transitive dependencies, `npm ls --all` lists 99 packages, all used only by the `web` build stage and by tests.

| Package | Version | License |
| --- | --- | --- |
| typescript | 5.9.3 | Apache-2.0 |
| vite | 8.3.8 | MIT |
| @vitejs/plugin-react | 6.3.0 | MIT |
| vitest | 5.1.1 | MIT |
| jsdom | 29.1.1 | MIT |
| @testing-library/react | 16.3.3 | MIT |
| @testing-library/dom | 10.4.2 | MIT |
| @types/node | 22.20.5 | MIT |
| @types/react | 19.3.0 | MIT |
| @types/react-dom | 19.3.0 | MIT |

### Container build stages

| Base image (tag) | Used for | In the runtime image? |
| --- | --- | --- |
| `debian:bookworm-slim` | `decoder` stage. Installs `g++`, `make`, `cmake`, `git`, `zlib1g-dev` from Debian to compile `ScanTape` | No. Only the `ScanTape` binary and `BUILD_INFO` are copied out |
| `node:22-bookworm-slim` | `web` stage. Runs `npm ci` and `npm run build` | No. Only `web/dist/` is copied out |
| `python:3.12-slim-bookworm` | `wheel` stage (builds the `taperescue` wheel) and the `runtime` stage | Yes, as the runtime base |

The `Dockerfile` refers to base images by tag, not digest. The `python:3.12-slim-bookworm` image used for `taperescue:local` was `python@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df`.

## Not bundled: BBC Micro ROMs and JSBeeb

TapeRescue contains no BBC Micro ROM images and no copy of the JSBeeb emulator, in either the repository or the container image. The `.dockerignore` excludes `*.rom` files and `jsbeeb` paths. A scan of the exported image found no `.rom` files and no paths containing `jsbeeb`. The web UI links to JSBeeb at https://bbc.xania.org/ as an ordinary outbound link. Users load the downloaded `.uef` file into JSBeeb from their own disk, and TapeRescue sends nothing to the emulator.
