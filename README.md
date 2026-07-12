# eWeLink Webhook Proxy

A lightweight load-balancing proxy that distributes webhook triggers across multiple eWeLink scene webhooks for the same device, bypassing per-webhook rate limits.

## How It Works

```
Your Automation ──> Proxy (single URL) ──> eWeLink Webhook A
                                        ├──> eWeLink Webhook B
                                        └──> eWeLink Webhook C
```

Each call to the proxy selects the next webhook via **round-robin** or **random** strategy, distributing load evenly across your eWeLink scenes.

## Quick Start

### Docker (recommended for Coolify)

```bash
docker-compose up -d
```

The proxy will be available on port `8000`.

### Local Development

```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

## Configuration

Edit `config.yaml` to define your devices and their webhook pools:

```yaml
devices:
  lamp:
    strategy: round-robin   # or: random
    actions:
      on:
        - https://api.ewelink.com/webhook/WEBHOOK_ON_1
        - https://api.ewelink.com/webhook/WEBHOOK_ON_2
      off:
        - https://api.ewelink.com/webhook/WEBHOOK_OFF_1
        - https://api.ewelink.com/webhook/WEBHOOK_OFF_2
```

The config file is **hot-reloaded** — just save changes, no restart needed.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Dashboard UI |
| `GET` | `/health` | Health check |
| `GET` | `/api/devices` | List all configured devices |
| `GET/POST` | `/{device}/{action}` | Trigger a device action |

### Trigger Examples

```bash
# Turn lamp on (round-robin selects next webhook)
curl http://proxy:8000/lamp/on

# Turn lamp off
curl http://proxy:8000/lamp/off

# With query params (forwarded to eWeLink)
curl http://proxy:8000/lamp/on?param=value
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_PATH` | `config.yaml` | Path to config file |
| `LOG_LEVEL` | `INFO` | Logging level |

## Coolify Deployment

1. Connect this repo to Coolify
2. Set build pack to **Dockerfile**
3. Mount `config.yaml` as a volume or use Coolify's file editor
4. Expose port `8000`
5. Deploy

Since this runs on your Tailscale network, no TLS is needed — Tailscale encrypts the wire.
