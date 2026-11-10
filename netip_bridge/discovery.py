"""Resposta à busca de dispositivos NetIP (broadcast UDP na porta 34569, mensagem 1530)."""

from __future__ import annotations

import asyncio
import logging
import socket

from . import protocol as p
from .config import NetIPConfig
from .device import VirtualDevice

log = logging.getLogger(__name__)

DISCOVERY_PORT = 34569


def local_ip_towards(remote: str) -> str:
    """IP local da interface usada para alcançar `remote`."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((remote, DISCOVERY_PORT))
        return sock.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        sock.close()


class DiscoveryResponder(asyncio.DatagramProtocol):
    def __init__(self, devices: list[VirtualDevice], cfg: NetIPConfig):
        self.devices = devices
        self.cfg = cfg
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) < p.HEADER_SIZE:
            return
        head, _ver, _sid, seq, _t, _c, msg_id, _len = p.HEADER.unpack_from(data)
        if head != p.HEAD_FLAG or msg_id != p.IPSEARCH_REQ:
            return
        ip = self.cfg.advertise_ip or local_ip_towards(addr[0])
        log.info("busca NetIP de %s:%d, anunciando %d dispositivo(s) em %s", addr[0], addr[1], len(self.devices), ip)
        for dev in self.devices:
            body = {"NetWork.NetCommon": dev.net_common(ip), "Ret": p.RET_OK, "SessionID": "0x00000000"}
            packet = p.pack_json(p.IPSEARCH_RSP, body, seq=seq)
            for target in {("255.255.255.255", DISCOVERY_PORT), addr}:
                try:
                    self.transport.sendto(packet, target)
                except OSError as exc:
                    log.debug("falha ao responder busca para %s: %s", target, exc)


async def start_discovery(devices: list[VirtualDevice], cfg: NetIPConfig) -> asyncio.DatagramTransport:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", DISCOVERY_PORT))
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: DiscoveryResponder(devices, cfg), sock=sock)
    log.info("busca automática NetIP ativa (UDP %d)", DISCOVERY_PORT)
    return transport
