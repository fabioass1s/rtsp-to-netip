"""Servidor TCP NetIP: login, sessões, comandos e envio de vídeo ao vivo."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import socket
import time
from dataclasses import dataclass, field
from typing import Any

from . import protocol as p
from .config import NetIPConfig
from .device import VirtualDevice, stream_kind
from .source import StreamSource, Subscriber

log = logging.getLogger(__name__)

IDLE_TIMEOUT = 90.0  # conexão de comando sem keepalive (AliveInterval é 20 s)
MAX_PAYLOAD = 1 << 20

StreamKey = tuple[int, str]  # (canal, "main" | "sub")


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Session:
    id: int
    user: str
    conns: set["Connection"] = field(default_factory=set)
    claims: dict[StreamKey, "Connection"] = field(default_factory=dict)

    @property
    def hex(self) -> str:
        return p.session_hex(self.id)


@dataclass
class MediaStream:
    source: StreamSource
    subscriber: Subscriber
    task: asyncio.Task
    implicit: bool  # iniciado sem "Claim" prévio nesta conexão


class Connection:
    def __init__(self, server: "NetIPServer", reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.server = server
        self.device = server.device
        self.reader = reader
        self.writer = writer
        peer = writer.get_extra_info("peername") or ("?", 0)
        self.peer = f"{peer[0]}:{peer[1]}"
        self.local_ip = (writer.get_extra_info("sockname") or ("0.0.0.0", 0))[0]
        self.session: Session | None = None
        self.media: dict[StreamKey, MediaStream] = {}
        self.media_conn = False  # conexão de mídia (fez Claim): o cliente não manda keepalive nela
        self.closed = False
        self._media_seq = 0

    @property
    def session_id(self) -> int:
        return self.session.id if self.session else 0

    # ------------------------------------------------------------- E/S
    async def run(self) -> None:
        try:
            while True:
                timeout = None if self.media or self.media_conn else IDLE_TIMEOUT
                header = await asyncio.wait_for(self.reader.readexactly(p.HEADER_SIZE), timeout)
                head, _ver, sid, seq, _total, _cur, msg_id, length = p.HEADER.unpack(header)
                if head != p.HEAD_FLAG or length > MAX_PAYLOAD:
                    log.warning("%s: dados inválidos (não é NetIP?), fechando", self.peer)
                    break
                payload = await self.reader.readexactly(length) if length else b""
                await self.handle(msg_id, sid, seq, payload)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("%s: erro inesperado", self.peer)
        finally:
            await self.close()

    async def reply(self, msg_id: int, body: dict[str, Any], seq: int) -> None:
        body.setdefault("SessionID", p.session_hex(self.session_id))
        await self.send(p.pack_json(msg_id, body, self.session_id, seq))

    async def send(self, data: bytes) -> None:
        if self.closed:
            return
        self.writer.write(data)
        await self.writer.drain()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for key in list(self.media):
            self.stop_media(key)
        self.server.connections.discard(self)
        if self.session:
            self.session.conns.discard(self)
            for key, conn in list(self.session.claims.items()):
                if conn is self:
                    del self.session.claims[key]
            if not self.session.conns:
                self.server.sessions.pop(self.session.id, None)
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        log.debug("%s: conexão encerrada", self.peer)

    # -------------------------------------------------------- despacho
    async def handle(self, msg_id: int, header_sid: int, seq: int, payload: bytes) -> None:
        data = p.parse_json(payload)
        if data is None and not payload.strip(b"\x00\r\n\t "):
            data = {}  # comando sem corpo (ex.: 1500 do DVR): ainda espera resposta
        if data is None:
            log.debug("%s: msg %d binária (%d bytes) ignorada", self.peer, msg_id, len(payload))
            return
        name = str(data.get("Name", ""))
        log.debug("%s: <- %d %s", self.peer, msg_id, name)

        if self.session is None and msg_id != p.LOGIN_REQ:
            self._attach_session(data.get("SessionID"), header_sid)
            if self.session is None:
                await self.reply(msg_id + 1, {"Name": name, "Ret": p.RET_NOT_LOGGED_IN}, seq)
                return

        handler = self.HANDLERS.get(msg_id)
        if handler is None:
            log.info("%s: mensagem %d (%s) não implementada, respondendo OK", self.peer, msg_id, name or "-")
            await self.reply(msg_id + 1, {"Name": name, "Ret": p.RET_OK}, seq)
            return
        await handler(self, msg_id, name, data, seq)

    def _attach_session(self, json_sid: Any, header_sid: int) -> None:
        for sid in (p.parse_session(json_sid), header_sid):
            session = self.server.sessions.get(sid) if sid else None
            if session:
                self.session = session
                session.conns.add(self)
                return

    # -------------------------------------------------------- comandos
    async def on_login(self, msg_id, name, data, seq):
        user = str(data.get("UserName", ""))
        ret = self.server.check_login(user, str(data.get("PassWord", "")))
        if ret != p.RET_OK:
            log.warning("%s: login recusado para o usuário '%s'", self.peer, user)
            await asyncio.sleep(1.0)
            await self.reply(p.LOGIN_RSP, {"Ret": ret}, seq)
            return
        if self.session:
            self.session.conns.discard(self)
        self.session = self.server.new_session(user)
        self.session.conns.add(self)
        log.info("%s: login de '%s' (sessão %s)", self.peer, user, self.session.hex)
        await self.reply(p.LOGIN_RSP, self.device.login_info(self.session.hex), seq)

    async def on_logout(self, msg_id, name, data, seq):
        await self.reply(msg_id + 1, {"Name": name, "Ret": p.RET_OK}, seq)
        await self.close()

    async def on_keepalive(self, msg_id, name, data, seq):
        await self.reply(msg_id + 1, {"Name": name or "KeepAlive", "Ret": p.RET_OK}, seq)

    async def on_sysinfo(self, msg_id, name, data, seq):
        value = self.device.sysinfo(name)
        await self._reply_value(msg_id, name, value, seq)

    async def on_config_get(self, msg_id, name, data, seq):
        value = self.device.get_config(name, self.local_ip)
        await self._reply_value(msg_id, name, value, seq)

    async def on_ability(self, msg_id, name, data, seq):
        await self._reply_value(msg_id, name, self.device.ability(name), seq)

    async def on_channel_title_get(self, msg_id, name, data, seq):
        await self._reply_value(msg_id, name or "ChannelTitle", self.device.channel_titles(), seq)

    async def on_config_set(self, msg_id, name, data, seq):
        log.info("%s: configuração '%s' recebida e ignorada (somente leitura)", self.peer, name)
        log.debug("%s: conteúdo: %s", self.peer, json.dumps(data.get(name), ensure_ascii=False)[:1000])
        await self.reply(msg_id + 1, {"Name": name, "Ret": p.RET_OK}, seq)

    async def on_time_query(self, msg_id, name, data, seq):
        await self.reply(msg_id + 1, {"Name": name, name or "OPTimeQuery": self.device.now_string(), "Ret": p.RET_OK}, seq)

    async def _reply_value(self, msg_id: int, name: str, value: Any, seq: int) -> None:
        if value is None:
            log.info("%s: '%s' (msg %d) não disponível", self.peer, name, msg_id)
            await self.reply(msg_id + 1, {"Name": name, "Ret": p.RET_CONFIG_NOT_EXIST}, seq)
        else:
            await self.reply(msg_id + 1, {"Name": name, name: value, "Ret": p.RET_OK}, seq)

    async def on_ptz(self, msg_id, name, data, seq):
        body = data.get("OPPTZControl") or {}
        param = body.get("Parameter") or {}
        channel = self.device.channel(_int(param.get("Channel")))
        if channel:
            command = str(body.get("Command", ""))
            asyncio.create_task(channel.ptz(command, _int(param.get("Preset"), -1), _int(param.get("Step"), 4)))
        await self.reply(msg_id + 1, {"Name": name, "Ret": p.RET_OK if channel else p.RET_ILLEGAL_REQUEST}, seq)

    async def on_snapshot(self, msg_id, name, data, seq):
        param = data.get(name) or data.get("OPSNAP") or {}
        channel = self.device.channel(_int(param.get("Channel")))
        jpeg = None
        if channel:
            try:
                jpeg = await channel.main.snapshot()
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: falha no snapshot: %s", self.peer, exc)
        if jpeg:
            await self.send(p.pack(p.NET_SNAP_RSP, jpeg, self.session_id, seq))
        else:
            await self.reply(p.NET_SNAP_RSP, {"Name": name, "Ret": p.RET_UNKNOWN_ERROR}, seq)

    async def on_monitor(self, msg_id, name, data, seq):
        body = data.get("OPMonitor") or {}
        param = body.get("Parameter") or {}
        action = str(body.get("Action", "")).lower()
        key = (_int(param.get("Channel")), stream_kind(param.get("StreamType")))
        session = self.session
        rsp = {"Name": name or "OPMonitor", "Ret": p.RET_OK}

        if self.device.channel(key[0]) is None:
            rsp["Ret"] = p.RET_ILLEGAL_REQUEST
        elif action == "claim":
            # Esta conexão será o canal de mídia. Se o stream já foi iniciado em
            # outra conexão da mesma sessão sem "Claim", transfere para esta.
            self.media_conn = True
            moved = False
            for conn in list(session.conns):
                stream = conn.media.get(key)
                if conn is not self and stream and stream.implicit:
                    conn.stop_media(key)
                    moved = True
            if moved:
                await self.start_media(key, implicit=False)
            else:
                session.claims[key] = self
        elif action == "start":
            target = session.claims.pop(key, None)
            implicit = target is None or target.closed
            if implicit:
                target = self
            await target.start_media(key, implicit)
            # Com mídia na mesma conexão (python-dvr, go2rtc) o firmware original não
            # responde ao "Start": o próximo pacote já é vídeo. Com conexão de mídia
            # separada (DVR/NVR/CMS), a resposta é obrigatória: sem ela o DVR desiste em ~4 s.
            if target is self:
                return
        elif action == "stop":
            for conn in list(session.conns):
                conn.stop_media(key)
        await self.reply(msg_id + 1, rsp, seq)

    HANDLERS = {
        p.LOGIN_REQ: on_login,
        p.LOGOUT_REQ: on_logout,
        p.KEEPALIVE_REQ: on_keepalive,
        p.SYSINFO_REQ: on_sysinfo,
        p.CONFIG_GET: on_config_get,
        p.DEFAULT_CONFIG_GET: on_config_get,
        p.CONFIG_SET: on_config_set,
        p.CHANNEL_TITLE_GET: on_channel_title_get,
        p.ABILITY_GET: on_ability,
        p.TIMEQUERY_REQ: on_time_query,
        p.PTZ_REQ: on_ptz,
        p.MONITOR_REQ: on_monitor,
        p.MONITOR_CLAIM: on_monitor,
        p.NET_SNAP_REQ: on_snapshot,
    }

    # ----------------------------------------------------------- mídia
    async def start_media(self, key: StreamKey, implicit: bool) -> None:
        if key in self.media or self.closed:
            return
        channel = self.device.channel(key[0])
        await channel.prepare()
        source = channel.source(key[1])
        sub = source.subscribe()
        task = asyncio.create_task(self._pump(key, sub))
        self.media[key] = MediaStream(source, sub, task, implicit)
        log.info("%s: transmitindo canal %d (%s) -> %s", self.peer, key[0], key[1], source.name)

    def stop_media(self, key: StreamKey) -> None:
        stream = self.media.pop(key, None)
        if stream:
            stream.task.cancel()
            stream.source.unsubscribe(stream.subscriber)
            log.info("%s: parou canal %d (%s)", self.peer, key[0], key[1])

    async def _pump(self, key: StreamKey, sub: Subscriber) -> None:
        chunk = self.server.cfg.chunk_size
        frames = sent = 0
        last_report = time.monotonic()
        try:
            while True:
                frame = await sub.queue.get()
                view = memoryview(frame)
                parts = []
                for off in range(0, len(frame), chunk):
                    piece = view[off : off + chunk]
                    parts.append(p.HEADER.pack(p.HEAD_FLAG, 1, self.session_id, self._media_seq, 0, 0, p.MONITOR_DATA, len(piece)))
                    parts.append(piece)
                    self._media_seq = (self._media_seq + 1) & 0xFFFFFFFF
                self.writer.write(b"".join(parts))
                await self.writer.drain()
                frames += 1
                sent += len(frame)
                now = time.monotonic()
                if now - last_report >= 10:
                    log.debug(
                        "%s: canal %d (%s): %d quadros, %d KiB em %.0f s, %d descarte(s) por lentidão",
                        self.peer, key[0], key[1], frames, sent // 1024, now - last_report, sub.dropped,
                    )
                    frames = sent = 0
                    last_report = now
        except (ConnectionError, OSError):
            self.writer.close()
        finally:
            stream = self.media.get(key)
            if stream and stream.subscriber is sub:
                self.stop_media(key)


class NetIPServer:
    def __init__(self, device: VirtualDevice, cfg: NetIPConfig):
        self.device = device
        self.cfg = cfg
        self.sessions: dict[int, Session] = {}
        self.connections: set[Connection] = set()
        self._next_sid = random.randint(1, 0x7FFF)
        self._server: asyncio.base_events.Server | None = None

    def check_login(self, user: str, password: str) -> int:
        if user != self.cfg.username:
            return p.RET_BAD_USER
        if password not in (p.sofia_hash(self.cfg.password), self.cfg.password):
            return p.RET_BAD_PASSWORD
        return p.RET_OK

    def new_session(self, user: str) -> Session:
        sid = self._next_sid
        self._next_sid = self._next_sid % 0x7FFFFFFF + 1
        session = Session(sid, user)
        self.sessions[sid] = session
        return session

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, self.cfg.bind, self.device.port, reuse_address=True)
        log.info(
            "dispositivo NetIP '%s' em %s:%d (%d canal(is): %s)",
            self.device.name, self.cfg.bind, self.device.port, len(self.device.channels),
            ", ".join(ch.name for ch in self.device.channels),
        )

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        conn = Connection(self, reader, writer)
        self.connections.add(conn)
        log.debug("%s: nova conexão na porta %d", conn.peer, self.device.port)
        await conn.run()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for conn in list(self.connections):
            await conn.close()
