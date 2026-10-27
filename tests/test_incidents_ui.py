"""Offscreen render + behaviour test of the Incidents page (yardguard/incidents_ui.py) in the real
main window: incident cards, detail with filmstrip and timeline, live updates, actions, the
"All alerts" toggle, the empty state and the dashboard strip, in light and dark.

    set QT_QPA_PLATFORM=offscreen & set QT_QPA_FONTDIR=C:\\Windows\\Fonts
    run.cmd tests\\test_incidents_ui.py
Screenshots go to .cache/tests/incidents/shots. Uses copies of real night snapshots, never the originals.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "incidents"
SHOTS = OUT / "shots"
EVENTS = OUT / "ui_events"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ["YARDGUARD_CONFIG"] = str(OUT / "config.json")  # never touch the user's config
os.environ["TEMP"] = os.environ["TMP"] = str(ROOT / ".cache" / "tmp")
sys.path.insert(0, str(ROOT))

from local_data import SNAPS, need_snapshots  # noqa: E402

need_snapshots()
FRAMES = ROOT / ".cache" / "tests" / "frames"

for p in (SHOTS, EVENTS, OUT / "empty"):
    shutil.rmtree(p, ignore_errors=True)
SHOTS.mkdir(parents=True, exist_ok=True)
CFG = {"sites": [{"id": "s1", "name": "Home yard", "host": "127.0.0.1", "port": 9, "brand": "dahua",
                  "cameras": [{"channel": 1, "name": "Yard parking", "role": "Parking"},
                              {"channel": 5, "name": "Yard / street side", "role": "Parking"},
                              {"channel": 2, "name": "Stairwell", "role": "Entrance"},
                              {"channel": 7, "name": "Hallway", "role": "Indoor"}]}],
       "active_site_id": "s1",
       "settings": {"events_dir": str(EVENTS), "brain_mode": "off", "monitoring_on": False}}
(OUT / "config.json").write_text(json.dumps(CFG), encoding="utf-8")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import QPoint, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from yardguard import clips, config, incidents_ui  # noqa: E402
from yardguard.engine import Alert, AlertStore, draw_boxes  # noqa: E402
from yardguard.gui import AppContext, MainWindow  # noqa: E402

assert config.CONFIG_PATH == OUT / "config.json"
failures: list[str] = []
CLIP_SHOTS = ROOT / ".cache" / "tests" / "clips" / "shots"
NIGHT_VIDEOS = {1: ROOT / ".cache" / "tests" / "night" / "long" / "ch1.mp4",
                5: ROOT / ".cache" / "tests" / "night" / "long" / "ch5.mp4"}


def make_clip(video: Path, out: Path, seconds: float = 20.0, skip_s: float = 30.0) -> Path:
    """A real 20 s clip, written the way the engine writes them (clips.ClipWriter)."""
    cap = cv2.VideoCapture(str(video))
    vfps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(skip_s * vfps))
    t0 = time.time() - 3600
    frames, pos = [], 0
    for k in range(int(seconds * 8) + 1):
        while pos <= k * vfps / 8:
            ok, frame = cap.read()
            pos += 1
        frames.append((t0 + k / 8, clips.encode_frame(cv2.resize(frame, (1280, 720)), t0 + k / 8)))
    cap.release()
    w = clips.ClipWriter(out, 8, frames, t0 + 8.0)
    w.finish()
    w.join(30)
    assert w.ok, w.error
    return out


shutil.rmtree(CLIP_SHOTS, ignore_errors=True)
CLIP_SHOTS.mkdir(parents=True, exist_ok=True)
CLIPS = {ch: make_clip(v, EVENTS / "clips" / f"test_ch{ch}.mp4") for ch, v in NIGHT_VIDEOS.items()}


def clip_shot(win, name: str) -> Path:
    pump(0.8)
    path = CLIP_SHOTS / f"{name}.png"
    win.grab().save(str(path))
    print("saved", path)
    return path


def wait_for(cond, seconds: float = 5.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def clip_checks(win, page, store, ids: dict, mode: str) -> None:
    """The clip player in the detail pane: autoplay, play / pause, seek, speed, Picture | Clip,
    the "being saved" state, pausing while hidden, and screenshots."""
    d = page.detail
    multi = ids["multi_alerts"]
    page.set_filter("all")
    page.show_incident(ids["multi"], multi[0])
    page.list.setFocus()
    ok = wait_for(lambda: d.player.loaded and d.player.playing and d.image._img is not None)
    check(ok and d.clip_mode(), f"{mode}: a moment with a clip autoplays it")
    if mode == "light":
        check(d.media.isVisible() and d.play_btn.isVisible() and d.seek.isVisible() and d.view_toggle.isVisible()
              and d.view_toggle.current() == 1, "player controls and the Picture | Clip toggle show")
        check(d.player.count == 161 and abs(d.player.fps - 8) < 0.01, f"clip loaded ({d.player.count} frames)")
        check(d.clip_time.text().endswith("/ 0:20"), f"time label {d.clip_time.text()}")
        d.player.pause()
        d.player.seek(8.0)
        wait_for(lambda: d.player.index == 64)
        check(d.player.index == 64 and d.clip_time.text() == "0:08 / 0:20",
              f"starts at the alert's moment ({d.player.index}, {d.clip_time.text()})")
        QTest.keyClick(page.list, Qt.Key_Space)
        check(d.player.playing, "Space plays")
        QTest.keyClick(page.list, Qt.Key_Space)
        check(not d.player.playing, "Space pauses")
        d.play_btn.click()
        check(d.player.playing and d.play_btn.accessibleName() == "Pause clip", "play button plays")
        d.play_btn.click()
        check(not d.player.playing, "play button pauses")
        d.seek.setValue(40)
        check(wait_for(lambda: d.player.index == 40) and d.clip_time.text() == "0:05 / 0:20",
              f"scrubbing seeks ({d.player.index}, {d.clip_time.text()})")
        before = d.image._img
        d.seek.setValue(150)
        check(wait_for(lambda: d.image._img is not before and d.player.index == 150), "the view shows the new frame")
        # speed: frames advanced in one second at 1x and 2x
        rates = {}
        for i, speed in ((0, 1.0), (1, 2.0)):
            d.speed.group.button(i).click()
            check(d.player.speed == speed, f"speed {speed:g}x set")
            d.player.seek_frame(0)
            wait_for(lambda: d.player.index == 0)
            d.player.play()
            t0 = time.time()
            pump(1.0)
            rates[speed] = d.player.index / (time.time() - t0)
            d.player.pause()
        print(f"  playback: {rates[1.0]:.1f} fps at 1x, {rates[2.0]:.1f} fps at 2x")
        check(6 <= rates[1.0] <= 9 and rates[2.0] > 1.6 * rates[1.0], f"2x plays twice as fast {rates}")
        d.speed.group.button(0).click()
        # looping: the frame after the last one is the first
        d.player.seek_frame(160)
        wait_for(lambda: d.player.index == 160)
        d.player.play()
        check(wait_for(lambda: d.player.index < 20, 2.0), "playback loops")
        # another moment of the same clip: jumps to its offset, same file stays open
        path = d.player.path
        QTest.keyClick(page.list, Qt.Key_Right)
        check(d.index == 1 and d.player.path == path and wait_for(lambda: abs(d.player.index - 100) <= 3, 2.0),
              f"next moment of the same clip jumps to its time ({d.player.index})")
        # Picture | Clip
        d.view_toggle.group.button(0).click()
        check(not d.clip_mode() and d.player.path == "" and not d.seek.isVisible()
              and "available" in d.clip_note.text(), "Picture shows the picture and releases the clip")
        d.view_toggle.group.button(1).click()
        check(wait_for(lambda: d.clip_mode() and d.player.playing), "Clip goes back to the clip")
        # a moment without a clip: picture, no controls
        d.select(3)
        check(not d.clip_mode() and not d.media.isVisible() and d.player.path == "", "no clip: just the picture")
        d.select(0)
        wait_for(lambda: d.player.playing)
        # page hidden: paused and the file released; shown again: plays on
        win.go_page(win.dashboard)
        check(d.player.path == "" and not d.player.playing, "hidden page releases the clip")
        win.go_page(win.incidents)
        check(wait_for(lambda: d.player.playing and d.player.loaded), "shown again: the clip plays on")
        # the file is not locked by the player once released (cleanup may delete it)
        d.release()
        pump(0.2)
        probe = Path(CLIPS[1]).with_name("lock_probe.mp4")
        shutil.copy(CLIPS[1], probe)
        d.player.open(str(probe), 0, play=False)
        wait_for(lambda: d.player.loaded)
        d.player.close()
        pump(0.2)
        try:
            probe.unlink()
            released = True
        except OSError:
            released = False
        check(released, "closing the player releases the file")
        page.show_incident(ids["multi"], multi[0])
        wait_for(lambda: d.player.playing)
    # being saved: shows the picture and a note, switches to the clip once it is written
    page.show_incident(ids["pending"], ids["saving_alert"])
    pump(0.3)
    check(not d.clip_mode() and d.media.isVisible() and d.clip_note.text() == "Clip is being saved…",
          f"{mode}: recording clip shows 'being saved' ({d.clip_note.text()!r})")
    clip_shot(win, f"{mode}_clip_saving")
    if mode == "light":
        a = store.get(ids["saving_alert"])
        a.clip_path = str(Path(shutil.copy(CLIPS[1], Path(store.get(multi[0]).clip_path).with_name(f"{a.id}.mp4"))))
        store.update(a)   # what the engine does when the clip file is complete
        win.ctx.alert_bridge.alert_updated.emit(store.get(a.id))
        win.ctx.alert_bridge.incident_updated.emit(store.get_incident(a.incident_id))
        check(wait_for(lambda: d.clip_mode() and d.player.playing), "clip appears when it is saved")
        check(not d.clip_note.isVisible(), "the 'being saved' note is gone")
    # screenshots of the player at both sizes
    page.show_incident(ids["multi"], multi[2])
    wait_for(lambda: d.player.loaded and d.player.playing)
    pump(1.2)
    d.player.pause()
    d.scroll.verticalScrollBar().setValue(0)
    clip_shot(win, f"{mode}_player_1280")
    win.resize(1600, 1000)
    pump(0.3)
    d.player.play()
    pump(0.6)
    d.player.pause()
    clip_shot(win, f"{mode}_player_1600")
    win.resize(1280, 800)
    pump(0.2)
    # storage settings with the clip limits
    win.go_page(win.settings)
    sec = win.settings.clips
    wait_for(lambda: "Clips use" in sec.usage.text() or "No clips" in sec.usage.text())
    scroll = sec.parentWidget()
    while scroll is not None and not hasattr(scroll, "ensureWidgetVisible"):
        scroll = scroll.parentWidget()
    if scroll is not None:
        scroll.ensureWidgetVisible(sec, 0, 140)
    check("Clips use" in sec.usage.text() and "of 5 GB" in sec.usage.text(), f"{mode}: usage {sec.usage.text()!r}")
    clip_shot(win, f"{mode}_storage_settings")
    if mode == "light":
        sec.clean_btn.click()
        check(wait_for(lambda: sec.clean_btn.isEnabled() and "Nothing to delete" in sec.usage.text()),
              f"Clean up now reports back ({sec.usage.text()!r})")
        before = win.ctx.cfg.settings.clip_max_days
        sec.max_days.setValue(12)
        saved = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))["settings"]
        check(saved.get("clip_max_days") == 12, "clip limits autosave")
        sec.max_days.setValue(before)
    win.go_page(win.incidents)
    pump(0.2)


def check(cond, msg: str) -> None:
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def pump(seconds: float = 0.3) -> None:
    end = time.time() + seconds
    while time.time() < end:
        QApplication.processEvents()
        time.sleep(0.01)


def shot(win, name: str) -> Path:
    pump(0.6)  # thumbnails and the big picture load on the thread pool
    path = SHOTS / f"{name}.png"
    win.grab().save(str(path))
    print("saved", path.name)
    return path


def imread(path: Path):
    return cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)


PICS = {  # channel -> source frame
    1: SNAPS / "check_ch1_20261023_010006.jpg",
    5: SNAPS / "check_ch5_20261023_010046.jpg",
    2: SNAPS / "check_ch2_20261023_010028.jpg",
    7: SNAPS / "check_ch7_20261023_010056.jpg",
}
CAMS = {1: "Yard parking", 5: "Yard / street side", 2: "Stairwell", 7: "Hallway"}
TITLES = {"night_presence": "Person near parked cars at night", "loitering": "Person lingering near cars",
          "prowling": "Person going from car to car", "car_moved": "Parked car may have been hit"}
DETAILS = {"night_presence": "In Parking spots at 02:14.", "loitering": "Stayed 45 s in Parking spots.",
           "prowling": "Was close to 3 different parked cars within 3 min in Parking spots.",
           "car_moved": "Parked for 2 h 10 min, then moved at 02:19. A person was next to it just before."}


def make_alert(ago_s: float, ch: int, rule: str, severity: str, verdict: str, boxes: list, findings=(),
               status: str = "new", title: str = "") -> Alert:
    """A realistic alert: annotated copy of a real night frame plus the crop the engine would save."""
    src = PICS[ch] if PICS[ch].exists() else FRAMES / f"ch{ch}.png"
    img = imread(src)
    ts = time.time() - ago_s
    aid = f"{datetime.fromtimestamp(ts):%Y%m%d-%H%M%S}-{ch}-{uuid.uuid4().hex[:6]}"
    folder = EVENTS / "alerts" / f"{datetime.fromtimestamp(ts):%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    bd = [{"label": b[0], "x1": b[1], "y1": b[2], "x2": b[3], "y2": b[4], "role": "subject" if i == 0 else "context"}
          for i, b in enumerate(boxes)]
    annotated = draw_boxes(img.copy(), bd[::-1])
    x1, y1 = min(b[1] for b in boxes) - 80, min(b[2] for b in boxes) - 80
    x2, y2 = max(b[3] for b in boxes) + 80, max(b[4] for b in boxes) + 80
    h, w = img.shape[:2]
    crop = annotated[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
    snap, crop_path = folder / f"{aid}.jpg", folder / f"{aid}_crop.jpg"
    snap.write_bytes(cv2.imencode(".jpg", annotated)[1].tobytes())
    crop_path.write_bytes(cv2.imencode(".jpg", crop)[1].tobytes())
    return Alert(aid, "s1", ch, CAMS[ch], ts, rule, title or TITLES.get(rule, rule), DETAILS.get(rule, ""), severity,
                 str(snap), str(crop_path), bd, verdict, list(findings), status)


P1 = [("person", 1060, 520, 1180, 820), ("car", 615, 125, 1040, 355), ("car", 1320, 450, 1885, 745)]
P5 = [("person", 1320, 30, 1370, 110), ("car", 642, 36, 895, 148), ("car", 913, 88, 1258, 230)]
L1 = [("person", 300, 560, 380, 760), ("car", 85, 177, 532, 507)]
C5 = [("car", 913, 88, 1258, 230)]
S2 = [("person", 820, 300, 1000, 760)]
H7 = [("person", 300, 200, 520, 480)]


def build(store: AlertStore) -> dict:
    """Oldest first, as the monitor would raise them, so grouping happens for real."""
    ids = {}
    fa = make_alert(3 * 86400, 7, "night_presence", "info", "unverified", H7, status="seen",
                    title="Person seen at night")
    store.add(fa)
    store.mark_incident(fa.incident_id, "false_alarm")
    ids["false_alarm"] = fa.incident_id
    old = make_alert(27 * 3600, 1, "loitering", "info", "unverified", L1, status="seen")
    store.add(old)
    ids["single_old"] = old.incident_id
    st = [make_alert(26 * 3600, 2, "night_presence", "info", "unverified", S2, status="seen",
                     title="Person seen at night"),
          make_alert(26 * 3600 - 40, 2, "loitering", "info", "unverified", S2, status="seen", title="Person lingering")]
    for a in st:
        store.add(a)
    store.mark_incident(st[0].incident_id, "handled")
    ids["handled"] = st[0].incident_id
    dis = make_alert(3 * 3600, 5, "night_presence", "warning", "rejected", P5, ["no person near the car"],
                     status="seen")
    store.add(dis)
    ids["dismissed"] = dis.incident_id
    multi = [
        make_alert(50 * 60, 1, "night_presence", "warning", "unverified", P1),
        make_alert(49 * 60 + 15, 1, "loitering", "warning", "rejected", L1, ["car is parked normally"]),
        make_alert(47 * 60, 5, "prowling", "critical", "confirmed", P5,
                   ["hand on a car door handle", "person looking into a car window"]),
        make_alert(45 * 60 + 30, 1, "night_presence", "warning", "unverified", P1),
        make_alert(44 * 60, 5, "car_moved", "warning", "confirmed", C5, ["open car door"]),
        make_alert(43 * 60, 1, "loitering", "warning", "unverified", L1),
    ]
    # clips: the first two moments on camera 1 share one, the prowling moment on camera 5 has its own
    day = store.dir / "clips" / f"{datetime.fromtimestamp(multi[0].ts):%Y-%m-%d}"
    day.mkdir(parents=True, exist_ok=True)
    local = {ch: Path(shutil.copy(src, day / f"{multi[0 if ch == 1 else 2].id}.mp4")) for ch, src in CLIPS.items()}
    for a, (clip, offset) in zip(multi, [(local[1], 8.0), (local[1], 12.5), (local[5], 8.0)]):
        a.clip_path, a.clip_offset_s = str(clip), offset
    for a in multi:
        store.add(a)
    ids["multi"] = multi[0].incident_id
    ids["multi_alerts"] = [a.id for a in multi]
    pend = [make_alert(4 * 60, 1, "night_presence", "warning", "pending", P1),
            make_alert(3 * 60, 1, "loitering", "warning", "pending", L1)]
    pend[1].clip_offset_s = 8.0   # recording: the clip is not finished yet
    for a in pend:
        store.add(a)
    ids["pending"] = pend[0].incident_id
    ids["saving_alert"] = pend[1].id
    return ids


def item_kinds(page) -> list[str]:
    return [page.list.item(i).data(incidents_ui.KIND_ROLE) for i in range(page.list.count())]


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    ctx = AppContext(app)
    ctx.ui = QSettings(str(OUT / "ui.ini"), QSettings.IniFormat)

    shots: dict[str, Path] = {}
    for mode in ("light", "dark"):
        ctx.alert_store = store = AlertStore(str(EVENTS / mode))  # same fresh data for both themes
        ids = build(store)
        if mode == "light":
            check(store.get_incident(ids["multi"]).alert_count == 6, "six alerts on two cameras form one incident")
            check(store.get_incident(ids["multi"]).channels == [1, 5], "multi-camera incident has both cameras")
            check(store.get_incident(ids["pending"]).verdict == "pending", "pending incident")
            check(len(store.list_incidents()) == 6, f"six incidents ({len(store.list_incidents())})")
        ctx.cfg.settings.theme = mode
        ctx.apply_theme()
        win = MainWindow(ctx)
        calls: list = []
        win.set_monitoring = lambda on, calls=calls: calls.append(on)  # never start real monitoring here
        win.resize(1280, 800)
        win.show()
        pump(0.3)
        page = win.incidents
        if mode == "light":
            names = [win.nav.button(i).text().strip().replace("&&", "&") for i in range(7)]
            check(names == ["Dashboard", "Incidents", "Areas", "Integrations", "Live view", "Sites & cameras",
                            "Settings"], f"nav {names}")
            check([win.stack.widget(i) for i in range(7)] == [win.dashboard, win.incidents, win.areas_page,
                                                              win.integrations_page, win.live, win.sites,
                                                              win.settings], "stack order")
            check("Ctrl+2" in win.nav.button(1).toolTip(), "Incidents at Ctrl+2")
            check(win.stack.currentWidget() is win.dashboard, "starts on the dashboard")
            first = page.incidents.get(page._current_id())
            check(first is not None and first.id == ids["pending"], "newest incident selected in the background")
            check(all(a.status == "new" for a in store.incident_alerts(ids["pending"])),
                  "nothing marked seen while the page is hidden")
            check(win.incident_badge.text() == "2", f"badge counts open incidents with new alerts "
                                                    f"({win.incident_badge.text()})")
            check(win.dashboard.strip.isVisible() and sum(i.isVisible() for i in win.dashboard.strip.items) == 2,
                  "dashboard strip shows two open incidents")
        shots[f"{mode}_dashboard"] = shot(win, f"{mode}_01_dashboard_strip")

        # Dashboard strip -> incident
        win.dashboard.strip.items[1].clicked.emit(win.dashboard.strip.items[1].incident_id)
        pump(0.2)
        check(win.stack.currentWidget() is win.incidents, "strip click opens the Incidents page")
        if mode == "light":
            check(page._current_id() == ids["multi"], "strip click selects that incident")
            check(win.incident_badge.text() == "1", f"badge goes down to 1 ({win.incident_badge.text()})")
        win.go_page(win.incidents)
        page.set_filter("open")
        page.show_incident(ids["pending"])
        pump(0.2)
        if mode == "light":
            kinds = item_kinds(page)
            # the AI-dismissed incident waits under "Dismissed by AI", not under Open
            check(kinds.count("incident") == 3 and kinds[0] == "day", f"open filter: 3 cards with day headers {kinds}")
            check(all(a.status == "seen" for a in store.incident_alerts(ids["pending"])),
                  "viewing an incident marks its alerts seen")
            check(win.incident_badge.isHidden(), f"badge gone once both are seen ({win.incident_badge.text()})")
            check("open" in page.header.subtitle.text() and "7 days" in page.header.subtitle.text(),
                  f"subtitle: {page.header.subtitle.text()}")
        shots[f"{mode}_page"] = shot(win, f"{mode}_02_incidents")

        # The multi-camera incident: detail with filmstrip and timeline
        page.show_incident(ids["multi"], ids["multi_alerts"][2])
        pump(0.2)
        d = page.detail
        if mode == "light":
            check(d.index == 2 and d.current_alert().id == ids["multi_alerts"][2], "opens with the given moment")
            check(len(d.entries) == 6 and d.film.alerts and len(d.film.alerts) == 6, "timeline and filmstrip")
            QTest.keyClick(page.list, Qt.Key_Right)
            check(d.index == 3, f"Right steps the filmstrip ({d.index})")
            QTest.keyClick(page.list, Qt.Key_Left)
            QTest.keyClick(page.list, Qt.Key_Left)
            check(d.index == 1, f"Left steps back ({d.index})")
            r = d.film.rect_of(4)
            QTest.mouseClick(d.film, Qt.LeftButton, Qt.NoModifier, r.center())
            check(d.index == 4, "clicking a thumbnail selects it")
            QTest.keyClick(d.film, Qt.Key_Home)
            check(d.index == 0, "Home in the filmstrip")
            QTest.mouseClick(d.entries[2], Qt.LeftButton, Qt.NoModifier, QPoint(30, 10))
            check(d.index == 2 and d.entries[2].property("selected"), "clicking a timeline entry shows its picture")
            check("Visited 3 cars" in d.entries[2].title.text(), f"plain words: {d.entries[2].title.text()}")
            check("Stayed 45 s next to a car" in d.entries[1].title.text(), f"plain words: {d.entries[1].title.text()}")
            check("+1 camera" in d.title.text(), f"title: {d.title.text()}")
            # keyboard: Down never lands on a day header
            page.list.setFocus()
            page.list.setCurrentRow(page._first_row())
            seen = []
            for _ in range(6):
                QTest.keyClick(page.list, Qt.Key_Down)
                seen.append(page.list.currentItem().data(incidents_ui.KIND_ROLE))
            check(set(seen) == {"incident"}, f"arrow keys skip day headers {seen}")
            page.show_incident(ids["single_old"])
            QTest.keyClick(page.list, Qt.Key_H)
            check(store.get_incident(ids["single_old"]).status == "handled", "H marks the incident handled")
            check(page._current_id() != ids["single_old"], "handled incident leaves the Open list")
            page.show_incident(ids["single_old"])
            QTest.keyClick(page.list, Qt.Key_H)
            check(store.get_incident(ids["single_old"]).status == "open", "H again reopens it")
            page.set_filter("open")
            page.show_incident(ids["multi"], ids["multi_alerts"][2])
            pump(0.1)
        d.scroll.verticalScrollBar().setValue(0)
        shots[f"{mode}_detail"] = shot(win, f"{mode}_03_detail_top")
        d.scroll.verticalScrollBar().setValue(d.scroll.verticalScrollBar().maximum())
        shot(win, f"{mode}_04_detail_timeline")
        d.scroll.verticalScrollBar().setValue(0)

        # Larger window
        win.resize(1600, 1000)
        pump(0.3)
        shots[f"{mode}_large"] = shot(win, f"{mode}_05_incidents_1600")
        win.resize(1280, 800)
        pump(0.2)
        clip_checks(win, page, store, ids, mode)

        if mode == "light":
            # Live updates: a new alert joins the pending incident, updated in place
            page.set_filter("all")
            page.show_incident(ids["multi"])
            sb = page.list.verticalScrollBar()
            sb.setValue(sb.maximum())
            pos = sb.value()
            live = make_alert(5, 5, "night_presence", "warning", "pending", P5)
            store.add(live)
            inc = store.get_incident(live.incident_id)
            check(inc.id == ids["pending"] and inc.alert_count == 3 and inc.channels == [1, 5],
                  "a new alert joins the open incident across cameras")
            ctx.alert_bridge.incident_updated.emit(inc)
            pump(0.1)
            check(page._current_id() == ids["multi"], "update keeps the selection")
            check(sb.value() == pos, f"update keeps the scroll position ({sb.value()} vs {pos})")
            check(page.incidents[ids["pending"]].alert_count == 3, "updated incident refreshed in place")
            # The user handles it; the next alert starts a new incident at the top
            page.show_incident(ids["pending"])
            d.handled_btn.click()
            pump(0.1)
            check(store.get_incident(ids["pending"]).status == "handled", "Mark handled saved")
            check(all(a.status == "seen" for a in store.incident_alerts(ids["pending"])), "handled -> alerts seen")
            page.set_filter("open")
            selected = page._current_id()
            fresh = make_alert(1, 1, "prowling", "critical", "pending", P1)
            store.add(fresh)
            check(fresh.incident_id != ids["pending"], "a handled incident does not absorb the next alert")
            ctx.alert_bridge.alert_added.emit(fresh)
            ctx.alert_bridge.incident_added.emit(store.get_incident(fresh.incident_id))
            pump(0.1)
            check(page.list.item(page._first_row()).data(incidents_ui.ID_ROLE) == fresh.incident_id,
                  "new incident inserted at the top")
            check(page.glow(fresh.incident_id) > 0, "new incident is highlighted")
            check(page._current_id() == selected, "a new incident does not steal the selection")
            shot(win, "light_06_live_new_incident")
            check(len(win.toasts.toasts) == 1, "toast for the new alert")
            # Toast click opens the incident and selects that alert
            win.go_page(win.dashboard)
            win.toasts.toasts[0].clicked.emit(fresh.id)
            pump(0.2)
            check(win.stack.currentWidget() is win.incidents and page._current_id() == fresh.incident_id,
                  "toast click opens the incident")
            check(d.current_alert().id == fresh.id, "toast click selects that alert in the filmstrip")
            win._open_alert(ids["multi_alerts"][4])
            pump(0.1)
            check(page._current_id() == ids["multi"] and d.current_alert().id == ids["multi_alerts"][4],
                  "toast for an older alert selects it inside its incident")
            # False alarm and undo
            d.false_btn.click()
            check(store.get_incident(ids["multi"]).status == "false_alarm", "False alarm saved")
            check({a.status for a in store.incident_alerts(ids["multi"])} == {"false_alarm"}, "alerts false alarm")
            page.show_incident(ids["multi"])
            d.false_btn.click()
            check(store.get_incident(ids["multi"]).status == "open", "Not a false alarm moves it back to Open")
            # Delete (confirm dialog auto-answered)
            orig = QMessageBox.exec
            QMessageBox.exec = lambda box: next(b for b in box.buttons() if b.text() == "Delete").click()
            target = store.get_incident(ids["dismissed"])
            files = [Path(p) for a in store.incident_alerts(target.id) for p in (a.snapshot_path, a.crop_path)]
            page.show_incident(target.id)
            d.delete_btn.click()
            QMessageBox.exec = orig
            pump(1.5)
            check(store.get_incident(target.id) is None and target.id not in page.incidents, "delete removes it")
            check(not any(f.exists() for f in files), "delete removes the pictures")
            # it was the only incident under "Dismissed by AI": nothing is left to select there
            check(page._current_id() != target.id, "the deleted incident is no longer selected")

            # All alerts toggle
            page.toggle.group.button(1).click()
            pump(0.2)
            check(page.views.currentWidget() is page.alerts and page.alerts.isVisible(), "All alerts view")
            check(page.toggle.parent() is page.alerts.header, "toggle moves into the alerts header")
        else:
            page.toggle.group.button(1).click()
        shots[f"{mode}_alerts"] = shot(win, f"{mode}_07_all_alerts")
        page.toggle.group.button(0).click()
        pump(0.1)
        check(page.views.currentIndex() == 0 and page.toggle.parent() is page.header, f"{mode}: back to incidents")

        # Empty state
        real = ctx.alert_store
        ctx.alert_store = AlertStore(str(OUT / "empty" / mode))
        page.reload()
        pump(0.1)
        check(page.body.currentWidget() is page.empty and page.empty.action.text() == "Turn on monitoring",
              f"{mode}: empty state offers to turn monitoring on")
        page.empty.action.click()
        check(calls == [True], f"{mode}: empty-state button turns monitoring on")
        shots[f"{mode}_empty"] = shot(win, f"{mode}_08_empty")
        sites = ctx.cfg.sites
        ctx.cfg.sites = []
        page.reload()
        check(page.empty.action.text() == "Open Sites & cameras", f"{mode}: no site -> Sites & cameras")
        page.empty.action.click()
        check(win.stack.currentWidget() is win.sites, f"{mode}: goes to Sites & cameras")
        ctx.cfg.sites = sites
        win.dashboard.strip.refresh()
        check(win.dashboard.strip.isHidden(), f"{mode}: dashboard strip hidden with no incidents")
        ctx.alert_store = real
        win.go_page(win.dashboard)
        win.dashboard.strip.refresh()
        if mode == "light":
            check(win.dashboard.strip.isVisible(), "strip back with incidents")
        win.close()
        win.deleteLater()
        pump(0.3)

    print("\nRepresentative screenshots:")
    for key in ("light_page", "dark_page", "light_detail", "light_dashboard"):
        print(" ", shots[key])
    print(f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
