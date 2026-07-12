import os
import sys
import json
import time
import random
import asyncio
import logging
from pathlib import Path
from itertools import cycle

import yaml
import httpx
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.yaml")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("ewelink-proxy")


class DeviceConfig:
    """Holds webhook pool and selection state for a single device."""

    def __init__(self, name: str, actions: dict, strategy: str = "round-robin"):
        self.name = name
        self.strategy = strategy
        # actions: { "on": [url1, url2, ...], "off": [url3, url4, ...] }
        self.actions = actions
        # round-robin state per action
        self._rr_state: dict[str, int] = {a: 0 for a in actions}

    def pick(self, action: str) -> str | None:
        """Select the next webhook URL for the given action."""
        pool = self.actions.get(action)
        if not pool:
            return None

        if self.strategy == "random":
            return random.choice(pool)

        # round-robin
        idx = self._rr_state.get(action, 0) % len(pool)
        self._rr_state[action] = (idx + 1) % len(pool)
        return pool[idx]

    def stats(self) -> dict:
        return {
            "strategy": self.strategy,
            "actions": {
                a: {"webhooks": len(urls), "next_index": self._rr_state.get(a, 0)}
                for a, urls in self.actions.items()
            },
        }


class ConfigManager:
    """Loads and hot-reloads the YAML config file."""

    def __init__(self, path: str):
        self.path = Path(path)
        self._mtime: float = 0.0
        self.devices: dict[str, DeviceConfig] = {}
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            logger.warning(f"Config file {self.path} not found – starting with no devices.")
            self.devices = {}
            return

        stat = self.path.stat()
        if stat.st_mtime == self._mtime:
            return  # unchanged

        self._mtime = stat.st_mtime
        raw = yaml.safe_load(self.path.read_text())
        if not raw or "devices" not in raw:
            logger.warning("Config file has no 'devices' key.")
            self.devices = {}
            return

        devices = {}
        for dev_name, dev_cfg in raw["devices"].items():
            strategy = dev_cfg.get("strategy", "round-robin")
            actions = {}
            for action_name, hooks in dev_cfg.get("actions", {}).items():
                # YAML treats bare on/off as booleans — normalize back
                if action_name is True:
                    action_name = "on"
                elif action_name is False:
                    action_name = "off"
                action_name = str(action_name)
                if isinstance(hooks, str):
                    hooks = [hooks]
                actions[action_name] = list(hooks)
            # also support flat webhooks list (single action "trigger")
            if not actions and "webhooks" in dev_cfg:
                wh = dev_cfg["webhooks"]
                if isinstance(wh, str):
                    wh = [wh]
                actions["trigger"] = list(wh)
            devices[dev_name] = DeviceConfig(dev_name, actions, strategy)
            logger.info(
                f"Loaded device '{dev_name}': {sum(len(v) for v in actions.values())} webhook(s), strategy={strategy}"
            )

        self.devices = devices
        logger.info(f"Config reloaded: {len(devices)} device(s).")

    def maybe_reload(self) -> None:
        """Reload if the file has been modified."""
        try:
            if self.path.exists() and self.path.stat().st_mtime != self._mtime:
                self.reload()
        except Exception:
            pass

    def get_device(self, name: str) -> DeviceConfig | None:
        self.maybe_reload()
        return self.devices.get(name)


config_mgr = ConfigManager(CONFIG_PATH)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="eWeLink Webhook Proxy",
    description="Load-balancing proxy that distributes requests across multiple eWeLink webhooks.",
    version="1.0.0",
)

templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    config_mgr.maybe_reload()
    devices = []
    for name, dev in config_mgr.devices.items():
        for action, hooks in dev.actions.items():
            devices.append({
                "device": name,
                "action": action,
                "count": len(hooks),
                "strategy": dev.strategy,
                "next_index": dev._rr_state.get(action, 0),
            })
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "devices": devices,
        "total_devices": len(config_mgr.devices),
    })


@app.get("/health")
async def health():
    return {"status": "ok", "devices": len(config_mgr.devices)}


@app.get("/api/devices")
async def list_devices():
    config_mgr.maybe_reload()
    return {
        name: dev.stats()
        for name, dev in config_mgr.devices.items()
    }


@app.api_route("/{device}/{action}", methods=["GET", "POST"])
async def trigger(device: str, action: str, request: Request):
    """
    Trigger a device action.

    Selects the next webhook via round-robin or random and forwards the request.
    The proxy responds quickly after triggering the eWeLink webhook.
    """
    dev = config_mgr.get_device(device)
    if not dev:
        raise HTTPException(status_code=404, detail=f"Device '{device}' not found")

    webhook_url = dev.pick(action)
    if not webhook_url:
        raise HTTPException(
            status_code=404,
            detail=f"Action '{action}' not found for device '{device}'",
        )

    # Forward to eWeLink webhook
    method = request.method
    headers = {"User-Agent": "ewelink-proxy/1.0"}
    # forward query params
    params = dict(request.query_params)

    logger.info(
        f"Trigger: device={device} action={action} strategy={dev.strategy} "
        f"-> {webhook_url}"
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if method == "GET":
                resp = await client.get(webhook_url, headers=headers, params=params)
            else:
                body = await request.body()
                resp = await client.post(webhook_url, headers=headers, params=params, content=body)

        logger.info(
            f"Response: device={device} action={action} status={resp.status_code}"
        )

        return JSONResponse({
            "ok": True,
            "device": device,
            "action": action,
            "webhook": webhook_url,
            "ewelink_status": resp.status_code,
        })
    except httpx.TimeoutException:
        logger.error(f"Timeout triggering {webhook_url}")
        return JSONResponse(
            {"ok": False, "error": "timeout", "webhook": webhook_url},
            status_code=504,
        )
    except Exception as e:
        logger.error(f"Error triggering {webhook_url}: {e}")
        return JSONResponse(
            {"ok": False, "error": str(e), "webhook": webhook_url},
            status_code=502,
        )
