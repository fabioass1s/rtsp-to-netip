"""Protocolo NetIP (também chamado DVRIP ou "Sofia"), usado por DVR/NVR/IPC Xiongmai.

Cada mensagem TCP tem um cabeçalho fixo de 20 bytes (little-endian):

    byte  0      0xFF
    byte  1      versão (0 = cliente, 1 = dispositivo)
    bytes 2-3    reservado
    bytes 4-7    ID da sessão
    bytes 8-11   número de sequência
    byte  12     total de pacotes
    byte  13     pacote atual
    bytes 14-15  ID da mensagem
    bytes 16-19  tamanho do corpo

O corpo é um JSON terminado em "\\n\\0" (comandos) ou dados binários
(mídia ao vivo, mensagem 1412).
"""

from __future__ import annotations

import hashlib
import json
import struct
from datetime import datetime
from typing import Any

HEADER = struct.Struct("<BBxxIIBBHI")
HEADER_SIZE = HEADER.size
HEAD_FLAG = 0xFF
TRAILER = b"\x0a\x00"

# IDs de mensagem (requisições; a resposta é sempre id + 1)
LOGIN_REQ = 1000
LOGIN_RSP = 1001
LOGOUT_REQ = 1002
KEEPALIVE_REQ = 1006
SYSINFO_REQ = 1020
CONFIG_SET = 1040
CONFIG_GET = 1042
DEFAULT_CONFIG_GET = 1044
CHANNEL_TITLE_SET = 1046
CHANNEL_TITLE_GET = 1048
ABILITY_GET = 1360
PTZ_REQ = 1400
MONITOR_REQ = 1410
MONITOR_DATA = 1412
MONITOR_CLAIM = 1413
SYSMANAGER_REQ = 1450
TIMEQUERY_REQ = 1452
IPSEARCH_REQ = 1530
IPSEARCH_RSP = 1531
NET_SNAP_REQ = 1560
NET_SNAP_RSP = 1561
SYNC_TIME_REQ = 1590

# Códigos de retorno ("Ret")
RET_OK = 100
RET_UNKNOWN_ERROR = 101
RET_ILLEGAL_REQUEST = 103
RET_NOT_LOGGED_IN = 105
RET_BAD_USER = 106
RET_BAD_PASSWORD = 203
RET_CONFIG_NOT_EXIST = 607

# Quadros de mídia (dentro das mensagens 1412)
FRAME_I = b"\x00\x00\x01\xfc"
FRAME_P = b"\x00\x00\x01\xfd"
FRAME_AUDIO = b"\x00\x00\x01\xfa"
FRAME_INFO = b"\x00\x00\x01\xf9"
CODEC_H264 = 0x02
CODEC_H265 = 0x03
AUDIO_G711A = 0x0E
AUDIO_RATE_8K = 0x02

_HASH_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def sofia_hash(password: str) -> str:
    """Hash de senha usado no login NetIP (MD5 reduzido a 8 caracteres)."""
    digest = hashlib.md5(password.encode("utf-8")).digest()
    return "".join(_HASH_CHARS[(digest[i] + digest[i + 1]) % 62] for i in range(0, 16, 2))


def pack(msg_id: int, payload: bytes = b"", session: int = 0, seq: int = 0, version: int = 1) -> bytes:
    return HEADER.pack(HEAD_FLAG, version, session, seq, 0, 0, msg_id, len(payload)) + payload


def pack_json(msg_id: int, body: dict[str, Any], session: int = 0, seq: int = 0, version: int = 1) -> bytes:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") + TRAILER
    return pack(msg_id, payload, session, seq, version)


def parse_json(payload: bytes) -> dict[str, Any] | None:
    """Decodifica o corpo JSON de uma mensagem; retorna None para corpos binários."""
    text = payload.strip(b"\x00\r\n\t ")
    if not text.startswith(b"{"):
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text.decode("utf-8", "replace"))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def session_hex(session: int) -> str:
    return f"0x{session:08X}"


def parse_session(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value:
        try:
            return int(value, 16) if value.lower().startswith("0x") else int(value)
        except ValueError:
            return None
    return None


def xm_timestamp(when: datetime) -> int:
    return (
        ((when.year - 2000) & 0x3F) << 26
        | when.month << 22
        | when.day << 17
        | when.hour << 12
        | when.minute << 6
        | when.second
    )


def video_iframe(payload: bytes, codec: int, fps: int, width: int, height: int, when: datetime | None = None) -> bytes:
    # Largura/altura vão divididas por 8 em um byte; acima de 2040 px fica 0 e o decodificador usa o SPS.
    w8 = width // 8 if width // 8 <= 0xFF else 0
    h8 = height // 8 if height // 8 <= 0xFF else 0
    ts = xm_timestamp(when or datetime.now())
    return FRAME_I + struct.pack("<BBBBII", codec, max(1, min(fps, 0xFF)), w8, h8, ts, len(payload)) + payload


def video_pframe(payload: bytes) -> bytes:
    return FRAME_P + struct.pack("<I", len(payload)) + payload


def audio_frame(payload: bytes, codec: int = AUDIO_G711A, rate: int = AUDIO_RATE_8K) -> bytes:
    return FRAME_AUDIO + struct.pack("<BBH", codec, rate, len(payload)) + payload
