import os
import json
import time
import random
import logging
from pathlib import Path
from collections import deque

import yaml
import httpx
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.yaml")
STATE_PATH = os.environ.get("STATE_PATH", "state.json")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
HISTORY_SIZE = int(os.environ.get("HISTORY_SIZE", "50"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("ewelink-proxy")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class WebhookAdd(BaseModel):
    url: str

class DeviceAdd(BaseModel):
    name: str
    strategy: str = "round-robin"

class StrategyUpdate(BaseModel):
    strategy: str


# ---------------------------------------------------------------------------
# Config Manager (config.yaml — source of truth)
# ---------------------------------------------------------------------------

class ConfigManager:
    """Loads, hot-reloads, and writes back config.yaml."""

    def __init__(self, path: str):
        self.path = Path(path)
        self._mtime: float = 0.0
        self._raw: dict = {}
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            logger.warning(f"Config file {self.path} not found.")
            self._raw = {"devices": {}}
            return
        stat = self.path.stat()
        if stat.st_mtime == self._mtime and self._raw:
            return
        self._mtime = stat.st_mtime
        raw = yaml.safe_load(self.path.read_text())
        if not raw:
            raw = {"devices": {}}
        if "devices" not in raw:
            raw["devices"] = {}
        self._raw = raw
        logger.info(f"Config reloaded: {len(raw.get('devices', {}))} device(s).")

    def maybe_reload(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_mtime != self._mtime:
                self.reload()
        except Exception:
            pass

    def save(self) -> None:
        """Write config back to YAML file."""
        with open(self.path, "w") as f:
            yaml.dump(self._raw, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        self._mtime = self.path.stat().st_mtime
        logger.info("Config saved to disk.")

    @property
    def devices(self) -> dict:
        self.maybe_reload()
        return self._raw.get("devices", {})

    def get_device(self, name: str) -> dict | None:
        self.maybe_reload()
        return self._raw.get("devices", {}).get(name)

    def add_device(self, name: str, strategy: str = "round-robin") -> dict:
        self.maybe_reload()
        if name in self._raw.get("devices", {}):
            raise ValueError(f"Device '{name}' already exists")
        if "devices" not in self._raw:
            self._raw["devices"] = {}
        self._raw["devices"][name] = {"strategy": strategy, "actions": {}}
        self.save()
        return self._raw["devices"][name]

    def delete_device(self, name: str) -> bool:
        self.maybe_reload()
        if name not in self._raw.get("devices", {}):
            return False
        del self._raw["devices"][name]
        self.save()
        return True

    def update_strategy(self, name: str, strategy: str) -> dict:
        self.maybe_reload()
        if name not in self._raw.get("devices", {}):
            raise KeyError(f"Device '{name}' not found")
        self._raw["devices"][name]["strategy"] = strategy
        self.save()
        return self._raw["devices"][name]

    def add_webhook(self, device: str, action: str, url: str) -> dict:
        self.maybe_reload()
        if device not in self._raw.get("devices", {}):
            raise KeyError(f"Device '{device}' not found")
        dev = self._raw["devices"][device]
        if "actions" not in dev:
            dev["actions"] = {}
        # normalize YAML booleans
        act_key = action
        if action == "on":
            act_key = "on"  # keep as string — yaml.dump with sort_keys=False preserves quoting? No.
        if act_key not in dev["actions"]:
            dev["actions"][act_key] = []
        if url in dev["actions"][act_key]:
            raise ValueError(f"Webhook URL already exists")
        dev["actions"][act_key].append(url)
        self.save()
        return dev

    def remove_webhook(self, device: str, action: str, url: str) -> dict:
        self.maybe_reload()
        if device not in self._raw.get("devices", {}):
            raise KeyError(f"Device '{device}' not found")
        dev = self._raw["devices"][device]
        actions = dev.get("actions", {})
        if action not in actions:
            raise KeyError(f"Action '{action}' not found")
        if url not in actions[action]:
            raise ValueError(f"Webhook URL not found")
        actions[action].remove(url)
        if not actions[action]:
            del actions[action]
        self.save()
        return dev

    def get_webhook_pool(self, device: str, action: str) -> list[str]:
        """Return the list of webhook URLs for a device/action."""
        dev = self.get_device(device)
        if not dev:
            return []
        actions = dev.get("actions", {})
        # handle YAML bool keys
        for key, val in actions.items():
            normalized = self._normalize_action(key)
            if normalized == action:
                return val if isinstance(val, list) else [val]
        return []

    def _normalize_action(self, key) -> str:
        if key is True:
            return "on"
        if key is False:
            return "off"
        return str(key)

    def normalized_actions(self, device: str) -> dict[str, list[str]]:
        """Return actions with normalized string keys."""
        dev = self.get_device(device)
        if not dev:
            return {}
        result = {}
        for key, val in dev.get("actions", {}).items():
            norm = self._normalize_action(key)
            if isinstance(val, str):
                val = [val]
            result[norm] = list(val)
        return result


# ---------------------------------------------------------------------------
# State Manager (state.json — round-robin persistence + history)
# ---------------------------------------------------------------------------

class StateManager:
    """Persists round-robin indices and trigger history to state.json."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.rr_indices: dict[str, dict[str, int]] = {}  # device -> action -> index
        self.history: deque = deque(maxlen=HISTORY_SIZE)
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            self.rr_indices = data.get("rr_indices", {})
            # Don't restore history — it's transient
        except Exception as e:
            logger.warning(f"Could not load state: {e}")

    def save(self) -> None:
        try:
            data = {"rr_indices": self.rr_indices}
            self.path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.error(f"Could not save state: {e}")

    def get_rr_index(self, device: str, action: str) -> int:
        return self.rr_indices.get(device, {}).get(action, 0)

    def advance_rr(self, device: str, action: str, pool_size: int) -> int:
        if device not in self.rr_indices:
            self.rr_indices[device] = {}
        idx = self.rr_indices[device].get(action, 0) % pool_size
        self.rr_indices[device][action] = (idx + 1) % pool_size
        self.save()
        return idx

    def add_history(self, entry: dict) -> None:
        self.history.appendleft(entry)

    def get_history(self) -> list:
        return list(self.history)


# ---------------------------------------------------------------------------
# Selector
# ---------------------------------------------------------------------------

def select_webhook(
    device: str,
    action: str,
    pool: list[str],
    strategy: str,
    state: StateManager,
) -> tuple[str, int]:
    """Returns (url, index_used)."""
    if strategy == "random":
        idx = random.randint(0, len(pool) - 1)
        return pool[idx], idx
    else:
        idx = state.advance_rr(device, action, len(pool))
        return pool[idx], idx


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

config_mgr = ConfigManager(CONFIG_PATH)
state_mgr = StateManager(STATE_PATH)

app = FastAPI(
    title="eWeLink Webhook Proxy",
    description="Load-balancing proxy for eWeLink device webhooks with management UI.",
    version="2.0.0",
)

templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {})


# ---------------------------------------------------------------------------
# API: Devices
# ---------------------------------------------------------------------------

@app.get("/api/devices")
async def list_devices():
    config_mgr.maybe_reload()
    result = {}
    for name in config_mgr.devices:
        dev = config_mgr.get_device(name)
        actions = config_mgr.normalized_actions(name)
        result[name] = {
            "strategy": dev.get("strategy", "round-robin"),
            "actions": {
                a: {
                    "webhooks": urls,
                    "count": len(urls),
                    "next_index": state_mgr.get_rr_index(name, a),
                }
                for a, urls in actions.items()
            },
        }
    return result


@app.post("/api/devices")
async def add_device(dev: DeviceAdd):
    try:
        config_mgr.add_device(dev.name, dev.strategy)
        logger.info(f"Device added: {dev.name}")
        return {"ok": True, "device": dev.name}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.delete("/api/devices/{name}")
async def delete_device(name: str):
    if config_mgr.delete_device(name):
        # clean up state
        if name in state_mgr.rr_indices:
            del state_mgr.rr_indices[name]
            state_mgr.save()
        logger.info(f"Device deleted: {name}")
        return {"ok": True}
    raise HTTPException(status_code=404, detail=f"Device '{name}' not found")


@app.put("/api/devices/{name}/strategy")
async def update_strategy(name: str, body: StrategyUpdate):
    try:
        config_mgr.update_strategy(name, body.strategy)
        return {"ok": True}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---------------------------------------------------------------------------
# API: Webhooks
# ---------------------------------------------------------------------------

@app.post("/api/devices/{device}/webhooks")
async def add_webhook(device: str, body: WebhookAdd):
    # Determine action from URL path or body — we accept action as query param
    # Actually the UI will send action in the body
    pass

@app.post("/api/devices/{device}/{action}/webhooks")
async def add_webhook_action(device: str, action: str, body: WebhookAdd):
    try:
        config_mgr.add_webhook(device, action, body.url)
        logger.info(f"Webhook added: {device}/{action} -> {body.url}")
        return {"ok": True}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.delete("/api/devices/{device}/{action}/webhooks")
async def remove_webhook(device: str, action: str, url: str):
    try:
        config_mgr.remove_webhook(device, action, url)
        logger.info(f"Webhook removed: {device}/{action} <- {url}")
        return {"ok": True}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---------------------------------------------------------------------------
# API: History
# ---------------------------------------------------------------------------

@app.get("/api/history")
async def get_history():
    return {"history": state_mgr.get_history()}


# ---------------------------------------------------------------------------
# Trigger endpoint
# ---------------------------------------------------------------------------

@app.api_route("/trigger/{device}/{action}", methods=["GET", "POST"])
async def trigger(device: str, action: str, request: Request):
    config_mgr.maybe_reload()
    dev = config_mgr.get_device(device)
    if not dev:
        raise HTTPException(status_code=404, detail=f"Device '{device}' not found")

    pool = config_mgr.get_webhook_pool(device, action)
    if not pool:
        raise HTTPException(
            status_code=404,
            detail=f"Action '{action}' not found for device '{device}'",
        )

    strategy = dev.get("strategy", "round-robin")
    webhook_url, idx_used = select_webhook(device, action, pool, strategy, state_mgr)

    method = request.method
    headers = {"User-Agent": "ewelink-proxy/2.0"}
    params = dict(request.query_params)

    ts = time.time()
    logger.info(f"Trigger: {device}/{action} [{strategy}] -> {webhook_url} (index {idx_used})")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if method == "GET":
                resp = await client.get(webhook_url, headers=headers, params=params)
            else:
                body_bytes = await request.body()
                resp = await client.post(webhook_url, headers=headers, params=params, content=body_bytes)

        status = resp.status_code
        logger.info(f"Response: {device}/{action} status={status}")

        entry = {
            "timestamp": ts,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
            "device": device,
            "action": action,
            "strategy": strategy,
            "webhook": webhook_url,
            "index": idx_used,
            "status": status,
            "ok": True,
        }
        state_mgr.add_history(entry)

        return JSONResponse({
            "ok": True,
            "device": device,
            "action": action,
            "webhook": webhook_url,
            "index": idx_used,
            "ewelink_status": status,
        })
    except httpx.TimeoutException:
        logger.error(f"Timeout: {webhook_url}")
        entry = {
            "timestamp": ts,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
            "device": device,
            "action": action,
            "strategy": strategy,
            "webhook": webhook_url,
            "index": idx_used,
            "status": 0,
            "ok": False,
            "error": "timeout",
        }
        state_mgr.add_history(entry)
        return JSONResponse({"ok": False, "error": "timeout"}, status_code=504)
    except Exception as e:
        logger.error(f"Error: {webhook_url}: {e}")
        entry = {
            "timestamp": ts,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
            "device": device,
            "action": action,
            "strategy": strategy,
            "webhook": webhook_url,
            "index": idx_used,
            "status": 0,
            "ok": False,
            "error": str(e),
        }
        state_mgr.add_history(entry)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "devices": len(config_mgr.devices)}
