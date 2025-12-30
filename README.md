# Single-Host Deploy Orchestrator (nginx + systemd + certbot)

Template-based deployment orchestrator for running multiple services on one host behind nginx with automatic TLS.

## Core Idea

- **Template-based deployment**: Services trigger via `repository_dispatch` with minimal payload
- **Zero boilerplate**: No deployment configs needed in service repositories  
- **Centralized control**: All deployment logic lives in this orchestrator repo
- **Native tools**: nginx, certbot, systemd, Docker Compose, Python/bash

## Quick Start

### 1. Service Repository Setup

Add this workflow to your service repo (`.github/workflows/deploy.yml`). Only `service` is required; everything else defaults (domain `{service}.example.com`, port `3000`, repo `git@github.com:AitorPo/{service}.git`, ref `main`).

```yaml
name: Deploy
on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger deployment
        run: |
          gh api repos/AitorPo/ci-cd-orchestration/dispatches \
            -f event_type=service-deploy \
            -f client_payload[service]=${{ github.event.repository.name }}
        env:
          GH_TOKEN: ${{ secrets.DEPLOY_ORCHESTRATOR_TOKEN }}
```

Add `client_payload[domain]` and `client_payload[port]` flags if you want to override the defaults.

### 2. Configure Secrets/Vars

- Service repo: secret `DEPLOY_ORCHESTRATOR_TOKEN` (GitHub token with `repo` scope). Optional repo variables `DOMAIN` and `PORT` for custom values.
- Orchestrator repo: secrets `DEPLOY_HOST`, `DEPLOY_SSH_KEY`, and `CERT_EMAIL`; optional `SERVICE_ENVS_JSON` and `PUSH_DEPLOY_SERVICES`.
- Sample values: see `docs/secrets.sample.env` for required/optional secrets in both the orchestrator and service repos.

### 3. Push to Deploy

Push to `main` → Automatic deployment! 🚀

📋 **Payload Reference**: [docs/PAYLOAD.md](docs/PAYLOAD.md)

## How It Works

```
Service Repo (push to main)
    ↓
GitHub Workflow sends repository_dispatch
    ↓
Orchestrator receives payload {"service": "my-app", "domain": "...", "port": "..."}
    ↓
Generates temp services/my-app.yml from service.yml template
    ↓
Renders nginx + systemd configs
    ↓
Deploys to production server
    ↓
Issues TLS certificate
    ↓
Service live at https://my-app.example.com
```

## Repository Structure

```
ci-cd-orchestration/
├── .github/workflows/deploy.yml   # GitHub Actions deploy workflow
├── README.md
├── service.yml                    # Template with placeholders
├── services/                      # Generated service configs (gitignored)
├── templates/
│   ├── nginx.conf.tmpl
│   └── systemd.service.tmpl
├── scripts/
│   ├── render.py
│   ├── one_click.py
│   ├── sync_and_deploy.py
│   ├── deploy.py
│   └── cert_ensure.py
├── generated/                     # Rendered nginx/systemd configs (gitignored)
└── docs/
    └── PAYLOAD.md                 # repository_dispatch payload reference
```

## Service Requirements

Your service repository must have:

1. **`compose.yml`** at repository root
2. **Health endpoint** (default: `/healthz`) returning HTTP 200
3. **Exposed port** matching the payload port value

Example `compose.yml`:

```yaml
services:
  app:
    build: .
    ports:
      - "127.0.0.1:3000:3000"  # Bind to localhost only
    environment:
      NODE_ENV: production
      DATABASE_URL: ${DATABASE_URL}  # Injected from orchestrator
    restart: unless-stopped
```

## Environment Variables

By default every repository secret is injected into each service drop-in (newlines are escaped). To control/env-limit per service, set orchestrator repo secret `SERVICE_ENVS_JSON`:

```json
{
  "my-service": "DATABASE_URL=postgres://...\nAPI_KEY=secret123\nNODE_ENV=production"
}
```

When set, those envs are:
1. Written to `/etc/systemd/system/<service>.service.d/env.conf`
2. Available to Docker Compose as `${VARIABLE_NAME}`
3. Automatically injected during deployment

If `SERVICE_ENVS_JSON` is unset/empty, the workflow falls back to writing all repository secrets into each service drop-in.

## Payload Reference

### Minimal (recommended)

```json
{
  "service": "my-app"
}
```

Uses sensible defaults:
- Domain: `my-app.example.com`
- Port: `3000`
- Repo: `git@github.com:AitorPo/my-app.git`
- Branch: `main`

### With Overrides

```json
{
  "service": "my-app",
  "domain": "app.example.com",
  "port": "8080",
  "repo_url": "git@github.com:MyOrg/my-app.git",
  "repo_ref": "production",
  "health_path": "/api/health",
  "migrate_cmd": "docker compose exec -T app npm run migrate"
}
```

See [docs/PAYLOAD.md](docs/PAYLOAD.md) for all supported fields.

## GitHub Actions Workflow

The orchestrator workflow triggers on:

- **`repository_dispatch`** (recommended) - Per-service deployments
- **`workflow_dispatch`** - Manual triggers
- **`push` to `main`** - When templates/scripts change

### Workflow Steps

1. **Generate config** - Creates temp `services/{service}.yml` from template
2. **Push env vars** - Writes systemd drop-ins (if `SERVICE_ENVS_JSON` set)
3. **Deploy configs** - Renders nginx/systemd, installs deps, issues certs
4. **Deploy service** - Clones repo, builds containers, starts service, health checks

### Push Behavior Control

Set `PUSH_DEPLOY_SERVICES` secret to control push deployments when a commit is pushed to main IN THIS REPO:
- Unset or `all`: Deploy all services (NOT recommended)
- `none`, `skip`, `false`, `0`: Skip service deployment (Recommended)
- `service-a, service-b`: Deploy only listed services (NOT recommended)

## Manual Operations

### Trigger Deployment via CLI

```bash
gh api repos/AitorPo/ci-cd-orchestration/dispatches \
  -f event_type=service-deploy \
  -f client_payload[service]=my-app \
  -f client_payload[domain]=my-app.example.com \
  -f client_payload[port]=3000
```

### Run Scripts Directly

```bash
# Generate configs (requires temp service.yml in services/)
./scripts/render.py --service my-app

# Deploy everything
./scripts/one_click.py user@host email@example.com --service my-app

# Sync and deploy service code
./scripts/sync_and_deploy.py user@host --service my-app

# Restart service + health check
./scripts/deploy.py my-app

# Issue/renew certificate
./scripts/cert_ensure.py my-app.example.com email@example.com
```

## Template System

The `service.yml` template contains placeholders replaced at runtime:

```yaml
name: __REPO_NAME__
domain: __DOMAIN__
upstream_port: __PORT__
working_dir: __WORKING_DIR__
repo_url: __REPO_URL__
repo_ref: __REPO_REF__
# ... etc
```

**Supported placeholders:**
- `__REPO_NAME__` - Service name (from payload)
- `__DOMAIN__` - Service domain
- `__PORT__` - Upstream port
- `__REPO_URL__` - Git repository URL
- `__REPO_REF__` - Git branch/tag
- `__WORKING_DIR__` - Working directory on host
- `__USER__` - System user
- `__UPSTREAM_HOST__` - Upstream host
- `__HEALTH_PATH__` - Health check endpoint
- `__STATIC_ROOT__` - Static files directory
- `__MIGRATE_CMD__` - Migration command

### Custom nginx locations

Add `locations` to `services/<name>.yml` to emit extra `location` blocks (keeps the default `/` location):

```yaml
locations:
  - path: /mcp/
    strip_prefix: true
    proxy_pass: http://127.0.0.1:4000  # defaults to http://<upstream_host>:<upstream_port>
    extra: |
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
```

- `path` is required.
- `proxy_pass` defaults to the service upstream; set `false` to skip proxying and rely only on `extra`.
- `strip_prefix: true` adds a trailing slash to `proxy_pass`, dropping the matched prefix when forwarding.
- `extra` is copied verbatim inside the location block for headers/rewrites/etc.
- From a repository_dispatch trigger, set `client_payload.locations` (JSON array) and the workflow writes the block into the generated service file automatically.

## Architecture

### Nginx Configuration

- HTTP → HTTPS redirect
- ACME challenge location for Let's Encrypt
- Security headers (HSTS, XSS protection, etc.)
- Optional static file serving
- Proxy to upstream service
- Health endpoint returning 200 OK

### Systemd Configuration

- `Type=oneshot` for Docker Compose services
- `Type=simple` for direct process services
- Automatic restarts (except for compose)
- Working directory management
- Environment variables from drop-ins

### TLS Certificates

- Automatic issuance via certbot
- Nginx plugin for seamless integration
- Auto-renewal (certbot handles this)
- Dummy certs created initially to allow nginx to start

## Host Requirements

Target server must have:

- **systemd** (Ubuntu 20.04+, Debian 11+, etc.)
- **SSH access** with sudo privileges
- **Python 3.11+** (for orchestrator scripts)

Dependencies auto-installed on Debian/Ubuntu:
- nginx
- certbot + python3-certbot-nginx
- docker.io + docker-compose-plugin

## Troubleshooting

### Deployment fails with "Service file not found"

Ensure `service` field in payload matches your repository name exactly.

### Health check fails

- Verify `/healthz` endpoint returns HTTP 200
- Check port matches what your app listens on
- Review logs: `ssh user@host sudo journalctl -u my-service -n 50`

### Container won't start

- Validate compose.yml: `docker compose config`
- Check environment variables in `SERVICE_ENVS_JSON`
- Review Docker logs: `ssh user@host docker compose -f /opt/my-service/compose.yml logs`

### Certificate issuance fails

- Ensure domain DNS points to your server
- Check nginx is accessible on port 80: `curl http://yourdomain.com/.well-known/`
- Review certbot logs: `ssh user@host sudo certbot certificates`

## Documentation

- **[PAYLOAD.md](docs/PAYLOAD.md)** - repository_dispatch payload reference and examples

## License

See LICENSE file.
