# Custos

Custos is the local execution boundary for the Alephain trading platform. It
runs a strategy deployment on an enrolled runner, protects the local process,
and reports signed execution facts.

It is deliberately narrow. It is not a business workflow service and not an
authorization gateway — it holds your keys, executes what it has been
instructed to execute, and proves what happened.

Documentation: **[custos.alephain.com](https://custos.alephain.com)**

## Where it sits

    human or API client
      -> ARX: authorize the intent, approve and sign the deployment command
      -> Custos: verify the command, reconcile local runtime, sign execution facts
      -> ARX: accept the signed facts and advance the deployment lifecycle

ARX is the platform side. Custos is the part that runs on your own
infrastructure, and it is open source so that you can audit exactly what it
does with your credentials before you hand them over.

The split is deliberate and narrow. ARX decides *what* should run and accepts
proof of what happened; Custos holds the keys and does the running. Neither
side can do the other's job: Custos has no path to create or approve a
deployment, and ARX never holds a credential or places an order.

## Runtime identity

`deployment_instance_id` is the only key for a running deployment. A
`deployment_spec_id` identifies immutable configuration provenance and may be
shared by multiple instances. Reconciler state, engine handles, watchdogs,
circuit breakers and deployment-scoped facts are therefore instance keyed.

Secret material is looked up by its signed credential scope identifier; every
use is bound and audited against the exact deployment instance.

## Trust boundary

Custos accepts deployment commands only from the signed ARX event stream. It
verifies the exact subject and exact event bytes against the configured Ed25519
public key before parsing anything. Live execution additionally requires the
signed promotion identifier and evidence digest carried by the deployment
itself — Custos does not count approvals or reconstruct an approval workflow of
its own; it refuses to act without evidence that one happened.

Runner facts are signed locally and carry tenant, mode, runner and exact
deployment instance identity.

Authorization is provisioned once, at enrollment, and is then out of the
delivery path: nothing relays a deployment command or a runner fact on the way
through. That is why an authorization outage can neither stop a running
deployment nor be used to inject one.

## Guarantees

Four properties hold regardless of what a deployment asks for:

1. **Keys never leave the host.** Credentials are decrypted in-process; the
   decryption key is never transmitted, logged or published.
2. **Live execution is always gated.** Reaching a live venue requires an engine
   that declares live capability, a matching venue, a matching code hash and a
   correctly scoped credential. The gate fails closed.
3. **Safety survives a disconnect.** Local exposure limits and the safety
   breaker keep working while the platform is unreachable. Losing the
   connection does not mean stopping, and it does not mean running unguarded.
4. **Money arithmetic is exact.** Prices, quantities and notionals use decimal
   arithmetic end to end and cross the wire as strings.

## Local use

    uv sync
    uv run arx-runner --help
    uv run arx-runner start --help

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and
[SECURITY.md](SECURITY.md) for vulnerability reporting.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
