"""Colors, style sheet and icons for the YardGuard app (light and dark)."""
from __future__ import annotations

from pathlib import Path
from string import Template

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

PALETTES = {
    "light": dict(
        bg="#f5f6f8", surface="#ffffff", surface2="#f0f2f5", border="#e2e5ea", border_strong="#cfd4dc",
        text="#111827", muted="#5f6877", primary="#2563eb", primary_hover="#1d4ed8", on_primary="#ffffff",
        danger="#c81e1e", danger_hover="#fdecec", success="#15803d", warning="#b45309",
        nav_active_bg="#e8efff", nav_active_text="#1d4ed8", sidebar="#ffffff", video_bg="#0b0d10",
    ),
    "dark": dict(
        bg="#0f1115", surface="#171a21", surface2="#20242d", border="#2b303b", border_strong="#3a404d",
        text="#e6e8ec", muted="#9aa3b2", primary="#3b82f6", primary_hover="#60a5fa", on_primary="#ffffff",
        danger="#f87171", danger_hover="#2a1517", success="#4ade80", warning="#fbbf24",
        nav_active_bg="#1d2940", nav_active_text="#a5c3ff", sidebar="#13161c", video_bg="#000000",
    ),
}

QSS = Template("""
* { font-family: "Segoe UI Variable Text", "Segoe UI"; font-size: 10pt; color: $text; outline: none; }
QMainWindow, QDialog, #Root { background: $bg; }
#Transparent, #qt_scrollarea_viewport { background: transparent; }
QScrollArea { background: transparent; border: none; }

#Sidebar { background: $sidebar; border-right: 1px solid $border; }
#AppTitle { font-size: 14pt; font-weight: 600; }
#SidebarCaption { color: $muted; font-size: 8pt; font-weight: 600; letter-spacing: 1px; }
#PageTitle { font-size: 18pt; font-weight: 600; }
#SectionTitle { font-size: 11.5pt; font-weight: 600; }
#CardTitle { font-size: 10.5pt; font-weight: 600; }
QLabel[muted="true"] { color: $muted; }
#Chip { background: $surface2; color: $muted; border-radius: 9px; padding: 2px 9px; font-size: 8.5pt; }

QPushButton {
    background: $surface; border: 1px solid $border_strong; border-radius: 6px;
    padding: 6px 14px; min-height: 22px;
}
QPushButton:hover { background: $surface2; }
QPushButton:pressed { background: $border; }
QPushButton:focus { border: 1px solid $primary; }
QPushButton:disabled { color: $muted; background: $surface2; border-color: $border; }
QPushButton[kind="primary"] { background: $primary; color: $on_primary; border: 1px solid $primary; font-weight: 600; }
QPushButton[kind="primary"]:hover { background: $primary_hover; border-color: $primary_hover; }
QPushButton[kind="primary"]:focus { border: 2px solid $nav_active_text; }
QPushButton[kind="primary"]:disabled { background: $border; border-color: $border; color: $muted; }
QPushButton[kind="danger"] { color: $danger; border-color: $danger; background: transparent; }
QPushButton[kind="danger"]:hover { background: $danger_hover; }
QPushButton[kind="ghost"] { background: transparent; border: 1px solid transparent; padding: 6px 8px; }
QPushButton[kind="ghost"]:hover { background: $surface2; }

QPushButton#NavButton {
    text-align: left; padding: 9px 12px; border: none; border-radius: 6px;
    background: transparent; color: $muted; font-weight: 500;
}
QPushButton#NavButton:hover { background: $surface2; color: $text; }
QPushButton#NavButton:checked { background: $nav_active_bg; color: $nav_active_text; font-weight: 600; }
#NavBadge {
    background: $danger; color: $surface; border-radius: 9px; padding: 0 6px;
    font-size: 8pt; font-weight: 700; min-width: 6px; min-height: 18px; max-height: 18px;
}

#Card { background: $surface; border: 1px solid $border; border-radius: 10px; }
#Card[selected="true"] { border: 2px solid $primary; }
#CameraCard { background: $surface; border: 1px solid $border; border-radius: 12px; }
#CameraCard:hover { border: 1px solid $border_strong; }
#CardThumb { background: $video_bg; border: none; border-top-left-radius: 11px; border-top-right-radius: 11px;
             border-bottom-left-radius: 0; border-bottom-right-radius: 0; color: #9aa3b2; }
#CardDetail { font-size: 9.5pt; }
#Divider { background: $border; border: none; min-height: 1px; max-height: 1px; }
#ModelRow { background: $bg; border: 1px solid $border; border-radius: 8px; }
#Chip[accent="true"] { background: $nav_active_bg; color: $nav_active_text; font-weight: 600; }
QProgressBar { background: $border; border: none; border-radius: 3px; }
QProgressBar::chunk { background: $primary; border-radius: 3px; }

QLineEdit, QSpinBox, QComboBox {
    background: $surface; border: 1px solid $border_strong; border-radius: 6px;
    padding: 5px 8px; min-height: 22px; selection-background-color: $primary; selection-color: $on_primary;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border: 1px solid $primary; }
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled { color: $muted; background: $surface2; }
QLineEdit[readOnly="true"] { background: $surface2; }
QComboBox { padding-right: 28px; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right; border: none; width: 26px; }
QComboBox::down-arrow { image: url($chev_down); width: 12px; height: 12px; }
QSpinBox { padding-right: 24px; }
QSpinBox::up-button, QSpinBox::down-button { subcontrol-origin: border; width: 22px; border: none; background: transparent; }
QSpinBox::up-button { subcontrol-position: top right; margin: 2px 2px 0 0; }
QSpinBox::down-button { subcontrol-position: bottom right; margin: 0 2px 2px 0; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: $surface2; border-radius: 4px; }
QSpinBox::up-arrow { image: url($chev_up); width: 10px; height: 10px; }
QSpinBox::down-arrow { image: url($chev_down); width: 10px; height: 10px; }
QCheckBox::indicator, QTableWidget::indicator {
    width: 16px; height: 16px; border: 1px solid $muted; border-radius: 4px; background: $surface;
}
QCheckBox::indicator:hover, QTableWidget::indicator:hover { border-color: $primary; }
QCheckBox::indicator:checked, QTableWidget::indicator:checked { background: $primary; border-color: $primary; image: url($check); }
QCheckBox::indicator:disabled { background: $surface2; border-color: $border_strong; }
QCheckBox::indicator:checked:disabled { background: $border_strong; border-color: $border_strong; }
QComboBox QAbstractItemView {
    background: $surface; border: 1px solid $border; padding: 4px;
    selection-background-color: $nav_active_bg; selection-color: $text;
}

QTableWidget, QListWidget {
    background: $surface; border: 1px solid $border; border-radius: 8px;
    gridline-color: $border; selection-background-color: $nav_active_bg; selection-color: $text;
}
QTableWidget::item { padding: 4px 6px; border-bottom: 1px solid $border; }
QHeaderView::section {
    background: $surface2; border: none; border-bottom: 1px solid $border;
    padding: 7px 8px; font-weight: 600; color: $muted;
}
QListWidget { padding: 4px; }
QListWidget::item { padding: 9px 10px; border-radius: 6px; margin: 1px 0; }
QListWidget::item:hover { background: $surface2; }
QListWidget::item:selected { background: $nav_active_bg; color: $nav_active_text; }

QCheckBox { spacing: 8px; }
QSlider::groove:horizontal { height: 4px; background: $border; border-radius: 2px; }
QSlider::sub-page:horizontal { background: $primary; border-radius: 2px; }
QSlider::handle:horizontal { background: $primary; width: 16px; height: 16px; margin: -6px 0; border-radius: 8px; }

#Thumb, #Video { background: $video_bg; border-radius: 8px; color: #9aa3b2; }
QLabel[status="online"] { color: $success; }
QLabel[status="offline"] { color: $danger; }
QLabel[status="nosignal"] { color: $warning; }
QLabel[status="checking"] { color: $muted; }

#Step { color: $muted; padding: 4px 12px; border-radius: 12px; font-weight: 500; }
#Step[state="current"] { background: $nav_active_bg; color: $nav_active_text; font-weight: 600; }
#Step[state="done"] { color: $success; }
#WizardFooter { background: $surface; border-top: 1px solid $border; }

QStatusBar { background: $sidebar; border-top: 1px solid $border; color: $muted; }
QStatusBar QLabel { color: $muted; }
QToolTip { background: $surface; color: $text; border: 1px solid $border; padding: 5px 8px; }
QMessageBox QLabel { font-size: 10pt; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $border_strong; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
""")

# Icon shapes (24x24, stroke style, adapted from the Lucide icon set, ISC licence)
ICONS = {
    "grid": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/>'
            '<rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/>',
    "video": '<path d="m16 13 5.223 3.482a.5.5 0 0 0 .777-.416V7.87a.5.5 0 0 0-.752-.432L16 10.5"/>'
             '<rect x="2" y="6" width="14" height="12" rx="2"/>',
    "server": '<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/>'
              '<line x1="6" x2="6.01" y1="6" y2="6"/><line x1="6" x2="6.01" y1="18" y2="18"/>',
    "sliders": '<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/>'
               '<line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/>'
               '<line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/>'
               '<line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/>'
               '<line x1="16" x2="16" y1="18" y2="22"/>',
    "refresh": '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>'
               '<path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
    "camera": '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/>'
              '<circle cx="12" cy="13" r="3"/>',
    "maximize": '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/>'
                '<path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>',
    "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "plus": '<path d="M5 12h14"/><path d="M12 5v14"/>',
    "trash": '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/>'
             '<path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>',
    "folder": '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4'
              'a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    "check": '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    "eye": '<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
    "scan": '<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/>'
            '<path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M7 12h10"/>',
    "shield": '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1'
              'c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
    "power": '<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.77.04"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    "pause": '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
    "play": '<path d="M7 4.5v15a1 1 0 0 0 1.5.86l12-7.5a1 1 0 0 0 0-1.72l-12-7.5A1 1 0 0 0 7 4.5Z"/>',
    "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "moon": '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "route": '<circle cx="6" cy="19" r="3"/><path d="M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15"/>'
             '<circle cx="18" cy="5" r="3"/>',
    "car": '<path d="M19 17h2c.6 0 1-.4 1-1v-3c0-.9-.7-1.7-1.5-1.9C18.7 10.6 16 10 16 10s-1.3-1.4-2.2-2.3'
           'c-.5-.4-1.1-.7-1.8-.7H5c-.6 0-1.1.4-1.4.9l-1.4 2.9A3.7 3.7 0 0 0 2 12v4c0 .6.4 1 1 1h2"/>'
           '<circle cx="7" cy="17" r="2"/><path d="M9 17h6"/><circle cx="17" cy="17" r="2"/>',
    "chev_right": '<path d="m9 18 6-6-6-6"/>',
}

LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#2563eb"/>'
    '<g transform="translate(12 11) scale(1.6667)" fill="none" stroke="#ffffff" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">' + ICONS["shield"] +
    '<circle cx="12" cy="11.5" r="3"/></g></svg>'
)


def resolve_theme(setting: str) -> str:
    if setting in ("light", "dark"):
        return setting
    scheme = QGuiApplication.styleHints().colorScheme()
    return "dark" if scheme == Qt.ColorScheme.Dark else "light"


def write_assets(p: dict, folder: Path) -> dict:
    """Small SVG files the style sheet points to (arrows, check mark) in the theme colors."""
    folder.mkdir(parents=True, exist_ok=True)
    shapes = {
        "chev_down": ('<path d="m6 9 6 6 6-6"/>', p["muted"]),
        "chev_up": ('<path d="m18 15-6-6-6 6"/>', p["muted"]),
        "check": ('<path d="M20 6 9 17l-5-5"/>', p["on_primary"]),
    }
    urls = {}
    for key, (shape, color) in shapes.items():
        path = folder / f"{key}_{color.strip('#')}.svg"
        path.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
                        f'stroke="{color}" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">'
                        f'{shape}</svg>', encoding="utf-8")
        urls[key] = path.as_posix()
    return urls


def build_qss(p: dict, assets: dict, extra: str = "") -> str:
    """extra: more rules in the same $token syntax (pages that ship their own styles)."""
    return Template(QSS.template + extra).substitute({**p, **assets})


def qt_palette(p: dict) -> QPalette:
    pal = QPalette()
    roles = {
        QPalette.Window: p["bg"], QPalette.WindowText: p["text"], QPalette.Base: p["surface"],
        QPalette.AlternateBase: p["surface2"], QPalette.Text: p["text"], QPalette.Button: p["surface"],
        QPalette.ButtonText: p["text"], QPalette.Highlight: p["primary"], QPalette.HighlightedText: p["on_primary"],
        QPalette.ToolTipBase: p["surface"], QPalette.ToolTipText: p["text"], QPalette.PlaceholderText: p["muted"],
        QPalette.Mid: p["border"], QPalette.Dark: p["border_strong"], QPalette.Light: p["surface"],
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(p["muted"]))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(p["muted"]))
    return pal


def _render(svg: str, size: int, scale: float) -> QPixmap:
    pm = QPixmap(int(size * scale), int(size * scale))
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    QSvgRenderer(QByteArray(svg.encode())).render(painter)
    painter.end()
    pm.setDevicePixelRatio(scale)
    return pm


def _stroke_svg(name: str, color: str) -> str:
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            f'stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            f'{ICONS[name]}</svg>')


def svg_icon(name: str, color: str, active: str | None = None, size: int = 18) -> QIcon:
    icon = QIcon()
    for scale in (1.0, 1.5, 2.0):
        icon.addPixmap(_render(_stroke_svg(name, color), size, scale), QIcon.Normal, QIcon.Off)
        if active:
            icon.addPixmap(_render(_stroke_svg(name, active), size, scale), QIcon.Normal, QIcon.On)
    return icon


def logo_pixmap(size: int, scale: float = 2.0) -> QPixmap:
    return _render(LOGO_SVG, size, scale)


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_render(LOGO_SVG, size, 1.0))
    return icon
