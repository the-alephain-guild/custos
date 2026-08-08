# custos-runner complete official runtime image.
#
# Multi-stage: `builder` installs the locked third-party runtime plus the three
# locally built Custos wheels against Python 3.12.
# `runtime` copies site-packages + the `arx-runner` console script over into
# a slim base and switches to the non-privileged `custos` user (UID/GID 1000).
#
# The builder consumes locally built runner and toolkit wheels. This decouples
# image construction from PyPI publication; CI stages the exact signed wheel
# artifacts produced by its build job.

ARG PYTHON_BASE_IMAGE=python:3.12.13-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

FROM ${PYTHON_BASE_IMAGE} AS builder

# Third-party dependencies are exported from uv.lock with hashes. Workspace
# packages are installed from exactly one local wheel apiece, without any
# dependency resolution or fallback to an externally published package.
COPY docker/runtime-requirements.lock /tmp/runtime-requirements.lock
COPY dist/*.whl /tmp/wheels/
RUN set -eux; \
    pip install \
      --root-user-action=ignore \
      --timeout 300 \
      --require-hashes \
      --no-deps \
      --requirement /tmp/runtime-requirements.lock; \
    set -- /tmp/wheels/custos_strategy_toolkit-*.whl; \
    test "$#" -eq 1; \
    test -f "$1"; \
    toolkit_wheel="$1"; \
    set -- /tmp/wheels/custos_strategy_toolkit_nautilus-*.whl; \
    test "$#" -eq 1; \
    test -f "$1"; \
    nautilus_wheel="$1"; \
    set -- /tmp/wheels/custos_runner-*.whl; \
    test "$#" -eq 1; \
    test -f "$1"; \
    runner_wheel="$1"; \
    pip install \
      --root-user-action=ignore \
      --no-deps \
      "${toolkit_wheel}" \
      "${nautilus_wheel}" \
      "${runner_wheel}"; \
    pip check

FROM ${PYTHON_BASE_IMAGE} AS runtime

# The vault runtime shells out to age and sops. Keep curl and CA roots in the
# final image for operator diagnostics and explicit standalone bootstrap use.
RUN apt-get update \
    && apt-get install -y --no-install-recommends age ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# sops is not packaged in Debian stable. Pin its upstream binary, map Docker's
# architecture names explicitly, and verify the release SHA-256 before install.
# Checksums: https://github.com/getsops/sops/releases/tag/v3.13.2
ARG TARGETARCH
ARG SOPS_VERSION=3.13.2
ARG SOPS_SHA256_AMD64=154dfe4cd70554bdd82b98e4cd4acf191d43d01ead6f00a73477aa44c4ac42ef
ARG SOPS_SHA256_ARM64=78abf2e15c86250a1553ae6f53aba96be6b2a8126f160b1534959add3467ad76
RUN set -eux; \
    architecture="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$architecture" in \
        amd64) checksum="$SOPS_SHA256_AMD64" ;; \
        arm64) checksum="$SOPS_SHA256_ARM64" ;; \
        *) echo "unsupported sops architecture: $architecture" >&2; exit 1 ;; \
    esac; \
    url="https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.${architecture}"; \
    curl --fail --location --silent --show-error "$url" --output /usr/local/bin/sops; \
    echo "$checksum  /usr/local/bin/sops" | sha256sum --check --strict -; \
    chmod 0755 /usr/local/bin/sops

# Non-root user (UID/GID 1000). We create a real HOME so that `PerKeyVault`,
# the reconcile loop's state file, and any structured logs can land under
# `~/.arx/` inside the container (which then maps out via VOLUME).
RUN useradd --uid 1000 --create-home --home-dir /home/custos custos
ENV HOME=/home/custos

# Copy the installed wheel from the builder stage (site-packages + the
# generated `arx-runner` console script). We intentionally do NOT re-run
# `pip install` here — a duplicate install is the classic multi-stage
# builder leak that FM11 / `test_docker_image_size.py` guards against.
COPY --from=builder /usr/local/lib/python3.12/site-packages \
                    /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/arx-runner /usr/local/bin/arx-runner

# Persist runtime state on the host. `docs/ops/05-deployment.md` §Docker
# Runtime Volume Mount documents the `-v ~/.arx:/home/custos/.arx` pattern
# so operator-provisioned KEK vaults survive container restarts.
VOLUME ["/home/custos/.arx"]

# Pre-create the mount point owned by `custos`: without this an anonymous
# volume mount inherits root ownership, and the first
# `arx-runner enroll` write to `~/.arx/runner.toml` fails with EACCES because
# we run as UID 1000.
RUN mkdir -p /home/custos/.arx /home/custos/.arx/vault /home/custos/.arx/state \
    && chown -R custos:custos /home/custos

USER 1000:1000
WORKDIR /opt/custos

# Keep the executable and default action separate so management commands work
# without overriding the entrypoint while the no-argument path remains the
# reconcile daemon.
ENTRYPOINT ["arx-runner"]
CMD ["start"]
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["arx-runner", "health"]

# OCI provenance labels let auditors trace a running image back to source.
# The concrete `revision` / `created` values are injected by
# the CI workflow via `docker build --label` overrides at release time.
LABEL org.opencontainers.image.title="custos-runner" \
      org.opencontainers.image.description="Non-custodial self-hosted execution runner (Alephain Guild)" \
      org.opencontainers.image.source="https://github.com/the-alephain-guild/custos" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="The Alephain Guild"
