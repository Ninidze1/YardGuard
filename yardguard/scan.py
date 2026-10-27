"""Find camera recorders (NVR/DVR) and IP cameras on the local network."""
from __future__ import annotations

import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

PORTS = (554, 37777, 8000, 80)
CAMERA_PORTS = {554, 37777, 8000}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# First 3 bytes of the MAC address (vendor prefix)
OUI = {
    "dahua": {"3C-EF-8C", "4C-11-BF", "90-02-A9", "E0-50-8B", "08-ED-ED", "14-A7-8B", "38-AF-29",
              "9C-14-63", "A0-BD-1D", "BC-32-5F", "6C-1C-71", "24-52-6A", "F4-B1-C2"},
    "hikvision": {"28-57-BE", "44-19-B6", "4C-BD-8F", "54-C4-15", "58-03-FB", "BC-AD-28", "C0-56-E3",
                  "E0-CA-3C", "18-68-CB", "C4-2F-90", "8C-E7-48", "A4-14-37", "24-48-45", "80-BE-AF",
                  "94-E1-AC", "64-DB-8B"},
}
BRAND_LABELS = {"dahua": "Dahua", "hikvision": "Hikvision", "custom": "Unknown brand"}


@dataclass
class Device:
    ip: str
    mac: str = ""
    brand: str = "custom"
    ports: tuple[int, ...] = ()

    @property
    def brand_label(self) -> str:
        return BRAND_LABELS.get(self.brand, "Unknown brand")


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _is_open(target: tuple[str, int], timeout: float) -> bool:
    try:
        with socket.create_connection(target, timeout=timeout):
            return True
    except OSError:
        return False


def _arp_table() -> dict[str, str]:
    out = subprocess.run(["arp", "-a"], capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    return {ip: mac.upper() for ip, mac in
            re.findall(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f]{2}(?:-[0-9a-f]{2}){5})", out, re.I)}


def guess_brand(mac: str, ports) -> str:
    prefix = mac[:8].upper()
    for brand, prefixes in OUI.items():
        if prefix in prefixes:
            return brand
    if 37777 in ports:
        return "dahua"
    if 8000 in ports:
        return "hikvision"
    return "custom"


def scan_network(timeout: float = 0.4) -> list[Device]:
    me = local_ip()
    prefix = me.rsplit(".", 1)[0]
    targets = [(f"{prefix}.{i}", p) for i in range(1, 255) for p in PORTS if f"{prefix}.{i}" != me]
    with ThreadPoolExecutor(256) as pool:
        results = list(pool.map(lambda t: (t, _is_open(t, timeout)), targets))

    found: dict[str, list[int]] = {}
    for (ip, port), ok in results:
        if ok:
            found.setdefault(ip, []).append(port)

    arp = _arp_table()
    devices = [Device(ip, arp.get(ip, ""), guess_brand(arp.get(ip, ""), ports), tuple(sorted(ports)))
               for ip, ports in found.items() if CAMERA_PORTS & set(ports)]
    return sorted(devices, key=lambda d: tuple(int(x) for x in d.ip.split(".")))


def subnet_label() -> str:
    return local_ip().rsplit(".", 1)[0] + ".x"
