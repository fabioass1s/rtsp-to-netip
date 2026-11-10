"""Inicialização do conversor: um servidor NetIP por dispositivo + busca automática."""

from __future__ import annotations

import asyncio
import logging
import signal

from .config import AppConfig
from .device import VirtualDevice
from .discovery import start_discovery
from .server import NetIPServer

log = logging.getLogger(__name__)


def _exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    # No Windows o asyncio loga um traceback quando o cliente derruba a conexão (RST); é inofensivo.
    if isinstance(context.get("exception"), (ConnectionResetError, ConnectionAbortedError)):
        return
    loop.default_exception_handler(context)


async def run_app(cfg: AppConfig) -> None:
    asyncio.get_running_loop().set_exception_handler(_exception_handler)
    devices = [VirtualDevice(dev, cfg.netip) for dev in cfg.devices]
    servers = [NetIPServer(dev, cfg.netip) for dev in devices]
    for server in servers:
        await server.start()

    discovery = None
    if cfg.netip.discovery:
        try:
            discovery = await start_discovery(devices, cfg.netip)
        except OSError as exc:
            log.warning("busca automática desativada (UDP 34569 indisponível): %s", exc)

    for dev in devices:
        for ch in dev.channels:
            if ch.onvif:
                asyncio.create_task(ch.prepare())
            if ch.main.always_on:
                ch.main.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: Ctrl+C gera KeyboardInterrupt
    log.info("conversor pronto (usuário NetIP: '%s')", cfg.netip.username)
    try:
        await stop.wait()
    finally:
        log.info("encerrando...")
        if discovery:
            discovery.close()
        for server in servers:
            await server.stop()
        for dev in devices:
            for ch in dev.channels:
                ch.main.stop()
                ch.sub.stop()
