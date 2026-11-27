"""Account discovery, the default account and per-account persistent state."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _fakes  # noqa: E402,F401

from instagram_sdk.accounts import (  # noqa: E402
    GLOBAL_COOLDOWN_S,
    Account,
    AccountRegistry,
    AccountStateStore,
    Budget,
    build_meta,
    meta_path_for,
    write_meta,
)
from instagram_sdk.errors import (  # noqa: E402
    AccountBlockedError,
    AccountConfigError,
    AmbiguousAccountError,
    BudgetExceededError,
    NoSessionError,
    UnknownAccountError,
)
from instagram_sdk.guard import BlockSignal  # noqa: E402
from instagram_sdk.settings import Settings  # noqa: E402


def _write_session(directory: Path, handle: str, user_id: str = "1") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{handle}_instagram_state.json"
    path.write_text(json.dumps({"cookies": [{"name": "sessionid", "value": "S"},
                                            {"name": "ds_user_id", "value": user_id}]}))
    return path


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.sessions = self.dir / "sessions"
        self.sessions.mkdir()

    def registry(self, **fields) -> AccountRegistry:
        base = {"session_file": None, "session_dir": self.sessions, "username": "",
                "default_account": "", "state_dir": self.dir / "state"}
        base.update(fields)
        return AccountRegistry(replace(Settings.from_env(), **base))


class ResolveTests(_Base):
    def test_explicit_account_normalized_and_validated(self) -> None:
        _write_session(self.sessions, "Carol")
        _write_session(self.sessions, "dave")
        reg = self.registry()
        self.assertEqual(reg.resolve("@CAROL").handle, "Carol")
        with self.assertRaises(UnknownAccountError) as ctx:
            reg.resolve("zed")
        self.assertIn("'Carol', 'dave'", str(ctx.exception))
        with self.assertRaises(UnknownAccountError):
            reg.resolve("../etc")

    def test_default_account_env_must_exist(self) -> None:
        _write_session(self.sessions, "carol")
        _write_session(self.sessions, "dave")
        self.assertEqual(self.registry(default_account="DAVE").default().source,
                         "default_account")
        with self.assertRaises(UnknownAccountError):
            # An explicit wrong value is never silently replaced.
            self.registry(default_account="zed").resolve(None)

    def test_session_file_is_an_account_and_the_default(self) -> None:
        _write_session(self.sessions, "carol")
        extra = _write_session(self.dir / "elsewhere", "erin")
        reg = self.registry(session_file=extra)
        self.assertEqual([a.handle for a in reg.list()], ["carol", "erin"])
        choice = reg.default()
        self.assertEqual((choice.account.handle, choice.source), ("erin", "session_file"))

    def test_session_file_with_odd_name_uses_username(self) -> None:
        odd = self.dir / "state.json"
        odd.write_text("{}")
        reg = self.registry(session_file=odd, username="frank")
        self.assertEqual(reg.resolve(None).handle, "frank")

    def test_username_alias(self) -> None:
        _write_session(self.sessions, "carol")
        _write_session(self.sessions, "dave")
        choice = self.registry(username="dave").default()
        self.assertEqual((choice.account.handle, choice.source), ("dave", "username"))

    def test_only_session_wins_over_mismatched_username_with_warning(self) -> None:
        _write_session(self.sessions, "carol")
        choice = self.registry(username="foo").default()
        self.assertEqual((choice.account.handle, choice.source), ("carol", "only_session"))
        self.assertIn("INSTAGRAM_USERNAME=foo", choice.warning)

    def test_none_and_several_never_guess(self) -> None:
        with self.assertRaises(NoSessionError):
            self.registry().resolve(None)
        _write_session(self.sessions, "bob")
        _write_session(self.sessions, "alice")
        with self.assertRaises(AmbiguousAccountError) as ctx:
            self.registry().resolve(None)  # never sorted()[0]
        self.assertIn("INSTAGRAM_DEFAULT_ACCOUNT", str(ctx.exception))

    def test_case_only_duplicates_are_a_config_error(self) -> None:
        _write_session(self.sessions, "carol")
        _write_session(self.sessions, "Carol")
        names = {p.name for p in self.sessions.iterdir()}
        if len(names) < 2:  # case-insensitive file system
            self.skipTest("file system ignores case")
        with self.assertRaises(AccountConfigError):
            self.registry().list()

    def test_invalid_handles_are_ignored(self) -> None:
        _write_session(self.sessions, "carol")
        (self.sessions / "..._instagram_state.json").write_text("{}")
        (self.sessions / "bad-name_instagram_state.json").write_text("{}")
        self.assertEqual([a.handle for a in self.registry().list()], ["carol"])

    def test_new_login_is_picked_up_without_restart(self) -> None:
        reg = self.registry()
        _write_session(self.sessions, "carol")
        self.assertEqual(reg.resolve(None).handle, "carol")
        _write_session(self.sessions, "dave")
        self.assertEqual(reg.resolve("dave").handle, "dave")

    def test_fixed_registry(self) -> None:
        path = _write_session(self.dir, "zoe")
        reg = AccountRegistry(Settings.from_env(), fixed=lambda: path)
        self.assertEqual(reg.resolve(None).handle, "zoe")
        with self.assertRaises(UnknownAccountError):
            reg.resolve("other")


class MetaTests(_Base):
    def test_sidecar_round_trip_and_fingerprint(self) -> None:
        path = _write_session(self.sessions, "carol", user_id="42")
        meta = build_meta(username="carol", user_id="42", locale="es-ES",
                          timezone_id="Europe/Madrid", viewport={"width": 1300, "height": 800})
        written = write_meta(path, meta)
        self.assertEqual(written, meta_path_for(path))
        self.assertEqual(written.name, "carol_instagram_meta.json")
        acct = self.registry().resolve("carol")
        self.assertEqual(acct.user_id, "42")
        self.assertEqual(
            acct.fingerprint_options(locale="en-US", timezone_id="Etc/UTC",
                                     viewport={"width": 1, "height": 1}),
            {"locale": "es-ES", "timezone_id": "Europe/Madrid",
             "viewport": {"width": 1300, "height": 800}},
        )
        # The sidecar is not mistaken for a session.
        self.assertEqual([a.handle for a in self.registry().list()], ["carol"])
        self.assertNotIn("sessionid", written.read_text())

    def test_without_sidecar_env_values_apply(self) -> None:
        acct = Account.from_session(_write_session(self.sessions, "old"))
        opts = acct.fingerprint_options(locale="en-US", timezone_id="Etc/UTC",
                                        viewport={"width": 1280, "height": 900})
        self.assertEqual(opts["locale"], "en-US")


class StateTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.now = [1_000_000.0]
        self.store = AccountStateStore(self.dir / "state", clock=lambda: self.now[0])
        self.carol = Account.from_session(_write_session(self.sessions, "carol"))
        self.dave = Account.from_session(_write_session(self.sessions, "dave"))

    def test_block_pauses_other_accounts_30_min(self) -> None:
        self.store.record(self.carol, BlockSignal("rate_limited", "429"))
        with self.assertRaises(AccountBlockedError) as ctx:
            self.store.check(self.dave)
        self.assertEqual(ctx.exception.kind, "global_cooldown")
        self.assertEqual(ctx.exception.until, self.now[0] + GLOBAL_COOLDOWN_S)
        self.now[0] += GLOBAL_COOLDOWN_S + 1
        self.store.check(self.dave)  # lifted
        with self.assertRaises(AccountBlockedError):
            self.store.check(self.carol)  # its own 12 h cooldown still runs

    def test_expired_session_does_not_pause_others(self) -> None:
        self.store.record(self.carol, BlockSignal("session_expired", "/accounts/login"))
        self.store.check(self.dave)

    def test_clear_lifts_the_global_pause_it_caused(self) -> None:
        self.store.record(self.carol, BlockSignal("checkpoint", "x"))
        self.assertTrue(self.store.clear("carol"))
        self.store.check(self.carol)
        self.store.check(self.dave)

    def test_failure_brake(self) -> None:
        self.assertFalse(self.store.record_failure(self.carol, "HTTP 500"))
        self.assertFalse(self.store.record_failure(self.carol, "HTTP 500"))
        self.store.record_success(self.carol)  # a success resets the streak
        self.assertFalse(self.store.record_failure(self.carol, "HTTP 500"))
        self.assertFalse(self.store.record_failure(self.carol, "HTTP 500"))
        self.assertTrue(self.store.record_failure(self.carol, "HTTP 500"))
        with self.assertRaises(AccountBlockedError) as ctx:
            self.store.check(self.carol)
        self.assertEqual(ctx.exception.kind, "failures")
        self.store.check(self.dave)  # our own brake is per account
        self.now[0] += 15 * 60 + 1
        self.store.check(self.carol)

    def test_failures_outside_window_do_not_count(self) -> None:
        self.store.record_failure(self.carol, "x")
        self.store.record_failure(self.carol, "x")
        self.now[0] += 11 * 60
        self.assertFalse(self.store.record_failure(self.carol, "x"))

    def test_budget_persists_per_account(self) -> None:
        budget = self.store.budget(self.carol, per_hour=2, per_day=10)
        budget.charge()
        budget.charge()
        self.store.save_usage(self.carol, budget, self.now[0])
        again = self.store.budget(self.carol, per_hour=2, per_day=10)
        with self.assertRaises(BudgetExceededError) as ctx:
            again.charge()
        self.assertGreater(ctx.exception.retry_after_s, 0)
        self.store.budget(self.dave, per_hour=2, per_day=10).charge()  # separate
        glob = json.loads((self.dir / "state" / "global.json").read_text())
        self.assertEqual(glob["last_account"], "carol")
        mode = os.stat(self.dir / "state" / "accounts" / "carol.json").st_mode & 0o777
        if os.name == "posix":
            self.assertEqual(mode, 0o600)


class BudgetTests(unittest.TestCase):
    def test_hour_window_slides_and_day_caps(self) -> None:
        now = [3600.0 * 1000]
        budget = Budget(per_hour=3, per_day=4, clock=lambda: now[0])
        for _ in range(3):
            budget.charge()
        with self.assertRaises(BudgetExceededError) as ctx:
            budget.charge()
        self.assertIn("per hour", str(ctx.exception))
        self.assertLessEqual(ctx.exception.retry_after_s, 3600 + 60)
        now[0] += 3601
        budget.charge()  # hour window moved
        with self.assertRaises(BudgetExceededError) as ctx:
            budget.charge()
        self.assertIn("per day", str(ctx.exception))

    def test_class_limits_are_stricter_and_also_count_general(self) -> None:
        budget = Budget(per_hour=150, per_day=800, clock=lambda: 1e9)
        for _ in range(30):
            budget.charge("follow")
        with self.assertRaises(BudgetExceededError):
            budget.charge("follow")
        budget.charge("general")
        self.assertEqual(budget.used("general", 3600), 31)

    def test_survives_json_round_trip_and_bad_data(self) -> None:
        budget = Budget(per_hour=5, per_day=5, clock=lambda: 1e9)
        budget.charge()
        restored = Budget(json.loads(json.dumps(budget.to_json())), per_hour=5, per_day=5,
                          clock=lambda: 1e9)
        self.assertEqual(restored.used("general", 3600), 1)
        Budget({"x": "y", "12": {"general": "bad"}}, per_hour=5, per_day=5)


if __name__ == "__main__":
    unittest.main()
