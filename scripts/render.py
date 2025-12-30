#!/usr/bin/env python3
"""
Render nginx and systemd configs from services/*.yml into ./generated.
Mirrors the prior bash logic but in Python.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = ROOT / "services"
TEMPLATE_DIR = ROOT / "templates"
OUT_DIR = ROOT / "generated"
NGINX_OUT = OUT_DIR / "nginx"
SYSTEMD_OUT = OUT_DIR / "systemd"


def load_services(selected: list[str] | None = None):
    if not selected:
        services = sorted(SERVICES_DIR.glob("*.yml"))
        if not services:
            print(f"No service files found in {SERVICES_DIR}")
            return []
        return services

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
    services = []
    for name in uniq:
        path = SERVICES_DIR / f"{name}.yml"
        if not path.exists():
            missing.append(name)
            continue
        services.append(path)

    if missing:
        raise SystemExit(f"Service file(s) not found in {SERVICES_DIR}: {', '.join(missing)}")

    return services


def parse_locations(raw_locations, upstream_host: str, upstream_port: str):
    """Normalize custom location definitions."""
    if not raw_locations:
        return []
    if not isinstance(raw_locations, list):
        print("Ignoring locations because it is not a list", file=sys.stderr)
        return []

    default_proxy = f"http://{upstream_host}:{upstream_port}"
    normalized = []
    for idx, entry in enumerate(raw_locations, 1):
        if not isinstance(entry, dict):
            print(f"Ignoring locations[{idx}] (expected mapping)", file=sys.stderr)
            continue

        path = entry.get("path")
        if not path:
            print(f"Ignoring locations[{idx}] (missing path)", file=sys.stderr)
            continue

        proxy_pass_val = entry.get("proxy_pass")
        # proxy_pass: false or proxy_pass: "" disables default proxying; useful for pure "extra" blocks.
        if proxy_pass_val is False:
            proxy_pass = ""
        else:
            proxy_pass = str(proxy_pass_val).strip() if proxy_pass_val else default_proxy

        strip_prefix = entry.get("strip_prefix")
        if isinstance(strip_prefix, str):
            strip_prefix = strip_prefix.lower() in ("true", "yes", "1", "on")
        else:
            strip_prefix = bool(strip_prefix)

        if strip_prefix and proxy_pass and not proxy_pass.endswith("/"):
            proxy_pass = proxy_pass + "/"

        extra = entry.get("extra") or ""
        normalized.append(
            {
                "path": str(path).strip(),
                "proxy_pass": proxy_pass,
                "extra": str(extra).rstrip(),
            }
        )
    return normalized


def parse_service(path: Path, *, strict: bool = False):
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # pragma: no cover - defensive
        msg = f"Failed to parse {path}: {exc}"
        if strict:
            raise SystemExit(msg)
        print(msg, file=sys.stderr)
        return None

    if not isinstance(data, dict):
        msg = f"Expected mapping in {path}, got {type(data).__name__}"
        if strict:
            raise SystemExit(msg)
        print(msg, file=sys.stderr)
        return None

    def _get(key, default=""):
        val = data.get(key, default)
        return val if val is not None else default

    def _normalize_bool(val):
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "yes", "1", "on")
        return False

    name = _get("name")
    domain = _get("domain")
    upstream_host = _get("upstream_host") or "127.0.0.1"
    upstream_port = _get("upstream_port")
    working_dir = _get("working_dir")
    user = _get("user") or "www-data"
    start_cmd = _get("start_cmd")
    stop_cmd = _get("stop_cmd") or "/bin/true"
    health_path = _get("health_path") or "/health"
    static_root = _get("static_root")
    migrate_cmd = _get("migrate_cmd")
    allow_plain_http = _normalize_bool(_get("allow_plain_http"))
    locations = parse_locations(_get("locations", []), upstream_host, upstream_port)
    start_cmd_lower = (start_cmd or "").lower()

    is_compose = "docker compose" in start_cmd_lower or "docker-compose" in start_cmd_lower
    if is_compose:
        svc_type = "oneshot"
        remain_after_exit = "yes"
        restart = "no"
    else:
        svc_type = "simple"
        remain_after_exit = "no"
        restart = "always"

    if static_root in ('""', "''"):
        static_root = ""
    if migrate_cmd in ('""', "''"):
        migrate_cmd = ""
    if stop_cmd in ('""', "''", ""):
        stop_cmd = "/bin/true"

    required = [("name", name), ("domain", domain), ("upstream_port", upstream_port), ("start_cmd", start_cmd), ("working_dir", working_dir)]
    missing = [k for k, v in required if not v]
    if missing:
        msg = f"Skipping {path.name}: missing {', '.join(missing)}"
        if strict:
            raise SystemExit(msg)
        print(msg, file=sys.stderr)
        return None

    return {
        "NAME": name,
        "DOMAIN": domain,
        "UPSTREAM_HOST": upstream_host,
        "UPSTREAM_PORT": str(upstream_port),
        "WORKING_DIR": working_dir,
        "USER": user,
        "START_CMD": start_cmd,
        "STOP_CMD": stop_cmd,
        "TYPE": svc_type,
        "REMAIN_AFTER_EXIT": remain_after_exit,
        "RESTART": restart,
        "HEALTH_PATH": health_path,
        "STATIC_ROOT": static_root or "",
        "MIGRATE_CMD": migrate_cmd or "",
        "ALLOW_PLAIN_HTTP": allow_plain_http,
        "LOCATIONS": locations,
    }


def build_nginx_blocks(static_root: str, host: str, port: str):
    if static_root:
        root_directive = f"    root {static_root};\n    index index.html;"
        location_root = (
            '        try_files $uri $uri/ @app;\n'
            '        add_header Cache-Control "no-cache, no-store";'
        )
        fallback_location = (
            "    location @app {\n"
            f"        proxy_pass http://{host}:{port};\n"
            "        proxy_set_header Host $host;\n"
            "        proxy_set_header X-Real-IP $remote_addr;\n"
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "        proxy_set_header X-Forwarded-Proto $scheme;\n"
            "    }\n"
        )
    else:
        root_directive = ""
        location_root = (
            f"        proxy_pass http://{host}:{port};\n"
            "        proxy_set_header Host $host;\n"
            "        proxy_set_header X-Real-IP $remote_addr;\n"
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "        proxy_set_header X-Forwarded-Proto $scheme;"
        )
        fallback_location = ""
    return root_directive, location_root, fallback_location


def render_custom_locations(locations: list[dict]):
    if not locations:
        return ""

    blocks = []
    for loc in locations:
        lines = [f"    location {loc['path']} {{"]
        proxy_pass = loc.get("proxy_pass")
        if proxy_pass:
            lines.extend(
                [
                    f"        proxy_pass {proxy_pass};",
                    "        proxy_set_header Host $host;",
                    "        proxy_set_header X-Real-IP $remote_addr;",
                    "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
                    "        proxy_set_header X-Forwarded-Proto $scheme;",
                ]
            )

        extra = loc.get("extra", "")
        if extra:
            for raw in extra.splitlines():
                raw = raw.rstrip()
                lines.append(f"        {raw}" if raw else "        ")

        lines.append("    }")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) + "\n\n"


def build_server_blocks(service: dict, root_directive: str, location_root: str, fallback_location: str, extra_locations: str):
    domain = service["DOMAIN"]
    health = service["HEALTH_PATH"]
    allow_plain_http = service.get("ALLOW_PLAIN_HTTP", False)

    def server_body(include_tls: bool):
        return (
            ("server {\n"
             "    listen 80;\n"
             f"    server_name {domain};\n\n"
             "    # ACME challenge\n"
             "    location /.well-known/acme-challenge/ {\n"
             "        root /var/www/certbot;\n"
             "    }\n\n"
             "    # Main location\n"
             "    location / {\n"
             f"{location_root}\n"
             "    }\n\n"
             f"{fallback_location}"
             f"{extra_locations}"
             "    # Health endpoint\n"
             f"    location {health} {{\n"
             "        access_log off;\n"
             '        return 200 "healthy\\n";\n'
             "        add_header Content-Type text/plain;\n"
             "    }\n"
             "}\n")
            if allow_plain_http or not include_tls else
            ("server {\n"
             "    listen 80;\n"
             f"    server_name {domain};\n\n"
             "    # ACME challenge\n"
             "    location /.well-known/acme-challenge/ {\n"
             "        root /var/www/certbot;\n"
             "    }\n\n"
             "    # Redirect HTTP to HTTPS\n"
             "    return 301 https://$host$request_uri;\n"
             "}\n")
        )

    http_block = server_body(include_tls=False)
    https_block = ""
    if not allow_plain_http:
        https_block = (
            "server {\n"
            "    listen 443 ssl;\n"
            "    listen [::]:443 ssl;\n"
            f"    server_name {domain};\n\n"
            "    # Certbot paths; adjust if you store certs elsewhere.\n"
            f"    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;\n"
            f"    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;\n"
            "    include /etc/letsencrypt/options-ssl-nginx.conf;\n\n"
            "    client_max_body_size 20m;\n"
            '    add_header X-Frame-Options "SAMEORIGIN" always;\n'
            '    add_header X-XSS-Protection "1; mode=block" always;\n'
            '    add_header X-Content-Type-Options "nosniff" always;\n'
            '    add_header Referrer-Policy "no-referrer-when-downgrade" always;\n'
            '    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;\n\n'
            f"{root_directive}\n"
            "    # Main location: either proxies or falls back from static to proxy.\n"
            "    location / {\n"
            f"{location_root}\n"
            "    }\n\n"
            f"{fallback_location}"
            f"{extra_locations}"
            "    # Health endpoint\n"
            f"    location {health} {{\n"
            "        access_log off;\n"
            '        return 200 "healthy\\n";\n'
            "        add_header Content-Type text/plain;\n"
            "    }\n"
            "}\n"
        )
    return http_block, https_block


def render_template(template_path: Path, dest_path: Path, replacements: dict):
    content = template_path.read_text(encoding="utf-8")
    for key, val in replacements.items():
        content = content.replace(f"__{key}__", val)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(content, encoding="utf-8")


def render_service(service: dict):
    root_directive, location_root, fallback_location = build_nginx_blocks(
        service["STATIC_ROOT"], service["UPSTREAM_HOST"], service["UPSTREAM_PORT"]
    )
    extra_locations = render_custom_locations(service.get("LOCATIONS") or [])
    http_block, https_block = build_server_blocks(
        service, root_directive, location_root, fallback_location, extra_locations
    )

    nginx_replacements = {
        "HTTP_BLOCK": http_block,
        "HTTPS_BLOCK": https_block,
    }
    systemd_replacements = {
        "NAME": service["NAME"],
        "WORKING_DIR": service["WORKING_DIR"],
        "USER": service["USER"],
        "START_CMD": service["START_CMD"],
        "STOP_CMD": service["STOP_CMD"],
        "TYPE": service["TYPE"],
        "REMAIN_AFTER_EXIT": service["REMAIN_AFTER_EXIT"],
        "RESTART": service["RESTART"],
    }

    render_template(TEMPLATE_DIR / "nginx.conf.tmpl", NGINX_OUT / f"{service['NAME']}.conf", nginx_replacements)
    render_template(TEMPLATE_DIR / "systemd.service.tmpl", SYSTEMD_OUT / f"{service['NAME']}.service", systemd_replacements)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--service",
        action="append",
        help="Service name to render (matches services/<name>.yml). Can repeat.",
    )
    args = parser.parse_args()

    NGINX_OUT.mkdir(parents=True, exist_ok=True)
    SYSTEMD_OUT.mkdir(parents=True, exist_ok=True)

    rendered = []
    strict = bool(args.service)
    for svc_file in load_services(args.service):
        parsed = parse_service(svc_file, strict=strict)
        if not parsed:
            continue
        render_service(parsed)
        rendered.append(parsed["NAME"])
        print(f"Rendered: {NGINX_OUT}/{parsed['NAME']}.conf and {SYSTEMD_OUT}/{parsed['NAME']}.service")

    if not rendered:
        if strict:
            raise SystemExit("No services rendered.")
        return 0

    print("\nNext steps (manual on host):")
    print("1) Copy nginx confs to /etc/nginx/sites-enabled/ and reload: nginx -t && systemctl reload nginx")
    print("2) Copy systemd units to /etc/systemd/system/ and reload: systemctl daemon-reload")
    print("3) Enable services: systemctl enable --now <service>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
