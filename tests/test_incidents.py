"""Incident grouping in yardguard.engine.AlertStore (plain python, deterministic, no Qt or AI).

    run.cmd tests\\test_incidents.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "incidents" / "engine"
os.environ["YARDGUARD_CONFIG"] = str(OUT.parent / "config.json")  # never touch the user's config
sys.path.insert(0, str(ROOT))

from yardguard.engine import Alert, AlertStore, incident_title, incident_verdict  # noqa: E402

T0 = 1_761_177_600.0  # fixed clock: 2025-10-23 00:00 UTC


def fresh(name: str, gap: float = 300.0) -> AlertStore:
    folder = OUT / name
    shutil.rmtree(folder, ignore_errors=True)
    return AlertStore(str(folder), incident_gap_s=gap)


_n = [0]


def alert(dt: float, channel: int = 1, rule: str = "night_presence", severity: str = "warning",
          verdict: str = "unverified", site: str = "s1", camera: str = "", title: str = "", files=None) -> Alert:
    _n[0] += 1
    snap, crop = files or ("", "")
    return Alert(f"a{_n[0]:03d}", site, channel, camera or {1: "Yard parking", 5: "Street"}.get(channel, f"Cam {channel}"),
                 T0 + dt, rule, title or "Person near parked cars at night", "detail", severity,
                 snap, crop, verdict=verdict)


def test_grouping_within_and_across_gap():
    store = fresh("gap")
    a, b, c = alert(0), alert(120), alert(400)   # 400 - 120 = 280 s < 300: still one incident
    for x in (a, b, c):
        store.add(x)
    assert a.incident_id and a.incident_id == b.incident_id == c.incident_id
    d = alert(400 + 301)                          # more than the gap after the end: new incident
    store.add(d)
    assert d.incident_id != a.incident_id
    incs = store.list_incidents()
    assert [i.id for i in incs] == [d.incident_id, a.incident_id]  # newest first
    first = store.get_incident(a.incident_id)
    assert first.start_ts == T0 and first.end_ts == T0 + 400 and first.alert_count == 3
    assert first.duration_s == 400 and first.status == "open" and first.unread == 3
    assert [x.id for x in store.incident_alerts(a.incident_id)] == [a.id, b.id, c.id]
    assert store.get(b.id).incident_id == a.incident_id  # stored on the alert row too
    # a custom gap per call (the live setting) wins over the store default
    e = alert(400 + 301 + 50)
    store.add(e, gap_s=10)
    assert e.incident_id != d.incident_id
    # other sites never join
    f = alert(400 + 301 + 60, site="s2")
    store.add(f)
    assert f.incident_id not in (d.incident_id, e.incident_id)
    assert store.open_incident_count() == 4
    store.close()


def test_cross_camera_grouping_and_title():
    store = fresh("cross")
    a = alert(0, 1, "night_presence", "warning")
    b = alert(60, 5, "prowling", "info", title="Person going from car to car", camera="Street")
    c = alert(90, 1, "loitering", "info", title="Person lingering near cars")
    for x in (a, b, c):
        store.add(x)
    inc = store.get_incident(a.incident_id)
    assert b.incident_id == a.incident_id == c.incident_id
    assert inc.channels == [1, 5] and inc.cameras == ["Yard parking", "Street"]
    assert inc.title == "Person near cars at night · Yard parking +1 camera", inc.title
    assert inc.cover_alert_id == a.id  # the most severe one
    assert incident_title([a]) == "Person near cars at night · Yard parking"
    store.close()


def test_handled_incident_does_not_absorb():
    store = fresh("handled")
    a = alert(0)
    store.add(a)
    store.mark_incident(a.incident_id, "handled")
    b = alert(30)  # well inside the gap, but the user already handled the first story
    store.add(b)
    assert b.incident_id != a.incident_id
    store.mark_incident(b.incident_id, "false_alarm")
    c = alert(60)
    store.add(c)
    assert c.incident_id not in (a.incident_id, b.incident_id)
    assert store.open_incident_count() == 1
    assert [i.id for i in store.list_incidents(status="handled")] == [a.incident_id]
    assert store.incident_counts() == {"open": 1, "handled": 1, "false_alarm": 1, "dismissed": 0}
    store.close()


def test_verdict_and_severity_derivation():
    assert incident_verdict(["rejected", "confirmed", "pending"]) == "confirmed"
    assert incident_verdict(["rejected", "pending", "unverified"]) == "pending"
    assert incident_verdict(["rejected", "rejected"]) == "rejected"
    assert incident_verdict(["rejected", "unverified"]) == "unverified"
    assert incident_verdict([]) == "unverified"
    store = fresh("verdict")
    a = alert(0, severity="info", verdict="pending")
    b = alert(30, severity="warning", verdict="pending")
    store.add(a)
    store.add(b)
    inc = store.get_incident(a.incident_id)
    assert inc.verdict == "pending" and inc.severity == "warning" and inc.cover_alert_id == b.id
    # brain answers: first rejected -> still pending (b is open)
    a.verdict = "rejected"
    store.update(a)
    assert store.get_incident(a.incident_id).verdict == "pending"
    b.verdict = "rejected"
    store.update(b)
    assert store.get_incident(a.incident_id).verdict == "rejected"
    # a confirmed alert turns critical: the incident follows on update()
    a.verdict, a.severity, a.brain_findings = "confirmed", "critical", ["hand on a car door handle"]
    store.update(a)
    inc = store.get_incident(a.incident_id)
    assert inc.verdict == "confirmed" and inc.severity == "critical" and inc.cover_alert_id == a.id
    # update() never moves an alert out of its incident, even from an object without incident_id
    stale = store.get(b.id)
    stale.incident_id = ""
    store.update(stale)
    assert store.get(b.id).incident_id == a.incident_id
    # stopping the monitor resolves pending verdicts: incidents follow
    c = alert(60, verdict="pending")
    store.add(c)
    store.mark_incident(a.incident_id, "handled")  # so c starts its own incident
    d = alert(61, verdict="pending")
    store.add(d)
    assert store.get_incident(d.incident_id).verdict == "pending"
    store.resolve_pending()
    assert store.get_incident(d.incident_id).verdict == "unverified"
    store.close()


def test_mark_incident_propagates():
    store = fresh("mark")
    alerts = [alert(i * 10) for i in range(3)]
    for x in alerts:
        store.add(x)
    inc_id = alerts[0].incident_id
    store.mark(alerts[0].id, "seen")
    assert store.get_incident(inc_id).unread == 2 and store.unread_incident_count() == 1
    store.mark_incident(inc_id, "false_alarm")
    assert {store.get(x.id).status for x in alerts} == {"false_alarm"}
    assert store.get_incident(inc_id).status == "false_alarm" and store.unread_incident_count() == 0
    store.mark_incident(inc_id, "open")  # undo
    assert {store.get(x.id).status for x in alerts} == {"seen"}
    store.mark_incident(inc_id, "handled")
    assert {store.get(x.id).status for x in alerts} == {"seen"}
    assert store.get_incident(inc_id).status == "handled" and store.open_incident_count() == 0
    try:
        store.mark_incident(inc_id, "bogus")
        raise AssertionError("unknown status accepted")
    except ValueError:
        pass
    store.close()


def test_unread_ignores_ai_dismissed():
    store = fresh("unread")
    a = alert(0, verdict="rejected")
    store.add(a)
    assert store.get_incident(a.incident_id).unread == 1  # the card still shows a dot
    assert store.unread_incident_count() == 0              # but the badge does not count it
    store.close()


def test_delete_incident_removes_files():
    store = fresh("delete")
    folder = store.dir / "alerts"
    folder.mkdir(parents=True)
    pics = []
    for i in range(2):
        snap, crop = folder / f"{i}.jpg", folder / f"{i}_crop.jpg"
        snap.write_bytes(b"x")
        crop.write_bytes(b"y")
        pics += [snap, crop]
    a = alert(0, files=(str(pics[0]), str(pics[1])))
    b = alert(20, files=(str(pics[2]), str(pics[3])))
    other = alert(5000)
    for x in (a, b, other):
        store.add(x)
    assert store.delete_incident(a.incident_id) == []
    assert store.get_incident(a.incident_id) is None and store.get(a.id) is None and store.get(b.id) is None
    assert not any(p.exists() for p in pics)
    assert store.get(other.id) is not None
    # a locked file: the records go, the path is reported for a retry
    c = alert(6000, files=(str(folder / "locked.jpg"), ""))
    (folder / "locked.jpg").write_bytes(b"z")
    store.add(c)
    real = Path.unlink

    def refuse(self, missing_ok=False):
        if self.name == "locked.jpg":
            raise PermissionError("in use")
        return real(self, missing_ok=missing_ok)

    Path.unlink = refuse
    try:
        left = store.delete_incident(c.incident_id)
    finally:
        Path.unlink = real
    assert left == [str(folder / "locked.jpg")] and store.get(c.id) is None, left
    # deleting the last alert of an incident removes the incident
    store.delete(other.id)
    assert store.get_incident(other.incident_id) is None
    store.close()


def test_migration_of_old_database():
    folder = OUT / "migrate"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    db = sqlite3.connect(str(folder / "alerts.db"))
    old_cols = ["id", "site_id", "channel", "camera", "ts", "rule", "title", "detail", "severity",
                "snapshot_path", "crop_path", "boxes", "verdict", "brain_findings", "status"]
    db.execute("CREATE TABLE alerts (id TEXT PRIMARY KEY, site_id TEXT, channel INTEGER, camera TEXT, ts REAL, "
               "rule TEXT, title TEXT, detail TEXT, severity TEXT, snapshot_path TEXT, crop_path TEXT, boxes TEXT, "
               "verdict TEXT, brain_findings TEXT, status TEXT)")
    rows = [  # id, dt, channel, verdict, status
        ("o1", 0, 1, "confirmed", "seen"), ("o2", 100, 5, "rejected", "seen"),       # story 1: all looked at
        ("o3", 5000, 1, "unverified", "new"), ("o4", 5200, 1, "unverified", "seen"),  # story 2: one still new
        ("o5", 9000, 5, "rejected", "false_alarm"),                                   # story 3: false alarm
    ]
    for aid, dt, ch, verdict, status in rows:
        db.execute(f"INSERT INTO alerts ({', '.join(old_cols)}) VALUES ({', '.join('?' * len(old_cols))})",
                   (aid, "s1", ch, f"Cam {ch}", T0 + dt, "night_presence", "Person near parked cars at night", "",
                    "warning", "", "", "[]", verdict, "[]", status))
    db.commit()
    db.close()
    store = AlertStore(str(folder))
    cols = {r[1] for r in sqlite3.connect(str(folder / "alerts.db")).execute("PRAGMA table_info(alerts)")}
    assert "incident_id" in cols
    incs = store.list_incidents()
    assert len(incs) == 3, [(i.id, i.alert_count) for i in incs]
    third, second, first = incs
    assert first.alert_count == 2 and first.status == "handled" and first.verdict == "confirmed"
    assert first.channels == [1, 5] and first.title.endswith("+1 camera")
    assert second.alert_count == 2 and second.status == "open" and second.unread == 1
    assert third.status == "false_alarm" and third.verdict == "rejected"
    assert store.get("o2").incident_id == first.id
    # new alerts after the migration group normally with the open story
    a = alert(5300, 1)
    store.add(a)
    assert a.incident_id == second.id
    store.close()
    store = AlertStore(str(folder))  # reopening does not group again
    assert len(store.list_incidents()) == 3
    store.close()


def test_add_again_keeps_incident():
    store = fresh("readd")
    a = alert(0)
    store.add(a)
    first = a.incident_id
    store.mark_incident(first, "handled")
    a.incident_id = ""
    store.add(a)  # INSERT OR REPLACE of the same alert
    assert a.incident_id == first and len(store.list_incidents()) == 1
    raw = json.loads(json.dumps(store.get(a.id).__dict__))
    assert raw["incident_id"] == first
    store.close()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = 0
    t0 = time.perf_counter()
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed in {time.perf_counter() - t0:.1f} s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
