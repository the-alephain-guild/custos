# Local engine wheel builds

The normal image build installs the engine from `runtime-requirements.lock`.
For local development, `docker-build-local-v030` can install a reviewed Linux
Python 3.12 wheel with the same engine version after installing the locked runtime:

```bash
make docker-build-local-v030 \
  LOCAL_IMAGE=custos-runner:local-engine \
  LOCAL_NAUTILUS_WHEEL=/path/to/nautilus_trader-wheel.whl \
  LOCAL_NAUTILUS_WHEEL_SHA256=<wheel-sha256> \
  LOCAL_NAUTILUS_REVISION=<engine-source-commit>
```

The build validates the wheel digest, distribution name and version before
installation, installs without resolving dependencies, and runs `pip check` again.
The image labels record the engine source revision and wheel digest alongside the
runner source revision. The wheel is an explicit local input; it does not change the
release lock or authorize publication under a stable tag.

Run the existing image contract gate against this exact image before using it:

```bash
CUSTOS_TEST_IMAGE=custos-runner:local-engine \
  CUSTOS_EXPECTED_REVISION="$(git rev-parse HEAD)" make test-docker-existing
```
