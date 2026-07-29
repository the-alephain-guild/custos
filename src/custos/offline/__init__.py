"""The offline deployment lane: unsigned desired state, sandbox and testnet only.

This lane exists so an operator can verify strategy logic on infrastructure they
own entirely — their machine, their identity, their NATS instance — without a
Crucible backend or a signing authority. It is not a degraded form of the signed
lane and never stands in for it: it carries its own contract, produces no
receipts, and nothing it observes is promotion evidence.

Its one hard bound is live, refused by :mod:`custos.offline.mode_guard`. See
``.claude/rules/mandatory-rules.md`` §Trust for the rule and
``verify_offline_lane`` in ``scripts/check-authority-docs.py`` for what enforces it.
"""
