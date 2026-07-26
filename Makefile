# custos Makefile
#
# Standalone open-source repository entrypoint. Standardized validation targets
# keep shell execution deterministic and avoid permission drift from ad-hoc commands.

.PHONY: help install install-nt install-lts fmt fmt-check lint check toolkit-typecheck test test-baseline test-nt test-docker test-docker-existing verify verify-base-clean verify-nt verify-runtime verify-runtime-existing verify-local-v030 verify-nats-revocation verify-authenticated-runtime-projection verify-runner-fact-publication clean toolkit-sync-check strategy-contract-assets check-strategy-contract-assets check-runner-machine-request-consumer-assets dist sign docker-build docker-build-local-v030 docker-sign verify-release release check-commit-hook commit-hook-dry-run

# Default target: help
.DEFAULT_GOAL := help

help:  ## List all targets and descriptions
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install:  ## Install dependencies (dev extra) — uv sync --extra dev
	uv sync --package custos-runner --extra dev

install-nt:  ## Install dependencies + NT runtime (requires Python 3.12+) — uv sync --extra dev --extra nautilus
	uv sync --python 3.12 --package custos-runner --extra dev --extra nautilus

install-lts:  ## Install release-engineering LTS toolchain (sigstore + pytest-docker)
	uv sync --extra dev --extra lts

fmt:  ## Format source files (ruff format writes changes)
	uv run --package custos-runner ruff format src/ tests/ scripts/ packages/

fmt-check:  ## Check formatting (ruff format --check, no file changes)
	uv run --package custos-runner ruff format --check src/ tests/ scripts/ packages/

lint:  ## Lint check (ruff check)
	uv run --package custos-runner ruff check src/ tests/ scripts/ packages/

toolkit-typecheck:  ## Whole-package strict typing plus versioned T4 -> T4b evidence
	uv run --package custos-runner --extra dev mypy --config-file packages/custos-strategy-toolkit/pyproject.toml packages/custos-strategy-toolkit/src/custos_toolkit
	uv run --package custos-runner --extra dev --extra nautilus mypy --config-file packages/custos-strategy-toolkit-nautilus/pyproject.toml packages/custos-strategy-toolkit-nautilus/src/custos_toolkit_nautilus
	uv run --package custos-runner --extra dev --extra nautilus python scripts/check-toolkit-typing-closure.py

check: fmt-check lint  ## Combined formatting check and lint

check-commit-hook:  ## Run the repository pre-commit hook directly (dry, no commit created)
	.git/hooks/pre-commit

commit-hook-dry-run:  ## Validate the pre-commit hook path in a dry way (no real commit, always zero exit on success)
	@echo "Running pre-commit hook in dry mode..."
	make check-commit-hook

test:  ## Run full pytest (base profile; NT tests importorskip; includes known-failing wire_shapes)
	uv run pytest tests/

verify-nats-revocation:  ## Run opt-in real NATS User-JWT resolver revocation gate
	CUSTOS_RUN_REAL_NATS_REVOCATION=1 uv run pytest tests/integration/test_nats_revocation.py -v

verify-authenticated-runtime-projection:  ## Run signed command through Custos and Crucible PostgreSQL projection
	@test -n "$(CRUCIBLE_REPO)" || { echo "CRUCIBLE_REPO is required" >&2; exit 2; }
	CUSTOS_RUN_REAL_NATS_REVOCATION=1 CRUCIBLE_REPO="$(CRUCIBLE_REPO)" uv run pytest tests/integration/test_nats_revocation.py -v

verify-runner-fact-publication:  ## Run opt-in real JetStream RunnerFact outbox/PubAck gate
	CUSTOS_RUN_REAL_RUNNER_FACT_PUBLICATION=1 uv run pytest tests/integration/test_runner_fact_publication.py -v

test-baseline:  ## Run the standalone green baseline
	# Base gate does not hard-require NT: if nautilus is missing, NT host tests are skipped by pytest.importorskip.
	uv run pytest tests/

test-nt:  ## Run NT gate (requires py3.12+): run NT host tests under --extra nautilus
	# Preflight hard gate: if NT is still unavailable in nautilus (py<3.12 or install failure), NT host tests are silently skipped by pytest.importorskip and verify-nt could appear green.
	# Validate NT importability first; fail early if missing.
	@uv run --extra nautilus python -c "import nautilus_trader; assert nautilus_trader.__version__" \
		|| (echo "❌ nautilus_trader is not installed with nautilus extra (requires Python 3.12+); NT gate cannot run"; exit 1)
	uv run --extra nautilus pytest tests/

verify: check test-baseline  ## Base release gate: check + green test-baseline
	@echo "✅ make verify passed"

verify-base-clean:  ## Clean dev-only sync followed by the base release gate
	uv sync --package custos-runner --extra dev
	$(MAKE) verify

verify-nt: check test-nt  ## NT release gate (requires py3.12+): check + green test-nt
	@echo "✅ make verify-nt passed"

clean:  ## Remove pycache / pytest cache / ruff cache
	find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache

# --- Plan 12 release engineering ---------------------------------------------
# `make dist` produces the signed-artifact input; `docker-build` consumes it.
# `verify-release` is the post-publish smoke gate the CI job invokes after
# uploading to PyPI + GHCR.

LOCAL_IMAGE ?= custos-runner:v0.3.0
SOURCE_REVISION := $(shell git rev-parse HEAD)

runtime-lock:  ## Export the sole hash-pinned production dependency set from uv.lock
	mkdir -p docker
	uv export --frozen --no-dev --extra nautilus --no-emit-workspace --no-header --format requirements-txt --output-file docker/runtime-requirements.lock

check-runtime-lock:  ## Fail when the committed production dependency set drifts from uv.lock
	@tmp="$$(mktemp)"; \
	trap 'rm -f "$$tmp"' EXIT; \
	uv export --frozen --no-dev --extra nautilus --no-emit-workspace --no-header --format requirements-txt --output-file "$$tmp"; \
	diff -u docker/runtime-requirements.lock "$$tmp"

dist: check-runtime-lock  ## Build the runner sdist and all three exact runtime wheels
	rm -rf dist/
	uv build --out-dir dist
	uv build --package custos-strategy-toolkit --wheel --out-dir dist
	uv build --package custos-strategy-toolkit-nautilus --wheel --out-dir dist

sign:  ## Sign every wheel under dist/ with sigstore keyless (requires OIDC; runs in CI)
	bash .github/workflows/scripts/sign-wheel.sh

docker-build: dist  ## Build custos-runner:test image from the local dist/*.whl wheel
	docker build \
		--label org.opencontainers.image.revision=$(SOURCE_REVISION) \
		--tag custos-runner:test \
		.

docker-build-local-v030: dist  ## Build the local v0.3.0 consumer image with source provenance
	@dirty="$$(git status --porcelain --untracked-files=normal)"; \
		if [ -n "$$dirty" ]; then \
			echo "local consumer image requires a clean worktree:" >&2; \
			echo "$$dirty" >&2; \
			exit 1; \
		fi
	docker build \
		--label org.opencontainers.image.revision=$(SOURCE_REVISION) \
		--tag $(LOCAL_IMAGE) \
		.

docker-sign:  ## Sign the built docker image with cosign keyless (requires OIDC; runs in CI)
	@echo "docker-sign is exercised by the CI release workflow (needs GHCR + OIDC)." >&2
	@echo "Run cosign manually only for out-of-band re-signing." >&2

test-docker-existing:  ## Run runtime contracts against CUSTOS_TEST_IMAGE (default custos-runner:test)
	uv run pytest -m docker tests/test_docker_non_root.py tests/test_docker_entrypoint_help.py tests/test_docker_image_size.py tests/test_docker_runtime_contract.py -v

test-docker: docker-build test-docker-existing  ## Build local image, then run complete runtime contracts

verify-runtime-existing: test-docker-existing  ## Gate an existing image and standalone deployment wire
	uv run pytest tests/integration/test_standalone_runtime.py -v

verify-runtime: test-docker  ## Gate the complete image and standalone deployment wire
	uv run pytest tests/integration/test_standalone_runtime.py -v

verify-local-v030: docker-build-local-v030  ## Build and gate the local downstream image
	CUSTOS_TEST_IMAGE=$(LOCAL_IMAGE) \
		CUSTOS_EXPECTED_REVISION=$(SOURCE_REVISION) \
		$(MAKE) verify-runtime-existing
	docker image inspect $(LOCAL_IMAGE) \
		--format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}'

verify-release:  ## Post-publish smoke: pull wheel + verify sig, pull image + verify sig + smoke run
	@bash .github/workflows/scripts/verify-release.sh $(VERSION)

release: dist sign docker-build docker-sign  ## Full local release rehearsal (real publish still lives in CI)
	@echo "Local release rehearsal complete. Publish to PyPI + GHCR runs in CI." >&2

toolkit-sync-check:  ## Diff vendored toolkit against upstream ps shared/ (+ optional pandas_ta) for drift
	@if [ -z "$$PS_ROOT" ]; then \
		echo "❌ PS_ROOT is required (path to a local philosophers-stone checkout)" >&2; \
		echo "   usage: PS_ROOT=/path/to/philosophers-stone make toolkit-sync-check" >&2; \
		exit 1; \
	fi; \
	PROVENANCE=docs/authority/strategy-toolkit-provenance.md; \
	PS_PINNED=$${PINNED_PS_SHA:-$$(awk -F'`' '/\*\*Upstream commit\*\*/{print $$2; exit}' "$$PROVENANCE")}; \
	if [ -z "$$PS_PINNED" ]; then \
		echo "❌ ps upstream commit not recorded in $$PROVENANCE" >&2; \
		exit 1; \
	fi; \
	echo "- **Upstream commit**: \`$$PS_PINNED\`"; \
	PS_HEAD=$$(git -C "$$PS_ROOT" rev-parse HEAD); \
	echo "ps upstream HEAD: $$PS_HEAD"; \
	PS_COMMITS=$$(git -C "$$PS_ROOT" log --oneline "$$PS_PINNED..$$PS_HEAD" -- shared/ 2>/dev/null); \
	PS_DIFFSTAT=$$(git -C "$$PS_ROOT" diff --stat "$$PS_PINNED..$$PS_HEAD" -- shared/ 2>/dev/null); \
	if [ -z "$$PS_DIFFSTAT" ]; then \
		echo "ps drift: no"; \
	else \
		echo "ps drift: yes"; \
		echo "-- new commits under shared/ --"; \
		echo "$$PS_COMMITS"; \
		echo "-- diff-stat --"; \
		echo "$$PS_DIFFSTAT"; \
	fi; \
	if [ -n "$$PANDAS_TA_ROOT" ]; then \
		PT_PINNED=$$(awk -F'`' '/\*\*Upstream commit\*\*/{print $$2}' "$$PROVENANCE" | sed -n '2p'); \
		PT_HEAD=$$(git -C "$$PANDAS_TA_ROOT" rev-parse HEAD); \
		echo "- **Upstream commit**: \`$$PT_PINNED\`"; \
		echo "pandas_ta upstream HEAD: $$PT_HEAD"; \
		PT_DIFFSTAT=$$(git -C "$$PANDAS_TA_ROOT" diff --stat "$$PT_PINNED..$$PT_HEAD" 2>/dev/null); \
		if [ -z "$$PT_DIFFSTAT" ]; then \
			echo "pandas_ta drift: no"; \
		else \
			echo "pandas_ta drift: yes"; \
			echo "$$PT_DIFFSTAT"; \
		fi; \
	else \
		echo "pandas_ta drift: N/A (PANDAS_TA_ROOT unset — manual check required)"; \
	fi; \
	if [ -n "$$PS_DIFFSTAT" ]; then exit 1; else exit 0; fi

strategy-contract-assets:  ## Generate strategy execution schemas, inventory, and lifecycle golden
	uv run python scripts/generate_strategy_contract_assets.py

check-strategy-contract-assets:  ## Fail when generated strategy contract assets drift
	uv run python scripts/generate_strategy_contract_assets.py --check

# ---- Future targets (pending plan rollout) ----
# typecheck:  ## pyright type checks (integrate in Plan 02+)
# 	uv run pyright src/ tests/
#
# docs:  ## Generate API docs when needed
# 	@echo "TODO"

# Architecture authority and document drift gate.
.PHONY: check-authority
check-toolkit-extraction:
	uv run python scripts/check-toolkit-extraction.py
	uv run --package custos-runner --extra dev --extra nautilus python scripts/check-toolkit-typing-closure.py

check-runner-machine-request-consumer-assets:
	uv run python scripts/generate_runner_machine_request_consumer_assets.py --check

check-authority: check-strategy-contract-assets check-toolkit-extraction check-runner-machine-request-consumer-assets
	@/usr/bin/python3 scripts/check-authority-docs.py

verify: check-authority

verify: toolkit-typecheck

.PHONY: verify-development-artifact

CUSTOS_DEVELOPMENT_ARTIFACT_ROOT ?= $(HOME)/.alephain/v1-team/strategy-artifacts

verify-development-artifact:  ## Verify one PS local artifact for sandbox execution
	@test -n "$(CUSTOS_DEVELOPMENT_SOURCE_SHA256)" || { echo "CUSTOS_DEVELOPMENT_SOURCE_SHA256 is required" >&2; exit 2; }
	@test -n "$(CUSTOS_DEVELOPMENT_PUBLICATION_RECEIPT_DIGEST)" || { echo "CUSTOS_DEVELOPMENT_PUBLICATION_RECEIPT_DIGEST is required" >&2; exit 2; }
	uv run --package custos-runner python scripts/verify_development_artifact.py \
		--artifact-root "$(CUSTOS_DEVELOPMENT_ARTIFACT_ROOT)" \
		--source-sha256 "$(CUSTOS_DEVELOPMENT_SOURCE_SHA256)" \
		--publication-receipt-digest "$(CUSTOS_DEVELOPMENT_PUBLICATION_RECEIPT_DIGEST)"

.PHONY: check-toolkit-extraction
