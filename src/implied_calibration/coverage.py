"""Polling-slot coverage includes unknown observation windows explicitly."""

from __future__ import annotations

from collections import Counter, defaultdict
from bisect import bisect_right
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import statistics
from typing import Any


def coverage_report(path: Path, as_of_ms: int | None = None, *, trailing_window: int = 20,
                    min_history: int = 3) -> dict[str, Any]:
    if not Path(path).is_file():
        raise ValueError("--db must name an existing database")
    if not 1 <= min_history <= trailing_window:
        raise ValueError("min_history must be between 1 and trailing_window")
    if as_of_ms is not None and (type(as_of_ms) is not int or as_of_ms < 0):
        raise ValueError("as_of_ms must be a nonnegative integer")
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        explicit_bound = as_of_ms is not None
        if as_of_ms is None:
            as_of_ms = connection.execute(
                "SELECT COALESCE(MAX(stamp), 0) FROM (SELECT MAX(fetched_at_ms) stamp FROM raw_records "
                "UNION ALL SELECT MAX(fetched_at_ms) FROM heartbeat)").fetchone()[0]
        snapshots = connection.execute(
            "SELECT * FROM snapshots WHERE fetched_at_ms<=? ORDER BY fetched_at_ms, snapshot_id", (as_of_ms,)).fetchall()
        events = [json.loads(row[0]) for row in connection.execute(
            "SELECT payload_json FROM heartbeat WHERE fetched_at_ms<=? ORDER BY fetched_at_ms, event_id", (as_of_ms,))]
        assert all(row["fetched_at_ms"] <= as_of_ms for row in snapshots)
        assert all(row["fetched_at_ms"] <= as_of_ms for row in events)
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            grouped[event["run_id"]].append(event)
        sessions = []
        warnings = []
        expected: dict[tuple[str, int], int] = {}
        cycles: dict[str, list[dict[str, Any]]] = {}
        cycle_starts: dict[str, list[int]] = {}
        for run_id, history in sorted(grouped.items()):
            starts = [row for row in history if row["event"] == "session_start"]
            ends = [row for row in history if row["event"] == "session_end"]
            if len(starts) != 1 or len(ends) > 1:
                warnings.append({"run_id": run_id, "reason": "ambiguous_session_bounds"})
                continue
            start = starts[0]
            closed = bool(ends)
            observation_end = ends[0]["fetched_at_ms"] if closed else (
                as_of_ms + 1 if explicit_bound else max(row["fetched_at_ms"] for row in history) + 1)
            end_ms = observation_end
            interval = start["interval_ms"]
            clock_changed = any(row["event"] == "clock_change" for row in history)
            if start.get("seconds") is not None and not clock_changed:
                end_ms = min(end_ms, start.get("anchor_wall_ms", start["fetched_at_ms"]) + start["seconds"] * 1000)
            if (end_ms < start["fetched_at_ms"] and not clock_changed) or start["first_slot_ms"] % interval:
                warnings.append({"run_id": run_id, "reason": "invalid_session_bounds"})
                continue
            session = {"run_id": run_id, "start_ms": start["fetched_at_ms"], "end_ms_exclusive": end_ms,
                       "first_slot_ms": start["first_slot_ms"], "interval_ms": interval,
                       "observation_end_ms_exclusive": observation_end,
                       "expected_slots_known": not clock_changed,
                       "start_precision": "anchor_wall_ms" if "anchor_wall_ms" in start else "heartbeat_timestamp",
                       "status": "closed" if closed else "incomplete",
                       "bound": "session_end" if closed else "explicit_as_of" if explicit_bound else "last_observed_event"}
            sessions.append(session)
            cycles[run_id] = sorted((row for row in history if row["event"] == "cycle"),
                                    key=lambda row: row["cycle_started_ms"])
            cycle_starts[run_id] = [row["cycle_started_ms"] for row in cycles[run_id]]
            slots = range(start["first_slot_ms"], end_ms, interval)
            if clock_changed:
                warnings.append({"run_id": run_id, "reason": "clock_change_expected_window_unknown"})
                declared = {row["scheduled_ms"] for row in cycles[run_id]}
                for event in history:
                    if event["event"] == "gap":
                        declared.update(range(event["first_slot_ms"], event["last_slot_ms"] + 1, interval))
                slots = sorted(declared)
            for slot in slots:
                expected[run_id, slot] = slot + interval if clock_changed else min(slot + interval, end_ms)
        received: set[tuple[str, int]] = set()
        unassigned = []
        inferred = 0
        duplicate_slots = 0
        for snapshot in snapshots:
            candidates = []
            for session in sessions:
                run_id = session["run_id"]
                position = bisect_right(cycle_starts[run_id], snapshot["request_sent_ms"]) - 1
                matching = []
                if position >= 0:
                    candidate = cycles[run_id][position]
                    if snapshot["response_received_ms"] <= candidate["cycle_finished_ms"]:
                        matching = [candidate]
                for cycle in matching:
                    key = run_id, cycle["scheduled_ms"]
                    if key in expected:
                        candidates.append((key, False))
                if (not matching and session["expected_slots_known"]
                        and session["first_slot_ms"] <= snapshot["request_sent_ms"] < session["end_ms_exclusive"]):
                    slot = session["first_slot_ms"] + ((snapshot["request_sent_ms"] - session["first_slot_ms"])
                                                       // session["interval_ms"]) * session["interval_ms"]
                    key = run_id, slot
                    if key in expected:
                        candidates.append((key, True))
            if len(candidates) != 1:
                unassigned.append(snapshot["snapshot_id"])
                continue
            key, fallback = candidates[0]
            inferred += int(fallback)
            duplicate_slots += int(key in received)
            received.add(key)
        daily: dict[int, dict[str, Any]] = {}

        def day(stamp: int) -> dict[str, Any]:
            ordinal = stamp // 86400000
            if ordinal not in daily:
                daily[ordinal] = {"date": datetime.fromtimestamp(ordinal * 86400, timezone.utc).date().isoformat(),
                                  "expected_slots": 0, "received_slots": 0, "missing_slots": 0,
                                  "successful_snapshots": 0, "longest_gap_ms": 0}
            return daily[ordinal]

        gaps: dict[int, list[tuple[int, int]]] = defaultdict(list)
        for key, end in expected.items():
            slot = key[1]
            row = day(slot)
            row["expected_slots"] += 1
            row["received_slots"] += int(key in received)
            row["missing_slots"] += int(key not in received)
            if key not in received:
                for ordinal in range(slot // 86400000, (end - 1) // 86400000 + 1):
                    gaps[ordinal].append((max(slot, ordinal * 86400000), min(end, (ordinal + 1) * 86400000)))
        for snapshot in snapshots:
            day(snapshot["fetched_at_ms"])["successful_snapshots"] += 1
        for ordinal, intervals in gaps.items():
            merged = []
            for left, right in sorted(intervals):
                if merged and left <= merged[-1][1]:
                    merged[-1] = merged[-1][0], max(merged[-1][1], right)
                else:
                    merged.append((left, right))
            day(ordinal * 86400000)["longest_gap_ms"] = max(right - left for left, right in merged)
        low_counts = []
        prior: list[int] = []
        for snapshot in snapshots:
            history = prior[-trailing_window:]
            if len(history) >= min_history:
                median = statistics.median(history)
                if snapshot["n_instruments"] < 0.5 * median:
                    low_counts.append({"snapshot_id": snapshot["snapshot_id"], "fetched_at_ms": snapshot["fetched_at_ms"],
                                       "n_instruments": snapshot["n_instruments"], "trailing_median": median})
            prior.append(snapshot["n_instruments"])
        expiry_counts = []
        if snapshots:
            expiry_counts = [dict(row) for row in connection.execute(
                "SELECT expiry_ms, COUNT(*) n_instruments, SUM(identity_status='verified') verified_instruments "
                "FROM quotes WHERE snapshot_id=? GROUP BY expiry_ms ORDER BY expiry_ms", (snapshots[-1]["snapshot_id"],))]
        issue_counts = dict(connection.execute(
            "SELECT reason, COUNT(*) FROM issues WHERE fetched_at_ms<=? GROUP BY reason ORDER BY reason", (as_of_ms,)))
        unplaced = dict(connection.execute(
            "SELECT reason, COUNT(*) FROM issues WHERE fetched_at_ms IS NULL GROUP BY reason ORDER BY reason"))
        unobserved = []
        windows = sorted((row["start_ms"], row["observation_end_ms_exclusive"]) for row in sessions
                         if row["observation_end_ms_exclusive"] >= row["start_ms"])
        rightmost = windows[0][1] if windows else 0
        for left, right in windows[1:]:
            if left > rightmost:
                unobserved.append({"start_ms": rightmost, "end_ms_exclusive": left, "duration_ms": left - rightmost})
            rightmost = max(rightmost, right)
        horizon = connection.execute("SELECT value FROM settings WHERE name='max_days_to_expiry'").fetchone()
        return {"as_of_ms": as_of_ms, "days": [daily[key] for key in sorted(daily)], "sessions": sessions,
                "max_days_to_expiry": int(horizon[0]), "unobserved_between_sessions": unobserved,
                "incomplete_sessions": sum(row["status"] == "incomplete" for row in sessions),
                "snapshots_without_session_slot": unassigned, "inferred_slot_assignments": inferred,
                "additional_snapshots_same_slot": duplicate_slots, "warnings": warnings,
                "low_instrument_counts": low_counts, "trailing_window": trailing_window, "min_history": min_history,
                "instruments_per_expiry": expiry_counts, "latest_snapshot_id": snapshots[-1]["snapshot_id"] if snapshots else None,
                "snapshot_status_counts": dict(sorted(Counter(row["status"] for row in snapshots).items())),
                "issue_counts": issue_counts, "unplaced_issue_counts": unplaced,
                "expected_window_policy": "Only declared session windows count; time between sessions is unobserved",
                "expiry_count_policy": "Retained instruments in the latest snapshot; source counts precede the horizon filter"}
    finally:
        connection.close()
