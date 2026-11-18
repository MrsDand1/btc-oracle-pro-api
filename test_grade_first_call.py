"""Grade-the-first-call + level bounce tracking + sit-out shadow flag.

Tests for the 2026-11-18 tracker change: the track record grades the
FIRST ENTER per window (minute <= 9) instead of the last signal, the
stored record is regraded once at boot, key-level touches record a
held/broke outcome, and a would_sit_out shadow flag is logged per
window (first ENTER confidence < 70) — logging only, never shown.

Layout: tracker.py is NOT imported (importing it starts the live poll
thread). The functions under test are extracted from the tracker
source by name and exec'd with a stubbed db() on a temp SQLite DB —
the same pattern as test_extra_spot.py. The file resolves the tracker
in either layout and fails loudly if it finds a copy without the new
functions (e.g. the pre-change base).

Run:  python3 -m unittest discover -s tests -v   (from the project root)
"""

import math
import re
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve()


def _find_tracker():
    candidates = [
        HERE.parent.parent / "tracker.py",           # staging layout
        HERE.parent.parent / "api" / "tracker.py",   # project layout
    ]
    for path in candidates:
        if path.is_file():
            src = path.read_text()
            for name in (
                "_first_enter", "_track_record_counts",
                "_track_record_counts_last_signal", "_regrade_series",
                "settle_window", "_record_level_bounces",
                "_level_bounce_summary",
            ):
                assert f"def {name}(" in src, (
                    f"{path} has no {name} — wrong tracker copy "
                    "(pre-grade-first-call base?)"
                )
            return path, src
    raise FileNotFoundError("tracker.py not found in any known layout")


TRACKER_PATH, TRACKER_SRC = _find_tracker()

_FUNCS = (
    "avg_spot", "_first_enter", "_track_record_counts",
    "_track_record_counts_last_signal", "_regrade_series",
    "settle_window", "_level_bounce_bucket", "_record_level_bounces",
    "_level_bounce_summary",
)
_CONSTS = (
    "SIT_OUT_CONFIDENCE_FLOOR", "LEVEL_BOUNCE_OBSERVE_SECS",
    "LEVEL_BOUNCE_STALE_SECS", "LEVEL_BOUNCE_BUCKETS",
)


def _extract(source, name):
    m = re.search(rf"^def {re.escape(name)}\(.*?\n(?=^def |\Z)", source,
                  re.S | re.M)
    assert m, f"could not extract {name} from tracker source"
    return m.group(0)


def _load_functions(db):
    """Exec the tested functions with a stubbed db() into a namespace."""
    ns = {"db": db, "time": time, "math": math,
          "datetime": datetime, "timezone": timezone}
    for name in _CONSTS:
        m = re.search(rf"^{name} = (.+)$", TRACKER_SRC, re.M)
        assert m, f"could not parse constant {name}"
        exec(f"{name} = {m.group(1)}", ns)
    for name in _FUNCS:
        exec(_extract(TRACKER_SRC, name), ns)
    return ns


SCHEMA = """
CREATE TABLE windows (
    ticker TEXT PRIMARY KEY, open_ts REAL, close_ts REAL,
    target REAL, target_provisional INTEGER,
    resolution TEXT, resolved_at REAL,
    strike_usd REAL, volume_24h REAL, open_interest REAL,
    last_price_cents REAL, market_result TEXT,
    expiration_value REAL, would_sit_out INTEGER
);
CREATE TABLE signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, window TEXT, ts REAL,
    minute REAL, direction TEXT, confidence REAL,
    action TEXT, reason TEXT
);
CREATE TABLE spot_samples (ts REAL PRIMARY KEY, price REAL);
CREATE TABLE track_record_summary (
    series TEXT PRIMARY KEY,
    settled INTEGER NOT NULL DEFAULT 0,
    correct INTEGER NOT NULL DEFAULT 0,
    grading TEXT DEFAULT 'last',
    last_settled INTEGER NOT NULL DEFAULT 0,
    last_correct INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE level_bounces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series TEXT, level_usd REAL, kind TEXT,
    touched_at REAL, touch_price REAL,
    decided_at REAL, decide_price REAL,
    outcome TEXT
);
"""


class _FakeConn:
    """Wraps the shared temp connection; close() is a no-op so
    settle_window's per-call close() cannot kill the test DB."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        return self._conn.execute(sql, params)

    def executescript(self, sql):
        return self._conn.executescript(sql)

    def commit(self):
        return self._conn.commit()

    def close(self):
        return None


class GradeFirstCallTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.conn = sqlite3.connect(self._tmp.name)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.ns = _load_functions(lambda: _FakeConn(self.conn))

    def tearDown(self):
        self.conn.close()

    # --- fixture helpers -------------------------------------------------
    def _window(self, ticker, resolution=None, target=64000.0,
                open_ts=1_000_000_000.0, close_ts=1_000_000_900.0):
        self.conn.execute(
            "INSERT INTO windows (ticker, open_ts, close_ts, target, "
            "target_provisional, resolution) VALUES (?, ?, ?, ?, 0, ?)",
            (ticker, open_ts, close_ts, target, resolution),
        )
        self.conn.commit()

    def _signal(self, window, minute, direction, confidence, action,
                ts=1_000_000_000.0):
        self.conn.execute(
            "INSERT INTO signals (window, ts, minute, direction, "
            "confidence, action, reason) VALUES (?, ?, ?, ?, ?, ?, '')",
            (window, ts + minute * 60, minute, direction, confidence, action),
        )
        self.conn.commit()

    def _spot(self, ts, price):
        self.conn.execute(
            "INSERT OR IGNORE INTO spot_samples (ts, price) VALUES (?, ?)",
            (ts, price),
        )
        self.conn.commit()

    def _counts_fixture(self):
        """Five settled BTC windows; first-call (3, 2), last-signal (5, 3).

        W1 YES: first ENTER UP 63 (win, flag 1), last ENTER DOWN (loss).
        W2 NO:  first ENTER DOWN 80 (win, flag 0), last ENTER UP (loss).
        W3 YES: first ENTER DOWN 85 (loss, flag 0), last ENTER UP (win).
        W4 YES: HOLD only — no ENTER; counted by the old rule (win),
                not by the first-call rule (not a trade).
        W5 YES: ENTER UP only at minute 10.5 (late) — counted by the
                old rule (win), not by the first-call rule.
        """
        self._window("KXBTC15M-26NOV180900-01", "YES")
        self._signal("KXBTC15M-26NOV180900-01", 0.5, "UP", 55, "HOLD")
        self._signal("KXBTC15M-26NOV180900-01", 2.0, "UP", 63, "ENTER")
        self._signal("KXBTC15M-26NOV180900-01", 5.0, "DOWN", 80, "ENTER")
        self._window("KXBTC15M-26NOV180900-02", "NO")
        self._signal("KXBTC15M-26NOV180900-02", 1.0, "DOWN", 80, "ENTER")
        self._signal("KXBTC15M-26NOV180900-02", 4.0, "UP", 70, "ENTER")
        self._window("KXBTC15M-26NOV180900-03", "YES")
        self._signal("KXBTC15M-26NOV180900-03", 3.0, "DOWN", 85, "ENTER")
        self._signal("KXBTC15M-26NOV180900-03", 6.0, "UP", 90, "ENTER")
        self._window("KXBTC15M-26NOV180900-04", "YES")
        self._signal("KXBTC15M-26NOV180900-04", 2.0, "UP", 55, "HOLD")
        self._window("KXBTC15M-26NOV180900-05", "YES")
        self._signal("KXBTC15M-26NOV180900-05", 10.5, "UP", 90, "ENTER")

    # --- first ENTER -----------------------------------------------------
    def test_first_enter_is_first_actionable(self):
        self._counts_fixture()
        f = self.ns["_first_enter"]
        row = f(self.conn, "KXBTC15M-26NOV180900-01")
        self.assertEqual((row["direction"], row["confidence"], row["minute"]),
                         ("UP", 63, 2.0))
        # Late-only ENTER is not actionable.
        self.assertIsNone(f(self.conn, "KXBTC15M-26NOV180900-05"))
        # HOLD-only window has no ENTER at all.
        self.assertIsNone(f(self.conn, "KXBTC15M-26NOV180900-04"))
        # Unknown window.
        self.assertIsNone(f(self.conn, "KXBTC15M-26NOV180900-99"))

    def test_first_enter_confidence_boundary(self):
        # Exactly 70 is NOT below the floor (flag 0); 69.9 is (flag 1).
        # Covered end-to-end in the settle tests; here, the lookup itself.
        self._window("KXBTC15M-26NOV181000-01", "YES")
        self._signal("KXBTC15M-26NOV181000-01", 1.0, "UP", 70, "ENTER")
        row = self.ns["_first_enter"](
            self.conn, "KXBTC15M-26NOV181000-01")
        self.assertEqual(row["confidence"], 70)

    # --- counts ----------------------------------------------------------
    def test_counts_grade_first_call(self):
        self._counts_fixture()
        self.assertEqual(
            self.ns["_track_record_counts"](self.conn, "KXBTC15M"), (3, 2))

    def test_counts_last_signal_is_the_old_rule(self):
        self._counts_fixture()
        self.assertEqual(
            self.ns["_track_record_counts_last_signal"](
                self.conn, "KXBTC15M"), (5, 3))

    def test_counts_empty_series(self):
        self.assertEqual(
            self.ns["_track_record_counts"](self.conn, "KXETH15M"), (0, 0))
        self.assertEqual(
            self.ns["_track_record_counts_last_signal"](
                self.conn, "KXETH15M"), (0, 0))

    # --- regrade ---------------------------------------------------------
    def test_regrade_migrates_old_row(self):
        self._counts_fixture()
        self.conn.execute(
            "INSERT INTO track_record_summary (series, settled, correct, "
            "grading, last_settled, last_correct) "
            "VALUES ('KXBTC15M', 5, 3, 'last', 0, 0)")
        self.conn.commit()
        self.ns["_regrade_series"](self.conn, "KXBTC15M")
        row = self.conn.execute(
            "SELECT * FROM track_record_summary WHERE series = 'KXBTC15M'"
        ).fetchone()
        self.assertEqual(
            (row["settled"], row["correct"], row["grading"],
             row["last_settled"], row["last_correct"]),
            (3, 2, "first", 5, 3))
        flags = dict(self.conn.execute(
            "SELECT ticker, would_sit_out FROM windows").fetchall())
        self.assertEqual(flags, {
            "KXBTC15M-26NOV180900-01": 1,   # first ENTER 63 < 70
            "KXBTC15M-26NOV180900-02": 0,   # first ENTER 80
            "KXBTC15M-26NOV180900-03": 0,   # first ENTER 85
            "KXBTC15M-26NOV180900-04": None,  # no ENTER
            "KXBTC15M-26NOV180900-05": None,  # late ENTER only
        })

    def test_regrade_is_idempotent(self):
        self._counts_fixture()
        self.conn.execute(
            "INSERT INTO track_record_summary (series, settled, correct, "
            "grading) VALUES ('KXBTC15M', 5, 3, 'last')")
        self.conn.commit()
        self.ns["_regrade_series"](self.conn, "KXBTC15M")
        first = self.conn.execute(
            "SELECT * FROM track_record_summary WHERE series = 'KXBTC15M'"
        ).fetchone()
        self.ns["_regrade_series"](self.conn, "KXBTC15M")
        second = self.conn.execute(
            "SELECT * FROM track_record_summary WHERE series = 'KXBTC15M'"
        ).fetchone()
        self.assertEqual(tuple(first), tuple(second))

    def test_regrade_seeds_missing_row(self):
        self._counts_fixture()
        self.ns["_regrade_series"](self.conn, "KXBTC15M")
        row = self.conn.execute(
            "SELECT * FROM track_record_summary WHERE series = 'KXBTC15M'"
        ).fetchone()
        self.assertEqual(
            (row["settled"], row["correct"], row["grading"],
             row["last_settled"], row["last_correct"]),
            (3, 2, "first", 5, 3))

    def test_regrade_fresh_database_inserts_zero(self):
        self.ns["_regrade_series"](self.conn, "KXBTC15M")
        row = self.conn.execute(
            "SELECT * FROM track_record_summary WHERE series = 'KXBTC15M'"
        ).fetchone()
        self.assertEqual(
            (row["settled"], row["correct"], row["grading"]),
            (0, 0, "first"))

    # --- settle_window ---------------------------------------------------
    def _settle_fixture(self, ticker, resolution_target, first_conf,
                        first_dir, last_dir, late_only=False):
        """Window + spot samples settling YES or NO + two ENTERs.

        close_ts = 1_000_000_900; final-minute samples decide the
        resolution against target 64000."""
        self._window(ticker, None, target=64000.0)
        if resolution_target == "YES":
            self._spot(1_000_000_840.0, 64010.0)
            self._spot(1_000_000_870.0, 64020.0)
        else:
            self._spot(1_000_000_840.0, 63990.0)
            self._spot(1_000_000_870.0, 63980.0)
        if late_only:
            self._signal(ticker, 10.5, first_dir, first_conf, "ENTER")
        else:
            self._signal(ticker, 2.0, first_dir, first_conf, "ENTER")
            self._signal(ticker, 5.0, last_dir, 80, "ENTER")

    def test_settle_window_grades_first_call(self):
        # YES window: first ENTER UP 63 (win, flag 1); last DOWN (loss).
        self._settle_fixture("KXBTC15M-26NOV181000-01", "YES", 63, "UP",
                             "DOWN")
        self.ns["settle_window"]("KXBTC15M-26NOV181000-01")
        row = self.conn.execute(
            "SELECT * FROM track_record_summary WHERE series = 'KXBTC15M'"
        ).fetchone()
        self.assertEqual(
            (row["settled"], row["correct"],
             row["last_settled"], row["last_correct"], row["grading"]),
            (1, 1, 1, 0, "first"))
        win = self.conn.execute(
            "SELECT resolution, would_sit_out FROM windows "
            "WHERE ticker = 'KXBTC15M-26NOV181000-01'").fetchone()
        self.assertEqual((win["resolution"], win["would_sit_out"]),
                         ("YES", 1))

    def test_settle_window_flag_boundary_70(self):
        # NO window: first ENTER DOWN 70 (win, flag 0 — exactly 70 is
        # not below the floor); last UP (loss under the old rule).
        self._settle_fixture("KXBTC15M-26NOV181000-02", "NO", 70, "DOWN",
                             "UP")
        self.ns["settle_window"]("KXBTC15M-26NOV181000-02")
        row = self.conn.execute(
            "SELECT * FROM track_record_summary WHERE series = 'KXBTC15M'"
        ).fetchone()
        self.assertEqual(
            (row["settled"], row["correct"],
             row["last_settled"], row["last_correct"]),
            (1, 1, 1, 0))
        win = self.conn.execute(
            "SELECT would_sit_out FROM windows "
            "WHERE ticker = 'KXBTC15M-26NOV181000-02'").fetchone()
        self.assertEqual(win["would_sit_out"], 0)

    def test_settle_window_late_enter_not_counted(self):
        # YES window, ENTER only at minute 10.5: resolution is still
        # recorded, but the window is not a trade — the graded count
        # stays 0 (no summary increment for first-call), the flag
        # stays NULL, and only the last-signal diagnostic counts it.
        self._settle_fixture("KXBTC15M-26NOV181000-03", "YES", 90, "UP",
                             "UP", late_only=True)
        self.ns["settle_window"]("KXBTC15M-26NOV181000-03")
        row = self.conn.execute(
            "SELECT * FROM track_record_summary WHERE series = 'KXBTC15M'"
        ).fetchone()
        self.assertEqual(
            (row["settled"], row["correct"],
             row["last_settled"], row["last_correct"], row["grading"]),
            (0, 0, 1, 1, "first"))
        win = self.conn.execute(
            "SELECT resolution, would_sit_out FROM windows "
            "WHERE ticker = 'KXBTC15M-26NOV181000-03'").fetchone()
        self.assertEqual((win["resolution"], win["would_sit_out"]),
                         ("YES", None))

    def test_settle_window_no_signals(self):
        self._window("KXBTC15M-26NOV181000-04", None, target=64000.0)
        self._spot(1_000_000_840.0, 64010.0)
        self._spot(1_000_000_870.0, 64020.0)
        self.ns["settle_window"]("KXBTC15M-26NOV181000-04")
        win = self.conn.execute(
            "SELECT resolution, would_sit_out FROM windows "
            "WHERE ticker = 'KXBTC15M-26NOV181000-04'").fetchone()
        self.assertEqual((win["resolution"], win["would_sit_out"]),
                         ("YES", None))
        self.assertIsNone(self.conn.execute(
            "SELECT * FROM track_record_summary WHERE series = 'KXBTC15M'"
        ).fetchone())

    def test_settle_window_already_resolved_is_noop(self):
        self._settle_fixture("KXBTC15M-26NOV181000-05", "YES", 63, "UP",
                             "DOWN")
        self.ns["settle_window"]("KXBTC15M-26NOV181000-05")
        self.ns["settle_window"]("KXBTC15M-26NOV181000-05")
        row = self.conn.execute(
            "SELECT * FROM track_record_summary WHERE series = 'KXBTC15M'"
        ).fetchone()
        self.assertEqual((row["settled"], row["correct"]), (1, 1))


class LevelBounceTestCase(unittest.TestCase):
    LEVELS = {
        "resistance": [{"price_usd": 64000.0, "touches": 2,
                        "last_tested": 2_000_000_000, "kind": "swing"}],
        "support": [{"price_usd": 63900.0, "touches": 1,
                     "last_tested": 2_000_000_000, "kind": "swing"}],
    }

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.conn = sqlite3.connect(self._tmp.name)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.ns = _load_functions(lambda: _FakeConn(self.conn))
        self.t0 = 2_000_000_000.0

    def tearDown(self):
        self.conn.close()

    def _record(self, spot, now):
        self.ns["_record_level_bounces"](
            "KXBTC15M", spot, self.LEVELS, 25, now, conn=self.conn)

    def _rows(self):
        return self.conn.execute(
            "SELECT * FROM level_bounces ORDER BY id").fetchall()

    def test_touch_detected_within_cluster(self):
        # Spot 63990 is $10 from the $64,000 resistance (cluster 25);
        # spot 63910 is $10 from the $63,900 support: two touches.
        self._record(63990.0, self.t0)
        self._record(63910.0, self.t0 + 60)
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {(r["kind"], r["level_usd"]) for r in rows},
            {("resistance", 64000.0), ("support", 63900.0)})
        self.assertTrue(all(r["outcome"] is None for r in rows))

    def test_no_touch_outside_cluster(self):
        # $30 from each level: outside the $25 cluster radius.
        self._record(63970.0, self.t0)
        self.assertEqual(len(self._rows()), 0)

    def test_no_duplicate_while_pending(self):
        self._record(63990.0, self.t0)
        self._record(63995.0, self.t0 + 60)
        self._record(63985.0, self.t0 + 300)
        self.assertEqual(len(self._rows()), 1)

    def test_resistance_held(self):
        self._record(63990.0, self.t0)
        self._record(63950.0, self.t0 + 900)  # <= 64000 after 15 min
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "held")
        self.assertEqual(rows[0]["decide_price"], 63950.0)

    def test_resistance_broke(self):
        self._record(63990.0, self.t0)
        self._record(64100.0, self.t0 + 900)  # > 64000 after 15 min
        self.assertEqual(self._rows()[0]["outcome"], "broke")

    def test_support_held_and_broke(self):
        self._record(63910.0, self.t0)
        self._record(63950.0, self.t0 + 900)  # >= 63900: held
        self.assertEqual(self._rows()[0]["outcome"], "held")
        # Fresh DB, second case: support breaks below the level.
        self.conn.execute("DELETE FROM level_bounces")
        self.conn.commit()
        self._record(63910.0, self.t0)
        self._record(63800.0, self.t0 + 900)  # < 63900: broke
        self.assertEqual(self._rows()[0]["outcome"], "broke")

    def test_not_decided_before_observe_window(self):
        self._record(63990.0, self.t0)
        self._record(64100.0, self.t0 + 899)  # 1s early: still pending
        self.assertIsNone(self._rows()[0]["outcome"])

    def test_stale_after_six_hours(self):
        self._record(63990.0, self.t0)
        self._record(63950.0, self.t0 + 7 * 3600)
        self.assertEqual(self._rows()[0]["outcome"], "stale")

    def test_no_spot_no_touch_no_crash(self):
        self.ns["_record_level_bounces"](
            "KXBTC15M", None, self.LEVELS, 25, self.t0, conn=self.conn)
        self.ns["_record_level_bounces"](
            "KXBTC15M", 63990.0, None, 25, self.t0, conn=self.conn)
        self.ns["_record_level_bounces"](
            "KXBTC15M", 63990.0, {}, 25, self.t0, conn=self.conn)
        self.assertEqual(len(self._rows()), 0)

    def test_summary_groups_by_bucket_and_rates(self):
        # Two touches near $63,900/$63,912 land in the same $25 bucket
        # (63900); one at $64,000 resistance; one stale (excluded).
        now = self.t0
        for level, kind, outcome, decided in (
            (63900.0, "support", "held", now + 100),
            (63912.0, "support", "broke", now + 200),
            (64000.0, "resistance", "held", now + 300),
            (63900.0, "support", "stale", now + 400),
        ):
            self.conn.execute(
                "INSERT INTO level_bounces (series, level_usd, kind, "
                "touched_at, touch_price, decided_at, decide_price, "
                "outcome) VALUES ('KXBTC15M', ?, ?, ?, ?, ?, ?, ?)",
                (level, kind, now, level, decided, level, outcome))
        self.conn.commit()
        levels, recent = self.ns["_level_bounce_summary"](
            self.conn, "KXBTC15M")
        by_key = {(g["kind"], g["level_usd"]): g for g in levels}
        self.assertEqual(
            (by_key[("support", 63900.0)]["touches"],
             by_key[("support", 63900.0)]["held"],
             by_key[("support", 63900.0)]["broke"],
             by_key[("support", 63900.0)]["hold_rate_pct"]),
            (2, 1, 1, 50.0))
        self.assertEqual(
            (by_key[("resistance", 64000.0)]["touches"],
             by_key[("resistance", 64000.0)]["hold_rate_pct"]),
            (1, 100.0))
        # Recent: newest first, decided only (stale excluded).
        self.assertEqual(
            [r["outcome"] for r in recent], ["held", "broke", "held"])
        self.assertEqual(recent[0]["level_usd"], 64000.0)

    def test_summary_empty(self):
        levels, recent = self.ns["_level_bounce_summary"](
            self.conn, "KXBTC15M")
        self.assertEqual((levels, recent), ([], []))

    def test_summary_series_filter(self):
        self.conn.execute(
            "INSERT INTO level_bounces (series, level_usd, kind, "
            "touched_at, touch_price, decided_at, decide_price, outcome) "
            "VALUES ('KXETH15M', 1800.0, 'support', 1, 1800.0, 2, 1800.0, "
            "'held')")
        self.conn.commit()
        levels, _ = self.ns["_level_bounce_summary"](self.conn, "KXBTC15M")
        self.assertEqual(levels, [])
        levels, _ = self.ns["_level_bounce_summary"](self.conn, "")
        self.assertEqual(len(levels), 1)


class WriteBudgetTestCase(unittest.TestCase):
    """Source-level write checks for the new code (same style as
    test_write_budget.py): the shadow flag must ride an existing
    write, and a bounce touch must cost at most 2 writes."""

    def test_shadow_flag_rides_resolution_update(self):
        m = re.search(r"def settle_window\(.*?\n(?=^def |\Z)", TRACKER_SRC,
                      re.S | re.M)
        body = m.group(0)
        updates = re.findall(r"UPDATE windows SET[^;]*?WHERE ticker",
                             body, re.S)
        self.assertEqual(len(updates), 1,
                         "settle_window must have exactly one windows "
                         "UPDATE (the resolution write)")
        self.assertIn("would_sit_out = ?", updates[0])

    def test_bounce_writes_bounded(self):
        m = re.search(r"def _record_level_bounces\(.*?\n(?=^def |\Z)",
                      TRACKER_SRC, re.S | re.M)
        body = m.group(0)
        self.assertEqual(body.count("INSERT INTO level_bounces"), 1)
        # One UPDATE to decide held/broke, one to mark stale.
        self.assertEqual(body.count("UPDATE level_bounces SET"), 2)
        # Touch detection itself is read-only: no INSERT per tick.
        self.assertNotIn("INSERT INTO signals", body)
        self.assertNotIn("UPDATE track_record_summary", body)

    def test_no_new_kalshi_poll_intervals(self):
        # The change adds no fetches at all: no new urlopen/fetch in
        # the new code paths.
        for name in ("_record_level_bounces", "_regrade_series",
                     "settle_window"):
            body = re.search(rf"def {name}\(.*?\n(?=^def |\Z)",
                             TRACKER_SRC, re.S | re.M).group(0)
            self.assertNotIn("urlopen", body)
            self.assertNotIn("fetch_json", body)

    def test_shadow_flag_never_in_endpoints(self):
        # would_sit_out is DB-only: no endpoint response may carry it.
        for name in ("track_record", "entries", "level_bounces"):
            body = re.search(rf"def {name}\(.*?\n(?=^def |\Z)",
                             TRACKER_SRC, re.S | re.M)
            if body:
                self.assertNotIn("would_sit_out", body.group(0))


if __name__ == "__main__":
    unittest.main()
