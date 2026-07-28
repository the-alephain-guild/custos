# Nautilus host V1

## Responsibility

`src/custos/engines/nautilus/host.py` adapts a verified Custos deployment to a
NautilusTrader `TradingNode`. It owns engine process construction, venue client
configuration, readiness observation, stop/reconfigure behavior and engine
telemetry. It does not own deployment authorization, StrategyRelease state,
artifact verification, credential scope policy or command acknowledgement.

## Sole execution chain

```text
Crucible signed DeploymentSpec command
  -> CommandIntakeCoordinator (exact bytes + durable desired state)
  -> authenticated StrategyReleaseArtifactResolverV1
  -> StrategyArtifactRuntimeV1 (BOM/attestation/member verification)
  -> immutable activation + NautilusRuntimeEntryPointLoaderV1
  -> ActivatedEngineArtifactV1
  -> EngineLifecycleSupervisor
  -> NtTradingNodeHost
  -> durable applied lifecycle + RunnerFact enqueue
  -> inbound command ACK
```

No source-path, artifact-path, registry-name, `code_hash`, `create_strategy`
factory or unsigned command branch exists.

## Engine ABI

```python
async def deploy(
    spec: dict,
    credential: dict,
    artifact: ActivatedEngineArtifactV1,
) -> str: ...
```

`spec` is the typed local view of Crucible's `execution_config`. `artifact`
contains the already-built strategy and immutable activation ID. The host adds
that strategy object to the node; it never imports strategy code itself.

## Fail-closed gates

Before engine start, `EngineLifecycleSupervisor` requires:

1. artifact runtime capability is READY;
2. signed command mode equals the local execution view;
3. the engine supports the signed connector;
4. testnet/live credentials are `trade_no_withdraw`;
5. live host capability is explicit;
6. live has signed Crucible promotion evidence;
7. the immutable production runtime receipt has enabled live execution.

The default live enable gate is false. It becomes true only in the composition
root that consumes the final exact-image receipt; it is not a compatibility or
operator bypass flag.

## Runtime identity and safety

`deployment_instance_id` keys active nodes, lifecycle authority, RunnerFact
contexts, watchdog state, breaker state and stop/restart operations.
`deployment_spec_id`, digest and generation are fencing provenance only.

Runner notional reservations, signed cap policy, fallback breaker and zombie
watchdog remain engine-neutral modules. They consume `ExecutionEngineProtocol`
Tier-2 methods and must be composed around the new command runtime coordinator;
they never restore the removed DeploymentReconciler path.

## Credential boundary

Credential material is decrypted only through
`VaultRunnerCredentialResolverV1`. The vault record must bind the signed scope
digest, and real-venue credentials must be `trade_no_withdraw`. Secrets are
used to construct the venue client, never written to state, logs, commands or
RunnerFacts.

## Import isolation

`NautilusRuntimeEntryPointLoaderV1` proves the adapter module originated under
the immutable activation root and rejects a module cached from another
activation. The entry point must implement `StrategyRuntimeAdapterV1`; there is
no class-discovery or legacy factory fallback.

## Toolkit sync discipline

The Custos toolkit packages are the sole execution authority for the v1.team
runner. Philosophers-Stone may retain its research source and the independent
legacy Crucible Python image lane, but Custos never imports either as a runtime
fallback and they are not evidence for the team release.

The crucible Docker preservation window keeps the existing
`deploy/nautilus/Dockerfile` and `deploy/hummingbot/Dockerfile.image` publication
and deployment paths available for the legacy Python product. Removing or
migrating those paths belongs to a future `crucible-runtime-migration` plan and
is not a prerequisite for v1.team.
