#!/usr/bin/env python3
"""
One-button deploy: render configs locally, copy to host, install deps, reload services, issue certs.

Usage: ./scripts/one_click.py <ssh_host> <cert_email> [--service name]...
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
GENERATED = ROOT / "generated"


def run(cmd, *, check=True, input_text=None):
    if isinstance(cmd, str):
        result = subprocess.run(cmd, shell=True, text=True, input=input_text)
    else:
        result = subprocess.run(cmd, text=True, input=input_text)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result


def iter_service_files(selected: list[str] | None = None):
    if not selected:
        return sorted(SERVICES_DIR.glob("*.yml"))

    uniq = []
    seen = set()
    for name in selected:
        if not name:
            continue
        if name in seen:
            continue
        seen.add(name)
        uniq.append(name)

    missing = []
    paths = []
    for name in uniq:
        path = SERVICES_DIR / f"{name}.yml"
        if not path.exists():
            missing.append(name)
            continue
        paths.append(path)

    if missing:
        raise FileNotFoundError(f"Service file(s) not found in {SERVICES_DIR}: {', '.join(missing)}")

    return paths


def parse_domains(selected: list[str] | None = None):
    domains = []
    for svc_file in iter_service_files(selected):
        for raw in svc_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line.startswith("domain:"):
                continue
            _, val = line.split(":", 1)
            d = val.strip()
            if d:
                domains.append(d)
            break
    return domains


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ssh_host", help="user@host to deploy on")
    parser.add_argument("cert_email", help="email for certbot registrations")
    parser.add_argument("--service", action="append", help="service name to limit (can repeat)")
    args = parser.parse_args()

    ssh_host, cert_email = args.ssh_host, args.cert_email

    print("[local] Rendering configs...")
    render_cmd = [sys.executable, str(ROOT / "scripts" / "render.py")]
    if args.service:
        for svc in args.service:
            render_cmd += ["--service", svc]
    run(render_cmd)

    domains = parse_domains(args.service)
    domains_str = " ".join(domains)
    tmp_remote = "/tmp/deploy-orchestrator"

    print(f"[local] Preparing remote temp dir on {ssh_host} ...")
    remote_tmp = shlex.quote(tmp_remote)
    run(["ssh", ssh_host, f"rm -rf {remote_tmp} && mkdir -p {remote_tmp}/nginx {remote_tmp}/systemd"])

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

        nginx_files = [str(GENERATED / "nginx" / f"{name}.conf") for name in uniq]
        systemd_files = [str(GENERATED / "systemd" / f"{name}.service") for name in uniq]
        for p in nginx_files + systemd_files:
            if not Path(p).exists():
                raise FileNotFoundError(f"Rendered file missing: {p}")

        print(f"[local] Copying selected generated configs to {ssh_host}:{tmp_remote} ...")
        run(["scp", *nginx_files, f"{ssh_host}:{tmp_remote}/nginx/"])
        run(["scp", *systemd_files, f"{ssh_host}:{tmp_remote}/systemd/"])
    else:
        print(f"[local] Copying generated configs to {ssh_host}:{tmp_remote} ...")
        run(["scp", "-r", str(GENERATED / "nginx"), f"{ssh_host}:{tmp_remote}/"])
        run(["scp", "-r", str(GENERATED / "systemd"), f"{ssh_host}:{tmp_remote}/"])

    print("[remote] Installing configs, reloading services, issuing certs...")
    remote_script = f"""set -euo pipefail

need_cmd() {{ command -v "$1" >/dev/null 2>&1; }}

ensure_systemd() {{
  if ! need_cmd systemctl; then
    echo "systemd is required on the host. Aborting."
    exit 1
  fi
}}

install_deps() {{
  missing_cmds=()
  pkgs=()
  need_cmd nginx || {{ missing_cmds+=("nginx"); pkgs+=("nginx"); }}
  need_cmd certbot || {{ missing_cmds+=("certbot"); pkgs+=("certbot" "python3-certbot-nginx"); }}
  need_cmd docker || {{ missing_cmds+=("docker"); pkgs+=("docker.io" "docker-compose-plugin"); }}
  need_cmd openssl || pkgs+=("openssl")
  if [ ${{#missing_cmds[@]}} -eq 0 ]; then
    echo "Dependencies already present: nginx, certbot, docker"
    return
  fi
  if need_cmd apt-get; then
    echo "Installing dependencies via apt-get for: ${{missing_cmds[*]}} ..."
    sudo apt-get update -y
    sudo apt-get install -y "${{pkgs[@]}}"
  else
    echo "Missing dependencies (${{missing_cmds[*]}}) and apt-get not available. Install nginx, certbot, and docker manually."
    exit 1
  fi
}}

ensure_dir() {{
  local dir="$1"
  local owner="${{2:-}}"
  [ -z "$dir" ] && return 0
  if [ ! -d "$dir" ]; then
    if ! sudo mkdir -p "$dir"; then
      echo "Failed to create $dir" >&2
      return 1
    fi
  fi
  if [ -n "$owner" ] && [ "$owner" != "root" ]; then
    sudo chown "$owner":"$owner" "$dir" || true
  fi
}}

TMP_REMOTE={shlex.quote(tmp_remote)}
DOMAINS="{domains_str}"
CERT_EMAIL={shlex.quote(cert_email)}

echo "Using TMP_REMOTE=$TMP_REMOTE"
ensure_systemd
install_deps

sudo mkdir -p /etc/nginx/sites-enabled /etc/systemd/system

# Ensure nginx options file exists to avoid test failures before certbot populates it
if [ ! -f /etc/letsencrypt/options-ssl-nginx.conf ]; then
  echo "Creating /etc/letsencrypt/options-ssl-nginx.conf ..."
  sudo mkdir -p /etc/letsencrypt
  cat <<'EOF' | sudo tee /etc/letsencrypt/options-ssl-nginx.conf >/dev/null
# Minimal defaults; replaced by certbot on first successful run.
ssl_session_cache shared:SSL:10m;
ssl_session_timeout 10m;
ssl_prefer_server_ciphers off;
ssl_session_tickets off;
EOF
fi

create_dummy_certs() {{
  for d in $DOMAINS; do
    live="/etc/letsencrypt/live/$d"
    full="$live/fullchain.pem"
    key="$live/privkey.pem"
    if [ -f "$full" ] && [ -f "$key" ]; then
      continue
    fi
    echo "Creating dummy cert for $d ..."
    sudo mkdir -p "$live"
    sudo openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
      -keyout "$key" -out "$full" -subj "/CN=$d" >/dev/null 2>&1
  done
}}

create_dummy_certs

if ls "$TMP_REMOTE/nginx"/*.conf >/dev/null 2>&1; then
  sudo cp "$TMP_REMOTE/nginx"/*.conf /etc/nginx/sites-enabled/
fi

if ls "$TMP_REMOTE/systemd"/*.service >/dev/null 2>&1; then
  sudo cp "$TMP_REMOTE/systemd"/*.service /etc/systemd/system/
fi

sudo systemctl daemon-reload

# Ensure nginx service is enabled; start if not running
sudo systemctl enable nginx || true

if ls "$TMP_REMOTE/systemd"/*.service >/dev/null 2>&1; then
  skipped=()
  start_failures=()
  for svc in "$TMP_REMOTE/systemd"/*.service; do
    name=$(basename "$svc" .service)
    echo "Enabling $name ..."
    sudo systemctl enable "$name" || true

    wd=$(sed -n 's/^WorkingDirectory=//p' "$svc" | head -n 1)
    user=$(sed -n 's/^User=//p' "$svc" | head -n 1)
    if ! ensure_dir "$wd" "$user"; then
      echo "Skipping start for $name: unable to ensure WorkingDirectory ($wd)"
      skipped+=("$name")
      continue
    fi

    exec_start=$(sed -n 's/^ExecStart=//p' "$svc" | head -n 1)
    if echo "$exec_start" | grep -Eq '(^|[[:space:]])docker([[:space:]]+compose|[-]compose)([[:space:]]|$)'; then
      compose_file=""
      set -- $exec_start
      while [ $# -gt 0 ]; do
        if [ "$1" = "-f" ] || [ "$1" = "--file" ]; then
          shift
          compose_file="${{1:-}}"
          break
        fi
        shift
      done

      if [ -n "${{compose_file:-}}" ] && [ ! -f "$compose_file" ]; then
        echo "Skipping start for $name: missing compose file ($compose_file)"
        skipped+=("$name")
        continue
      fi
    fi

    echo "Starting $name ..."
    if ! sudo systemctl start "$name"; then
      echo "Failed to start $name (continuing)."
      start_failures+=("$name")
    fi
  done

  if [ "${{#skipped[@]}}" -ne 0 ]; then
    echo "Services enabled but not started: ${{skipped[*]}}"
  fi
  if [ "${{#start_failures[@]}}" -ne 0 ]; then
    echo "Services failed to start: ${{start_failures[*]}}"
  fi
fi

sudo nginx -t
if systemctl is-active --quiet nginx; then
  sudo systemctl reload nginx
else
  sudo systemctl start nginx || true
fi

for d in $DOMAINS; do
  echo "Issuing/renewing cert for $d ..."
  sudo certbot --nginx -d "$d" -m "$CERT_EMAIL" --agree-tos --redirect --non-interactive || true
done

sudo nginx -t
if systemctl is-active --quiet nginx; then
  sudo systemctl reload nginx
else
  sudo systemctl start nginx || true
fi

echo "Done."
"""

    run(["ssh", ssh_host, "bash", "-s"], input_text=remote_script)

    print(f"All done. Deployed configs and attempted cert issuance for: {domains_str or '<none>'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
