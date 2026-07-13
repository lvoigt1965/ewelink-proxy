import os
import json
import time
import random
import logging
from pathlib import Path
from collections import deque

import yaml
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.yaml")
STATE_PATH = os.environ.get("STATE_PATH", "state.json")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
HISTORY_SIZE = int(os.environ.get("HISTORY_SIZE", "50"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("ewelink-proxy")


class WebhookAdd(BaseModel):
    url: str

class DeviceAdd(BaseModel):
    name: str
    strategy: str = "round-robin"

class StrategyUpdate(BaseModel):
    strategy: str


class ConfigManager:
    def __init__(self, path: str):
        self.path = Path(path)
        self._mtime: float = 0.0
        self._raw: dict = {}
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
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
        self._raw["devices"][name] = {"strategy": strategy, "webhooks": []}
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

    def add_webhook(self, device: str, url: str) -> dict:
        self.maybe_reload()
        if device not in self._raw.get("devices", {}):
            raise KeyError(f"Device '{device}' not found")
        dev = self._raw["devices"][device]
        if "webhooks" not in dev:
            dev["webhooks"] = []
        if url in dev["webhooks"]:
            raise ValueError(f"Webhook URL already exists")
        dev["webhooks"].append(url)
        self.save()
        return dev

    def remove_webhook(self, device: str, url: str) -> dict:
        self.maybe_reload()
        if device not in self._raw.get("devices", {}):
            raise KeyError(f"Device '{device}' not found")
        dev = self._raw["devices"][device]
        webhooks = dev.get("webhooks", [])
        if url not in webhooks:
            raise ValueError(f"Webhook URL not found")
        webhooks.remove(url)
        self.save()
        return dev

    def get_webhooks(self, device: str) -> list[str]:
        dev = self.get_device(device)
        if not dev:
            return []
        return dev.get("webhooks", [])


class StateManager:
    def __init__(self, path: str):
        self.path = Path(path)
        self.rr_indices: dict[str, int] = {}
        self.history: deque = deque(maxlen=HISTORY_SIZE)
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            self.rr_indices = data.get("rr_indices", {})
        except Exception as e:
            logger.warning(f"Could not load state: {e}")

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps({"rr_indices": self.rr_indices}, indent=2))
        except Exception as e:
            logger.error(f"Could not save state: {e}")

    def get_rr_index(self, device: str) -> int:
        return self.rr_indices.get(device, 0)

    def advance_rr(self, device: str, pool_size: int) -> int:
        idx = self.rr_indices.get(device, 0) % pool_size
        self.rr_indices[device] = (idx + 1) % pool_size
        self.save()
        return idx

    def add_history(self, entry: dict) -> None:
        self.history.appendleft(entry)

    def get_history(self) -> list:
        return list(self.history)


def select_webhook(device: str, pool: list[str], strategy: str, state: StateManager) -> tuple[str, int]:
    if strategy == "random":
        idx = random.randint(0, len(pool) - 1)
        return pool[idx], idx
    else:
        idx = state.advance_rr(device, len(pool))
        return pool[idx], idx


config_mgr = ConfigManager(CONFIG_PATH)
state_mgr = StateManager(STATE_PATH)

app = FastAPI(title="eWeLink Webhook Proxy", version="3.0.0")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {})


@app.get("/health")
async def health():
    return {"status": "ok", "devices": len(config_mgr.devices)}


@app.get("/api/devices")
async def list_devices():
    config_mgr.maybe_reload()
    result = {}
    for name in config_mgr.devices:
        dev = config_mgr.get_device(name)
        hooks = dev.get("webhooks", [])
        result[name] = {
            "strategy": dev.get("strategy", "round-robin"),
            "webhooks": hooks,
            "count": len(hooks),
            "next_index": state_mgr.get_rr_index(name),
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


@app.post("/api/devices/{device}/webhooks")
async def add_webhook(device: str, body: WebhookAdd):
    try:
        config_mgr.add_webhook(device, body.url)
        logger.info(f"Webhook added: {device} -> {body.url}")
        return {"ok": True}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.delete("/api/devices/{device}/webhooks")
async def remove_webhook(device: str, url: str):
    try:
        config_mgr.remove_webhook(device, url)
        logger.info(f"Webhook removed: {device} <- {url}")
        return {"ok": True}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/history")
async def get_history():
    return {"history": state_mgr.get_history()}


@app.api_route("/trigger/{device}", methods=["GET", "POST"])
async def trigger(device: str, request: Request):
    config_mgr.maybe_reload()
    dev = config_mgr.get_device(device)
    if not dev:
        raise HTTPException(status_code=404, detail=f"Device '{device}' not found")

    pool = config_mgr.get_webhooks(device)
    if not pool:
        raise HTTPException(status_code=404, detail=f"No webhooks configured for device '{device}'")

    strategy = dev.get("strategy", "round-robin")
    webhook_url, idx_used = select_webhook(device, pool, strategy, state_mgr)

    method = request.method
    headers = {}

    ts = time.time()
    logger.info(f"Trigger: {device} [{strategy}] -> {webhook_url} (index {idx_used})")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if method == "GET":
                resp = await client.get(webhook_url, headers=headers)
            else:
                body_bytes = await request.body()
                resp = await client.post(webhook_url, headers=headers, content=body_bytes)

        status = resp.status_code
        ewelink_body = resp.text[:500] if resp.text else ""
        logger.info(f"Response: {device} status={status} body={ewelink_body}")

        entry = {
            "timestamp": ts,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
            "device": device,
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
            "webhook": webhook_url,
            "index": idx_used,
            "ewelink_status": status,
            "ewelink_response": ewelink_body,
        })
    except httpx.TimeoutException:
        logger.error(f"Timeout: {webhook_url}")
        entry = {
            "timestamp": ts,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
            "device": device,
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
            "strategy": strategy,
            "webhook": webhook_url,
            "index": idx_used,
            "status": 0,
            "ok": False,
            "error": str(e),
        }
        state_mgr.add_history(entry)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
