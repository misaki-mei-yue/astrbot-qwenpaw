import hashlib
import re
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from astrbot_plugin_qwenpaw_bridge.identity import IdentityError, IdentityStore


class IdentityStoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "identities.db"
        self.db = sqlite3.connect(self.path)
        self.now = 1000.0
        self.store = IdentityStore(self.db, clock=lambda: self.now)
        self.wx = ("weixin", "wx-bot", "same-id", "wx:private:one", True)
        self.qq = ("qq", "qq-bot", "same-id", "qq:private:one", True)

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def test_opaque_identity_survives_restart_and_multiple_private_origins(self):
        identity = self.store.resolve(*self.wx)
        self.assertRegex(identity["person_id"], r"^p_[0-9a-f]{32}$")
        self.assertRegex(identity["agent_id"], r"^abp_[0-9a-f]{32}$")
        self.assertEqual(identity["scope"], "private")
        self.assertEqual(identity, self.store.resolve(*self.wx[:3], "wx:private:another", True))
        self.store.activate(identity["person_id"])
        self.db.close()
        self.db = sqlite3.connect(self.path)
        self.store = IdentityStore(self.db, clock=lambda: self.now)
        self.assertEqual(identity, self.store.resolve(*self.wx))
        self.assertTrue(self.store.get_person(identity["person_id"])["activated"])

    def test_same_string_user_id_across_platforms_and_bots_is_separate(self):
        identities = [self.store.resolve(*source) for source in (
            self.wx, self.qq,
            ("weixin", "wx-other-bot", "same-id", self.wx[3], True),
            ("qq", "wx-bot", "same-id", self.wx[3], True),
        )]
        self.assertEqual(4, len({item["agent_id"] for item in identities}))
        self.assertEqual(4, len({item["person_id"] for item in identities}))

    def test_group_workspaces_separate_members_groups_platforms_and_private(self):
        private = self.store.resolve(*self.wx)
        group = self.store.resolve(*self.wx[:3], "group-one", False)
        self.assertRegex(group["agent_id"], r"^abg_[0-9a-f]{32}$")
        self.assertEqual(group["person_id"], private["person_id"])
        self.assertEqual(group["scope"], "group")
        other_groups = [self.store.resolve(*source) for source in (
            (*self.wx[:3], "group-two", False),
            (*self.wx[:2], "other-user", "group-one", False),
            ("qq", "wx-bot", "same-id", "group-one", False),
        )]
        self.assertEqual(5, len({item["agent_id"] for item in [private, group, *other_groups]}))
        self.assertEqual(group, self.store.resolve(*self.wx[:3], "group-one", False))
        self.assertFalse(self.store.get_person(private["person_id"])["activated"])

    def test_link_moves_only_unused_account_and_preserves_old_person_and_group(self):
        source = self.store.resolve(*self.wx)
        target = self.store.resolve(*self.qq)
        group_before = self.store.resolve(*self.qq[:3], "group", False)
        old_person = self.store.get_person(target["person_id"])
        self.store.activate(source["person_id"])
        code = self.store.create_link(*self.wx)
        result = self.store.redeem_link(*self.qq, code)
        self.assertEqual(result, {**source, "already_linked": False})
        self.assertEqual(self.store.resolve(*self.qq), source)
        self.assertEqual(self.store.resolve(*self.wx), source)
        self.assertEqual(self.store.get_person(target["person_id"]), old_person)
        self.assertEqual(self.store.resolve(*self.qq[:3], "group", False)["agent_id"], group_before["agent_id"])
        self.assertNotEqual(source["agent_id"], group_before["agent_id"])

    def test_link_code_is_high_entropy_and_only_hash_is_stored(self):
        codes = [self.store.create_link(*self.wx) for _ in range(12)]
        self.assertEqual(len(set(codes)), len(codes))
        for code in codes:
            self.assertRegex(code, r"^[A-Za-z0-9_-]{43}$")
        rows = self.db.execute("SELECT code_hash, expires FROM link_codes").fetchall()
        self.assertEqual({row[0] for row in rows},
                         {hashlib.sha256(code.encode()).hexdigest() for code in codes})
        self.assertTrue(all(row[1] == 1600 for row in rows))
        sql_dump = "\n".join(self.db.iterdump())
        self.assertTrue(all(code not in sql_dump for code in codes))

    def test_code_survives_restart_is_single_use_and_does_not_create_replay_account(self):
        code = self.store.create_link(*self.wx)
        self.db.close()
        self.db = sqlite3.connect(self.path)
        self.store = IdentityStore(self.db, clock=lambda: self.now)
        linked = self.store.redeem_link(*self.qq, code)
        with self.assertRaisesRegex(IdentityError, "已使用"):
            self.store.redeem_link("qq", "bot", "third", "origin", True, code)
        self.assertIsNone(self.store.lookup("qq", "bot", "third"))
        self.assertEqual(self.store.resolve(*self.qq)["agent_id"], linked["agent_id"])

    def test_expiry_at_exact_ten_minute_boundary(self):
        code = self.store.create_link(*self.wx)
        self.now += 600
        with self.assertRaisesRegex(IdentityError, "过期"):
            self.store.redeem_link(*self.qq, code)
        self.assertIsNone(self.store.lookup(*self.qq[:3]))
        self.assertIsNone(self.db.execute("SELECT consumed FROM link_codes").fetchone()[0])

    def test_private_only_create_and_redeem_and_rejection_keeps_code(self):
        with self.assertRaisesRegex(IdentityError, "私聊"):
            self.store.create_link(*self.wx[:4], False)
        self.assertIsNone(self.store.lookup(*self.wx[:3]))
        code = self.store.create_link(*self.wx)
        with self.assertRaisesRegex(IdentityError, "私聊"):
            self.store.redeem_link(*self.qq[:4], False, code)
        self.store.redeem_link(*self.qq, code)

    def test_self_redeem_rejected_without_consuming_code(self):
        code = self.store.create_link(*self.wx)
        with self.assertRaisesRegex(IdentityError, "另一个账号"):
            self.store.redeem_link(*self.wx, code)
        self.store.redeem_link(*self.qq, code)

    def test_activated_target_refuses_merge_without_modifying_existing_data(self):
        source = self.store.resolve(*self.wx)
        target = self.store.resolve(*self.qq)
        self.store.activate(target["person_id"])
        # Stand in for pre-existing bridge routes and memory records: identity
        # operations must never import, rewrite or delete either table.
        self.db.execute("CREATE TABLE routes (session_id TEXT, origin TEXT, user_id TEXT)")
        self.db.execute("CREATE TABLE memory_records (agent_id TEXT, content TEXT)")
        self.db.execute("INSERT INTO routes VALUES ('legacy', 'origin', 'user')")
        self.db.execute("INSERT INTO memory_records VALUES (?, '用户以前保存的记忆')", (target["agent_id"],))
        self.db.commit()
        before_routes = self.db.execute("SELECT * FROM routes").fetchall()
        before_memory = self.db.execute("SELECT * FROM memory_records").fetchall()
        code = self.store.create_link(*self.wx)
        with self.assertRaisesRegex(IdentityError, "原有记忆"):
            self.store.redeem_link(*self.qq, code)
        self.assertEqual(self.store.resolve(*self.qq), target)
        self.assertEqual(self.store.resolve(*self.wx), source)
        self.assertEqual(before_routes, self.db.execute("SELECT * FROM routes").fetchall())
        self.assertEqual(before_memory, self.db.execute("SELECT * FROM memory_records").fetchall())
        self.assertIsNone(self.db.execute("SELECT consumed FROM link_codes").fetchone()[0])
        # A refusal must not burn the code; an unused, different account can use it.
        self.store.redeem_link("qq", "bot", "third", "third-private", True, code)

    def test_target_with_other_linked_account_refuses_silent_merge(self):
        source = self.store.resolve(*self.wx)
        target = self.store.resolve(*self.qq)
        other = ("qq", "other", "third", "third-private", True)
        self.store.redeem_link(*other, self.store.create_link(*self.qq))
        code = self.store.create_link(*self.wx)
        with self.assertRaisesRegex(IdentityError, "绑定关系"):
            self.store.redeem_link(*self.qq, code)
        self.assertEqual(self.store.resolve(*self.qq), target)
        self.assertEqual(self.store.resolve(*other), target)
        self.assertEqual(self.store.resolve(*self.wx), source)

    def test_already_linked_is_idempotent_even_after_activation(self):
        source = self.store.resolve(*self.wx)
        self.store.redeem_link(*self.qq, self.store.create_link(*self.wx))
        self.store.activate(source["person_id"])
        code = self.store.create_link(*self.wx)
        result = self.store.redeem_link(*self.qq, code)
        self.assertEqual(result, {**source, "already_linked": True})
        with self.assertRaisesRegex(IdentityError, "已使用"):
            self.store.redeem_link(*self.qq, code)

    def test_binding_does_not_merge_linked_accounts_group_workspaces(self):
        wx_group = self.store.resolve(*self.wx[:3], "same-group", False)
        qq_group = self.store.resolve(*self.qq[:3], "same-group", False)
        self.store.redeem_link(*self.qq, self.store.create_link(*self.wx))
        self.assertEqual(wx_group["agent_id"], self.store.resolve(*self.wx[:3], "same-group", False)["agent_id"])
        self.assertEqual(qq_group["agent_id"], self.store.resolve(*self.qq[:3], "same-group", False)["agent_id"])
        self.assertNotEqual(wx_group["agent_id"], qq_group["agent_id"])

    def test_stale_code_from_person_that_was_relinked_is_rejected(self):
        stale = self.store.create_link(*self.qq)
        self.store.redeem_link(*self.qq, self.store.create_link(*self.wx))
        with self.assertRaisesRegex(IdentityError, "身份已经改变"):
            self.store.redeem_link("qq", "bot", "third", "origin", True, stale)
        self.assertIsNone(self.store.lookup("qq", "bot", "third"))

    def test_fields_reject_empty_long_and_control_characters_without_echoing_them(self):
        for value in ("", " ", "abc\x00secret", "abc\nsecret", "abc\u200bsecret", "x" * 1100, None):
            for index in range(4):
                with self.subTest(value=value, index=index):
                    args = list(self.wx)
                    args[index] = value
                    with self.assertRaises(IdentityError) as caught:
                        self.store.resolve(*args)
                    self.assertNotIn("secret", str(caught.exception))
        for value in (None, 1, "private"):
            with self.assertRaises(IdentityError):
                self.store.resolve(*self.wx[:4], value)
        for code in (None, "a" * 42, "a" * 44, "secret\n", "密" * 43):
            with self.assertRaises(IdentityError) as caught:
                self.store.redeem_link(*self.qq, code)
            self.assertNotIn("secret", str(caught.exception))

    def test_lengths_enforced_per_field_and_unicode_identity_supported(self):
        self.store.resolve("微" * 128, "机" * 256, "人" * 512, "聊" * 1024, True)
        for index, limit in enumerate((128, 256, 512, 1024)):
            args = list(self.wx)
            args[index] = "x" * (limit + 1)
            with self.assertRaises(IdentityError):
                self.store.resolve(*args)

    def test_lookup_and_person_reads_do_not_create_accounts(self):
        self.assertIsNone(self.store.lookup(*self.wx[:3]))
        self.assertIsNone(self.store.get_person("p_" + "a" * 32))
        with self.assertRaisesRegex(IdentityError, "找不到"):
            self.store.activate("p_" + "a" * 32)
        identity = self.store.resolve(*self.wx)
        self.assertEqual(self.store.lookup(*self.wx[:3]), identity)
        self.assertEqual(self.store.get_person(identity["person_id"]), {
            "person_id": identity["person_id"], "agent_id": identity["agent_id"], "activated": False})

    def test_shared_connection_row_factory_preserved_and_outer_transaction_not_committed(self):
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE unrelated (value TEXT)")
        self.db.execute("INSERT INTO unrelated VALUES ('uncommitted')")
        store = IdentityStore(self.db, clock=lambda: self.now)
        self.assertIs(self.db.row_factory, sqlite3.Row)
        identity = store.resolve(*self.wx)
        self.assertTrue(self.db.in_transaction)
        other = sqlite3.connect(self.path)
        try:
            self.assertEqual(other.execute("SELECT COUNT(*) FROM unrelated").fetchone()[0], 0)
            self.assertEqual(other.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
        finally:
            other.close()
        self.db.rollback()
        self.assertIsNone(store.get_person(identity["person_id"]))

    def test_redemption_failure_rolls_back_account_and_does_not_burn_code(self):
        code = self.store.create_link(*self.wx)
        self.db.execute("""CREATE TRIGGER simulated_storage_error BEFORE UPDATE ON link_codes
            BEGIN SELECT RAISE(ABORT, 'private diagnostic secret'); END""")
        with self.assertRaisesRegex(IdentityError, "暂时无法保存") as caught:
            self.store.redeem_link(*self.qq, code)
        self.assertNotIn("secret", str(caught.exception))
        self.assertIsNone(self.store.lookup(*self.qq[:3]))
        self.assertIsNone(self.db.execute("SELECT consumed FROM link_codes").fetchone()[0])
        self.db.execute("DROP TRIGGER simulated_storage_error")
        self.store.redeem_link(*self.qq, code)

    def test_concurrent_redemption_only_one_account_can_consume_code(self):
        code = self.store.create_link(*self.wx)
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def redeem(user):
            connection = sqlite3.connect(self.path, timeout=10)
            try:
                store = IdentityStore(connection, clock=lambda: self.now)
                barrier.wait(timeout=5)
                results.append((user, store.redeem_link("qq", "qq-bot", user, "private", True, code)))
            except Exception as exc:
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=redeem, args=(user,)) for user in ("alice", "bob")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=12)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], IdentityError)
        loser = "bob" if results[0][0] == "alice" else "alice"
        self.assertIsNone(self.store.lookup("qq", "qq-bot", loser))


if __name__ == "__main__":
    unittest.main()
