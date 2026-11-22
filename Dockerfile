# syntax=docker/dockerfile:1
#
# TapeRescue single-container image (Requirement 17.6): the patched ScanTape
# decoder, the FastAPI server, and the built web UI.
#
#   docker build -t taperescue .
#   docker run --rm -p 8000:8000 taperescue
#
# The image holds no BBC Micro ROM files and no emulator (Requirement 19.3).
# The only bundled captures are the generated STARMAP fixtures (Requirement 19.4).

# ---------------------------------------------------------------------------
# Stage 1: build the patched decoder from the vendored source (no network).
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS decoder

# build_decoder.sh needs git (git init / git apply in build/decoder-src and the
# tree-id check), cmake, a C++17 compiler, make, and zlib headers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        g++ make cmake git zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY third_party/ third_party/
COPY patches/ patches/
COPY scripts/build_decoder.sh scripts/build_decoder.sh
# ScanTape exits 255 after printing usage; the check only needs the text,
# which must list the patched -json option.
RUN scripts/build_decoder.sh \
    && { build/decoder/ScanTape > /tmp/usage.txt 2>&1 || true; } \
    && grep -q -- '-json <file>' /tmp/usage.txt \
    && cat build/decoder/BUILD_INFO

# ---------------------------------------------------------------------------
# Stage 2: build the web UI. web/package.json requires Node ^22.12 or >=24
# (Vite 8), so this uses Node 22 rather than Node 20.
# ---------------------------------------------------------------------------
FROM node:22-bookworm-slim AS web

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 3: build the taperescue wheel.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS wheel

WORKDIR /src
COPY pyproject.toml LICENSE TapeRescue-Kiro-Brief.md ./
COPY taperescue/ taperescue/
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /dist .

# ---------------------------------------------------------------------------
# Stage 4: runtime.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# ScanTape links libz, libstdc++ and libgcc_s dynamically. The decoder stage
# uses the same Debian release (bookworm), so glibc and libstdc++ match.
RUN apt-get update \
    && apt-get install -y --no-install-recommends zlib1g libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 taperescue \
    && useradd --system --uid 10001 --gid taperescue --home-dir /nonexistent \
        --no-create-home --shell /usr/sbin/nologin taperescue

# The server extra dependencies, pinned to the versions the API tests ran on.
COPY --from=wheel /dist/ /tmp/dist/
RUN pip install --no-cache-dir \
        "$(ls /tmp/dist/taperescue-*.whl)[server]" \
        "fastapi==0.165.0" \
        "starlette==1.10.0" \
        "uvicorn==0.54.0" \
        "python-multipart==0.0.34" \
        "anyio==4.16.0" \
    && rm -rf /tmp/dist

WORKDIR /app
# server/ is not part of the taperescue wheel (pyproject.toml packages only
# taperescue*), so it is copied as a source package and imported from /app.
COPY server/__init__.py server/app.py server/sessions.py server/
COPY --from=decoder /src/build/decoder/ScanTape /app/bin/ScanTape
COPY --from=decoder /src/build/decoder/BUILD_INFO /app/bin/BUILD_INFO
COPY --from=web /web/dist/ /app/web/dist/
COPY fixtures/starmap/clean.wav fixtures/starmap/damaged_a.wav \
     fixtures/starmap/damaged_b.wav /app/fixtures/starmap/

ENV TAPERESCUE_DECODER=/app/bin/ScanTape \
    TAPERESCUE_FIXTURES=/app/fixtures/starmap \
    TAPERESCUE_STATIC=/app/web/dist \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Numeric IDs so the runtime can verify the user is not root (uid 10001 is
# the taperescue user created above).
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000"]
