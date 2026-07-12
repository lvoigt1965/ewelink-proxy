# eWeLink Webhook Proxy

A lightweight load-balancing proxy that distributes webhook triggers across multiple eWeLink scene webhooks for the same device, bypassing per-webhook rate limits. Includes a full management web UI.

## How It Works

```
Your Automation ──> Proxy (single URL) ──> eWeLink Webhook A
                                        ├──> eWeLink Webhook B
                                        └──> eWeLink Webhook C
```

Each call to the proxy selects the next webhook via **round-robin** or **random** strategy, distributing load evenly across your eWeLink scenes.

## Features

- **Management Web UI** — add/remove devices, manage webhook pools, trigger devices directly
- **Round-robin persistence** — state survives restarts via `state.json`
- **Trigger history** — last 50 triggers with timestamps and results
- **Hot-reload config** — edit `config.yaml` directly, changes are picked up automatically
- **Dual write-back** — UI changes are saved to `config.yaml` (human-readable, git-trackable)
- **Dashboard** — live overview of all devices, webhook pools, and next-indices

## Quick Start

### Docker (Coolify)

```bash
docker-compose up -d
```

The proxy will be available on port `8001`. Open `http://<host>:8001/` for the management UI.

### Local Development

```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8001
```

## Management UI

Navigate to `http://<host>:8001/` to access the dashboard.

### Devices Tab

- **Add Device** — name it and choose strategy (round-robin/random)
- **Add Webhook** — paste eWeLink scene webhook URLs per action (on/off/toggle)
- **Trigger** — test any device/action directly from the UI
- **Strategy** — switch between round-robin and random per device
- **Delete** — remove a device and all its webhooks

### History Tab

- Shows last 50 trigger events with timestamp, device, action, HTTP status, and which webhook was selected
- Auto-refreshes every 5 seconds

## Configuration

You can manage everything via the web UI, or edit `config.yaml` directly:

```yaml
devices:
  lamp:
    strategy: round-robin
    actions:
      "on":
        - https://api.ewelink.com/webhook/WEBHOOK_ON_1
        - https://api.ewelink.com/webhook/WEBHOOK_ON_2
      "off":
        - https://api.ewelink.com/webhook/WEBHOOK_OFF_1
        - https://api.ewelink.com/webhook/WEBHOOK_OFF_2
```

> **Note:** Quote action keys like `"on"` and `"off"` — YAML treats bare `on`/`off` as booleans.

The config file is **hot-reloaded** — just save changes, no restart needed.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Management dashboard UI |
| `GET` | `/health` | Health check |
| `GET` | `/api/devices` | List all devices with webhook pools and state |
| `POST` | `/api/devices` | Add a new device |
| `DELETE` | `/api/devices/{name}` | Delete a device |
| `PUT` | `/api/devices/{name}/strategy` | Update device strategy |
| `POST` | `/api/devices/{device}/{action}/webhooks` | Add a webhook to a device/action |
| `DELETE` | `/api/devices/{device}/{action}/webhooks?url=...` | Remove a webhook |
| `GET` | `/api/history` | Recent trigger history |
| `GET/POST` | `/trigger/{device}/{action}` | Trigger a device action |

### Trigger Examples

```bash
# Turn lamp on (round-robin selects next webhook)
curl http://proxy:8001/trigger/lamp/on

# Turn lamp off
curl http://proxy:8001/trigger/lamp/off
```

## Persistence

| Data | Storage | Survives Restart? |
|------|---------|-------------------|
| Device/webhook config | `config.yaml` | ✅ Yes |
| Round-robin indices | `state.json` (or `/data/state.json` in Docker) | ✅ Yes |
| Trigger history | In-memory ring buffer (last 50) | ❌ No (transient) |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_PATH` | `config.yaml` | Path to config file |
| `STATE_PATH` | `state.json` (or `/data/state.json` in Docker) | Path to state file |
| `LOG_LEVEL` | `INFO` | Logging level |
| `HISTORY_SIZE` | `50` | Max trigger history entries in memory |

## Coolify Deployment

1. Connect this repo to Coolify
2. Set build pack to **Dockerfile**
3. Expose port `8000`
4. Mount a persistent volume at `/data` for `state.json`
5. Optionally mount your `config.yaml` or use the UI
6. Deploy

Since this runs on your Tailscale network, no TLS is needed — Tailscale encrypts the wire.
