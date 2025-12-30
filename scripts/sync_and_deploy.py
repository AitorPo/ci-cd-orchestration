#!/usr/bin/env python3
"""
Sync, build, and deploy all services defined in services/*.yml on a remote host.
- Clones/pulls each repo
- Checks out the configured ref
- Runs optional pull/build/migrate commands
- Starts the service (start_cmd)
- Health-checks via HTTP

Usage: ./scripts/sync_and_deploy.py <ssh_host> [--service name]...
ssh_host example: user@server.example.com
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = ROOT / "services"


def run(cmd, *, check=True, input_text=None):
    if isinstance(cmd, str):
        result = subprocess.run(cmd, shell=True, text=True, input=input_text)
    else:
        result = subprocess.run(cmd, text=True, input=input_text)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result


def parse_service(path: Path):
    data = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        data[key.strip()] = val.strip()

    def _get(key, default=""):
        return data.get(key, default)

    name = _get("name")
    domain = _get("domain")
    repo_url = _get("repo_url")
    repo_ref = _get("repo_ref") or "main"
    working_dir = _get("working_dir")
    compose_file = _get("compose_file")
    build_cmd = _get("build_cmd")
    start_cmd = _get("start_cmd")
    migrate_cmd = _get("migrate_cmd")
    health_path = _get("health_path") or "/health"

    def _clean(val: str | None) -> str:
        if val in ('""', "''", None):
            return ""
        return val

    compose_file = _clean(compose_file)
    build_cmd = _clean(build_cmd)
    start_cmd = _clean(start_cmd)
    migrate_cmd = _clean(migrate_cmd)

    if not name or not repo_url or not working_dir:
        return None

    return {
        "name": name,
        "domain": domain,
        "repo_url": repo_url,
        "repo_ref": repo_ref,
        "working_dir": working_dir,
        "compose_file": compose_file or "",
        "build_cmd": build_cmd or "",
        "start_cmd": start_cmd or "",
        "migrate_cmd": migrate_cmd or "",
        "health_path": health_path,
    }


def render_remote_script(svc):
    wd = shlex.quote(svc["working_dir"])
    repo = shlex.quote(svc["repo_url"])
    ref = shlex.quote(svc["repo_ref"])
    compose_file = shlex.quote(svc["compose_file"]) if svc["compose_file"] else ""
    build_cmd = svc["build_cmd"]
    start_cmd = svc["start_cmd"]
    migrate_cmd = svc["migrate_cmd"]
    domain = svc["domain"]
    health_path = svc["health_path"]
    service_name = shlex.quote(svc["name"])

    pull_cmd = ""
    ensure_net = ""
    if compose_file:
        pull_cmd = f'docker compose -f {compose_file} pull'
        project = Path(svc["compose_file"]).parent.name
        network = f"{project}_default"
        ensure_net = f'docker network inspect {shlex.quote(network)} >/dev/null 2>&1 || docker network create {shlex.quote(network)}'

    lines = [
        "set -euo pipefail",
        "set -x",
        'trap \'echo "[svc] failed at: $BASH_COMMAND" >&2\' ERR',
        f'SERVICE_NAME={service_name}',
        'service_unit="/etc/systemd/system/${SERVICE_NAME}.service"',
        'echo "[svc] syncing $SERVICE_NAME"',
        'need() { command -v "$1" >/dev/null 2>&1; }',
        'ensure_dir() {',
        '  local dir="$1"',
        '  if [ ! -d "$dir" ]; then',
        '    if ! mkdir -p "$dir" 2>/dev/null; then',
        '      if command -v sudo >/dev/null 2>&1; then',
        '        sudo mkdir -p "$dir"',
        '      else',
        '        echo "Cannot create directory $dir; missing permissions (tried without sudo)." >&2',
        '        exit 1',
        '      fi',
        '    fi',
        '  fi',
        '  if [ ! -w "$dir" ] && command -v sudo >/dev/null 2>&1; then',
        '    sudo chown "$(id -u):$(id -g)" "$dir" || true',
        '  fi',
        '}',
        'if ! need git || ! need docker; then',
        '  if need apt-get; then',
        '    sudo apt-get update -y',
        '    sudo apt-get install -y git docker.io docker-compose-plugin',
        '  else',
        '    echo "git/docker required on host; install manually."',
        '    exit 1',
        '  fi',
        'fi',
        f'echo "[svc] ensuring working_dir {wd}"',
        f'ensure_dir {wd}',
        f'if [ ! -d {wd} ] || [ ! -d {wd}/.git ]; then',
        f'  echo "[svc] cloning {repo} into {wd}"',
        f'  if ! git clone {repo} {wd}; then',
        f'    echo "[svc] git clone failed (repo={repo}, dir={wd})" >&2',
        '    exit 1',
        '  fi',
        'fi',
        f'cd {wd}',
        'if [ ! -d .git ]; then',
        '  echo "[svc] clone missing (.git not found in working_dir)" >&2',
        '  exit 1',
        'fi',
        'git fetch --all --prune',
        f'git checkout {ref}',
        f'git pull origin {ref}',
        f'git reset --hard origin/{ref} || git reset --hard {ref}',
        # If envs were pushed via SERVICE_ENVS_JSON, they live in a systemd drop-in.
        # sync_and_deploy runs commands over SSH (not via systemd), so we load them explicitly
        # so docker compose `${VAR}` placeholders resolve correctly.
        'load_systemd_env_dropin() {',
        '  local svc="$1"',
        '  local drop="/etc/systemd/system/${svc}.service.d/env.conf"',
        '  [ -r "$drop" ] || return 0',
        '  while IFS= read -r line; do',
        '    case "$line" in',
        '      Environment=*)',
        '        kv="${line#Environment=}"',
        '        if [[ "$kv" == \\"*\\" ]]; then',
        '          kv="${kv#\\"}"',
        '          kv="${kv%\\"}"',
        '          kv="${kv//\\\\\\"/\\"}"',
        "        elif [[ \"$kv\" == \\'*\\' ]]; then",
        "          kv=\"${kv#\\'}\"",
        "          kv=\"${kv%\\'}\"",
        "          kv=\"${kv//\\\\\\'/\\'}\"",
        "        fi",
        '        if [[ "$kv" == *=* ]]; then',
        '          export "$kv"',
        '        fi',
        '        ;;',
        '    esac',
        '  done < "$drop"',
        '}',
        'load_systemd_env_dropin "$SERVICE_NAME"',
        'use_fallback=1',
    ]
    if ensure_net:
        lines.append(ensure_net)
    if pull_cmd:
        lines.append(pull_cmd)
    if build_cmd:
        lines.append(build_cmd)
    lines.extend([
        'if [ -f "$service_unit" ] && command -v systemctl >/dev/null 2>&1; then',
        '  sudo systemctl daemon-reload',
        '  sudo systemctl reset-failed "$SERVICE_NAME" || true',
        '  sudo systemctl enable "$SERVICE_NAME" || true',
        '  echo "Restarting $SERVICE_NAME via systemd ..."',
        '  if sudo systemctl restart "$SERVICE_NAME"; then',
        '    use_fallback=0',
        '  else',
        '    echo "systemd restart failed; will fall back to start_cmd."',
        '  fi',
        'else',
        '  echo "No systemd unit for $SERVICE_NAME; will use start_cmd."',
        'fi',
    ])
    if start_cmd:
        lines.extend([
            'if [ "$use_fallback" -eq 1 ]; then',
            start_cmd,
            'fi',
        ])
    if migrate_cmd:
        lines.append(migrate_cmd)
    if domain:
        url = f"http://{domain}{health_path}"
        lines.append(f'curl -fsS --max-time 10 "{url}" || true')

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ssh_host", help="user@host to deploy on")
    parser.add_argument("--service", action="append", help="service name to limit (can repeat)")
    args = parser.parse_args()

    services = []
    if args.service:
        uniq = []
        seen = set()
        for name in args.service:
            if not name:
                continue
            if name in seen:
                continue
            seen.add(name)
            uniq.append(name)

        missing_files = []
        invalid = []
        for name in uniq:
            svc_file = SERVICES_DIR / f"{name}.yml"
            if not svc_file.exists():
                missing_files.append(name)
                continue
            svc = parse_service(svc_file)
            if not svc:
                invalid.append(name)
                continue
            services.append(svc)

        if missing_files or invalid:
            available = ", ".join(sorted(p.stem for p in SERVICES_DIR.glob("*.yml"))) or "<none>"
            if missing_files:
                print(f"Service file(s) not found: {', '.join(missing_files)}", file=sys.stderr)
            if invalid:
                print(
                    f"Invalid service definition(s) (missing name/repo_url/working_dir): {', '.join(invalid)}",
                    file=sys.stderr,
                )
            print(f"Available services: {available}", file=sys.stderr)
            return 1
    else:
        for svc_file in sorted(SERVICES_DIR.glob("*.yml")):
            svc = parse_service(svc_file)
            if not svc:
                continue
            services.append(svc)

    if not services:
        print("No services with repo_url/working_dir found.")
        return 0

    for svc in services:
        print(f"[remote:{args.ssh_host}] Syncing {svc['name']} ...")
        script = render_remote_script(svc)
        run(["ssh", args.ssh_host, "bash", "-s"], input_text=script)

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
