# syntax=docker/dockerfile:1

# Builds the NautilusTrader wheel from the Guild fork at the commit uv.lock pins.
#
# This is a tool, not part of any image. The runtime image installs
# NautilusTrader from a published wheel pinned by sha256; producing that wheel
# is what this file is for. It exists only while `uv.lock` still resolves
# NautilusTrader from a git commit, and retires together with that git source,
# `make nautilus-wheel` and `scripts/nautilus_source_pin.py`.
#
# Every input is derived rather than written down: the commit and version come
# from `uv.lock` via `scripts/nautilus_source_pin.py`, the Rust toolchain from
# the checkout's own `rust-toolchain.toml`, and the maturin version from the
# fork's own `[build-system]` table. A constant copied into this file would be
# a second claim about the same thing, free to drift from the first without
# anything going red.
#
# Nothing but the wheel leaves here, so the stage does no housekeeping on its
# own layers.
#
# Memory: the fork's `[profile.release]` uses `lto = "fat"` with
# `codegen-units = 1`. Linking `nautilus-pyo3` under those settings needs well
# over 8 GiB; a 7.7 GiB Docker VM gets the linker killed by the OOM killer
# after roughly eighty minutes of compilation, reported as
# `ResourceExhausted: cannot allocate memory`. Raise the VM's memory rather
# than relaxing the profile -- a wheel built with different optimisation
# settings is not the wheel the fork's own release build produces.

ARG PYTHON_BASE_IMAGE=python:3.12.13-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

FROM ${PYTHON_BASE_IMAGE} AS nautilus-wheel-builder

ARG NT_GIT_URL
ARG NT_GIT_SHA
ARG NT_VERSION
ARG NT_SUBDIRECTORY
ARG CARGO_BUILD_JOBS=2

# Mirrors the fork's own build environment (.docker/nautilus_trader.dockerfile).
ENV CC=clang \
    PYO3_PYTHON=/usr/local/bin/python3 \
    CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    CARGO_BUILD_JOBS=${CARGO_BUILD_JOBS} \
    PATH=/usr/local/cargo/bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin

# Docker leaves an unsupplied build argument empty rather than failing. Without
# this the build would compile whatever the remote's default branch happens to
# be, and the wheel would carry no honest answer about what is inside it.
RUN set -eu; \
    test -n "${NT_GIT_URL}" || { echo "NT_GIT_URL is required; pass --build-arg" >&2; exit 2; }; \
    test -n "${NT_VERSION}" || { echo "NT_VERSION is required; pass --build-arg" >&2; exit 2; }; \
    test -n "${NT_SUBDIRECTORY}" || { echo "NT_SUBDIRECTORY is required; pass --build-arg" >&2; exit 2; }; \
    echo "${NT_GIT_SHA}" | grep -Eq '^[0-9a-f]{40}$' \
      || { echo "NT_GIT_SHA must be a 40-character commit sha, got '${NT_GIT_SHA}'" >&2; exit 2; }

# Package list taken from the fork's `.docker/nautilus_trader.dockerfile`,
# which is what its own release build uses.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates capnproto clang curl git libcapnp-dev lld make patchelf pkg-config

WORKDIR /src
RUN set -eux; \
    git init --quiet .; \
    git remote add origin "${NT_GIT_URL}"; \
    git fetch --depth 1 --quiet origin "${NT_GIT_SHA}"; \
    git checkout --quiet FETCH_HEAD; \
    test "$(git rev-parse HEAD)" = "${NT_GIT_SHA}"

# The toolchain is whatever the checkout asks for. Naming a version here would
# let this file and `rust-toolchain.toml` disagree silently.
RUN set -eux; \
    channel="$(python -c "import pathlib, tomllib; print(tomllib.loads(pathlib.Path('rust-toolchain.toml').read_text())['toolchain']['channel'])")"; \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --no-modify-path --profile minimal --default-toolchain "${channel}"; \
    rustc --version | grep -Fq "${channel}"

# The fork pins maturin exactly; we install that pin and then check the binary
# agrees. Should the fork ever relax the pin to a range, the build stops here
# rather than quietly compiling with some other maturin.
RUN set -eux; \
    maturin_pin="$(python -c "import re, sys, tomllib, pathlib; reqs = tomllib.loads(pathlib.Path('${NT_SUBDIRECTORY}/pyproject.toml').read_text())['build-system']['requires']; pins = [r.strip() for r in reqs if re.fullmatch(r'maturin==[0-9][0-9A-Za-z.+!-]*', r.strip())]; sys.exit('the fork must pin maturin exactly in [build-system].requires, found: %r' % (reqs,)) if len(pins) != 1 else print(pins[0].split('==', 1)[1])")"; \
    pip install --root-user-action=ignore "maturin==${maturin_pin}"; \
    maturin --version | grep -Fq "${maturin_pin}"

# `--locked` keeps Cargo.lock authoritative. The wheel is found by a glob that
# says nothing about the version, and the version is then checked against the
# filename -- accepting either the literal spelling or the escaped one, because
# whether maturin escapes `+` in a local version is its business, not an
# assumption worth betting a build on. An unmatched glob leaves the pattern
# itself as the single argument, so `test -f` rather than the count is what
# catches an empty output directory.
# The cargo target directory is cached so that a retry does not recompile the
# six hundred odd dependency crates again; cargo's own fingerprints decide what
# is still valid, so the cache cannot make the wheel stale.
#
# Memory is sampled because this step is the one that fails on a machine that
# is merely large. A trajectory printed every thirty seconds survives the OOM
# killer, which a final measurement would not.
RUN --mount=type=cache,target=/src/target,sharing=locked \
    set -eux; \
    cd "${NT_SUBDIRECTORY}"; \
    ( while sleep 30; do \
        printf 'cgroup memory: current=%s peak=%s\n' \
          "$(cat /sys/fs/cgroup/memory.current 2>/dev/null || echo unavailable)" \
          "$(cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo unavailable)"; \
      done ) & \
    sampler="$!"; \
    maturin build --locked --release --out /wheels; \
    kill "$sampler" 2>/dev/null || true; \
    printf 'cgroup memory peak: %s\n' "$(cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo unavailable)"; \
    set -- /wheels/nautilus_trader-*.whl; \
    test "$#" -eq 1; \
    test -f "$1"; \
    python -c "import re, sys; name, version = sys.argv[1], sys.argv[2]; escaped = re.sub(r'[^\w\d.]+', '_', version); sys.exit('built %s, which carries neither %s nor %s' % (name, version, escaped)) if ('-' + version + '-') not in name and ('-' + escaped + '-') not in name else None" "$(basename "$1")" "${NT_VERSION}"; \
    printf 'built %s\n' "$(basename "$1")"

# `docker build --output` copies this stage's filesystem out; keeping it to the
# wheel alone means the caller receives exactly the artifact and nothing else.
FROM scratch AS wheel
COPY --from=nautilus-wheel-builder /wheels/ /
