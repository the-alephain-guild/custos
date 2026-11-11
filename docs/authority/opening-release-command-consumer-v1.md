# Independent opening-release command consumer

The consumer pins `alephain.crucible.runner_opening_release_command.v1` by its
declared public authority, revision, schema wire fingerprint, shared vectors and
exact asset digests. The pin lives in `docs/authority/vendor/contract-pins-v1.json`.
The command schema is vendored with its producer identity; it is not a second
consumer-owned schema.

`tests/test_wire_opening_release_producer.py` reads the local pinned assets and
exercises the actual verified-event parser and exact-byte authenticator. It
checks the shared acceptance vectors, native producer signatures, subject and
event-byte substitution, signature changes and unknown signing keys. The public
fixture key is not a runtime credential. Strict integer decoding remains
mandatory: JSON Schema's mathematical integer can accept a floating token such
as `1.0`, and those vectors explicitly record the schema distinction.

Public provenance records the producer's declared stable authority instead of
its implementation repository. Explicit local asset mappings prevent private
repository paths from becoming public asset names. Resynchronization preserves
this provenance, and producer comparison still checks revision changes and
same-revision asset changes. Existing pinned asset bytes, revisions and historical
acceptance receipts are not rewritten by the provenance conversion.

This is contract acceptance only. It does not enable an issuer, a successful
opening acknowledgement, an engine trading gate or a ready runtime capability.
The current release observer still retains a matching observation until the
acknowledgement transaction exists. Complete inventory, registered process
authority, lifecycle ordering and first-order fencing remain required before
runtime release can be enabled.
