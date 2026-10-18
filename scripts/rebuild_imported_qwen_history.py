#!/usr/bin/env python3
"""Offline, fail-closed native QwenPaw Scroll history rebuild.

Run in QwenPaw's Python environment AFTER its service is stopped. By default
only private temporary databases are written. --apply replaces just history.db
and the target session's agent.scroll; source context messages remain exact.
Never calls a model, sends a message, or touches another Agent workspace.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
from uuid import uuid4


class VerificationError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise VerificationError(code)


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state_context(state):
    context = state.get("agent", {}).get("state", {}).get("context")
    require(isinstance(context, list), "invalid_state_context")
    return context


def load_single_private_workspace(workspace, agent_id):
    from qwenpaw.agents.context.scroll.sync import preflight_sessions

    cfg = load_json(workspace / "agent.json")
    require(cfg.get("id") == agent_id, "agent_id_mismatch")
    identity = (workspace / "BRIDGE_IDENTITY.md").read_text(encoding="utf-8")
    require("范围是 private" in identity and agent_id in identity,
            "workspace_is_not_verified_private_scope")
    registry = load_json(workspace / "chats.json")
    chats = registry.get("chats", [])
    require(len(chats) == 1, "requires_exactly_one_registered_private_chat")
    chat = chats[0]
    require(chat.get("channel") == "astrbot" and bool(chat.get("user_id")),
            "chat_scope_mismatch")
    preflight = preflight_sessions(workspace / "sessions", workspace / "chats.json")
    require(not preflight.registry_error, "invalid_native_chat_registry")
    require(len(preflight.session_files) == 1, "unhandled_additional_session_files")
    path, relative = preflight.session_files[0]
    require(preflight.registry.mapping.get(relative) == chat.get("session_id"),
            "unregistered_session_file")
    path = Path(path).resolve()
    require(path.is_relative_to(workspace), "session_path_outside_workspace")
    return cfg, identity, chat, path, load_json(path)


def read_rows(path):
    # All database operations execute inside Linux. Read-only URI never opens
    # the original database through Windows SQLite or a writable HistoryStore.
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        require(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                "sqlite_integrity_failed")
        return [dict(row) for row in conn.execute(
            "SELECT * FROM conversation_history ORDER BY seq")]


def json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            pass
    return value


def row_semantics(row):
    result = {k: v for k, v in row.items() if k != "seq"}
    for key in ("blocks", "metadata", "tool_input"):
        result[key] = json_value(result.get(key))
    return result


def rows_by_key(rows):
    result = {}
    for row in rows:
        key = (row.get("session_id"), row.get("dedup_key"))
        require(bool(key[1]) and key not in result, "missing_or_duplicate_dedup_key")
        result[key] = row_semantics(row)
    return result


def native_replay(path, state, session_id, agent_id):
    from agentscope.state import AgentState
    from qwenpaw.agents.context.scroll.history import HistoryStore
    from qwenpaw.agents.context.scroll.manager import ScrollContextManager

    parsed = AgentState.model_validate(state["agent"]["state"])
    manager = None
    store = HistoryStore(path)
    try:
        require(store.quarantined_to is None, "native_store_quarantined")
        manager = ScrollContextManager(history=store, session_id=session_id,
                                       agent_id=agent_id)
        manager._persist_new(SimpleNamespace(state=parsed))
        require(not store.degraded, "native_store_degraded")
        return manager.to_dict()
    finally:
        store.close()


def no_archived_scroll(scroll):
    require(isinstance(scroll, dict), "missing_scroll_state")
    require(not scroll.get("synthetic_ids"), "existing_synthetic_context_requires_manual_review")
    require(not scroll.get("index", {}).get("tiers"),
            "existing_eviction_spans_require_seq_remapping")
    require(scroll.get("continuation_summary") is None,
            "existing_continuation_summary_requires_manual_review")


def run(workspace, before_workspace, agent_id, *, expected_imported,
        expected_before, apply=False):
    from agentscope.message import Msg

    workspace = Path(workspace).resolve(strict=True)
    before_workspace = Path(before_workspace).resolve(strict=True)
    require(workspace != before_workspace, "backup_must_be_independent")
    require(not workspace.is_relative_to(before_workspace)
            and not before_workspace.is_relative_to(workspace),
            "backup_and_target_must_not_contain_each_other")
    require(workspace.name == agent_id and agent_id.startswith("abp_"),
            "target_is_not_exact_private_agent_directory")
    cfg, identity, chat, session_path, state = load_single_private_workspace(workspace, agent_id)
    _, old_identity, old_chat, old_path, old_state = load_single_private_workspace(before_workspace, agent_id)
    require(identity == old_identity, "private_identity_changed")
    for field in ("id", "session_id", "user_id", "channel"):
        require(chat.get(field) == old_chat.get(field), "chat_identity_changed_" + field)
    old_context = state_context(old_state)
    context = state_context(state)
    require(len(old_context) == expected_before, "unexpected_baseline_message_count")
    require(len(context) == expected_imported + expected_before, "unexpected_imported_message_count")
    require(context[expected_imported:] == old_context, "new_messages_were_not_preserved_exactly")
    require(len({m.get("id") for m in context}) == len(context), "duplicate_message_id")
    for item in context[:expected_imported]:
        msg = Msg.from_dict(item)
        require(msg.role in ("user", "assistant"), "invalid_import_role")
        require(item.get("metadata", {}).get("migration") == "astrbot-legacy-v1",
                "missing_import_provenance")
        require(all(block.get("type") == "text" for block in item.get("content", [])),
                "legacy_import_must_contain_only_text")
    retention = cfg.get("running", {}).get("light_context_config", {}).get("scroll_config", {}).get("history_retention_days")
    require(retention == 0, "set_history_retention_days_to_zero_before_import")
    old_scroll = old_state.get("agent", {}).get("scroll")
    current_scroll = state.get("agent", {}).get("scroll")
    no_archived_scroll(old_scroll)
    require(current_scroll == old_scroll, "scroll_changed_before_rebuild")
    db_path = workspace / "history.db"
    before_db_path = before_workspace / "history.db"
    original_session_bytes = session_path.read_bytes()
    guard_paths = [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm"),
                   session_path, workspace / "agent.json", workspace / "chats.json"]
    guard = {p: digest(p) for p in guard_paths if p.is_file()}
    baseline_rows = read_rows(before_db_path)
    require(all(r["agent_id"] == agent_id and r["session_id"] == chat["session_id"]
                for r in baseline_rows), "baseline_contains_another_scope")
    require(rows_by_key(read_rows(db_path)) == rows_by_key(baseline_rows),
            "target_history_changed_since_backup")
    with tempfile.TemporaryDirectory(prefix="qwen-legacy-rebuild-") as tempdir:
        tempdir = Path(tempdir)
        replay_before = tempdir / "baseline-native.db"
        native_replay(replay_before, old_state, chat["session_id"], agent_id)
        require(rows_by_key(read_rows(replay_before)) == rows_by_key(baseline_rows),
                "baseline_history_cannot_be_replayed_without_loss")
        rebuilt = tempdir / "rebuilt-native.db"
        new_scroll = native_replay(rebuilt, state, chat["session_id"], agent_id)
        rebuilt_rows = read_rows(rebuilt)
        rebuilt_by_key = rows_by_key(rebuilt_rows)
        require(all(rebuilt_by_key.get(key) == value
                    for key, value in rows_by_key(baseline_rows).items()),
                "rebuilt_history_lost_or_changed_existing_rows")
        old_ids = {m["id"] for m in context[:expected_imported]}
        old_rows = [r for r in rebuilt_rows if r["dedup_key"] in old_ids]
        remaining_rows = [r for r in rebuilt_rows if r["dedup_key"] not in old_ids]
        require(len(old_rows) == expected_imported, "not_all_legacy_messages_indexed")
        require(len(remaining_rows) == len(baseline_rows), "unexpected_extra_or_missing_current_rows")
        require(max(r["seq"] for r in old_rows) < min(r["seq"] for r in remaining_rows),
                "legacy_messages_not_before_current_messages")
        for key in ("seen_tool_result_ids", "seen_thinking_block_ids",
                    "folded_thinking_block_ids", "synthetic_ids", "index",
                    "continuation_summary", "summary_update_failed"):
            if key in old_scroll:
                new_scroll[key] = copy.deepcopy(old_scroll[key])
        updated = copy.deepcopy(state)
        updated["agent"]["scroll"] = new_scroll
        check = copy.deepcopy(updated)
        check["agent"]["scroll"] = current_scroll
        require(check == state, "unexpected_non_scroll_session_change")
        require(state_context(updated)[expected_imported:] == old_context,
                "current_messages_changed_after_replay")
        # A second replay must be idempotent; all tool/thinking blocks remain
        # in their original Msg dictionaries, and the original state is never
        # serialized back through a potentially lossy model_dump.
        native_replay(rebuilt, updated, chat["session_id"], agent_id)
        require(rows_by_key(read_rows(rebuilt)) == rebuilt_by_key, "native_replay_not_idempotent")
        report = {"success": True, "applied": False,
                  "imported_messages": expected_imported,
                  "existing_messages_preserved": len(old_context),
                  "existing_history_rows_preserved": len(baseline_rows),
                  "history_rows_after": len(rebuilt_rows),
                  "current_raw_messages_exact": True,
                  "native_baseline_replay_equivalent": True,
                  "native_replay_idempotent": True,
                  "chronological_seq_order": True,
                  "private_scope_verified": True,
                  "existing_tool_and_thinking_preserved": True}
        if not apply:
            require(session_path.read_bytes() == original_session_bytes, "dry_run_changed_session")
            return report
        require(all(p.exists() and digest(p) == value for p, value in guard.items()),
                "source_changed_during_rebuild")
        require(not any(p.exists() for p in guard_paths if p not in guard),
                "new_source_file_appeared_during_rebuild")
        backup = workspace / (".legacy-history-rebuild-before-" + uuid4().hex)
        backup.mkdir(mode=0o700)
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(db_path) + suffix)
            if source.exists():
                shutil.copy2(source, backup / ("history.db" + suffix))
        (backup / "session.json").write_bytes(original_session_bytes)
        candidate_db = workspace / (".history-rebuilt-" + uuid4().hex + ".db")
        candidate_session = session_path.with_name(".session-rebuilt-" + uuid4().hex + ".json")
        shutil.copy2(rebuilt, candidate_db)
        candidate_session.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            # Only stopped-service stale sidecars from this exact database.
            for suffix in ("-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)
            os.replace(candidate_db, db_path)
            os.replace(candidate_session, session_path)
            require(rows_by_key(read_rows(db_path)) == rebuilt_by_key,
                    "activation_database_verification_failed")
            require(load_json(session_path) == updated, "activation_session_verification_failed")
        except Exception:
            for suffix in ("-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)
            for suffix in ("", "-wal", "-shm"):
                source = backup / ("history.db" + suffix)
                if source.exists():
                    shutil.copy2(source, Path(str(db_path) + suffix))
            session_path.write_bytes(original_session_bytes)
            raise
        finally:
            candidate_db.unlink(missing_ok=True)
            candidate_session.unlink(missing_ok=True)
        report["applied"] = True
        report["private_rollback_directory"] = str(backup)
        (workspace / "legacy-history-rebuild-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--before-workspace", required=True, type=Path)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--expected-imported-messages", type=int, required=True)
    parser.add_argument("--expected-before-messages", type=int, required=True)
    parser.add_argument("--apply", action="store_true",
                        help="Caller confirms QwenPaw service is stopped; activate verified files")
    args = parser.parse_args()
    try:
        require(args.workspace.is_absolute() and args.before_workspace.is_absolute(),
                "workspace_arguments_must_be_absolute")
        report = run(args.workspace, args.before_workspace, args.agent_id,
                     expected_imported=args.expected_imported_messages,
                     expected_before=args.expected_before_messages, apply=args.apply)
    except Exception as exc:
        # Never stringify arbitrary exceptions containing message/tool content.
        print(json.dumps({"success": False,
                          "error": str(exc) if isinstance(exc, VerificationError)
                          else type(exc).__name__}))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
