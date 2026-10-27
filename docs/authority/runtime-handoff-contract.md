# Flat runtime handoff contract V1

Status: accepted contract-only foundation for implementation after independent Astra review. This contract defines an explicit transfer protocol; connecting a dashboard or registering a runner does not transfer trading control.

## Scope

First release: one operator-controlled host, one dedicated venue account, one active execution run. Both local-to-platform and platform-to-local transfer require a verified flat boundary. Live position adoption, cross-host transfer and automatic fallback are unsupported. Trading mode remains unchanged; control source changes explicitly.

The platform retains its identity, business approval, admission, account opening and signed-command requirements. A local permit cannot satisfy platform admission. Failure of a platform service does not authorize a local controller.

## Transfer evidence

An immutable transfer record binds schema version, transfer UUID, normalized account identity, source control source/run/authorization_epoch, intended target source and run, expected target manifest/artifact identity, observed account boundary and source credential disposition. Target platform identifiers must be real, not invented to fit a local row.

Flat evidence covers all open positions on both sides, open orders and queued/potentially sent intents, complete relevant history/chain scan coverage and a fresh account snapshot. Coverage block/time/revision and local adapter journal identity are recorded. Account state that cannot be read is unknown, not zero.

No-unresolved-intent is independent of no-open-position. A queued open can recreate exposure after an empty account snapshot. Cancelling a request is not sufficient until terminal evidence or effective credential revocation makes later execution impossible. Old credential authority must be revoked or equivalently fenced and verified before the new source can open exposure. Final account/history/chain observation uses the independent read-only capability and must remain available after the old session is revoked; attempting to reconnect the old execution client is not this proof. The operator performs main-wallet revocation; the daemon never receives the withdrawal/master-wallet private key.

## State machine

- `active`: the current source alone may request trading under its valid authority.
- `draining`: transfer accepted and persisted; new decisions and exposure are rejected; only authorized reductions, cancellation and account checks may continue.
- `flat_verified`: source node ended, account and history coverage prove no position/order/unresolved intent, and old trading authority is fenced.
- `target_prepared`: the target's normal authorization, admission and account-opening preconditions are accepted, with trading still disabled.
- `transferred`: one local compare-and-update commits the expected old authorization_epoch to a new target authorization_epoch and permanently retires old source callbacks.
- `target_active`: the target re-verifies current account/intent state and satisfies its normal readiness checks.
- `blocked`: missing, stale or conflicting evidence; no source may increase risk until explicit recovery resolves it.

This local state machine does not duplicate platform business states. Public platform messages and local transfer records must be distinct contracts. The prepare and commit interfaces do not exist yet and must be implemented and consumed using shared vectors before a real transfer is accepted.

## Failure, retry and cancellation

Every operation is bound to transfer UUID and exact payload digest. Same UUID/content retries are idempotent; same UUID/different content is a conflict. Database commit precedes externally visible transfer success. An acknowledgement cannot claim that the target is trading.

A crash before ownership commit does not release draining restrictions. A crash after commit does not restore old authority. A replacement reads durable phase, authorization_epoch, process_generation and the registered store identities before any activation. Crash recovery increments only process_generation, retaining the immutable transfer UUID, payload and source authorization_epoch; resumed operations bind to the new process fence without rewriting the original transfer evidence. A missing or substituted state/journal store blocks resumption. Pending or late old callbacks are rejected at the actual order boundary, not only by UI state.

Cancellation before ownership commit is an explicit operator action. It requires a fresh flat/account check, proof the target never obtained executable authority and a new valid source permit when the old credential has been revoked. Cancellation does not reuse a revoked key or auto-clear unrelated risk latches. After ownership commit, reversing direction is a new full transfer.

Flat evidence is invalidated by new activity, missing coverage or time bounds exceeding policy. Recheck immediately before commit and again before target activation. Credential fencing plus the dedicated-account assumption closes the interval between these checks; a process lock alone cannot close it.

If platform preparation expires or fails, remain blocked/flat rather than silently return control. Existing local emergency de-risking does not confer deployment/configuration authority.

## History and economic boundary

Keep the source run and its local event/journal history read-only. Create a real target instance with explicit opening boundary and account valuation. Do not replay local trades as newly emitted platform fills or import local PnL into platform capital, NAV, settlement or promotion eligibility. Local audit export identifies its origin and evidence level.

## Required acceptance matrix

Cover local/platform direction changes; nonflat accounts; zero positions with a queued open; complete and incomplete history; key revocation pending or failed; old callback after commit; concurrent target/source activation; conflicting retries; stale flat evidence; crash at every persistent boundary; expired target approval; forged deployment identity; account identity aliases; missing or substituted registered journal; process-generation changes during retry; read-only observation after revocation; read-only attachment; and transfer cancellation after old-key revocation. Real platform round trips are separately accepted only after platform dependencies exist.
