#!/usr/bin/env python3
"""
Deploy a service by name:
- run migrate_cmd if present
- reload systemd manager (picks up drop-ins)
- restart systemd unit
- health check over HTTPS

Usage: ./scripts/deploy.py <service-name>
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = ROOT / "services"


def run(cmd, *, check=True):
    if isinstance(cmd, str):
        result = subprocess.run(cmd, shell=True, text=True)
    else:
        result = subprocess.run(cmd, text=True)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result


def parse_service(service_name: str):
    svc_file = SERVICES_DIR / f"{service_name}.yml"
    if not svc_file.exists():
        raise FileNotFoundError(f"Service file not found: {svc_file}")

    data = {}
    for raw in svc_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        data[key.strip()] = val.strip()

    def _get(key, default=""):
        return data.get(key, default)

    domain = _get("domain")
    migrate_cmd = _get("migrate_cmd")
    health_path = _get("health_path") or "/health"

    if migrate_cmd in ('""', "''"):
        migrate_cmd = ""

    if not domain:
        raise ValueError(f"Service {service_name} missing 'domain'")

    return {
        "domain": domain,
        "migrate_cmd": migrate_cmd or "",
        "health_path": health_path,
    }


def main():
    if len(sys.argv) != 2:
        print("Usage: deploy.py <service-name>")
        return 1

    service_name = sys.argv[1]
    svc = parse_service(service_name)

    if svc["migrate_cmd"]:
        print(f"Running migrations for {service_name} ...")
        run(svc["migrate_cmd"])

    print("Reloading systemd manager ...")
    run(["systemctl", "daemon-reload"])

    print(f"Restarting systemd unit {service_name} ...")
    run(["systemctl", "restart", service_name])

    url = f"https://{svc['domain']}{svc['health_path']}"
    print(f"Checking health at {url} ...")
    run(["curl", "-fsS", url])
    print()
    print(f"Deploy complete for {service_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
