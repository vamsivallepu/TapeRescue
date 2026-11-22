#!/usr/bin/env bash
# Build the pinned AtomTapeUtils ScanTape decoder with the TapeRescue patch.
#
# Steps:
#   1. Fetch the pinned revision into third_party/AtomTapeUtils only when that
#      tree is absent (the only step that needs network access).
#   2. Verify the vendored tree is the pinned revision.
#   3. Copy it to build/decoder-src/ (the vendored tree is never modified).
#   4. git apply --check, then git apply, the JSON observation patch.
#   5. Build the ScanTape target with the upstream CMake files and copy the
#      binary to build/decoder/ScanTape.
#
# Usage: scripts/build_decoder.sh [--unpatched] [--jobs N]
#   --unpatched  Build the stock decoder without the patch. Required when the
#                patch file does not exist; otherwise a missing patch is an error.
#
# Environment overrides (used by tests):
#   TAPERESCUE_ATU_URL     upstream repository URL
#   TAPERESCUE_ATU_VENDOR  vendored source directory
#   TAPERESCUE_ATU_PATCH   patch file
#   TAPERESCUE_BUILD_DIR   output root (default: <repo>/build)
#
# Exit codes:
#   0 success
#   1 usage error or missing tool
#   2 failed to fetch the pinned revision
#   3 patch does not apply to the pinned revision
#   4 vendored tree does not match the pinned revision
#   5 decoder build failed
#   6 patch file missing (pass --unpatched to build the stock decoder)
set -euo pipefail

readonly ATU_REVISION="98d1a8d1bed00aa22f1d72e3d5e91a254a69ec64"
# git tree id of the pinned commit (git rev-parse <rev>^{tree}).
readonly ATU_TREE="faf78c9b7ed3cf4a1cd2c2aaabc68716e8ec9f86"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
atu_url="${TAPERESCUE_ATU_URL:-https://github.com/joakimoh/AtomTapeUtils.git}"
vendor_dir="${TAPERESCUE_ATU_VENDOR:-$repo_root/third_party/AtomTapeUtils}"
patch_file="${TAPERESCUE_ATU_PATCH:-$repo_root/patches/atomtapeutils-json-observations.patch}"
build_root="${TAPERESCUE_BUILD_DIR:-$repo_root/build}"
src_dir="$build_root/decoder-src"
cmake_dir="$build_root/decoder-build"
out_dir="$build_root/decoder"

unpatched=0
jobs="$(nproc 2>/dev/null || echo 2)"

die() {
    local code="$1"
    shift
    echo "error: $*" >&2
    exit "$code"
}

log() { echo "build_decoder: $*"; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --unpatched) unpatched=1 ;;
        --jobs)
            [ "$#" -ge 2 ] || die 1 "--jobs needs a value"
            jobs="$2"
            shift
            ;;
        -h|--help)
            sed -n '2,33p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) die 1 "unknown option: $1" ;;
    esac
    shift
done

for tool in git cmake tar; do
    command -v "$tool" >/dev/null 2>&1 || die 1 "required tool not found: $tool"
done

# Git commands below run with a fixed identity-free environment and must not
# discover the TapeRescue repository from a parent directory.
export GIT_CONFIG_NOSYSTEM=1

fetch_revision() {
    local tmp
    tmp="$(mktemp -d)"
    fetch_failed() {
        rm -rf "$tmp"
        echo "error: failed to fetch AtomTapeUtils revision $ATU_REVISION" >&2
        exit 2
    }
    log "fetching $ATU_REVISION from $atu_url"
    { git init -q "$tmp/repo" \
        && git -C "$tmp/repo" remote add origin "$atu_url" \
        && git -C "$tmp/repo" fetch -q --depth 1 origin "$ATU_REVISION" \
        && [ "$(git -C "$tmp/repo" rev-parse FETCH_HEAD)" = "$ATU_REVISION" ]; } || fetch_failed
    mkdir -p "$tmp/tree"
    git -C "$tmp/repo" archive --format=tar "$ATU_REVISION" | tar -x -C "$tmp/tree" || fetch_failed
    printf '%s\n' "$ATU_REVISION" > "$tmp/tree/REVISION"
    mkdir -p "$(dirname "$vendor_dir")"
    mv "$tmp/tree" "$vendor_dir" || fetch_failed
    rm -rf "$tmp"
    log "vendored $ATU_REVISION into $vendor_dir"
}

# Compute the git tree id of a directory without touching any repository.
tree_id_of() {
    local dir="$1" tmp id
    tmp="$(mktemp -d)"
    git init -q --bare "$tmp/g"
    git --git-dir="$tmp/g" --work-tree="$dir" add -A -- . ':!REVISION' ':!.git'
    id="$(git --git-dir="$tmp/g" write-tree)"
    rm -rf "$tmp"
    printf '%s' "$id"
}

verify_revision() {
    if [ -e "$vendor_dir/.git" ]; then
        local head
        head="$(git -C "$vendor_dir" rev-parse HEAD 2>/dev/null || true)"
        [ "$head" = "$ATU_REVISION" ] \
            || die 4 "vendored tree is at '$head', expected AtomTapeUtils revision $ATU_REVISION"
    else
        [ -f "$vendor_dir/REVISION" ] \
            || die 4 "missing $vendor_dir/REVISION; expected AtomTapeUtils revision $ATU_REVISION"
        local recorded
        recorded="$(tr -d '[:space:]' < "$vendor_dir/REVISION")"
        [ "$recorded" = "$ATU_REVISION" ] \
            || die 4 "REVISION is '$recorded', expected AtomTapeUtils revision $ATU_REVISION"
    fi
    local tree
    tree="$(tree_id_of "$vendor_dir")"
    [ "$tree" = "$ATU_TREE" ] \
        || die 4 "vendored source tree $tree differs from AtomTapeUtils revision $ATU_REVISION (tree $ATU_TREE)"
    log "verified vendored source is revision $ATU_REVISION (tree $ATU_TREE)"
}

prepare_source() {
    rm -rf "$src_dir"
    mkdir -p "$src_dir"
    tar -C "$vendor_dir" --exclude=./.git -cf - . | tar -C "$src_dir" -xf -
    # A repository of its own, so git apply resolves patch paths against
    # build/decoder-src/ and not against the enclosing TapeRescue repository.
    git init -q "$src_dir"
}

apply_patch() {
    if [ ! -f "$patch_file" ]; then
        if [ "$unpatched" -eq 1 ]; then
            log "patch $patch_file not present; building the stock decoder (--unpatched)"
            return
        fi
        die 6 "patch file not found: $patch_file (pass --unpatched to build the stock decoder)"
    fi
    if [ "$unpatched" -eq 1 ]; then
        log "--unpatched given; not applying $patch_file"
        return
    fi
    if ! git -C "$src_dir" apply --check "$patch_file"; then
        echo "error: patch does not apply to pinned revision" >&2
        exit 3
    fi
    if ! git -C "$src_dir" apply --verbose "$patch_file"; then
        echo "error: patch does not apply to pinned revision" >&2
        exit 3
    fi
    log "applied $patch_file"
}

build() {
    rm -rf "$cmake_dir"
    log "configuring with $(cmake --version | head -n 1)"
    cmake -S "$src_dir" -B "$cmake_dir" -DCMAKE_BUILD_TYPE=Release \
        || die 5 "decoder build failed (cmake configure)"
    cmake --build "$cmake_dir" --target ScanTape -j "$jobs" \
        || die 5 "decoder build failed (ScanTape target)"
    # Upstream sets CMAKE_RUNTIME_OUTPUT_DIRECTORY to <source>/bin.
    local built="$src_dir/bin/ScanTape"
    [ -x "$built" ] || die 5 "decoder build failed: $built not produced"
    mkdir -p "$out_dir"
    cp "$built" "$out_dir/ScanTape"
    {
        echo "revision=$ATU_REVISION"
        if [ "$unpatched" -eq 1 ] || [ ! -f "$patch_file" ]; then
            echo "patch=none"
        else
            echo "patch=$(basename "$patch_file")"
            echo "patch_sha256=$(sha256sum "$patch_file" | cut -d' ' -f1)"
        fi
    } > "$out_dir/BUILD_INFO"
    log "built $out_dir/ScanTape"
}

if [ ! -d "$vendor_dir" ]; then
    fetch_revision
fi
verify_revision
prepare_source
apply_patch
build
