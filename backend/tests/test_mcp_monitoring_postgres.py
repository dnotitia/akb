"""Collector replay, retention, replica coverage and paging on migrated PostgreSQL."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.exceptions import NotFoundError
from app.mcp_monitoring.collector import Collector, Observation
from app.mcp_monitoring.report import read_report
from tests.test_event_tail_postgres import _fresh_database


async def test_upgrade_preserves_existing_observations_and_metric_definitions():
    from app.db import postgres

    async with _fresh_database() as pool:
        observation, actor, trace = uuid4(), uuid4(), uuid4()
        await pool.execute("""
            INSERT INTO mcp_monitoring.tool_calls
                (observation_id, finished_at, actor_id, account_kind, auth_kind, tool,
                 outcome, duration_ms, trace_id, span_id, search_returned)
            VALUES ($1, now(), $2, 'human', 'oauth', 'akb_search', 'ok', 8, $3, $4, 0)
        """, observation, actor, trace, b"12345678")
        await pool.execute("""
            INSERT INTO mcp_monitoring.metric_definitions VALUES
                ('retired_tool', 'empty', '{"kind":"zero_rate","label":"Empty","dimensions":[]}')
        """)
        # The earlier schema has individual call data but no conversation columns
        # and no receipt for the public migration.
        await pool.execute("""
            ALTER TABLE mcp_monitoring.tool_calls
                DROP COLUMN conversation_key, DROP COLUMN conversation_agent;
            DELETE FROM schema_migrations WHERE filename = '137_mcp_monitoring.py'
        """)
        await postgres._apply_migrations()
        row = await pool.fetchrow("""
            SELECT c.actor_id, c.trace_id, c.span_id, c.conversation_key, c.conversation_agent,
                   (m.samples->>'empty_results')::boolean AS empty
            FROM mcp_monitoring.tool_calls c JOIN mcp_monitoring.measured_calls m USING (observation_id)
            WHERE c.observation_id = $1
        """, observation)
        assert tuple(row) == (actor, trace, b"12345678", None, None, True)
        assert await pool.fetchval("""
            SELECT descriptor->>'label' FROM mcp_monitoring.metric_definitions
            WHERE tool='retired_tool' AND key='empty'
        """) == "Empty"


async def test_replica_flush_replay_and_retention():
    async with _fresh_database() as pool:
        now = datetime.now(UTC)
        actor, pat = uuid4(), uuid4()
        original = Observation(uuid4(), now, actor, "human", "pat", pat, "akb_help", "ok", 2)
        first, second = Collector(), Collector()
        first.queue.extend([original, original._replace(finished_at=now - timedelta(days=32), observation_id=uuid4())])
        second.queue.extend(
            [
                original._replace(observation_id=uuid4()),
                original._replace(observation_id=uuid4(), account_kind="service", auth_kind="service", pat_id=None),
            ]
        )
        await asyncio.gather(first.flush(), second.flush())
        # Commit succeeded but the writer did not observe the acknowledgement.
        first.queue.append(original)
        await first.flush(stopping=True)
        await second.flush(stopping=True)
        assert await pool.fetchval("SELECT COUNT(*) FROM mcp_monitoring.tool_calls") == 4
        await first.tick()
        assert await pool.fetchval("SELECT COUNT(*) FROM mcp_monitoring.tool_calls") == 3
        end = datetime.now(UTC)
        unfiltered = await read_report(now, end, actor, "pat", None, None, 0, 0, 0)
        credential, = unfiltered["credentials"]
        ref = credential["pat_ref"]
        assert ref != pat
        result = await read_report(now, end, actor, "pat", ref, None, 0, 0, 0)
        assert result["summary"]["calls"] == 2 and result["summary"]["active_users"] == 1
        assert result["credentials"][0]["label"] == credential["label"]
        with pytest.raises(NotFoundError):
            await read_report(now, end, uuid4(), "pat", ref, None, 0, 0, 0)
        result = await read_report(now - timedelta(seconds=1), now, None, None, None, None, 0, 0, 0)
        assert result["summary"]["calls"] == 0
        assert await pool.fetchval("""
            SELECT NOT has_schema_privilege('public','mcp_monitoring','USAGE')
               AND NOT has_table_privilege('public','mcp_monitoring.tool_calls','SELECT')
        """)


async def test_credential_status_uses_current_catalog_for_a_historical_window():
    async with _fresh_database() as pool:
        start = datetime.now(UTC) - timedelta(days=2)
        end = start + timedelta(hours=1)
        owner = await pool.fetchval(
            "INSERT INTO users (username, email, password_hash) VALUES ('observer','observer@example.org','disabled') RETURNING id"
        )
        expired_at = end + timedelta(hours=1)
        writer = Collector()
        keys = {}
        for name, expiry in [("active", None), ("expired", expired_at), ("deleted", None)]:
            keys[name] = await pool.fetchval(
                """INSERT INTO tokens (user_id,name,token_hash,token_prefix,expires_at)
                   VALUES ($1,$2,$3,'fixture',$4) RETURNING id""",
                owner, name, uuid4().hex, expiry,
            )
            writer.queue.append(Observation(uuid4(), start, owner, "human", "pat", keys[name], "akb_help", "ok", 2))
        observed = end - timedelta(minutes=1)
        writer.queue.append(Observation(uuid4(), observed, owner, "human", "oauth", None, "akb_help", "tool_error", 1))
        await writer.flush(stopping=True)
        await pool.execute("DELETE FROM tokens WHERE id=$1", keys["deleted"])

        async def report(user):
            return await read_report(start, end, user, None, None, None, 0, 0, 0)

        grouped = await report(None)
        assert {r["auth_kind"]: r["credential_status"] for r in grouped["credentials"]} == {
            "pat": None, "oauth": "session_unverified",
        }
        result = await report(owner)
        assert result["summary"]["calls"] == 4
        assert result["credential_status_as_of"] > expired_at
        refs = {r["credential_status"]: r["pat_ref"] for r in result["credentials"]}
        assert len(set(refs.values())) == 4
        assert not set(keys.values()) & set(refs.values())
        statuses = {ref: state for state, ref in refs.items()}
        for section in ("credentials", "recent_calls"):
            assert {r["pat_ref"]: r["credential_status"] for r in result[section]} == statuses
            assert next(r["pat_expires_at"] for r in result[section] if r["pat_ref"] == refs["expired"]) == expired_at
        assert next(r["last_observed_at"] for r in result["credentials"] if r["auth_kind"] == "oauth") == observed
        await pool.execute("UPDATE users SET account_status='suspended' WHERE id=$1", owner)
        suspended = await report(owner)
        assert {r["pat_ref"]: r["credential_status"] for r in suspended["credentials"]} == {
            **statuses, refs["active"]: "account_suspended",
        }


async def test_coverage_gaps_and_untruncated_totals():
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=6)
    hour = timedelta(hours=1)
    async with _fresh_database() as pool:
        writer = uuid4()
        await pool.execute(
            "INSERT INTO mcp_monitoring.writers VALUES ($1,$2,$3,$3,true,1)", writer, start + hour, start + 4 * hour
        )
        await pool.execute(
            "INSERT INTO mcp_monitoring.gaps VALUES ($1,$2,$3)", writer, start + 2 * hour, start + 3 * hour
        )
        await pool.execute(
            """
            INSERT INTO mcp_monitoring.tool_calls
            SELECT gen_random_uuid(), $1, gen_random_uuid(), 'human','oauth',null,'akb_help','ok',1
            FROM generate_series(1,51)
        """,
            start + 3 * hour,
        )
        result = await read_report(start, start + 4 * hour, None, None, None, None, 0, 0, 0)
        assert [(i["coverage"], i["calls"]) for i in result["series"]] == [
            ("unmeasured", None),
            ("complete", 0),
            ("unmeasured", None),
            ("complete", 51),
        ]
        ids = [row["observation_id"] for row in result["recent_calls"]]
        assert len(set(ids)) == 50 and ids == sorted(ids, reverse=True)
        assert result["summary"]["active_users"] == 51
        assert len(result["users"]) == 50 and result["has_more"]["users"]
        page2 = await read_report(start, start + 4 * hour, None, None, None, None, 1, 0, 0)
        assert len(page2["users"]) == 1 and not page2["has_more"]["users"]
        assert page2["summary"] == result["summary"]
        assert page2["users"][0]["user_id"] not in {row["user_id"] for row in result["users"]}
        # The idle tail of a second writer cannot be called fully observed just
        # because the first writer made a later commit.
        await pool.execute(
            "INSERT INTO mcp_monitoring.writers VALUES ($1,$2,$3,$3,false,0)",
            uuid4(),
            start + hour,
            start + hour + hour / 2,
        )
        late = await read_report(start + hour, start + 2 * hour, None, None, None, None, 0, 0, 0)
        assert late["series"][0]["coverage"] == "partial" and late["collection"]["stale_writers"] == 1

        await pool.execute(
            """INSERT INTO mcp_monitoring.writers
            VALUES ($1,$2,$3,$3,true,0,false)""",
            uuid4(),
            start + hour,
            start + 2 * hour,
        )
        paused = await read_report(start + hour, start + 2 * hour, None, None, None, None, 0, 0, 0)
        assert paused["series"][0]["calls"] is None and paused["series"][0]["coverage"] == "unmeasured"


async def test_usage_groups_from_observations_and_old_rows():
    async with _fresh_database() as pool:
        since = datetime.now(UTC)
        user, pat = uuid4(), uuid4()
        # Older replicas can still flush their own column layout during rollout.
        for actor, tool, count, size, depth, followup, duration in [
            (user, "akb_search", None, None, None, None, 1),
            (user, "akb_search", 0, None, None, None, 1), (user, "akb_search", 3, None, None, None, 1),
            (uuid4(), "akb_search", 0, None, None, None, 1),
            (user, "akb_browse", None, 5, 0, False, 2), (user, "akb_browse", None, 5, 0, True, 8),
            (user, "akb_browse", None, None, -1, False, 9), (user, "akb_browse", None, None, None, None, 1),
        ]:
            await pool.execute("""
                INSERT INTO mcp_monitoring.tool_calls
                    (observation_id,finished_at,actor_id,account_kind,auth_kind,pat_id,tool,outcome,duration_ms,
                     search_returned,browse_page_size,browse_depth,browse_followup)
                VALUES (gen_random_uuid(),$1,$2,'human','pat',$3,$4,'ok',$5,$6,$7,$8,$9)
            """, since, actor, pat, tool, duration, count, size, depth, followup)
        end = datetime.now(UTC)
        unfiltered = await read_report(since, end, user, "pat", None, None, 0, 0, 0)
        args = (since, end, user, "pat", unfiltered["credentials"][0]["pat_ref"], None, 0, 0, 0)
        report = await read_report(*args)
        assert report["search_results"] == {"measured": 2, "empty": 1}
        assert len(report["browse_latency"]) == 3
        grouped = await read_report(*args, browse_group="page")
        assert {(r["followup"], r["calls"], r["p95_ms"]) for r in grouped["browse_latency"]} == {
            (False, 2, 9), (True, 1, 8),
        }
        size_report = await read_report(*args, browse_group="page_size")
        assert [(r["page_size"], r["calls"], r["p95_ms"]) for r in size_report["browse_latency"]] == [
            (5, 2, 8), (None, 1, 9),
        ]


async def test_added_and_retired_metric_history(monkeypatch):
    import json

    from app.mcp_monitoring.usage import ZeroRate
    from mcp_server.tools import TOOL_METRICS

    async with _fresh_database():
        tool = "fixture_lookup"
        monkeypatch.setitem(TOOL_METRICS, tool, (ZeroRate("miss", "Lookup misses", "returned"),))
        since = datetime.now(UTC)
        recorder = Collector()
        for empty in (False, True):
            recorder.queue.append(Observation(uuid4(), since, uuid4(), "human", "oauth", None, tool, "ok", 1,
                                              usage=json.dumps({"miss": empty})))
        await recorder.flush()
        args = (since, datetime.now(UTC), None, None, None, tool, 0, 0, 0)
        active, = (await read_report(*args))["metrics"]
        assert active["active"] and (active["matched"], active["calls"]) == (1, 2)
        monkeypatch.delitem(TOOL_METRICS, tool)
        archived, = (await read_report(*args))["metrics"]
        assert archived == {**active, "active": False}


async def test_operations_are_actor_scoped_ordered_and_independent_of_recent_limit():
    from app.mcp_monitoring.report import read_operation_calls

    async with _fresh_database():
        start = datetime.now(UTC) - timedelta(minutes=5)
        end = start + timedelta(minutes=4)
        actor, other, pat, trace = (uuid4() for _ in range(4))
        recorder = Collector()

        def append(actor_id, tool, offset, duration, parent=None, old=False, sampled=False, auth="oauth"):
            began = start + timedelta(milliseconds=offset)
            recorder.queue.append(Observation(
                uuid4(), began + timedelta(milliseconds=duration), actor_id, "human", auth,
                pat if auth == "pat" else None, tool, "ok", duration,
                trace if sampled else None, b"12345678" if sampled else None,
                started_at=None if old else began, request_trace_id=parent,
            ))
        # The long call starts first and completes last. A rolling-upgrade
        # replica contributes a legacy sampled call in the same trace.
        append(actor, "slow", 0, 1500, parent=trace)
        append(actor, "legacy", 10, 100, old=True, sampled=True)
        for i in range(50):
            append(actor, "fast", 20 + i, 1, parent=trace, auth="pat")
        append(other, "other-user", 70, 1, parent=trace)
        # Having a recorded span without an incoming context still counts individually.
        append(actor, "standalone", 80, 1, sampled=True)
        append(actor, "standalone", 82, 1)
        await recorder.flush()
        result = await read_report(start, end, None, None, None, None, 0, 0, 0)
        assert len(result["recent_calls"]) == 50
        assert len(result["operations"]) == 4
        op = next(r for r in result["operations"] if r["actor_id"] == actor and r["correlated"])
        assert op["id"] != trace
        assert (op["calls"], op["started_at"], op["elapsed_ms"]) == (52, start, 1500)
        actor_report = await read_report(start, end, actor, "pat", None, None, 0, 0, 0)
        pat_ref = actor_report["credentials"][0]["pat_ref"]
        assert len({r["id"] for r in result["operations"] if not r["correlated"]}) == 2
        detail = await read_operation_calls(start, end, actor, None, None, None, op["id"], 0)
        assert detail["has_more"] and [c["tool"] for c in detail["calls"][:2]] == ["slow", "legacy"]
        last = await read_operation_calls(start, end, actor, None, None, None, op["id"], 1)
        assert not last["has_more"] and [c["position"] for c in last["calls"]] == [51, 52]
        filtered = await read_operation_calls(start, end, actor, "pat", pat_ref, "fast", op["id"], 0)
        assert len(filtered["calls"]) == 50 and not filtered["has_more"]
        assert not (await read_operation_calls(start, end, other, "pat", pat_ref, None, op["id"], 0))["calls"]
        subset = await read_report(start, end, actor, "pat", pat_ref, "fast", 0, 0, 0)
        assert [(r["calls"], r["elapsed_ms"]) for r in subset["operations"]] == [(50, 50)]


async def test_turn_and_trace_with_equal_keys_have_separate_sequences(monkeypatch):
    from app.config import settings
    from app.mcp_monitoring.report import read_operation_calls

    async with _fresh_database():
        start = datetime.now(UTC) - timedelta(minutes=1)
        end = datetime.now(UTC)
        key, actor, other, pat = (uuid4() for _ in range(4))
        writer = Collector()
        call = Observation(uuid4(), end - timedelta(seconds=1), actor, "human", "oauth", None,
                           "akb_help", "ok", 4, started_at=start, agent_turn_id=key)
        sampled = call._replace(observation_id=uuid4(), auth_kind="pat", pat_id=pat, trace_id=uuid4(),
                                span_id=b"12345678", started_at=start + timedelta(seconds=1))
        writer.queue.extend([call, sampled, call._replace(observation_id=uuid4(), actor_id=other),
                             call._replace(observation_id=uuid4(), agent_turn_id=None, request_trace_id=key)])
        await writer.flush()
        result = await read_report(start, end, None, None, None, None, 0, 0, 0)
        assert {(r["actor_id"], r["kind"], r["calls"]) for r in result["operations"]} == {
            (actor, "turn", 2), (other, "turn", 1), (actor, "trace", 1),
        }
        monkeypatch.setattr(settings.mcp_monitoring, "tempo_query_url", "http://localhost")
        refs = {r["kind"]: r["id"] for r in result["operations"] if r["actor_id"] == actor}
        assert len(set(refs.values())) == 2 and key not in refs.values()
        detail = await read_operation_calls(start, end, actor, None, None, None, refs["turn"], 0, "turn")
        assert [(c["observation_id"], c["trace_available"]) for c in detail["calls"]] == [
            (call.observation_id, False), (sampled.observation_id, True),
        ]
        assert len((await read_operation_calls(start, end, actor, None, None, None, refs["trace"], 0))["calls"]) == 1


async def test_conversation_takes_precedence_and_preserves_unlabelled_history():
    from app.mcp_monitoring.report import read_operation_calls

    async with _fresh_database():
        start = datetime.now(UTC) - timedelta(minutes=1)
        actor, key, pat, first_turn, parent = (uuid4() for _ in range(5))
        base = Observation(uuid4(), start, actor, "human", "oauth", None, "akb_help", "ok", 1,
                           conversation_key=key, conversation_agent="Codex")
        writer = Collector()
        events = []
        for i in range(6):
            began = start + timedelta(seconds=i)
            event = base._replace(observation_id=uuid4(), started_at=began, finished_at=began + timedelta(milliseconds=1),
                                  auth_kind="pat" if i < 3 else "oauth", pat_id=pat if i < 3 else None,
                                  agent_turn_id=first_turn if i < 3 else None, request_trace_id=parent if i >= 3 else None)
            events.append(event)
        # A separate trace sharing the same UUID as the conversation and two old
        # turn-only observations must still resolve to their own detail routes.
        old = events[0]._replace(conversation_key=None, conversation_agent=None)
        writer.queue.extend([*events, old._replace(observation_id=uuid4()), old._replace(observation_id=uuid4()),
                             old._replace(observation_id=uuid4(), agent_turn_id=None, request_trace_id=key),
                             old._replace(observation_id=uuid4(), agent_turn_id=None)])
        await writer.flush()
        end = datetime.now(UTC)
        report = await read_report(start, end, actor, None, None, None, 0, 0, 0)
        assert report["summary"]["calls"] == 10
        assert {(r["kind"], r["calls"], r["agent"]) for r in report["operations"]} == {
            ("conversation", 6, "Codex"), ("trace", 1, None), ("turn", 2, None), ("individual", 1, None),
        }
        refs = {r["kind"]: r["id"] for r in report["operations"]}
        pat_ref = next(r["pat_ref"] for r in report["credentials"] if r["auth_kind"] == "pat")
        detail = await read_operation_calls(start, end, actor, None, None, None, refs["conversation"], 0, "conversation")
        assert [r["observation_id"] for r in detail["calls"]] == [e.observation_id for e in events]
        assert len((await read_operation_calls(start, end, actor, None, None, None, refs["trace"], 0))["calls"]) == 1
        filtered = await read_operation_calls(start, end, actor, "pat", pat_ref, None, refs["conversation"], 0, "conversation")
        assert [r["observation_id"] for r in filtered["calls"]] == [e.observation_id for e in events[:3]]


async def test_service_jobs_and_details_stay_out_of_human_reports():
    from app.mcp_monitoring.report import read_operation_calls

    async with _fresh_database():
        actor, job, pat = uuid4(), uuid4(), uuid4()
        start = datetime.now(UTC) - timedelta(minutes=1)
        service = Observation(uuid4(), start, actor, "service", "service", None, "akb_put", "ok", 2,
                              started_at=start, conversation_key=job, conversation_agent="document-importer")
        writer = Collector()
        writer.queue.extend([
            service, service._replace(observation_id=uuid4(), auth_kind="pat", pat_id=pat),
            service._replace(observation_id=uuid4(), account_kind="human"),
            service._replace(observation_id=uuid4(), account_kind="human", auth_kind="oauth"),
        ])
        await writer.flush()
        end = datetime.now(UTC)
        args = (start, end, actor, None, None, "akb_put")
        human = await read_report(*args, 0, 0, 0)
        machine = await read_report(*args, 0, 0, 0, caller_kind="service")
        assert human["summary"]["calls"] == 1
        assert machine["summary"]["calls"] == machine["users"][0]["calls"] == machine["tools"][0]["calls"] == 3
        assert machine["summary"]["service_key_calls"] == 2
        assert len(machine["operations"]) == 1 and machine["operations"][0]["calls"] == 3
        assert {r["auth_kind"] for r in machine["recent_calls"]} == {"service", "pat"}
        matching_operation = machine["operations"][0]
        assert matching_operation["agent"] == "MCP client" and matching_operation["id"] != job
        detail = await read_operation_calls(*args, matching_operation["id"], 0, "conversation", "service")
        assert {r["observation_id"] for r in detail["calls"]} == {r["observation_id"] for r in machine["recent_calls"]}
        assert len((await read_operation_calls(*args, human["operations"][0]["id"], 0, "conversation"))["calls"]) == 1
        # Revoked service-owned PATs have no live token row.
        pat_ref = next(r["pat_ref"] for r in machine["credentials"] if r["auth_kind"] == "pat")
        filtered = await read_report(start, end, actor, "pat", pat_ref, None, 0, 0, 0, caller_kind="service")
        assert filtered["summary"]["calls"] == 1
