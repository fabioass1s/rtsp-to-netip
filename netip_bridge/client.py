"""Cliente NetIP simples para testes: login, informações, vídeo ao vivo, snapshot e PTZ."""

from __future__ import annotations

import json
import socket
import struct
import time
from pathlib import Path
from typing import Any, Iterator

from . import protocol as p


class NetIPError(Exception):
    pass


class NetIPClient:
    def __init__(self, host: str, port: int = 34567, timeout: float = 10.0):
        self.sock = socket.create_connection((host, port), timeout)
        self.session = 0
        self.seq = 0

    def close(self) -> None:
        self.sock.close()

    def send(self, msg_id: int, body: dict[str, Any]) -> None:
        body.setdefault("SessionID", p.session_hex(self.session))
        payload = json.dumps(body).encode() + p.TRAILER
        self.sock.sendall(p.pack(msg_id, payload, self.session, self.seq, version=0))
        self.seq += 1

    def _read(self, size: int) -> bytes:
        buf = bytearray()
        while len(buf) < size:
            chunk = self.sock.recv(size - len(buf))
            if not chunk:
                raise NetIPError("conexão fechada pelo dispositivo")
            buf += chunk
        return bytes(buf)

    def recv(self) -> tuple[int, bytes]:
        head, _ver, _sid, _seq, _t, _c, msg_id, length = p.HEADER.unpack(self._read(p.HEADER_SIZE))
        if head != p.HEAD_FLAG:
            raise NetIPError("resposta não é NetIP")
        return msg_id, self._read(length) if length else b""

    def request(self, msg_id: int, body: dict[str, Any]) -> dict[str, Any]:
        self.send(msg_id, body)
        while True:
            rsp_id, payload = self.recv()
            if rsp_id == msg_id + 1:
                return p.parse_json(payload) or {}

    def login(self, user: str, password: str) -> dict[str, Any]:
        rsp = self.request(
            p.LOGIN_REQ,
            {"EncryptType": "MD5", "LoginType": "DVRIP-Web", "PassWord": p.sofia_hash(password), "UserName": user},
        )
        if rsp.get("Ret") != p.RET_OK:
            raise NetIPError(f"login falhou (Ret={rsp.get('Ret')})")
        self.session = p.parse_session(rsp.get("SessionID")) or 0
        return rsp

    def get(self, msg_id: int, name: str) -> Any:
        rsp = self.request(msg_id, {"Name": name})
        return rsp.get(name) if rsp.get("Ret") == p.RET_OK else None


def parse_frames(buf: bytearray) -> Iterator[tuple[int, dict[str, Any], bytes]]:
    """Extrai quadros XM completos de `buf` (consome o que foi lido)."""
    while len(buf) >= 8:
        if buf[:3] != b"\x00\x00\x01" or buf[3] not in (0xFC, 0xFD, 0xFA, 0xF9, 0xFE):
            nxt = buf.find(b"\x00\x00\x01", 1)
            del buf[: nxt if nxt > 0 else len(buf)]
            continue
        kind = buf[3]
        meta: dict[str, Any] = {}
        if kind in (0xFC, 0xFE):
            if len(buf) < 16:
                return
            codec, fps, w8, h8, _ts, size = struct.unpack("<BBBBII", buf[4:16])
            meta = {"codec": codec, "fps": fps, "width": w8 * 8, "height": h8 * 8}
            hdr = 16
        elif kind == 0xFD:
            (size,) = struct.unpack("<I", buf[4:8])
            hdr = 8
        else:
            codec, rate, size = struct.unpack("<BBH", buf[4:8])
            meta = {"codec": codec, "rate": rate}
            hdr = 8
        if len(buf) < hdr + size:
            return
        payload = bytes(buf[hdr : hdr + size])
        del buf[: hdr + size]
        yield kind, meta, payload


def run_client(args) -> int:
    cmd = NetIPClient(args.host, args.port)
    info = cmd.login(args.user, args.password)
    print(f"login ok: sessão {info.get('SessionID')}, {info.get('ChannelNum')} canal(is), tipo {info.get('DeviceType ')}")
    sysinfo = cmd.get(p.SYSINFO_REQ, "SystemInfo") or {}
    print(f"SystemInfo: modelo={sysinfo.get('DeviceModel')} serial={sysinfo.get('SerialNo')} canais={sysinfo.get('VideoInChannel')}")
    titles = cmd.get(p.CHANNEL_TITLE_GET, "ChannelTitle")
    print(f"canais: {titles}")
    encode = cmd.get(p.CONFIG_GET, "Simplify.Encode")
    if encode and args.channel < len(encode):
        for kind in ("MainFormat", "ExtraFormat"):
            v = encode[args.channel][kind]["Video"]
            print(f"  {kind}: {v['Compression']} {v['Resolution']} {v['FPS']} fps")

    if args.ptz:
        param = {"Channel": args.channel, "MenuOpts": "Enter", "Pattern": "SetBegin", "Preset": 65535, "Step": 5, "Tour": 0}
        cmd.request(p.PTZ_REQ, {"Name": "OPPTZControl", "OPPTZControl": {"Command": args.ptz, "Parameter": param}})
        time.sleep(1.0)
        param["Preset"] = -1
        cmd.request(p.PTZ_REQ, {"Name": "OPPTZControl", "OPPTZControl": {"Command": args.ptz, "Parameter": param}})
        print(f"PTZ {args.ptz} enviado (1 s)")

    if args.snapshot:
        cmd.send(p.NET_SNAP_REQ, {"Name": "OPSNAP", "OPSNAP": {"Channel": args.channel}})
        while True:
            msg_id, payload = cmd.recv()
            if msg_id == p.NET_SNAP_RSP:
                break
        if payload[:2] == b"\xff\xd8":
            Path(args.snapshot).write_bytes(payload)
            print(f"snapshot salvo em {args.snapshot} ({len(payload)} bytes)")
        else:
            print(f"snapshot falhou: {p.parse_json(payload)}")

    if args.seconds <= 0:
        cmd.close()
        return 0

    params = {
        "Channel": args.channel,
        "CombinMode": "NONE",
        "StreamType": "Main" if args.stream == "main" else "Extra1",
        "TransMode": "TCP",
    }
    claim = {"Name": "OPMonitor", "OPMonitor": {"Action": "Claim", "Parameter": params}}
    start = {"Name": "OPMonitor", "OPMonitor": {"Action": "Start", "Parameter": params}}
    # fluxo dos clientes CMS/NVR (--separate): segunda conexão só para mídia
    media = NetIPClient(args.host, args.port) if args.separate else cmd
    media.session = cmd.session
    rsp = media.request(p.MONITOR_CLAIM, claim)
    if rsp.get("Ret") != p.RET_OK:
        raise NetIPError(f"Claim recusado: {rsp}")
    cmd.send(p.MONITOR_REQ, start)  # o dispositivo não responde ao Start; o vídeo começa direto
    media.sock.settimeout(30.0)  # tolera a câmera de origem reconectando

    out = open(args.out, "wb") if args.out else None
    audio_out = open(f"{args.out}.alaw", "wb") if args.out else None
    stats = {"I": 0, "P": 0, "audio": 0, "bytes": 0}
    first_frame = None
    meta_i: dict[str, Any] = {}
    buf = bytearray()
    started = time.monotonic()
    last_keepalive = started
    try:
        while time.monotonic() - started < args.seconds:
            msg_id, payload = media.recv()
            if msg_id != p.MONITOR_DATA:
                continue
            buf += payload
            for kind, meta, data in parse_frames(buf):
                if first_frame is None:
                    first_frame = time.monotonic() - started
                if kind in (0xFC, 0xFE):
                    stats["I"] += 1
                    meta_i = meta
                elif kind == 0xFD:
                    stats["P"] += 1
                elif kind == 0xFA:
                    stats["audio"] += 1
                    if audio_out:
                        audio_out.write(data)
                    continue
                else:
                    continue
                stats["bytes"] += len(data)
                if out:
                    out.write(data)
            if time.monotonic() - last_keepalive > 10:
                cmd.send(p.KEEPALIVE_REQ, {"Name": "KeepAlive"})
                last_keepalive = time.monotonic()
    finally:
        if out:
            out.close()
            audio_out.close()
        media.close()
        if media is not cmd:
            cmd.close()

    codec = {p.CODEC_H264: "H.264", p.CODEC_H265: "H.265"}.get(meta_i.get("codec"), str(meta_i.get("codec")))
    print(
        f"recebido em {args.seconds:.0f} s: {stats['I']} quadros I, {stats['P']} quadros P, "
        f"{stats['audio']} quadros de áudio, {stats['bytes'] / 1024:.0f} KiB de vídeo"
    )
    if meta_i:
        print(f"vídeo: {codec} {meta_i['width']}x{meta_i['height']} {meta_i['fps']} fps; primeiro quadro em {first_frame:.2f} s")
    if args.out:
        print(f"vídeo salvo em {args.out}" + (f" (áudio G.711A em {args.out}.alaw)" if stats["audio"] else ""))
    return 0 if stats["I"] else 1
