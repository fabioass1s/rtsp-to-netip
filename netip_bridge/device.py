"""Dispositivo NetIP virtual: canais (câmeras), identidade e respostas de configuração."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
import threading
import time
from datetime import datetime
from typing import Any

from .config import CameraConfig, DeviceConfig, NetIPConfig
from .onvif import OnvifCamera, OnvifError, Profile
from .source import StreamSource

log = logging.getLogger(__name__)

# Resoluções conhecidas pelo SDK Xiongmai: (índice, nome, largura, altura)
RESOLUTIONS = [
    (0, "D1", 704, 576), (1, "HD1", 704, 288), (2, "BCIF", 352, 576), (3, "CIF", 352, 288),
    (4, "QCIF", 176, 144), (5, "VGA", 640, 480), (6, "QVGA", 320, 240), (7, "SVCD", 480, 480),
    (8, "QQVGA", 160, 128), (9, "ND1", 240, 192), (10, "960H", 928, 576), (11, "720P", 1280, 720),
    (12, "960P", 1280, 960), (13, "UXGA", 1600, 1200), (14, "1080P", 1920, 1080), (15, "WUXGA", 1920, 1200),
    (16, "2_5M", 1872, 1408), (17, "3M", 2048, 1536), (18, "5M", 2592, 1944), (19, "1080N", 960, 1080),
    (20, "4M", 2560, 1440), (21, "6M", 3072, 2048), (22, "8M", 3264, 2448), (23, "12M", 4000, 3000),
    (24, "4K", 3840, 2160),
]
COMPRESSION_MASK = 0x88  # H.264 | H.265

# Comando PTZ NetIP -> (pan, tilt, zoom)
PTZ_MOVES = {
    "DirectionUp": (0, 1, 0), "DirectionDown": (0, -1, 0),
    "DirectionLeft": (-1, 0, 0), "DirectionRight": (1, 0, 0),
    "DirectionLeftUp": (-1, 1, 0), "DirectionLeftDown": (-1, -1, 0),
    "DirectionRightUp": (1, 1, 0), "DirectionRightDown": (1, -1, 0),
    "ZoomTile": (0, 0, 1), "ZoomTele": (0, 0, 1), "ZoomWide": (0, 0, -1),
}


def resolution(width: int, height: int) -> tuple[int, str]:
    best = min(RESOLUTIONS, key=lambda r: abs(r[2] - width) + abs(r[3] - height))
    return best[0], best[1]


def ip_hex(ip: str) -> str:
    """IP no formato NetIP: inteiro little-endian em hexadecimal (192.168.1.100 -> 0x6401A8C0)."""
    return "0x%08X" % int.from_bytes(socket.inet_aton(ip), "little")


def stream_kind(value: Any) -> str:
    return "main" if str(value or "Main").lower() in ("main", "mainstream", "0") else "sub"


class Channel:
    def __init__(self, index: int, cfg: CameraConfig, netip: NetIPConfig):
        self.index = index
        self.cfg = cfg
        self.name = cfg.name
        self.onvif = None
        if cfg.onvif:
            o = cfg.onvif
            self.onvif = OnvifCamera(o.host, o.port, o.username, o.password)
        self.profiles: dict[str, Profile | None] = {}
        self._urls = {"main": cfg.main_url, "sub": cfg.sub_url}
        self._resolved = self.onvif is None
        self._lock = threading.Lock()
        self._ptz_lock = asyncio.Lock()
        self._ptz_token: str | None = None

        def source(kind: str) -> StreamSource:
            return StreamSource(
                f"{cfg.name}/{kind}",
                lambda: self.url(kind),
                transport=cfg.rtsp_transport,
                audio=cfg.audio,
                fps=cfg.fps,
                gop_cache=netip.gop_cache,
                idle_timeout=netip.idle_timeout,
                always_on=cfg.always_on and kind == "main",
            )

        self.main = source("main")
        self.sub = source("sub")

    # -------------------------------------------------------------- ONVIF
    def resolve(self) -> None:
        """Consulta a câmera via ONVIF (bloqueante): URLs RTSP, perfis e token PTZ."""
        with self._lock:
            if self._resolved:
                return
            cam = self.onvif
            o = self.cfg.onvif
            cam.connect()
            profiles = cam.get_profiles()
            if not profiles:
                raise OnvifError("a câmera não retornou perfis de vídeo")

            def pick(selector, default):
                if selector is None:
                    return None
                for p in profiles:
                    if p.token == selector or p.name == selector:
                        return p
                if isinstance(selector, int) and 0 <= selector < len(profiles):
                    return profiles[selector]
                return default

            main = pick(o.main_profile, profiles[0])
            sub = pick(o.sub_profile, None)
            if sub is main:
                sub = None
            if not self._urls["main"]:
                self._urls["main"] = cam.get_stream_uri(main.token)
            if not self._urls["sub"] and sub:
                self._urls["sub"] = cam.get_stream_uri(sub.token)
            self.profiles = {"main": main, "sub": sub}
            self._ptz_token = main.token if main.has_ptz else next((p.token for p in profiles if p.has_ptz), main.token)
            self._resolved = True
            log.info(
                "[%s] ONVIF ok: principal=%s %dx%d%s%s",
                self.name, main.token, main.width, main.height,
                f", sub={sub.token} {sub.width}x{sub.height}" if sub else ", sem substream",
                ", PTZ disponível" if cam.ptz_url else "",
            )

    async def prepare(self) -> None:
        if self._resolved:
            return
        try:
            await asyncio.to_thread(self.resolve)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] falha ao consultar ONVIF (%s:%s): %s", self.name, self.cfg.onvif.host, self.cfg.onvif.port, exc)

    def url(self, kind: str) -> str:
        if not self._resolved:
            self.resolve()
        url = self._urls.get(kind) or self._urls["main"]
        if not url:
            raise RuntimeError("nenhuma URL RTSP disponível")
        return url

    @property
    def has_sub(self) -> bool:
        return bool(self._urls["sub"])

    def source(self, kind: str) -> StreamSource:
        return self.sub if kind == "sub" and self.has_sub else self.main

    def describe(self, kind: str) -> tuple[int, int, int, str]:
        """(largura, altura, fps, codec) do stream, usando o melhor dado disponível."""
        info = self.source(kind).info
        if info.width:
            return info.width, info.height, info.fps, info.codec or "h264"
        prof = self.profiles.get(kind) if self.has_sub or kind == "main" else self.profiles.get("main")
        if prof and prof.width:
            codec = "hevc" if re.search(r"265|hevc", prof.encoding, re.I) else "h264"
            return prof.width, prof.height, prof.fps or 25, codec
        return (1920, 1080, 25, "h264") if kind == "main" else (704, 576, 25, "h264")

    # ---------------------------------------------------------------- PTZ
    async def ptz(self, command: str, preset: int, step: int) -> None:
        if not self.onvif or not self.cfg.ptz:
            log.debug("[%s] PTZ ignorado (%s): canal sem ONVIF/PTZ", self.name, command)
            return
        async with self._ptz_lock:  # garante que o "parar" não ultrapasse o "mover"
            try:
                await asyncio.to_thread(self._ptz_sync, command, preset, step)
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] PTZ %s falhou: %s", self.name, command, exc)

    def _ptz_sync(self, command: str, preset: int, step: int) -> None:
        self.resolve()
        cam, token = self.onvif, self._ptz_token
        if command in PTZ_MOVES:
            if preset == -1:  # o NetIP manda Preset=-1 ao soltar o botão
                cam.stop(token)
            else:
                speed = max(0.1, min(1.0, (step or 4) / 8))
                pan, tilt, zoom = PTZ_MOVES[command]
                cam.continuous_move(token, pan * speed, tilt * speed, zoom * speed)
        elif command == "GotoPreset":
            cam.goto_preset(token, preset)
        elif command == "SetPreset":
            cam.set_preset(token, preset)
        elif command == "ClearPreset":
            cam.remove_preset(token, preset)
        else:
            log.info("[%s] comando PTZ não suportado: %s", self.name, command)
            return
        log.debug("[%s] PTZ %s preset=%s step=%s", self.name, command, preset, step)


class VirtualDevice:
    SOFTWARE_VERSION = "V4.03.R11.00000000.10010.040400.0000000"

    def __init__(self, cfg: DeviceConfig, netip: NetIPConfig):
        self.cfg = cfg
        self.netip = netip
        self.port = cfg.port
        self.name = cfg.name
        self.channels = [Channel(i, cam, netip) for i, cam in enumerate(cfg.cameras)]
        seed = hashlib.md5(f"{cfg.name}:{cfg.port}".encode()).digest()
        self.serial = seed.hex()[:16]
        self.mac = "02:" + ":".join(f"{b:02x}" for b in seed[:5])  # MAC administrado localmente
        self.device_type = netip.device_type or ("IPC" if len(self.channels) == 1 else "DVR")
        self._started = time.monotonic()

    def channel(self, index: int) -> Channel | None:
        return self.channels[index] if 0 <= index < len(self.channels) else None

    # ---------------------------------------------------------- respostas
    def login_info(self, session_hex: str) -> dict[str, Any]:
        return {
            "AliveInterval": 20,
            "ChannelNum": len(self.channels),
            "DataUseAES": False,
            "DeviceType ": self.device_type,  # o espaço no nome é igual ao do firmware original
            "ExtraChannel": 0,
            "Ret": 100,
            "SessionID": session_hex,
        }

    def system_info(self) -> dict[str, Any]:
        n = len(self.channels)
        return {
            "AlarmInChannel": 0,
            "AlarmOutChannel": 0,
            "AudioInChannel": n if any(c.cfg.audio for c in self.channels) else 0,
            "BuildTime": "2026-01-01 00:00:00",
            "CombineSwitch": 0,
            "DeviceModel": "NetIP-Bridge",
            "DeviceRunTime": "0x%08X" % int((time.monotonic() - self._started) // 60),
            "DeviceType": 0,
            "DigChannel": 0,
            "EncryptVersion": "Unknown",
            "ExtraChannel": 0,
            "HardWare": "NetIP-Bridge",
            "HardWareVersion": "Unknown",
            "SerialNo": self.serial,
            "SoftWareVersion": self.SOFTWARE_VERSION,
            "TalkInChannel": 0,
            "TalkOutChannel": 0,
            "UpdataTime": "",
            "UpdataType": "0x00000000",
            "VideoInChannel": n,
            "VideoOutChannel": 1,
        }

    def sysinfo(self, name: str) -> Any:
        if name == "SystemInfo":
            return self.system_info()
        if name == "StorageInfo":
            return []
        if name == "WorkState":
            loss = sum(1 << c.index for c in self.channels if not c.main.info.online and c.main.viewers)
            return {
                "AlarmState": {
                    "AlarmIn": "0x00000000", "AlarmOut": "0x00000000", "VideoBlind": "0x00000000",
                    "VideoLoss": "0x%08X" % loss, "VideoMotion": "0x00000000",
                },
                "ChannelState": [{"Bitrate": 0, "Record": False} for _ in self.channels],
            }
        return None

    def net_common(self, ip: str) -> dict[str, Any]:
        mask = self.netip.netmask
        gateway = self.netip.gateway
        if not gateway:
            net = ipaddress.ip_network(f"{ip}/{mask}", strict=False)
            gateway = str(net.network_address + 1)
        return {
            "GateWay": ip_hex(gateway),
            "HostIP": ip_hex(ip),
            "HostName": self.name,
            "HttpPort": 80,
            "MAC": self.mac,
            "MaxBps": 0,
            "MonMode": "TCP",
            "SSLPort": 8443,
            "Submask": ip_hex(mask),
            "TCPMaxConn": 10,
            "TCPPort": self.port,
            "TransferPlan": "Quality",
            "UDPPort": self.port + 1,
            "UseHSDownLoad": False,
        }

    def _encode(self, ch: Channel) -> dict[str, Any]:
        def fmt(kind: str) -> dict[str, Any]:
            w, h, fps, codec = ch.describe(kind)
            return {
                "AudioEnable": ch.cfg.audio,
                "Video": {
                    "BitRate": 4096 if kind == "main" else 512,
                    "BitRateControl": "VBR",
                    "Compression": "H.265" if codec == "hevc" else "H.264",
                    "FPS": fps,
                    "GOP": 2,
                    "Quality": 4,
                    "Resolution": resolution(w, h)[1],
                },
                "VideoEnable": True,
            }

        return {"ExtraFormat": fmt("sub"), "MainFormat": fmt("main")}

    def _widget(self, ch: Channel) -> dict[str, Any]:
        attr = {"BackColor": "0x00000000", "EncodeBlend": False, "FrontColor": "0xF0FFFFFF",
                "PreviewBlend": False, "RelativePos": [6144, 6144, 0, 0]}
        return {
            "ChannelTitle": {"Name": ch.name, "SerialNo": ""},
            "ChannelTitleAttribute": attr,
            "Covers": [],
            "CoversNum": 0,
            "TimeTitleAttribute": dict(attr, RelativePos=[0, 0, 0, 0]),
        }

    def get_config(self, name: str, local_ip: str) -> Any:
        """Valor de uma configuração ("Nome" ou "Nome.[canal]"); None se não existir."""
        match = re.fullmatch(r"(.+?)(?:\.\[(\d+)\])?", name)
        if not match:
            return None
        base, idx = match.group(1), match.group(2)
        per_channel = {
            "Simplify.Encode": self._encode,
            "AVEnc.VideoWidget": self._widget,
        }
        if base in per_channel:
            values = [per_channel[base](ch) for ch in self.channels]
            if idx is None:
                return values
            i = int(idx)
            return values[i] if i < len(values) else None
        if idx is not None:
            return None
        if base == "General.General":
            return {
                "AutoLogout": 0, "FontSize": 24, "IranCalendarEnable": 0, "LocalNo": 0,
                "MachineName": self.name, "OverWrite": "OverWrite", "ScreenAutoShutdown": 10,
                "ScreenSaveTime": 0, "VideoOutPut": "Auto",
            }
        if base == "General.Location":
            dst = {"Day": 1, "Hour": 1, "Minute": 1, "Month": 1, "Week": 0, "Year": 2026}
            return {
                "DSTEnd": dst, "DSTRule": "Off", "DSTStart": dst, "DateFormat": "YYMMDD",
                "DateSeparator": "-", "IranCalendar": 0, "Language": "English", "TimeFormat": "24",
                "VideoFormat": "PAL", "WorkDay": 62,
            }
        if base == "NetWork.NetCommon":
            return self.net_common(self.netip.advertise_ip or local_ip)
        if base == "ChannelTitle":
            return self.channel_titles()
        return None

    def channel_titles(self) -> list[str]:
        return [ch.name for ch in self.channels]

    def ability(self, name: str) -> Any:
        if name == "SystemFunction":
            return {
                "AlarmFunction": {"AlarmConfig": False, "BlindDetect": False, "LossDetect": True, "MotionDetect": False,
                                  "NetAbort": True, "NetAlarm": False, "NetIpConflict": False, "StorageFailure": False,
                                  "StorageLowSpace": False, "StorageNotExist": False, "VideoAnalyze": False},
                "CommFunction": {"CommRS232": False, "CommRS485": False},
                "EncodeFunction": {"CombineStream": False, "DoubleStream": True, "SmartH264": False,
                                   "SnapStream": False, "WaterMark": False},
                "InputMethod": {"NoSupportChinese": True},
                "NetServerFunction": {"NetDHCP": False, "NetDNS": False, "NetNTP": False, "NetRTSP": False,
                                      "NetUPNP": False, "NetWifi": False},
                "OtherFunction": {"SupportShowH265X": True, "SupportTimeZone": True, "TwoWayVoiceTalk": False,
                                  "SupportPTZTour": False, "SupportOSDInfo": False},
                "PreviewFunction": {"Talk": False, "Tour": False},
            }
        if name == "EncodeCapability":
            main_masks, sub_masks, powers = [], [], []
            for ch in self.channels:
                mw, mh, mfps, _ = ch.describe("main")
                sw, sh, _, _ = ch.describe("sub")
                main_masks.append("0x%08X" % (1 << resolution(mw, mh)[0]))
                sub_masks.append("0x%08X" % (1 << resolution(sw, sh)[0]))
                powers.append(mw * mh * mfps)
            audio = any(c.cfg.audio for c in self.channels)
            comp = "0x%08X" % COMPRESSION_MASK
            return {
                "ChannelMaxSetSync": 0,
                "CombEncodeInfo": [{"CompressionMask": "0x00000000", "Enable": False, "HaveAudio": False,
                                    "ResolutionMask": "0x00000000", "StreamType": "None"}],
                "Compression": comp,
                "EncodeInfo": [
                    {"CompressionMask": comp, "Enable": True, "HaveAudio": audio,
                     "ResolutionMask": main_masks[0], "StreamType": "MainStream"},
                    {"CompressionMask": comp, "Enable": True, "HaveAudio": audio,
                     "ResolutionMask": sub_masks[0], "StreamType": "ExtraStream"},
                ],
                "ExImageSizePerChannel": sub_masks,
                "ImageSizePerChannel": main_masks,
                "MaxBitrate": 16384,
                "MaxEncodePower": sum(powers),
                "MaxEncodePowerPerChannel": ["0x%08X" % p for p in powers],
            }
        return None

    @staticmethod
    def now_string() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
