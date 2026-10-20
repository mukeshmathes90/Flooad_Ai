"""
Print the computer's active IPv4 address(es) so the ESP32 firmware can be pointed
at the right machine.

Usage:
    python tools/local_ip.py

Output lines (easy to parse from a .bat file):
    PRIMARY_IP=192.168.43.100
    ALL_IPS=127.0.0.1,192.168.43.100
"""

from __future__ import annotations

import socket
import subprocess
import sys


def primary_ip() -> str:
    """Best-effort 'default route' local IP (the one the hotspot sees)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packets are actually sent
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def all_ipv4() -> list[str]:
    ips: list[str] = []
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    # de-prioritise loopback
    return [ip for ip in ips if ip != "127.0.0.1"] + (["127.0.0.1"] if "127.0.0.1" in ips else [])


def wifi_ipv4() -> str:
    """Parse `ipconfig` for the wireless adapter IPv4 (Windows only)."""
    if sys.platform != "win32":
        return ""
    try:
        out = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    current_wifi = False
    for line in out.splitlines():
        low = line.lower()
        if "adapter" in low:
            current_wifi = ("wi-fi" in low or "wireless" in low or "wlan" in low)
        if current_wifi and "ipv4" in low:
            parts = line.split(":")
            if len(parts) >= 2:
                ip = parts[-1].strip()
                if ip:
                    return ip
    return ""


def main() -> None:
    prim = primary_ip()
    wifi = wifi_ipv4()
    chosen = wifi or prim
    ips = all_ipv4()
    if prim and prim not in ips:
        ips.insert(0, prim)

    print(f"PRIMARY_IP={chosen}")
    print(f"WIFI_IP={wifi}")
    print(f"DEFAULT_ROUTE_IP={prim}")
    print(f"ALL_IPS={','.join(ips)}")
    if chosen:
        print(f"ESP32_BACKEND_URL=http://{chosen}:8000/api/sensors/data")
    else:
        print("ESP32_BACKEND_URL=(could not detect - check your network connection)")


if __name__ == "__main__":
    main()
