"""Offscreen render + behaviour checks for the Integrations page (integrations_ui, integrations_setup,
integrations_routes).

Run:  run.cmd tests\\test_integrations_ui.py
Uses the real IntegrationHub (yardguard.integrations.hub) on the shared FakeCtx (synthetic config, temp
events folder) with fake channel classes registered through base.register_test_channel, a fake
Telegram pairing listener, a fake Tailscale and a fake phone-access server (plus one check against the
real WebRemote bound to 127.0.0.1). Nothing reaches a real service. PNGs (light and dark, 1280x800 and
1600x1000) go to .cache\\tests\\integrations\\ui\\.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "tests" / "integrations" / "ui"
if OUT.exists():
    shutil.rmtree(OUT, ignore_errors=True)
OUT.mkdir(parents=True, exist_ok=True)
SHOTS = OUT / "shots"
SHOTS.mkdir(exist_ok=True)
os.environ["YARDGUARD_CONFIG"] = str(OUT / "config.json")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton, QApplication, QCheckBox, QHBoxLayout, QLabel, QMenu, QPushButton, QRadioButton, QScrollArea,
    QWidget,
)

from yardguard import config  # noqa: E402

assert config.CONFIG_PATH == OUT / "config.json", "tests must never write the real config"

TOKEN = "7000000001:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawZ"
failures: list[str] = []
KEEP: list = []


def check(cond, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


def pump(app, ms: int = 60) -> None:
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def wait_until(app, fn, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(0.01)
    return False


# --- context -------------------------------------------------------------------------------------

def ui_context(app):
    """The shared FakeCtx plus what pages need from AppContext: palette, icons, theme."""
    from fakes_integrations import FakeCtx
    from yardguard.config import new_area
    from yardguard.theme import PALETTES, svg_icon

    ctx = FakeCtx(str(OUT / "ctx"), cameras=((1, "Front gate"), (2, "Driveway"), (3, "Backyard")))
    ctx.app = app
    ctx.palette = PALETTES["light"]
    ctx._bindings = []
    ctx.ui = QSettings(str(OUT / "ui.ini"), QSettings.IniFormat)
    ctx.ui.clear()
    site = ctx.cfg.active_site()
    site.cameras[0].areas = [
        new_area("log_plates", "line", [(0.1, 0.6), (0.9, 0.6)], name="Gate barrier"),
        new_area("count", "line", [(0.2, 0.3), (0.8, 0.3)], name="Gate line"),
        new_area("loitering", "polygon", [(0.1, 0.1), (0.4, 0.1), (0.4, 0.4)], name="Front door"),
    ]
    site.cameras[1].areas = [new_area("parking_spot", "polygon", [(0.1, 0.1), (0.3, 0.1), (0.3, 0.3)],
                                      name="My parking spot with a very long name next to the hedge")]

    def icon(name, role="text", size=18):
        p = ctx.palette
        if role == "nav":
            return svg_icon(name, p["muted"], active=p["nav_active_text"], size=size)
        return svg_icon(name, p[role], size=size)

    def bind_icon(widget, name, role="text"):
        if isinstance(widget, QPushButton) and widget.text() and not widget.text().startswith(" "):
            widget.setText(" " + widget.text())
        widget.setIcon(icon(name, role))
        ctx._bindings.append((widget, name, role))

    def apply_theme(name):
        from yardguard.alerts_ui import EXTRA_QSS as A
        from yardguard.areas_ui import EXTRA_QSS as AR
        from yardguard.incidents_ui import EXTRA_QSS as I
        from yardguard.integrations_ui import EXTRA_QSS as IN
        from yardguard.settings_ui import EXTRA_QSS as S
        from yardguard.theme import build_qss, qt_palette, write_assets
        ctx.palette = PALETTES[name]
        app.setPalette(qt_palette(ctx.palette))
        assets = write_assets(ctx.palette, OUT / "assets")
        app.setStyleSheet(build_qss(ctx.palette, assets, A + I + AR + IN) + S)
        alive = []
        for w, n, r in ctx._bindings:
            try:
                w.setIcon(icon(n, r))
                alive.append((w, n, r))
            except RuntimeError:
                pass
        ctx._bindings[:] = alive

    ctx.icon, ctx.bind_icon, ctx.apply_theme = icon, bind_icon, apply_theme
    return ctx


class FakeWebSignals(QObject):
    devices_changed = Signal()
    status_changed = Signal()


class FakeWeb:
    """Phone-access stand-in with WebRemote's public surface (nothing listens)."""

    def __init__(self, ctx):
        from yardguard.webremote import normalize_remote
        self.ctx = ctx
        self._norm = normalize_remote
        self.signals = FakeWebSignals()
        self.changed = None
        self.running = False
        self.error = ""
        self.fail_port = None
        self._devices: list[dict] = []
        self.calls: list[str] = []
        self.pairings = 0

    @property
    def remote(self) -> dict:
        s = self.ctx.cfg.settings
        if not isinstance(getattr(s, "remote", None), dict):
            s.remote = {}
        return self._norm(s.remote)

    def apply_settings(self):
        self.calls.append("apply")
        if not self.remote["enabled"]:
            self.running, self.error = False, ""
        elif self.fail_port == self.remote["port"]:
            self.running, self.error = False, f"Port {self.fail_port} is used by another program. Choose another port."
        else:
            self.running, self.error = True, ""
        self.signals.status_changed.emit()

    def start(self):
        self.calls.append("start")
        self.apply_settings()

    def stop(self):
        self.calls.append("stop")
        self.running = False
        self.signals.status_changed.emit()

    def restart(self):
        self.stop()
        self.start()

    def new_pairing(self, ttl=300):
        self.pairings += 1
        return "K7P2-9QXM", "http://192.168.1.10:8765/#pair=K7P29QXM"

    def devices(self):
        return [dict(d) for d in self._devices]

    def connected_count(self):
        return sum(1 for d in self._devices if time.time() - d.get("last_seen", 0) < 120)

    def remove_device(self, device_id):
        self._devices = [d for d in self._devices if d["id"] != device_id]
        self.signals.devices_changed.emit()

    def pair(self, name):
        now = time.time()
        self._devices.append({"id": f"d-{len(self._devices) + 1:08x}", "name": name, "created": now,
                              "last_seen": now, "last_ip": "192.168.1.23"})
        self.signals.devices_changed.emit()


class FakeListener:
    """telegram.PairingListener stand-in: two chats answer shortly after start()."""

    instances: list = []

    def __init__(self, token, code, api_base="", duration=180):
        self.token, self.code, self.api_base = token, code, api_base
        self.error = ""
        self.started = 0.0
        self.stopped = False
        FakeListener.instances.append(self)

    def start(self):
        self.started = time.time()

    def stop(self):
        self.stopped = True

    def found(self):
        if not self.started or time.time() - self.started < 0.15:
            return []
        return [{"id": -1001234567890, "title": "Family", "type": "supergroup", "with_code": False, "user": "Sam"},
                {"id": 111222333, "title": "Alex", "type": "private", "with_code": True, "user": "Alex"}]


def make_channels(base, model):
    """Fake channel classes (registered for every kind) that record calls and never touch a network."""
    made = {}
    for kind in base.CATALOG:
        real = base.load_channel_class(kind)
        caps = dict(getattr(real, "CAPS", base.Channel.CAPS))
        keys = tuple(getattr(real, "SECRET_KEYS", base.SECRET_KEYS.get(kind, ())))

        class Fake(base.Channel):
            KIND = kind
            CAPS = caps
            SECRET_KEYS = keys
            checks: list = []
            sends: list = []
            check_result = None
            send_result = None

            @classmethod
            def check(cls, params, secrets):
                cls.checks.append((dict(params), dict(secrets)))
                if cls.check_result is not None:
                    return cls.check_result
                data = {"bot_username": "MyYardBot", "bot_name": "Our yard"} if cls.KIND == "telegram" else {}
                return model.CheckResult(True, "Found @MyYardBot" if cls.KIND == "telegram" else "Looks good", data=data)

            def send(self, event, ctx):
                type(self).sends.append((event.kind, event.title, ctx.pictures))
                time.sleep(0.01)
                return type(self).send_result or model.SendResult(True, ms=412)

            def describe(self):
                p = self.conf.params
                if self.KIND == "telegram":
                    chats = ", ".join(c.get("title", "") for c in p.get("chats", []) if c.get("enabled", True))
                    return f"@{p.get('bot_username') or 'bot'} to {chats or 'nobody yet'}"
                if self.KIND == "email":
                    return f"{p.get('sender', '')} to {', '.join(p.get('to') or [])}"
                if self.KIND == "ntfy":
                    return f"Topic {str(p.get('topic', ''))[:13]}... on ntfy.sh"
                return base.CATALOG[self.KIND]["title"]

        Fake.__name__ = f"Fake{kind.title()}Channel"
        Fake.checks, Fake.sends = [], []
        base.register_test_channel(kind, Fake)
        made[kind] = Fake
    return made


# --- screenshot host ---------------------------------------------------------------------------

def host_for(page):
    """Window like the app's: 232 px sidebar + the page."""
    host = QWidget()
    host.setObjectName("Root")
    row = QHBoxLayout(host)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)
    side = QWidget()
    side.setObjectName("Sidebar")
    side.setFixedWidth(232)
    row.addWidget(side)
    row.addWidget(page, 1)
    return host


def shot(widget, name: str, app) -> Path:
    pump(app, 80)
    path = SHOTS / f"{name}.png"
    widget.grab().save(str(path))
    return path


def clipped(root: QWidget) -> list[str]:
    """Visible one-line labels and buttons narrower than their text (elided labels are fine)."""
    from yardguard.integrations_setup import ElidedLabel
    bad = []
    for w in root.findChildren(QLabel):
        if not w.isVisibleTo(root) or w.wordWrap() or isinstance(w, ElidedLabel) or not w.text().strip():
            continue
        if w.textFormat() == Qt.RichText or (w.pixmap() is not None and not w.pixmap().isNull()):
            continue
        if w.objectName() in ("Step",) or w.width() <= 0:
            continue
        adv = w.fontMetrics().horizontalAdvance(w.text())
        if adv > w.width() + 1 or w.sizeHint().width() > w.width() + 2:
            bad.append(f"label {w.text()!r} needs {max(adv, w.sizeHint().width())} has {w.width()}")
    for w in root.findChildren(QAbstractButton):
        if not w.isVisibleTo(root) or not w.text().strip() or w.width() <= 0:
            continue
        if isinstance(w, (QPushButton, QCheckBox, QRadioButton)) and w.sizeHint().width() > w.width() + 2:
            if isinstance(w, QCheckBox) and w.width() >= w.fontMetrics().horizontalAdvance(w.text()) + 22:
                continue
            bad.append(f"button {w.text()!r} needs {w.sizeHint().width()} has {w.width()}")
    return bad


def no_hscroll(root: QWidget) -> bool:
    return all(not s.horizontalScrollBar().isVisible() for s in root.findChildren(QScrollArea))


def answer_menu(app, text: str) -> None:
    """Trigger the menu entry starting with `text` of the next popup menu."""
    def go(tries=[0]):
        menu = app.activePopupWidget()
        if not isinstance(menu, QMenu):
            tries[0] += 1
            if tries[0] < 100:
                QTimer.singleShot(20, go)
            return
        act = next((a for a in menu.actions() if a.text().startswith(text)), None)
        menu.close()
        if act is not None:
            act.trigger()
    QTimer.singleShot(30, go)


# --- main ----------------------------------------------------------------------------------------

def main() -> int:
    t0 = time.time()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))

    from yardguard.integrations import base, model, policy
    from yardguard.integrations.hub import IntegrationHub
    from yardguard.integrations import telegram as tg_mod
    from yardguard.webremote import tailscale as ts_mod
    from yardguard import integrations_routes as ir
    from yardguard import integrations_setup as isu
    from yardguard import integrations_ui as iu

    ctx = ui_context(app)
    ctx.apply_theme("light")
    chans = make_channels(base, model)
    tg_mod.PairingListener = FakeListener
    ts_mod.find_cli = lambda: "tailscale"
    ts_mod.status = lambda cli=None: {"installed": True, "running": True, "backend_state": "Running",
                                      "dns_name": "pc.tail1234.ts.net", "online": True, "error": ""}
    ts_mod.serve_on = lambda port, cli=None: (True, "https://pc.tail1234.ts.net")
    asked: list[str] = []
    answers = {"default": True}

    def fake_confirm(parent, title, text, yes, no="Cancel", danger=False):
        asked.append(text)
        return answers.get(yes, answers["default"])

    isu.confirm = iu.confirm = fake_confirm
    iu.ask_text = lambda parent, title, prompt, text="": "Family phones"

    hub = IntegrationHub(ctx)
    ctx.integrations = hub
    ctx.web = FakeWeb(ctx)
    opened: list[str] = []
    monitoring: list[bool] = []
    hub.set_handlers(set_monitoring=monitoring.append, open_incident=opened.append)
    hub.health_delay_ms = 10 ** 8
    hub.start()

    class Win:
        def __init__(self):
            self.messages, self.pages = [], []
            self.settings, self.areas_page = object(), object()

        def notify(self, text, error=False):
            self.messages.append(("ERR " if error else "") + text)

        def go_page(self, page):
            self.pages.append(page)

        def open_incident(self, incident_id):
            opened.append("win:" + incident_id)

    win = Win()

    # === 1. empty Connections ===================================================================
    for theme in ("light", "dark"):
        ctx.apply_theme(theme)
        page = iu.IntegrationsPage(ctx, win)
        host = host_for(page)
        for size in ((1280, 800), (1600, 1000)):
            host.resize(*size)
            host.show()
            pump(app, 120)
            shot(host, f"connections_empty_{theme}_{size[0]}", app)
            bad = clipped(page)
            check(not bad, f"[{theme} {size[0]}] empty Connections: no clipped text {bad[:3]}")
            check(no_hscroll(page) and page.minimumSizeHint().width() <= size[0] - 232,
                  f"[{theme} {size[0]}] empty Connections: no horizontal scroll")
        conn = page.connections
        check(conn.empty_banner.isVisible() and not conn.mon_banner.isVisible(), f"[{theme}] empty banner only")
        check(set(conn.catalog_cards) == set(base.CATALOG), f"[{theme}] one catalogue card per kind")
        check(not conn.catalog_cards["telegram"].chip.isVisibleTo(conn.catalog_cards["telegram"])
              and conn.catalog_cards["telegram"].btn.isVisibleTo(conn.catalog_cards["telegram"]),
              f"[{theme}] catalogue card: Set up button, no Set up chip")
        check(conn.phone.chip.text() == "Off" and conn.phone.setup_btn.isVisible(), f"[{theme}] phone access off")
        host.close()
        KEEP.append(host)   # pages live as long as the app does (deleting them mid-test only adds noise)
        pump(app, 30)

    # === 2. Telegram setup dialog, all steps, finish ==============================================
    ctx.apply_theme("light")
    for theme in ("light", "dark"):
        ctx.apply_theme(theme)
        dlg = isu.TelegramSetup(ctx, hub)
        dlg.show()
        pump(app, 80)
        s0 = dlg.steps[0]
        check(dlg.focusWidget() is s0.token, f"[{theme}] tg step 1: focus starts in the token field "
                                             f"({type(dlg.focusWidget()).__name__})")
        check(not dlg.next_btn.isEnabled(), f"[{theme}] tg step 1: Next needs a token")
        s0.token.setText("not a token")
        dlg.next()
        pump(app, 50)
        check(dlg.index == 0 and s0.errors["token"].isVisible() and "123456789" in s0.errors["token"].text(),
              f"[{theme}] tg step 1: bad token shown under the field")
        shot(dlg, f"setup_telegram_1_error_{theme}", app)
        bad = clipped(dlg)
        check(not bad, f"[{theme}] tg step 1: no clipped text {bad[:3]}")
        s0.token.setText("  bot" + TOKEN + " ")
        dlg.next()
        check(wait_until(app, lambda: dlg.index == 1), f"[{theme}] tg step 1: valid token -> step 2")
        check(chans["telegram"].checks and chans["telegram"].checks[-1][1].get("token") == TOKEN,
              f"[{theme}] tg check got the cleaned token")
        check(dlg.params.get("bot_username") == "MyYardBot", f"[{theme}] bot name from check data")
        s1 = dlg.steps[1]
        check(not dlg.next_btn.isEnabled(), f"[{theme}] tg step 2: Next needs a chat")
        s1.connect_btn.click()
        check(wait_until(app, lambda: len(s1.rows) == 2), f"[{theme}] tg step 2: both chats listed")
        ticked = {cid for cid, r in s1.rows.items() if r.box.isChecked()}
        check(ticked == {111222333}, f"[{theme}] chat that sent the code is ticked ({ticked})")
        check("not the code" in s1.rows[-1001234567890].detail.text() and s1.unsure.isVisible(),
              f"[{theme}] chat without the code: not ticked, explained")
        # a stranger's private chat that messages the bot (without the code) is never ticked by itself
        real_listener = s1.listener
        s1.listener = types.SimpleNamespace(error="", found=lambda: [
            {"id": 999000111, "title": "Stranger", "type": "private", "with_code": False, "user": "Stranger"}])
        s1._poll()
        check(999000111 in s1.rows and not s1.rows[999000111].box.isChecked(),
              f"[{theme}] stranger's private chat without the code is not ticked")
        s1.listener = real_listener
        check(s1.qr.isVisible() and "t.me/MyYardBot?start=" in s1.qr.text_value, f"[{theme}] deep-link QR shown")
        check("left" in s1.wait.text(), f"[{theme}] countdown text ({s1.wait.text()})")
        check(not any("1234567890" in r.box.text() for r in s1.rows.values()), f"[{theme}] chat ids not shown")
        shot(dlg, f"setup_telegram_2_{theme}", app)
        bad = clipped(dlg)
        check(not bad, f"[{theme}] tg step 2: no clipped text {bad[:3]}")
        dlg.next()
        check(wait_until(app, lambda: dlg.index == 2), f"[{theme}] tg step 2 -> step 3")
        check(FakeListener.instances[-1].stopped or wait_until(app, lambda: FakeListener.instances[-1].stopped),
              f"[{theme}] listener stopped when leaving step 2")
        s2 = dlg.steps[2]
        check(dlg.next_btn.text() == "Finish", f"[{theme}] last step says Finish")
        s2.send_btn.click()
        check(wait_until(app, lambda: s2.ask.isVisible()), f"[{theme}] test sent -> Did it arrive?")
        check(chans["telegram"].sends and chans["telegram"].sends[-1][0] == "test", f"[{theme}] test event sent")
        s2._help()
        pump(app, 30)
        shot(dlg, f"setup_telegram_3_{theme}", app)
        bad = clipped(dlg)
        check(not bad, f"[{theme}] tg step 3: no clipped text {bad[:3]}")
        if theme == "light":
            before = len(ctx.cfg.settings.integrations)
            done_ids: list[str] = []
            dlg.finished_dest.connect(done_ids.append)
            s2.yes_btn.click()
            pump(app, 80)
            items = ctx.cfg.settings.integrations
            check(len(items) == before + 1 and done_ids and items[-1]["id"] == done_ids[0],
                  "Finish -> hub.add_destination")
            d = items[-1]
            enc = d.get("secrets", {}).get("token", "")
            check(enc and enc != TOKEN and config.decrypt(enc) == TOKEN, "token stored encrypted")
            raw = (OUT / "config.json").read_text(encoding="utf-8")
            check(TOKEN not in raw and "AAHdqTcvCH1vGWJ" not in raw, "no plain token in config.json")
            check([c["id"] for c in d["params"]["chats"] if c["enabled"]] == [111222333], "ticked chat saved")
            check(d["params"]["allow_control"] is True, "allow control saved")
            check(not dlg.isVisible() and not dlg.typed, "dialog closed, typed secrets cleared")
        else:
            answers["Discard"] = True
            dlg.reject()
            pump(app, 40)
            check(not dlg.isVisible() and any("Discard this setup" in a for a in asked), "[dark] cancel asks to discard")
        dlg.deleteLater()

    tg_id = ctx.cfg.settings.integrations[-1]["id"]
    tg_conf = hub._conf(tg_id) if hasattr(hub, "_conf") else None

    # editing: saved token is never shown, Next checks with the saved one
    dlg = isu.TelegramSetup(ctx, hub, tg_conf)
    dlg.show()
    pump(app, 50)
    s0 = dlg.steps[0]
    check(s0.token.text() == "" and s0.token.placeholderText() == "Saved - type to replace",
          "edit: saved token not put in the field")
    check(dlg.next_btn.isEnabled(), "edit: Next enabled with the saved token")
    n = len(chans["telegram"].checks)
    dlg.next()
    check(wait_until(app, lambda: dlg.index == 1) and chans["telegram"].checks[n][1]["token"] == TOKEN,
          "edit: check uses the saved token")
    s1 = dlg.steps[1]
    check(1 in [len(s1.rows)] and next(iter(s1.rows.values())).detail.text() == "Already connected",
          "edit: existing chat listed")
    hub_ch = hub.channel(tg_id)
    paused = []
    if hub_ch is not None:
        hub_ch.pause_polling = lambda: paused.append("pause")
        hub_ch.resume_polling = lambda: paused.append("resume")
    s1.connect_btn.click()
    pump(app, 50)
    dlg.reject()
    check(wait_until(app, lambda: paused == ["pause", "resume"]), f"edit: poller paused then resumed ({paused})")
    dlg.deleteLater()

    # === 3. other setup dialogs (screenshots + key behaviour) =====================================
    for theme in ("light", "dark"):
        ctx.apply_theme(theme)
        # ntfy
        dlg = isu.NtfySetup(ctx, hub)
        dlg.show()
        pump(app, 80)
        st = dlg.steps[0]
        check(dlg.params["topic"].startswith("yardguard-") and len(dlg.params["topic"]) >= 30,
              f"[{theme}] ntfy random topic")
        check(st.qr.isVisible() and st.qr.text_value.endswith(dlg.params["topic"]), f"[{theme}] ntfy QR of topic")
        shot(dlg, f"setup_ntfy_1_{theme}", app)
        check(not clipped(dlg), f"[{theme}] ntfy: no clipped text {clipped(dlg)[:3]}")
        st.server.setText("ftp://nope")
        dlg.next()
        pump(app, 30)
        check(dlg.index == 0 and "https://" in st.status.text() + st.errors["server"].text(),
              f"[{theme}] ntfy: bad server refused")
        dlg.dirty = False
        dlg.reject()
        # email
        dlg = isu.EmailSetup(ctx, hub)
        dlg.show()
        pump(app, 60)
        shot(dlg, f"setup_email_1_{theme}", app)
        check(not clipped(dlg), f"[{theme}] email 1: no clipped text {clipped(dlg)[:3]}")
        dlg.steps[0].options["other"].radio.click()
        dlg.next()
        check(wait_until(app, lambda: dlg.index == 1), f"[{theme}] email: provider -> account")
        ea = dlg.steps[1]
        check(ea.host.isVisibleTo(dlg) and ea.port.isVisibleTo(dlg), f"[{theme}] email Other shows server fields")
        check(ea.address.placeholderText() == "you@example.com" and not ea.help_btn.isVisibleTo(dlg),
              f"[{theme}] email Other: neutral example, no app-password link")
        check(wait_until(app, lambda: dlg.focusWidget() is ea.address), f"[{theme}] email: focus in the address")
        ea.security.setCurrentIndex(ea.security.findData("ssl"))
        check(ea.port.value() == 465, f"[{theme}] email: SSL sets port 465")
        ea.security.setCurrentIndex(ea.security.findData("starttls"))
        check(ea.port.value() == 587, f"[{theme}] email: STARTTLS sets port 587")
        ea.port.setValue(2525)
        ea.security.setCurrentIndex(ea.security.findData("ssl"))
        check(ea.port.value() == 2525, f"[{theme}] email: a typed port is kept")
        ea.port.setValue(587)
        ea.security.setCurrentIndex(ea.security.findData("starttls"))
        ea.address.setText("alex@example.com")
        ea.password.setText("app-password-123")
        chans["email"].check_result = model.CheckResult(False, "Password not accepted. Gmail and Yahoo need an app "
                                                               "password.", field="password")
        ea.host.setText("smtp.example.com")
        dlg.next()
        check(wait_until(app, lambda: ea.errors["password"].isVisible()) and dlg.index == 1,
              f"[{theme}] email: wrong password shown under the field")
        shot(dlg, f"setup_email_2_error_{theme}", app)
        check(not clipped(dlg), f"[{theme}] email 2: no clipped text {clipped(dlg)[:3]}")
        # Gmail: the app password link sits by the password, and the error says how to get one
        dlg.back()
        pump(app, 30)
        dlg.steps[0].options["gmail"].radio.click()
        dlg.next()
        check(wait_until(app, lambda: dlg.index == 1), f"[{theme}] email: gmail -> account")
        check(ea.help_btn.isVisibleTo(dlg) and ea.address.placeholderText() == "you@gmail.com"
              and "2-Step Verification" in ea.app_note.text(), f"[{theme}] email Gmail: app password link + 2-Step")
        dlg.next()
        check(wait_until(app, lambda: ea.errors["password"].isVisible()) and "Gmail did not accept"
              in ea.errors["password"].text() and "2-Step" in ea.errors["password"].text(),
              f"[{theme}] email Gmail: wrong password names Gmail ({ea.errors['password'].text()!r})")
        shot(dlg, f"setup_email_gmail_error_{theme}", app)
        check(not clipped(dlg), f"[{theme}] email gmail error: no clipped text {clipped(dlg)[:3]}")
        chans["email"].check_result = None
        dlg.dirty = False
        dlg.reject()
        # discord, webhook
        for cls, name in ((isu.DiscordSetup, "discord"), (isu.WebhookSetup, "webhook")):
            dlg = cls(ctx, hub)
            dlg.show()
            pump(app, 60)
            shot(dlg, f"setup_{name}_1_{theme}", app)
            check(not clipped(dlg), f"[{theme}] {name}: no clipped text {clipped(dlg)[:3]}")
            dlg.steps[0].url.setText("https://example.com/hook" if name == "discord" else "notaurl")
            dlg.next()
            pump(app, 30)
            check(dlg.index == 0 and dlg.steps[0].errors["url"].isVisible(), f"[{theme}] {name}: bad URL refused")
            dlg.dirty = False
            dlg.reject()
        # Home Assistant without the add-on
        real_avail = isu.mqtt_available
        isu.mqtt_available = lambda: False
        dlg = isu.MqttSetup(ctx, hub)
        dlg.show()
        pump(app, 60)
        check(dlg.index == 0 and dlg.steps[0].install_btn.isVisible() and not dlg.next_btn.isEnabled(),
              f"[{theme}] HA: needs add-on step first")
        shot(dlg, f"setup_mqtt_addon_{theme}", app)
        check(not clipped(dlg), f"[{theme}] HA add-on: no clipped text {clipped(dlg)[:3]}")
        dlg.dirty = False
        dlg.reject()
        isu.mqtt_available = real_avail
        dlg = isu.MqttSetup(ctx, hub)
        dlg.show()
        pump(app, 40)
        check(dlg.steps[dlg.index].strip == "Broker", f"[{theme}] HA with add-on starts at the broker")
        dlg.steps[1].host.setText("homeassistant.local")
        dlg.next()
        check(wait_until(app, lambda: dlg.index == 2), f"[{theme}] HA broker -> discovery")
        shot(dlg, f"setup_mqtt_discovery_{theme}", app)
        check("yardguard/" in dlg.steps[2].topics.toPlainText(), f"[{theme}] HA topic list")
        dlg.dirty = False
        dlg.reject()

        # Phone access: steps 1-3
        ctx.cfg.settings.remote = {}
        dlg = isu.PhoneAccessDialog(ctx, hub)
        dlg.show()
        pump(app, 60)
        shot(dlg, f"phone_1_{theme}", app)
        check(not clipped(dlg), f"[{theme}] phone 1: no clipped text {clipped(dlg)[:3]}")
        check(dlg.steps[0].key() == "lan", f"[{theme}] phone: home Wi-Fi is the default")
        dlg.steps[0].options["away"].radio.click()
        dlg.next()
        check(wait_until(app, lambda: dlg.index == 1), f"[{theme}] phone: away -> Tailscale step")
        ts = dlg.steps[1]
        check(wait_until(app, lambda: ts.enable_btn.isEnabled()), f"[{theme}] tailscale ready -> Enable")
        ts.enable_btn.click()
        check(wait_until(app, lambda: dlg.public_url == "https://pc.tail1234.ts.net"), f"[{theme}] secure link on")
        shot(dlg, f"phone_2_{theme}", app)
        check(not clipped(dlg), f"[{theme}] phone 2: no clipped text {clipped(dlg)[:3]}")
        dlg.next()
        check(wait_until(app, lambda: dlg.index == 2), f"[{theme}] phone: -> add phone")
        r = isu.remote_settings(ctx)
        check(r["enabled"] and not r["lan"] and r["public_url"] == "https://pc.tail1234.ts.net",
              f"[{theme}] phone: remote saved (away, localhost bind)")
        pair = dlg.steps[2]
        check(pair.qr.isVisible() and pair.code.text() == "K7P2-9QXM", f"[{theme}] phone: QR + code shown")
        ctx.web.pair("Alex's iPhone")
        check(wait_until(app, lambda: "Paired: Alex's iPhone" in pair.wait.text()), f"[{theme}] paired shown")
        shot(dlg, f"phone_3_{theme}", app)
        check(not clipped(dlg), f"[{theme}] phone 3: no clipped text {clipped(dlg)[:3]}")
        dlg.next()
        pump(app, 30)
        check(not dlg.isVisible() and isu.remote_settings(ctx)["enabled"], f"[{theme}] phone: Done keeps it on")
        ctx.web._devices.clear()
        dlg.deleteLater()

    # cancelling phone access before pairing turns it back off
    ctx.cfg.settings.remote = {}
    dlg = isu.PhoneAccessDialog(ctx, hub, isu.PhoneAccessDialog.STEP_PAIR)
    dlg.show()
    pump(app, 40)
    check(ctx.web.running, "phone: pair step starts the server")
    dlg.reject()
    pump(app, 40)
    check(not isu.remote_settings(ctx)["enabled"] and not ctx.web.running, "phone: cancel before pairing -> off")
    # port in use
    ctx.web.fail_port = 8765
    dlg = isu.PhoneAccessDialog(ctx, hub, isu.PhoneAccessDialog.STEP_PAIR)
    dlg.show()
    pump(app, 40)
    pair = dlg.steps[2]
    check("Port 8765 is used" in pair.status.text() and not pair.qr.isVisible(), "phone: port problem shown")
    dlg.dirty = False
    dlg.reject()
    ctx.web.fail_port = None
    isu.remote_settings(ctx).update(enabled=True, lan=True, public_url="")
    ctx.web.apply_settings()
    ctx.web.pair("Alex's iPhone")
    ctx.web.pair("Sam's Android")

    # === 4. Connections with destinations ===========================================================
    em = base.new_integration("email")
    em["params"].update(preset="gmail", host="smtp.gmail.com", port=587, sender="alex@example.com",
                        username="alex@example.com", to=["alex@example.com", "sam@example.com"])
    base.IntegrationConf(em).set_secret("password", "app-password-123")
    hub.add_destination(em)
    pump(app, 50)
    em_ch = hub.channel(em["id"])
    em_ch.set_status("problem", "Password not accepted. Gmail and Yahoo need an app password.")
    ntfy = base.new_integration("ntfy")
    ntfy["enabled"] = False
    hub.add_destination(ntfy)
    # a second Telegram destination (Sam's chat): the two must be told apart everywhere
    tg2 = base.new_integration("telegram")
    tg2["params"].update(bot_username="MyYardBot", chats=[{"id": 222333444, "title": "Sam", "type": "private",
                                                           "enabled": True}])
    base.IntegrationConf(tg2).set_secret("token", TOKEN)
    hub.add_destination(tg2)
    pump(app, 50)
    for theme in ("light", "dark"):
        ctx.apply_theme(theme)
        page = iu.IntegrationsPage(ctx, win)
        host = host_for(page)
        for size in ((1280, 800), (1600, 1000)):
            host.resize(*size)
            host.show()
            page.select_tab(0)
            pump(app, 150)
            shot(host, f"connections_{theme}_{size[0]}", app)
            bad = clipped(page)
            check(not bad, f"[{theme} {size[0]}] Connections: no clipped text {bad[:3]}")
            check(no_hscroll(page), f"[{theme} {size[0]}] Connections: no horizontal scroll")
        conn = page.connections
        tg_card, em_card, nt_card = conn.cards[tg_id], conn.cards[em["id"]], conn.cards[ntfy["id"]]
        check(tg_card.chip.text() == "Connected" and "@MyYardBot to Alex" in tg_card.line1.full_text(),
              f"[{theme}] telegram card connected + describe()")
        check(em_card.chip.text() == "Problem" and em_card.fix_btn.isVisibleTo(em_card)
              and not em_card.test_btn.isVisibleTo(em_card) and "app password" in em_card.status.full_text(),
              f"[{theme}] email card: Problem + Fix + reason")
        check(nt_card.chip.text() == "Paused", f"[{theme}] disabled card says Paused")
        check(nt_card.line1.full_text().startswith("Topic "), f"[{theme}] paused card describes its topic "
                                                               f"({nt_card.line1.full_text()!r})")
        check(tg_card.title.full_text() == "Telegram · Alex"
              and conn.cards[tg2["id"]].title.full_text() == "Telegram · Sam",
              f"[{theme}] two Telegram cards told apart ({tg_card.title.full_text()!r})")
        check(conn.phone.chip.text() == "On" and "2 phones paired" in conn.phone.line1.full_text(),
              f"[{theme}] phone access on, 2 phones")
        check(not conn.empty_banner.isVisible() and conn.mon_banner.isVisible(), f"[{theme}] monitoring-off banner")
        check(tg_card.test_btn.accessibleName() == "Send a test message to Telegram · Alex",
              f"[{theme}] accessible name")
        if theme == "dark":
            host.close()
            KEEP.append(host)
            pump(app, 30)

    # behaviour on the light page (rebuilt)
    ctx.apply_theme("light")
    page = iu.IntegrationsPage(ctx, win)
    host = host_for(page)
    host.resize(1280, 800)
    host.show()
    page.select_tab(0)
    pump(app, 120)
    conn = page.connections
    # one column when narrow
    host.resize(1040, 800)
    pump(app, 120)
    check(conn._columns == 1, "narrow viewport: one column of cards")
    host.resize(1280, 800)
    pump(app, 120)
    check(conn._columns == 2, "wide viewport: two columns")
    # Send test
    tg_card = conn.cards[tg_id]
    tg_card.test_btn.click()
    check(wait_until(app, lambda: "Test sent" in conn.cards[tg_id].status.full_text()),
          f"Send test -> status line ({conn.cards[tg_id].status.full_text()!r})")
    check("412 ms" in conn.cards[tg_id].status.full_text(), "Send test shows the time")
    # Switch -> set_enabled
    calls = []
    orig_set = hub.set_enabled
    hub.set_enabled = lambda i, on: (calls.append((i, on)), orig_set(i, on))
    conn.cards[tg_id].switch.click()
    pump(app, 60)
    check(calls == [(tg_id, False)] and not base.IntegrationConf(ctx.cfg.settings.integrations[0]).enabled
          and conn.cards[tg_id].chip.text() == "Paused", "Switch off -> set_enabled + Paused")
    conn.cards[tg_id].setFocus()
    QTest.keyClick(conn.cards[tg_id], Qt.Key_Space)
    pump(app, 60)
    check(calls[-1] == (tg_id, True), "Space toggles the switch")
    hub.set_enabled = orig_set
    # Rename
    conn.cards[ntfy["id"]].rename()
    pump(app, 60)
    check(any(d.get("name") == "Family phones" for d in ctx.cfg.settings.integrations), "Rename saves the name")
    # Remove asks first
    asked.clear()
    answers["Remove"] = False
    conn.cards[ntfy["id"]].remove()
    check(asked and "stops sending" in asked[-1] and any(d["id"] == ntfy["id"] for d in ctx.cfg.settings.integrations),
          "Remove asks; No keeps it")
    answers["Remove"] = True
    conn.cards[ntfy["id"]].remove()
    pump(app, 60)
    check(all(d["id"] != ntfy["id"] for d in ctx.cfg.settings.integrations) and ntfy["id"] not in conn.cards,
          "Remove; Yes removes it")
    # Start monitoring through the hub
    conn._start_monitoring()
    check(wait_until(app, lambda: monitoring == [True]), "Start monitoring -> set_monitoring handler")
    # Phone access switch off asks + calls the server
    answers["Turn off"] = True
    conn.phone.switch.click()
    pump(app, 50)
    check(not isu.remote_settings(ctx)["enabled"] and not ctx.web.running and conn.phone.chip.text() == "Off",
          "phone access switch off")
    isu.remote_settings(ctx)["enabled"] = True
    ctx.web.apply_settings()
    conn.refresh_state()

    # Snooze menu + chip
    answer_menu(app, "For 1 hour")
    page._pause_menu()
    pump(app, 80)
    until = hub.snoozed_until()
    check(3500 < until - time.time() < 3700 and page.snooze_bar.isVisible()
          and "Messages paused until" in page.snooze_lbl.text(), "Pause for 1 hour -> chip")
    check("Critical" in page.snooze_bar.toolTip(), "chip tooltip says critical still comes")
    shot(host, "connections_snoozed_light_1280", app)
    page.resume_btn.click()
    pump(app, 50)
    check(hub.snoozed_until() == 0 and not page.snooze_bar.isVisible(), "Resume clears the snooze")
    answer_menu(app, "Until")
    page._pause_menu()
    pump(app, 80)
    check(abs(hub.snoozed_until() - iu.next_morning()) < 2, "Pause until 07:00")
    page.snooze(0)

    # Ctrl+Tab switches tabs
    host.activateWindow()
    page.tabs.setFocus()
    pump(app, 30)
    page.select_tab(0)
    QTest.keyClick(page.tabs, Qt.Key_Tab, Qt.ControlModifier)
    pump(app, 40)
    a = page.stack.currentIndex()
    QTest.keyClick(page.tabs, Qt.Key_Tab, Qt.ControlModifier | Qt.ShiftModifier)
    pump(app, 40)
    b = page.stack.currentIndex()
    check(a == 1 and b == 0, f"Ctrl+Tab / Ctrl+Shift+Tab switch tabs ({a}, {b})")
    check(int(ctx.ui.value("integrations/tab", -1)) == 0, "tab remembered in ctx.ui")

    # === 5. What to send ===========================================================================
    # back on: telegram enabled
    hub.set_enabled(tg_id, True)
    for theme in ("light", "dark"):
        ctx.apply_theme(theme)
        page.select_tab(1)
        page.routes.reload()
        for dest, name in ((tg_id, "telegram"), (em["id"], "email")):
            page.routes.select(dest)
            for size in ((1280, 800), (1600, 1000)):
                host.resize(*size)
                pump(app, 150)
                shot(host, f"routes_{name}_{theme}_{size[0]}", app)
                bad = clipped(page)
                check(not bad, f"[{theme} {size[0]}] What to send ({name}): no clipped text {bad[:3]}")
                check(no_hscroll(page), f"[{theme} {size[0]}] What to send ({name}): no horizontal scroll")
            inner = page.routes.scroll.widget()
            inner_h = inner.sizeHint().height()
            full = QWidget()
            full.resize(1, 1)
            path = SHOTS / f"routes_{name}_{theme}_full.png"
            inner.grab().save(str(path))
            if name == "telegram":
                check(page.routes.updates.group.button(0).text() == "Update the same message"
                      and page.routes.clip_row.isVisibleTo(page.routes), f"[{theme}] telegram: edit + clip options")
            else:
                check(page.routes.updates.group.button(0).text() == "Only if it gets more serious"
                      and not page.routes.clip_row.isVisibleTo(page.routes), f"[{theme}] email: no edit, no clip")
                check(page.routes.pictures.current() == 2 and page.routes.sum_switch.isChecked(),
                      f"[{theme}] email defaults: both pictures + summary")
            check(inner_h > 0, f"[{theme}] routes content renders")
    ctx.apply_theme("light")
    host.resize(1280, 800)
    ed = page.routes
    ed.select(tg_id)
    pump(app, 60)
    picker_names = ([b.text() for b in ed.picker.group.buttons()] if hasattr(ed.picker, "group")
                    else [ed.picker.itemText(i) for i in range(ed.picker.count())])
    check("Telegram · Alex" in picker_names and "Telegram · Sam" in picker_names,
          f"What to send: Telegram destinations told apart ({picker_names})")
    conf = base.IntegrationConf(next(d for d in ctx.cfg.settings.integrations if d["id"] == tg_id))
    check(ed.plates_card.isVisibleTo(ed), "plates card shown (a log_plates area exists)")
    check(any("Already sent as an incident" in w.text() for w in ed.findChildren(QLabel) if w.isVisibleTo(ed)),
          "areas with Alert me say 'Already sent as an incident'")
    # autosave debounce
    saves = []
    orig_update = hub.update_destination
    hub.update_destination = lambda i: (saves.append(i), orig_update(i))
    ed.level.group.button(0).click()
    ed.offline_min.setValue(9)
    ed.cooldown.setValue(120)
    pump(app, 250)
    check(saves == [] and ed.saved_lbl.text() == "Saving…", "edits wait for the debounce")
    check(wait_until(app, lambda: saves == [tg_id], 2.0), f"one update_destination after 600 ms ({saves})")
    check(ed.saved_lbl.text() == "All changes saved", "Saved text shown")
    check(conf.routes["preset"] == "custom" and ed.preset_opts["custom"].radio.isChecked(), "edit -> Custom preset")
    check(conf.routes["incidents"]["min_severity"] == "critical" and conf.routes["offline"]["minutes"] == 9
          and conf.routes["rate"]["cooldown_s"] == 120, "values written into routes")
    # presets
    ed.preset_opts["everything"].radio.click()
    pump(app, 60)
    check(conf.routes["preset"] == "everything" and conf.routes["incidents"]["min_severity"] == "info"
          and conf.routes["plates"] == "all" and ed.level.current() == 2 and ed.plates.current() == 3,
          "Everything preset applied and shown")
    ed.preset_opts["important"].radio.click()
    pump(app, 60)
    check(conf.routes["preset"] == "important" and conf.routes["incidents"]["min_severity"] == "warning",
          "Only important preset applied")
    check(ed.explain_lbl.isVisible() and "Right now" in ed.explain_lbl.text(), "policy.explain summary shown")
    # area routing
    area_id = next(a for a, w in ed.area_widgets.items() if len(w["chips"]) >= 2)
    w = ed.area_widgets[area_id]
    w["mode"].group.button(1).click()
    pump(app, 30)
    check(conf.routes["areas"][area_id]["mode"] == "instant" and conf.routes["areas"][area_id]["kinds"] == [],
          "area every time with default kinds -> []")
    chips = list(w["chips"].items())
    chips[0][1].click()
    pump(app, 30)
    check(conf.routes["areas"][area_id]["kinds"] == [chips[1][0]], "area kinds chip edit saved as a list")
    chips[1][1].click()
    pump(app, 30)
    check(conf.routes["areas"][area_id]["mode"] == "off" and w["mode"].current() == 0,
          "unticking every kind turns the area off (never an empty list meaning 'usual kinds')")
    # cameras
    ed.all_cams.click()
    pump(app, 30)
    check(ed.cam_flow.isVisible(), "unticking All cameras shows the camera list")
    ed.cam_boxes[2].click()
    ed.cam_boxes[3].click()
    pump(app, 30)
    check(conf.routes["incidents"]["cameras"] == [1], "only Front gate chosen")
    ed.cam_boxes[1].click()
    pump(app, 30)
    check(conf.routes["incidents"]["cameras"] == [1] and ed.cam_boxes[1].isChecked(), "last camera stays ticked")
    # quiet hours across midnight
    ed.quiet_switch.click()
    from PySide6.QtCore import QTime
    ed.q_start.setTime(QTime(23, 30))
    ed.q_end.setTime(QTime(6, 45))
    ed.day_btns[6].click()
    ed.q_mode.group.button(1).click()
    ed.flush()
    q = conf.routes["quiet"]
    check(q["enabled"] and q["start"] == "23:30" and q["end"] == "06:45" and q["mode"] == "hold"
          and 6 not in q["days"], f"quiet hours across midnight saved as strings ({q})")
    from datetime import datetime
    sat_2350 = datetime(2026, 10, 24, 23, 50).timestamp()   # a Saturday
    fri_2350 = datetime(2026, 10, 23, 23, 50).timestamp()
    check(policy.in_quiet_hours(q, fri_2350) and not policy.in_quiet_hours(dict(q, days=[0]), fri_2350),
          "policy reads the saved quiet hours")
    check(sat_2350 > 0, "date helper")
    # brain off disables "AI confirms"
    ctx.cfg.settings.brain_mode = "off"
    ed._load()
    check(not ed.confirmed.isEnabled() and ed.brain_link.isVisibleTo(ed) and not ed.unchecked.isEnabled(),
          "brain off disables the AI-confirmed choice and links to Settings")
    ed.brain_link.click()
    check(win.pages and win.pages[-1] is win.settings, "link opens Settings")
    ctx.cfg.settings.brain_mode = "verify"
    ed._load()
    check(ed.confirmed.isEnabled(), "brain on enables it again")
    # copy settings to another destination
    ed._copy_to(base.IntegrationConf(em))
    check(em["routes"]["quiet"]["start"] == "23:30", "Copy settings to... copies routes")
    # switching destination saves pending edits first
    ed.sum_switch.click()
    ed.select(em["id"])
    check(saves and saves[-1] == tg_id, "switching destination flushes the pending save")
    hub.update_destination = orig_update

    # === 6. Activity ================================================================================
    from yardguard.integrations.model import Decision, Delivery, Event, new_delivery_id
    now = time.time()
    rows = [("incident_new", "Person near the car at night", "sent", "", 2, 412, tg_id, "inc-0123456789ab"),
            ("incident_update", "Person near the car at night", "edited", "", 1, 230, tg_id, "inc-0123456789ab"),
            ("incident_new", "Someone at the front door", "failed",
             "The bot was blocked or removed from chat 1001234567890. Open Telegram and press Start again.",
             6, 0, tg_id, "inc-00000000000a"),
            ("incident_new", "Car moved in the driveway", "retrying", "No internet connection", 2, 0, em["id"], ""),
            ("incident_new", "Person in the backyard", "skipped", "Below your minimum level (warning)", 0, 0,
             em["id"], ""),
            ("incident_new", "Person near the gate", "waiting_ai", "Waiting for the AI's answer", 0, 0, tg_id, ""),
            ("incident_new", "Lingering at Front door", "held", "Quiet hours", 0, 0, em["id"], ""),
            ("summary", "Daily summary", "expired", "", 6, 0, em["id"], ""),
            ("test", "Test message", "sent", "", 1, 388, tg_id, "")]
    for i, (kind, title, status, reason, attempts, ms, dest, inc) in enumerate(rows):
        ev = Event(kind, now - i * 600, title, incident_id=inc)
        d = Delivery(new_delivery_id(), dest, ev, Decision("send", reason), now - i * 600)
        dname = "Telegram" if dest == tg_id else "Email"
        hub.log.add(d, status=status, reason=reason, dest_kind="telegram" if dest == tg_id else "email",
                    dest_name=dname)
        hub.log.update(d.id, attempts=attempts, ms=ms)
    for theme in ("light", "dark"):
        ctx.apply_theme(theme)
        page.select_tab(2)
        act = page.activity
        act.refresh()
        for size in ((1280, 800), (1600, 1000)):
            host.resize(*size)
            pump(app, 150)
            act.refresh()
            failed = next(i for i in range(act.list.count()) if act.list.item(i).data(iu.ROW_ROLE)["status"] == "failed")
            act.list.setCurrentRow(failed)
            pump(app, 80)
            shot(host, f"activity_{theme}_{size[0]}", app)
            bad = clipped(page)
            check(not bad, f"[{theme} {size[0]}] Activity: no clipped text {bad[:3]}")
            check(no_hscroll(page), f"[{theme} {size[0]}] Activity: no horizontal scroll")
        check(act.list.count() >= len(rows), f"[{theme}] activity rows listed ({act.list.count()})")
        dest_items = [act.dest.itemText(i) for i in range(act.dest.count())]
        check("Telegram · Alex" in dest_items and "Telegram · Sam" in dest_items,
              f"[{theme}] activity filter tells the Telegram chats apart ({dest_items})")
        check(act.dest_w > 70 and act.dest_text({"dest_id": tg_id, "dest_name": "Telegram"}) == "Telegram · Alex",
              f"[{theme}] activity destination column sized from the labels ({act.dest_w})")
        check(act.retry_btn.isVisible() and act.open_btn.isVisible(), f"[{theme}] failed row: Retry + Open incident")
        why = act.d_vals["why"].text()
        check("blocked" in why and "1001234567890" not in why and "…890" in why, f"[{theme}] chat id masked ({why})")
    act = page.activity
    tone, text = iu.status_line({"status": "retrying", "attempts": 2})
    check(text == "Retrying (2 of 6)" and tone == "nosignal", "status words: retrying")
    check(iu.status_line({"status": "sent", "ms": 412})[1] == "Sent", "status words: sent (list, no timing)")
    check(iu.status_line({"status": "sent", "ms": 412}, timing=True)[1] == "Sent · took less than a second"
          and iu.status_line({"status": "sent", "ms": 1400}, timing=True)[1] == "Sent · took 1 s",
          "status words: sent (detail, plain timing)")
    check(iu.status_line({"status": "expired"})[1] == "Expired (older than 30 min)", "status words: expired")
    check(iu.status_line({"status": "skipped", "reason": "Quiet hours"})[1] == "Skipped: Quiet hours",
          "status words: skipped")
    check("AAH" not in isu.safe_text(f"error at https://api.telegram.org/bot{TOKEN}/getMe"), "safe_text hides tokens")
    # Problems filter
    act.filter.group.button(1).click()
    pump(app, 60)
    statuses = {act.list.item(i).data(iu.ROW_ROLE)["status"] for i in range(act.list.count())}
    check(statuses and "sent" not in statuses and "skipped" not in statuses, f"Problems filter ({statuses})")
    act.filter.group.button(0).click()
    pump(app, 40)
    # open incident through the hub handler
    failed = next(i for i in range(act.list.count()) if act.list.item(i).data(iu.ROW_ROLE)["status"] == "failed")
    act.list.setCurrentRow(failed)
    act.open_btn.click()
    check(opened and opened[-1] == "inc-00000000000a", f"Open incident -> handler ({opened[-1:]})")
    # retry a failed row: the hub re-queues it and the fake channel sends it
    rid = act.list.currentItem().data(iu.ID_ROLE)
    act.retry_btn.click()
    check(wait_until(app, lambda: (hub.log.get(rid) or {}).get("status") in ("sent", "edited"), 5.0),
          f"Retry re-sends ({(hub.log.get(rid) or {}).get('status')})")
    check(any(m.startswith("Sending it again") for m in win.messages), "Retry tells the user")
    # narrow: detail panel below
    host.resize(1040, 800)
    pump(app, 120)
    check(act._detail_below, "narrow: detail below the list")
    host.resize(1280, 800)
    pump(app, 120)
    # clear log asks
    answers["Clear log"] = False
    act.clear()
    check(hub.log.list(limit=5), "Clear log No keeps rows")
    answers["Clear log"] = True
    act.clear()
    pump(app, 60)
    check(not hub.log.list(limit=5) and act.body.currentIndex() == 0, "Clear log Yes -> empty state")
    shot(host, "activity_empty_light_1280", app)

    # === 7. real WebRemote (127.0.0.1 only) with the Phone access dialog =========================
    import socket
    from yardguard.webremote import WebRemote
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    ctx._integrations_web_signals = None
    ctx.cfg.settings.remote = {}
    real = WebRemote(ctx, hub)
    ctx.web = real
    dlg = isu.PhoneAccessDialog(ctx, hub)
    dlg.show()
    pump(app, 40)
    dlg.steps[0].options["local"].radio.click()
    dlg.steps[0].port.setValue(free)
    dlg.next()
    check(wait_until(app, lambda: dlg.index == 2), "real web: local -> pair step")
    check(real.running and real._bound == ("127.0.0.1", free), f"real web: bound to 127.0.0.1:{free}")
    pair = dlg.steps[2]
    check(pair.url.text() == f"http://127.0.0.1:{free}" and pair.url.copy_value.startswith(
        f"http://127.0.0.1:{free}/#pair=") and pair._link == pair.url.copy_value,
          "real web: short address shown, full pair link copied/opened")
    check(not pair.qr.isVisible() and pair.open_btn.isVisible() and "this PC" in dlg.subtitle.text(),
          "real web: local mode -> no QR, Open in browser, PC wording")
    dlg.reject()
    pump(app, 100)
    check(not real.running and not isu.remote_settings(ctx)["enabled"], "real web: cancel stops it again")
    shot(dlg, "phone_real_web", app)

    # === shutdown ===================================================================================
    page.close()
    host.close()
    hub.shutdown()
    real.stop()
    for kind in base.CATALOG:
        base.register_test_channel(kind, None)
    pump(app, 200)
    left = [t.name for t in threading.enumerate() if t.name.startswith("yg-")]
    check(not left, f"no yg- threads left ({left})")
    ctx.close()
    elapsed = time.time() - t0
    check(elapsed < 60, f"finished in {elapsed:.1f} s")
    print(f"\n{len(failures)} failure(s)")
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
