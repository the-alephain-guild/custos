from __future__ import annotations

from pathlib import Path

MAKEFILE = Path(__file__).resolve().parents[1] / "Makefile"


def test_makefile_defines_local_v030_image_contract() -> None:
    text = MAKEFILE.read_text()

    assert "LOCAL_IMAGE ?= custos-runner:v0.5.1" in text
    assert "docker-build-local-v030:" in text
    assert "verify-local-v030:" in text
    assert "org.opencontainers.image.revision" in text
    assert "CUSTOS_TEST_IMAGE=$(LOCAL_IMAGE)" in text
    assert "verify-runtime-existing" in text


def test_generic_docker_build_injects_source_revision() -> None:
    text = MAKEFILE.read_text()
    start = text.index("docker-build: dist")
    end = text.index("docker-build-local-v030:", start)
    block = text[start:end]

    assert "--label org.opencontainers.image.revision=$(SOURCE_REVISION)" in block


def test_local_consumer_build_requires_clean_provenance() -> None:
    text = MAKEFILE.read_text()
    start = text.index("docker-build-local-v030:")
    end = text.index("docker-sign:", start)
    block = text[start:end]

    assert "git status --porcelain --untracked-files=normal" in block
    assert "CUSTOS_EXPECTED_REVISION=$(SOURCE_REVISION)" in text
