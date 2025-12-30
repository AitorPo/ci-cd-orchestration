# Repository Dispatch Payload Reference

## Trigger Deployment

```bash
gh api repos/AitorPo/ci-cd-orchestration/dispatches \
  -f event_type=service-deploy \
  -f client_payload[service]=my-service \
  -f client_payload[domain]=my-service.example.com \
  -f client_payload[port]=3000
```

## Required Field

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `service` | string | Service name (must match repo name) | `"my-app"` |

## Optional Fields

All fields have sensible defaults. Only override when needed.

| Field | Default | Description |
|-------|---------|-------------|
| `domain` | `{service}.example.com` | Service domain |
| `port` | `"3000"` | Upstream port |
| `repo_url` | `git@github.com:AitorPo/{service}.git` | Git repository URL |
| `repo_ref` | `"main"` | Git branch/tag/ref |
| `working_dir` | `/opt/{service}` | Working directory on host |
| `user` | `"root"` | System user to run service |
| `upstream_host` | `"127.0.0.1"` | Upstream host for nginx |
| `health_path` | `"/healthz"` | Health check endpoint |
| `static_root` | `""` | Static files directory |
| `migrate_cmd` | `""` | Migration command |
| `locations` | `[]` | Extra nginx locations (list of `{path, proxy_pass, strip_prefix, extra}`) |

## Examples

### Minimal (recommended)

```json
{
  "event_type": "service-deploy",
  "client_payload": {
    "service": "my-app"
  }
}
```

Uses defaults: domain `my-app.example.com`, port `3000`, repo `git@github.com:AitorPo/my-app.git`, branch `main`.

### With Custom Domain & Port

```json
{
  "event_type": "service-deploy",
  "client_payload": {
    "service": "my-app",
    "domain": "app.example.com",
    "port": "8080"
  }
}
```

### Full Custom Configuration

```json
{
  "event_type": "service-deploy",
  "client_payload": {
    "service": "my-app",
    "domain": "app.example.com",
    "port": "8080",
    "repo_url": "git@github.com:MyOrg/my-app.git",
    "repo_ref": "production",
    "health_path": "/api/health",
    "migrate_cmd": "docker compose exec -T app npm run migrate"
  }
}
```

### With Custom Locations

Pass an array of location objects (JSON string is fine) to render extra nginx `location` blocks:

```json
{
  "event_type": "service-deploy",
  "client_payload": {
    "service": "my-app",
    "domain": "app.example.com",
    "locations": [
      {
        "path": "/mcp/",
        "strip_prefix": true,
        "proxy_pass": "http://127.0.0.1:4000",
        "extra": "proxy_set_header Host $host;"
      }
    ]
  }
}
```

## From GitHub Actions

```yaml
- name: Trigger deployment
  run: |
    gh api repos/AitorPo/ci-cd-orchestration/dispatches \
      -f event_type=service-deploy \
      -f client_payload[service]=${{ github.event.repository.name }} \
      -f client_payload[domain]=${{ vars.DOMAIN }} \
      -f client_payload[port]=${{ vars.PORT }}
  env:
    GH_TOKEN: ${{ secrets.DEPLOY_ORCHESTRATOR_TOKEN }}
```

## Service Requirements

Your service repository must have:

1. **`compose.yml`** at repository root
2. **Health endpoint** that returns 200 OK (default: `/healthz`)
3. **Port exposed** matching the `port` value

## Environment Variables

By default every repository secret is written into each service drop-in (newlines escaped). To scope per service, set orchestrator repo secret `SERVICE_ENVS_JSON`:

```json
{
  "my-service": "DATABASE_URL=postgres://...\nAPI_KEY=secret123"
}
```

See full guide: [docs/SERVICE_REPO_QUICKSTART.md](SERVICE_REPO_QUICKSTART.md). If `SERVICE_ENVS_JSON` is empty/unset, all repository secrets are written into each service drop-in.
