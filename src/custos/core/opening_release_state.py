"""Atomic opening decisions and generation-scoped platform execution permissions."""

from __future__ import annotations

import base64
import json
import time
from decimal import Context, Decimal, localcontext
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def install(connection):
    statements = """
        CREATE TABLE IF NOT EXISTS runner_opening_acknowledgements (
          release_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, generation INTEGER NOT NULL,
          command_fingerprint TEXT NOT NULL, account_identity BLOB NOT NULL,
          fact_batch_id TEXT NOT NULL, signed_fact_batch BLOB NOT NULL,
          observation BLOB NOT NULL, recorded_at_ns INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runner_opening_permissions (
          instance_id TEXT NOT NULL, generation INTEGER NOT NULL, owner_id TEXT NOT NULL,
          process_epoch INTEGER NOT NULL, command_fingerprint TEXT NOT NULL,
          release_id TEXT NOT NULL REFERENCES runner_opening_acknowledgements(release_id),
          continuity_inventory BLOB NOT NULL, recorded_at_ns INTEGER NOT NULL,
          PRIMARY KEY(instance_id,generation,owner_id)
        );
        CREATE TABLE IF NOT EXISTS runner_opening_trade_events (
          event_id TEXT PRIMARY KEY, stream_key TEXT NOT NULL, generation INTEGER NOT NULL,
          seq INTEGER NOT NULL, kind TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runner_opening_lifecycle (
          instance_id TEXT NOT NULL, generation INTEGER NOT NULL, lifecycle_state TEXT NOT NULL,
          command_fingerprint TEXT NOT NULL, PRIMARY KEY(instance_id,generation)
        );
        """
    for statement in statements.split(";"):
        if statement.strip():
            connection.execute(statement)
    present = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    for name, statement in evidence_triggers().items():
        if name not in present:
            connection.execute(statement)


def evidence_triggers():
    triggers = {}
    for table in (
        "runner_opening_acknowledgements",
        "runner_opening_permissions",
        "runner_opening_trade_events",
        "runner_opening_lifecycle",
    ):
        for operation in ("UPDATE", "DELETE"):
            name = f"{table}_no_{operation.lower()}"
            triggers[name] = (
                f"CREATE TRIGGER {name} "
                f"BEFORE {operation} ON {table} BEGIN "
                "SELECT RAISE(ABORT,'opening execution evidence is retained'); END"
            )
        name = f"{table}_no_replace"
        triggers[name] = (
            f"CREATE TRIGGER {name} BEFORE INSERT ON {table} "
            f"WHEN EXISTS(SELECT 1 FROM {table} WHERE "
            + (
                "instance_id=NEW.instance_id AND generation=NEW.generation AND owner_id=NEW.owner_id"
                if table == "runner_opening_permissions"
                else "instance_id=NEW.instance_id AND generation=NEW.generation"
                if table == "runner_opening_lifecycle"
                else "release_id=NEW.release_id"
                if table == "runner_opening_acknowledgements"
                else "event_id=NEW.event_id"
            )
            + ") BEGIN SELECT RAISE(ABORT,'opening execution evidence is retained'); END"
        )

    return triggers


def _verified_ack(identity, row):
    from custos.core.runner_fact import RunnerStateDurabilityError, runner_fact_signing_preimage
    from custos.core.runner_opening_release_fact import RunnerOpeningReleaseFactV1

    try:
        payload = bytes(row["signed_fact_batch"])
        batch = json.loads(payload)
        Ed25519PublicKey.from_public_bytes(identity.public_key_bytes).verify(
            base64.urlsafe_b64decode(batch["signature"] + "=="), runner_fact_signing_preimage(batch)
        )
        (raw_fact,) = batch["facts"]
        fact = RunnerOpeningReleaseFactV1.model_validate(
            {key: value for key, value in raw_fact.items() if key != "seq"}
        )
        if (
            batch["batch_id"] != row["fact_batch_id"]
            or batch["deployment_instance_id"] != row["instance_id"]
            or batch["generation"] != row["generation"]
            or fact.outcome != "acknowledged"
            or fact.release_id != row["release_id"]
            or json.loads(row["account_identity"])
            != fact.account_inventory.observations.identity.rows[0].model_dump(mode="json")
        ):
            raise ValueError("acknowledgement identity differs")
        return UUID(row["fact_batch_id"]), payload
    except Exception as error:
        raise RunnerStateDurabilityError("opening acknowledgement history is invalid") from error


def _require_observation(store, desired, observation, manifest, *, initial=True):
    from custos.core.opening_inventory_contract import (
        normalize_opening_inventory,
        require_opening_inventory_support,
    )
    from custos.core.opening_observation import OpeningObservationBundle
    from custos.core.runner_command_intake import VerifiedRunnerCommand
    from custos.core.runner_fact import (
        RunnerStateAuthorityError,
        _canonical_json_bytes,
        _sha256_hex,
        _signed_decimal,
    )

    authority = store._authority_resolver(
        VerifiedRunnerCommand(
            desired.command,
            desired.command_fingerprint,
            desired.verification_receipt,
        )
    )
    command = desired.command
    expected_authority = {
        "tenant_id": command.tenant_id,
        "trading_mode": command.trading_mode,
        "runner_id": command.runner_id,
        "deployment_instance_id": command.deployment_instance_id,
        "deployment_spec_id": command.deployment_spec_id,
        "deployment_spec_digest": command.deployment_spec_digest,
        "generation": command.generation,
        "strategy_id": command.strategy_id,
    }
    if any(getattr(authority, field) != expected for field, expected in expected_authority.items()):
        raise RunnerStateAuthorityError(
            "opening observation signing authority differs from current verified desired"
        )
    if (
        not isinstance(observation, OpeningObservationBundle)
        or observation.authority != authority
        or observation.token.source_kind != "platform"
        or observation.token.command_fingerprint != desired.command_fingerprint
        or (initial and observation.native is None)
        or observation.trading_gate != "awaiting_release"
        or _sha256_hex(_canonical_json_bytes(manifest)) != authority.capability_manifest_digest
        or manifest.get("fact_kind_projectors", {}).get("RunnerOpeningReleaseFact.v1") != "risk"
        or manifest.get("runner_fact_contracts", {}).get("risk", {}).get("opening_release") != "v1"
    ):
        raise RunnerStateAuthorityError(
            "opening acknowledgement lacks its current registered observation"
        )
    from custos.core.opening_observation import IndependentRiskSample

    sample = observation.native or observation.independent
    if observation.native is None and not isinstance(sample, IndependentRiskSample):
        raise RunnerStateAuthorityError(
            "opening continuity has no independently observed risk equity"
        )
    inventory = normalize_opening_inventory(
        observation.inventory,
        authority=authority,
        amount=_signed_decimal(sample.equity, "opening observation equity", bounded=True),
        currency=observation.currency,
        observed_at=sample.observed_at,
    )
    require_opening_inventory_support(inventory, manifest)
    return authority, sample, inventory


def _require_owner(owner, lease, observation):
    from custos.core.platform_opening_observation import PlatformOpeningObservation
    from custos.core.runner_fact import RunnerStateAuthorityError

    if (
        not isinstance(owner, PlatformOpeningObservation)
        or owner.process_lease is not lease
        or owner.token != observation.token
        or owner.authority != observation.authority
    ):
        raise RunnerStateAuthorityError("opening observation lacks its synchronous host owner")


def commit_acknowledged(store, authenticator, release_id, lease, manifest, observation, owner=None):
    from custos.core.runner_fact import (
        RunnerStateAuthorityError,
        _canonical_json_bytes,
        _signed_decimal,
    )
    from custos.core.runner_opening_release_fact import opening_release_fact
    from custos.core.utc_time import utc_timestamp_key

    with store._outbox._connect() as connection:
        prior = connection.execute(
            "SELECT * FROM runner_opening_acknowledgements WHERE release_id=?", (str(release_id),)
        ).fetchone()
        if prior is not None:
            return _verified_ack(store._identity, prior)
        retained = connection.execute(
            "SELECT * FROM runner_opening_release_inbox WHERE release_id=?", (str(release_id),)
        ).fetchone()
    if retained is None:
        raise RunnerStateAuthorityError("opening release is absent")
    verified = authenticator.verify_opening_release(
        subject=retained["exact_subject"],
        signed_envelope_bytes=bytes(retained["signed_envelope"]),
    )
    if lease is None or lease.authority.deployment_instance_id != UUID(
        verified.command.deployment_instance_id
    ):
        raise RunnerStateAuthorityError(
            "opening acknowledgement requires its held platform process"
        )
    _require_owner(owner, lease, observation)
    with (
        store.opening_control_action(lease.authority, observation_owner=owner) as connection,
        lease.dispatch_control(connection),
    ):
        prior = connection.execute(
            "SELECT * FROM runner_opening_acknowledgements WHERE release_id=?", (str(release_id),)
        ).fetchone()
        if prior is not None:
            return _verified_ack(store._identity, prior)
        current = connection.execute(
            "SELECT * FROM runner_opening_release_inbox WHERE release_id=?", (str(release_id),)
        ).fetchone()
        if current is None or current["state"] != "awaiting_observation":
            raise RunnerStateAuthorityError("opening attempt is no longer awaiting observation")
        if bytes(current["exact_event_bytes"]) != verified.exact_signed_event_bytes:
            raise RunnerStateAuthorityError("opening command differs from retained bytes")
        desired = store._require_opening_release_desired(connection, verified)
        authority, sample, inventory = _require_observation(store, desired, observation, manifest)
        command = verified.command
        command.require_current_target(desired.command, inventory_identity(inventory))
        lease.require_observed_account(inventory)
        if utc_timestamp_key(sample.observed_at) < utc_timestamp_key(command.issued_at):
            raise RunnerStateAuthorityError("opening observation predates release")
        with localcontext(Context(prec=64)):
            if abs(sample.equity - Decimal(command.basis_amount)) > Decimal("0.00000001"):
                raise RunnerStateAuthorityError(
                    "opening observation does not match the frozen basis"
                )
        if connection.execute(
            "SELECT 1 FROM runner_opening_trade_events WHERE stream_key=?", (authority.stream_key,)
        ).fetchone():
            raise RunnerStateAuthorityError("the instance traded before opening acknowledgement")
        fact = opening_release_fact(
            authority=authority,
            release_id=command.release_id,
            release_command_event_id=json.loads(verified.exact_signed_event_bytes)["event_id"],
            basis_version_digest=command.basis_version_digest,
            outcome="acknowledged",
            reason=None,
            amount=_signed_decimal(sample.equity, "opening acknowledgement equity", bounded=True),
            currency=sample.currency,
            observed_at=sample.observed_at,
            account_inventory=inventory,
        )
        batch_id = store._outbox._enqueue_in_transaction(
            connection,
            authority,
            store._identity,
            [fact],
            opening_release_id=command.release_id,
        )
        if batch_id is None:
            raise RunnerStateAuthorityError("opening acknowledgement lost its atomic signed batch")
        payload = bytes(
            connection.execute(
                "SELECT payload FROM runner_fact_outbox WHERE batch_id=?", (str(batch_id),)
            ).fetchone()[0]
        )
        identity = _canonical_json_bytes(inventory["observations"]["identity"]["rows"][0])
        connection.execute(
            "INSERT INTO runner_opening_acknowledgements VALUES(?,?,?,?,?,?,?,?,?)",
            (
                command.release_id,
                command.deployment_instance_id,
                command.generation,
                desired.command_fingerprint,
                identity,
                str(batch_id),
                payload,
                _canonical_json_bytes(inventory),
                time.time_ns(),
            ),
        )
        _insert_permission(connection, lease, desired, command.release_id, inventory)
        connection.execute(
            "UPDATE runner_opening_release_inbox SET state='retired',retired_at_ns=? WHERE release_id=?",
            (time.time_ns(), command.release_id),
        )
        return batch_id, payload


def inventory_identity(inventory):
    from custos.core.opening_inventory_contract import NativeIdentity, ReportedIdentity

    (identity,) = inventory["observations"]["identity"]["rows"]
    return (
        NativeIdentity if identity["kind"] == "sandbox_account" else ReportedIdentity
    ).model_validate(identity)


def _insert_permission(connection, lease, desired, release_id, inventory):
    from custos.core.runner_fact import _canonical_json_bytes

    connection.execute(
        "INSERT INTO runner_opening_permissions VALUES(?,?,?,?,?,?,?,?)",
        (
            str(lease.authority.deployment_instance_id),
            lease.authority.generation,
            str(lease.owner_id),
            lease.process_epoch,
            desired.command_fingerprint,
            release_id,
            _canonical_json_bytes(inventory),
            time.time_ns(),
        ),
    )


def require_permission(identity, connection, authority, lease=None):
    from custos.core.runner_fact import RunnerStateAuthorityError

    rows = connection.execute(
        "SELECT permission.*,ack.signed_fact_batch,ack.fact_batch_id,ack.account_identity,"
        "ack.observation,ack.command_fingerprint AS original_fingerprint,ack.generation AS original_generation "
        "FROM runner_opening_permissions permission JOIN runner_opening_acknowledgements ack USING(release_id) "
        "WHERE permission.instance_id=? AND permission.generation=?",
        (str(authority.deployment_instance_id), authority.generation),
    ).fetchall()
    for row in rows:
        if lease is not None and (
            row["owner_id"] != str(lease.owner_id) or row["process_epoch"] != lease.process_epoch
        ):
            continue
        desired = connection.execute(
            "SELECT command_fingerprint FROM desired_deployments WHERE deployment_instance_id=?",
            (row["instance_id"],),
        ).fetchone()
        if desired is None or row["command_fingerprint"] != desired[0]:
            continue
        ack = dict(row)
        ack["generation"] = row["original_generation"]
        _verified_ack(identity, ack)
        return
    raise RunnerStateAuthorityError(
        "opening execution permission is absent for the current process"
    )


def rebind(store, lease, manifest, observation, owner=None):
    from custos.core.runner_fact import RunnerStateAuthorityError
    from custos.core.utc_time import render_utc_nanos, utc_timestamp_key

    if lease is None:
        raise RunnerStateAuthorityError("opening continuity requires its held platform process")
    _require_owner(owner, lease, observation)
    with (
        store.opening_control_action(lease.authority, observation_owner=owner) as connection,
        lease.dispatch_control(connection),
    ):
        current = connection.execute(
            "SELECT * FROM desired_deployments WHERE deployment_instance_id=?",
            (str(lease.authority.deployment_instance_id),),
        ).fetchone()
        desired = store._decode_durable_desired_command(
            current, lease.authority.deployment_instance_id
        )
        if desired.command.lifecycle_state != "running":
            raise RunnerStateAuthorityError(
                "opening continuity requires the current running command"
            )
        prior = connection.execute(
            "SELECT * FROM runner_opening_acknowledgements WHERE instance_id=? ORDER BY generation DESC LIMIT 1",
            (str(lease.authority.deployment_instance_id),),
        ).fetchone()
        if prior is None:
            raise RunnerStateAuthorityError("an unanswered opening attempt cannot be inherited")
        _, signed = _verified_ack(store._identity, prior)
        batch = json.loads(signed)
        command = desired.command
        for field in (
            "tenant_id",
            "trading_mode",
            "runner_id",
            "deployment_instance_id",
            "deployment_spec_id",
            "deployment_spec_digest",
        ):
            if batch[field] != str(getattr(command, field)):
                raise RunnerStateAuthorityError(
                    "opening continuity differs from the released scope"
                )
        if (
            command.generation < prior["generation"]
            or connection.execute(
                "SELECT 1 FROM runner_opening_lifecycle WHERE instance_id=? AND generation>? "
                "AND lifecycle_state IN ('stopped','archived')",
                (prior["instance_id"], prior["generation"]),
            ).fetchone()
        ):
            raise RunnerStateAuthorityError("a stopped opening authority cannot be resumed")
        existing = connection.execute(
            "SELECT 1 FROM runner_opening_permissions WHERE instance_id=? AND generation=? AND owner_id=?",
            (
                str(lease.authority.deployment_instance_id),
                lease.authority.generation,
                str(lease.owner_id),
            ),
        ).fetchone()
        if existing is not None:
            require_permission(store._identity, connection, lease.authority, lease)
            return prior["release_id"]
        _, sample, inventory = _require_observation(
            store, desired, observation, manifest, initial=False
        )
        captures = [
            json.loads(row[0])["native_capture"]
            for row in connection.execute(
                "SELECT continuity_inventory FROM runner_opening_permissions WHERE instance_id=? AND release_id=?",
                (prior["instance_id"], prior["release_id"]),
            )
        ]
        capture = inventory["native_capture"]
        if (
            inventory["status"] != "observed"
            or inventory["native_capture"] is None
            or not all(value is True for value in inventory["completeness"].values())
            or inventory["observations"]["identity"]["rows"]
            != [json.loads(prior["account_identity"])]
            or utc_timestamp_key(render_utc_nanos(capture["observed_at_ns"]))
            < utc_timestamp_key(command.issued_at)
            or any(
                capture["observed_at_ns"] < previous["observed_at_ns"]
                or (
                    capture["node_instance_id"] == previous["node_instance_id"]
                    and capture["capture_sequence"] <= previous["capture_sequence"]
                )
                for previous in captures
            )
            or utc_timestamp_key(sample.observed_at) < utc_timestamp_key(command.issued_at)
        ):
            raise RunnerStateAuthorityError("opening account continuity is unproven or reset")
        lease.require_observed_account(inventory)
        _insert_permission(connection, lease, desired, prior["release_id"], inventory)
        require_permission(store._identity, connection, lease.authority, lease)
        return prior["release_id"]


def has_acknowledgement(store, instance):
    with store._outbox._connect() as connection:
        row = connection.execute(
            "SELECT * FROM runner_opening_acknowledgements WHERE instance_id=? ORDER BY generation DESC LIMIT 1",
            (str(instance),),
        ).fetchone()
        if row is None:
            return False
        _verified_ack(store._identity, row)
        return True
