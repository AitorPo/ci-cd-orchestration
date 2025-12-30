#!/usr/bin/env python3
"""
Issue or renew certificates for a domain using certbot and nginx plugin.

Usage: ./scripts/cert_ensure.py <domain> <email>
"""
from __future__ import annotations

import subprocess
import sys


def run(cmd, *, check=True):
    result = subprocess.run(cmd, text=True)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result


def main():
    if len(sys.argv) != 3:
        print("Usage: cert_ensure.py <domain> <email>")
        return 1

    domain, email = sys.argv[1], sys.argv[2]

    print(f"Requesting/renewing cert for {domain} ...")
    run(["certbot", "--nginx", "-d", domain, "-m", email, "--agree-tos", "--redirect", "--non-interactive"])

    print("Testing nginx config and reloading...")
    run(["nginx", "-t"])
    run(["systemctl", "reload", "nginx"])

    print(f"Cert check done for {domain}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
