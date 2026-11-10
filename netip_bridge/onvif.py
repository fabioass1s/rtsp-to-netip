"""Cliente ONVIF mínimo (SOAP sobre HTTP): URLs RTSP, PTZ e descoberta WS-Discovery."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import logging
import os
import re
import socket
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit
from xml.sax.saxutils import escape

import requests
from requests.auth import HTTPDigestAuth

log = logging.getLogger(__name__)

NS_DEVICE = "http://www.onvif.org/ver10/device/wsdl"
NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
NS_PTZ = "http://www.onvif.org/ver20/ptz/wsdl"

_ENVELOPE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tt="http://www.onvif.org/ver10/schema">'
    "<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
)
_SECURITY = (
    '<wsse:Security s:mustUnderstand="1" '
    'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
    'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
    "<wsse:UsernameToken><wsse:Username>{user}</wsse:Username>"
    '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>'
    '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce}</wsse:Nonce>'
    "<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>"
)


class OnvifError(Exception):
    pass


@dataclass
class Profile:
    token: str
    name: str
    encoding: str
    width: int
    height: int
    fps: int
    has_ptz: bool


def _int(text: str | None, default: int = 0) -> int:
    try:
        return int(float(text)) if text else default
    except ValueError:
        return default


def with_credentials(uri: str, username: str, password: str, host: str | None = None) -> str:
    """Insere usuário/senha na URL RTSP (se ainda não houver) e troca o host, se pedido."""
    parts = urlsplit(uri)
    hostname = host or parts.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname + (f":{parts.port}" if parts.port else "")
    if parts.username:
        userinfo = parts.netloc.rsplit("@", 1)[0]
        netloc = f"{userinfo}@{netloc}"
    elif username:
        netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class OnvifCamera:
    def __init__(self, host: str, port: int = 80, username: str = "", password: str = "", timeout: float = 8.0):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout
        self.base = f"http://{host}:{port}"
        self.device_url = f"{self.base}/onvif/device_service"
        self.media_url = self.device_url
        self.ptz_url: str | None = None
        self._offset = dt.timedelta(0)
        self._http = requests.Session()
        self._presets: dict[str, str] | None = None

    # ------------------------------------------------------------------ SOAP
    def _security(self) -> str:
        if not self.username:
            return ""
        nonce = os.urandom(16)
        created = (dt.datetime.now(dt.timezone.utc) + self._offset).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()
        return _SECURITY.format(
            user=escape(self.username),
            digest=base64.b64encode(digest).decode(),
            nonce=base64.b64encode(nonce).decode(),
            created=created,
        )

    def call(self, url: str, body: str, auth: bool = True) -> ET.Element:
        headers = {"Content-Type": "application/soap+xml; charset=utf-8"}

        def post(http_auth=None):
            envelope = _ENVELOPE.format(header=self._security() if auth else "", body=body)
            return self._http.post(url, data=envelope.encode("utf-8"), headers=headers, timeout=self.timeout, auth=http_auth)

        try:
            resp = post()
            if resp.status_code == 401 and auth and self.username:
                # algumas câmeras exigem HTTP Digest além (ou no lugar) do WS-Security
                resp = post(HTTPDigestAuth(self.username, self.password))
        except requests.RequestException as exc:
            raise OnvifError(f"{url}: {exc}") from None

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            raise OnvifError(f"{url}: resposta inválida (HTTP {resp.status_code})") from None
        fault = root.find(".//{*}Fault")
        if fault is not None or resp.status_code >= 400:
            reason = " ".join(t.strip() for t in (fault.itertext() if fault is not None else []) if t.strip())
            raise OnvifError(f"{url}: {reason or f'HTTP {resp.status_code}'}")
        result = root.find("{*}Body")
        if result is None:
            raise OnvifError(f"{url}: resposta SOAP sem Body")
        return result

    def _rebase(self, xaddr: str | None) -> str | None:
        """Usa o caminho do XAddr, mas com o host/porta configurados (evita IPs internos/NAT)."""
        if not xaddr:
            return None
        parts = urlsplit(xaddr.strip().split()[0])
        return urlunsplit(("http", f"{self.host}:{self.port}", parts.path or "/", parts.query, ""))

    # ----------------------------------------------------------- Device/Media
    def sync_time(self) -> None:
        """Ajusta o relógio usado no WS-Security ao relógio da câmera."""
        try:
            body = self.call(self.device_url, f'<GetSystemDateAndTime xmlns="{NS_DEVICE}"/>', auth=False)
        except OnvifError as exc:
            log.debug("GetSystemDateAndTime falhou: %s", exc)
            return
        utc = body.find(".//{*}UTCDateTime")
        if utc is None:
            return
        try:
            cam = dt.datetime(
                _int(utc.findtext("{*}Date/{*}Year")),
                _int(utc.findtext("{*}Date/{*}Month")),
                _int(utc.findtext("{*}Date/{*}Day")),
                _int(utc.findtext("{*}Time/{*}Hour")),
                _int(utc.findtext("{*}Time/{*}Minute")),
                _int(utc.findtext("{*}Time/{*}Second")),
                tzinfo=dt.timezone.utc,
            )
        except ValueError:
            return
        self._offset = cam - dt.datetime.now(dt.timezone.utc)

    def connect(self) -> None:
        self.sync_time()
        body = self.call(self.device_url, f'<GetCapabilities xmlns="{NS_DEVICE}"><Category>All</Category></GetCapabilities>')
        self.media_url = self._rebase(body.findtext(".//{*}Capabilities/{*}Media/{*}XAddr")) or self.device_url
        self.ptz_url = self._rebase(body.findtext(".//{*}Capabilities/{*}PTZ/{*}XAddr"))

    def get_profiles(self) -> list[Profile]:
        body = self.call(self.media_url, f'<GetProfiles xmlns="{NS_MEDIA}"/>')
        profiles = []
        for prof in body.iter():
            if not prof.tag.endswith("}Profiles"):
                continue
            vec = prof.find("{*}VideoEncoderConfiguration")
            if vec is None:
                continue
            profiles.append(
                Profile(
                    token=prof.get("token", ""),
                    name=prof.findtext("{*}Name", ""),
                    encoding=vec.findtext("{*}Encoding", ""),
                    width=_int(vec.findtext("{*}Resolution/{*}Width")),
                    height=_int(vec.findtext("{*}Resolution/{*}Height")),
                    fps=_int(vec.findtext("{*}RateControl/{*}FrameRateLimit")),
                    has_ptz=prof.find("{*}PTZConfiguration") is not None,
                )
            )
        return profiles

    def get_stream_uri(self, token: str) -> str:
        body = self.call(
            self.media_url,
            f'<GetStreamUri xmlns="{NS_MEDIA}"><StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>'
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></StreamSetup>"
            f"<ProfileToken>{escape(token)}</ProfileToken></GetStreamUri>",
        )
        uri = body.findtext(".//{*}MediaUri/{*}Uri")
        if not uri:
            raise OnvifError(f"GetStreamUri sem URI para o perfil {token}")
        return with_credentials(uri.strip(), self.username, self.password, host=self.host)

    # -------------------------------------------------------------------- PTZ
    def _ptz(self, body: str) -> None:
        if not self.ptz_url:
            raise OnvifError("a câmera não anunciou serviço PTZ")
        self.call(self.ptz_url, body)

    def continuous_move(self, token: str, pan: float, tilt: float, zoom: float) -> None:
        velocity = ""
        if pan or tilt:
            velocity += f'<tt:PanTilt x="{pan:.2f}" y="{tilt:.2f}"/>'
        if zoom:
            velocity += f'<tt:Zoom x="{zoom:.2f}"/>'
        self._ptz(
            f'<ContinuousMove xmlns="{NS_PTZ}"><ProfileToken>{escape(token)}</ProfileToken>'
            f"<Velocity>{velocity}</Velocity></ContinuousMove>"
        )

    def stop(self, token: str) -> None:
        self._ptz(
            f'<Stop xmlns="{NS_PTZ}"><ProfileToken>{escape(token)}</ProfileToken>'
            "<PanTilt>true</PanTilt><Zoom>true</Zoom></Stop>"
        )

    def _preset_token(self, token: str, number: int) -> str:
        if self._presets is None:
            self._presets = {}
            try:
                body = self.call(self.ptz_url, f'<GetPresets xmlns="{NS_PTZ}"><ProfileToken>{escape(token)}</ProfileToken></GetPresets>')
                for preset in body.iter():
                    if preset.tag.endswith("}Preset") and preset.get("token"):
                        self._presets[preset.get("token")] = preset.findtext("{*}Name", "")
            except OnvifError as exc:
                log.debug("GetPresets falhou: %s", exc)
        wanted = str(number)
        for ptoken, name in self._presets.items():
            if ptoken == wanted or name == wanted or re.search(rf"(?<!\d){number}$", ptoken or ""):
                return ptoken
        return wanted

    def goto_preset(self, token: str, number: int) -> None:
        preset = self._preset_token(token, number)
        self._ptz(
            f'<GotoPreset xmlns="{NS_PTZ}"><ProfileToken>{escape(token)}</ProfileToken>'
            f"<PresetToken>{escape(preset)}</PresetToken></GotoPreset>"
        )

    def set_preset(self, token: str, number: int) -> None:
        preset = self._preset_token(token, number)
        head = f'<SetPreset xmlns="{NS_PTZ}"><ProfileToken>{escape(token)}</ProfileToken><PresetName>{number}</PresetName>'
        try:
            self._ptz(f"{head}<PresetToken>{escape(preset)}</PresetToken></SetPreset>")
        except OnvifError:
            self._ptz(f"{head}</SetPreset>")  # preset novo: a câmera escolhe o token
        self._presets = None

    def remove_preset(self, token: str, number: int) -> None:
        preset = self._preset_token(token, number)
        self._ptz(
            f'<RemovePreset xmlns="{NS_PTZ}"><ProfileToken>{escape(token)}</ProfileToken>'
            f"<PresetToken>{escape(preset)}</PresetToken></RemovePreset>"
        )
        self._presets = None


# ------------------------------------------------------------- WS-Discovery
_PROBE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
    'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    "<e:Header><w:MessageID>uuid:{msg_id}</w:MessageID>"
    "<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
    "<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>"
    "<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>"
)


def discover(timeout: float = 3.0) -> list[dict[str, str]]:
    """Procura câmeras ONVIF na rede local via multicast WS-Discovery."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    sock.sendto(_PROBE.format(msg_id=uuid.uuid4()).encode(), ("239.255.255.250", 3702))
    found: dict[str, dict[str, str]] = {}
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                root = ET.fromstring(data)
            except ET.ParseError:
                continue
            xaddrs = (root.findtext(".//{*}XAddrs") or "").split()
            scopes = (root.findtext(".//{*}Scopes") or "").split()
            info = {"ip": addr[0], "xaddr": xaddrs[0] if xaddrs else ""}
            for scope in scopes:
                for key in ("name", "hardware"):
                    prefix = f"onvif://www.onvif.org/{key}/"
                    if scope.startswith(prefix):
                        info[key] = requests.utils.unquote(scope[len(prefix):])
            found[info["xaddr"] or addr[0]] = info
    finally:
        sock.close()
    return list(found.values())
