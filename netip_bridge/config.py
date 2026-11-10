"""Leitura e validação do arquivo de configuração (YAML)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


@dataclass
class OnvifConfig:
    host: str
    port: int = 80
    username: str = ""
    password: str = ""
    main_profile: int | str = 0  # índice ou token do perfil ONVIF do stream principal
    sub_profile: int | str | None = 1  # idem para o substream (None = não usar)


@dataclass
class CameraConfig:
    name: str
    main_url: str | None = None
    sub_url: str | None = None
    onvif: OnvifConfig | None = None
    port: int | None = None
    audio: bool = False
    fps: int | None = None
    rtsp_transport: str = "tcp"
    ptz: bool = True
    always_on: bool = False


@dataclass
class NetIPConfig:
    bind: str = "0.0.0.0"
    base_port: int = 34567
    username: str = "admin"
    password: str = ""
    discovery: bool = True
    advertise_ip: str | None = None
    netmask: str = "255.255.255.0"
    gateway: str | None = None
    device_type: str | None = None
    chunk_size: int = 8192
    idle_timeout: float = 20.0
    gop_cache: bool = True


@dataclass
class DeviceConfig:
    port: int
    name: str
    cameras: list[CameraConfig] = field(default_factory=list)


@dataclass
class AppConfig:
    netip: NetIPConfig
    devices: list[DeviceConfig]
    log_level: str = "INFO"


def _build(cls, data: dict[str, Any], where: str):
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        log.warning("%s: chaves desconhecidas ignoradas: %s", where, ", ".join(sorted(unknown)))
    try:
        return cls(**{k: v for k, v in data.items() if k in known})
    except TypeError as exc:
        raise ConfigError(f"{where}: {exc}") from None


def _camera(raw: Any, index: int) -> CameraConfig:
    where = f"cameras[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: esperado um objeto")
    raw = dict(raw)
    rtsp = raw.pop("rtsp", None) or {}
    if isinstance(rtsp, str):
        rtsp = {"main": rtsp}
    onvif_raw = raw.pop("onvif", None)
    raw.setdefault("name", f"Camera {index + 1}")
    cam: CameraConfig = _build(CameraConfig, raw, where)
    cam.main_url = rtsp.get("main") or None
    cam.sub_url = rtsp.get("sub") or None
    if onvif_raw:
        if not isinstance(onvif_raw, dict) or not onvif_raw.get("host"):
            raise ConfigError(f"{where}.onvif: informe pelo menos 'host'")
        cam.onvif = _build(OnvifConfig, onvif_raw, f"{where}.onvif")
    if not cam.main_url and not cam.onvif:
        raise ConfigError(f"{where} ({cam.name}): informe 'rtsp.main' ou 'onvif.host'")
    if cam.rtsp_transport not in ("tcp", "udp", "http", "auto"):
        raise ConfigError(f"{where}: rtsp_transport deve ser tcp, udp, http ou auto")
    return cam


def parse_config(data: dict[str, Any]) -> AppConfig:
    if not isinstance(data, dict):
        raise ConfigError("o arquivo de configuração deve ser um objeto YAML")
    netip: NetIPConfig = _build(NetIPConfig, data.get("netip") or {}, "netip")
    cameras_raw = data.get("cameras") or []
    if not isinstance(cameras_raw, list) or not cameras_raw:
        raise ConfigError("defina ao menos uma câmera em 'cameras'")
    cameras = [_camera(raw, i) for i, raw in enumerate(cameras_raw)]

    # Câmeras sem porta recebem portas sequenciais a partir de base_port.
    # Câmeras com a mesma porta viram canais de um único dispositivo NetIP.
    used = {c.port for c in cameras if c.port}
    next_port = netip.base_port
    for cam in cameras:
        if cam.port is None:
            while next_port in used:
                next_port += 1
            cam.port = next_port
            used.add(next_port)

    devices: dict[int, DeviceConfig] = {}
    for cam in cameras:
        dev = devices.setdefault(cam.port, DeviceConfig(port=cam.port, name=cam.name))
        dev.cameras.append(cam)
    for dev in devices.values():
        if len(dev.cameras) > 1:
            dev.name = f"NetIP Bridge {dev.port}"

    return AppConfig(netip=netip, devices=list(devices.values()), log_level=str(data.get("log_level", "INFO")).upper())


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"arquivo de configuração não encontrado: {path}")
    with path.open(encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"YAML inválido em {path}: {exc}") from None
    return parse_config(data)
