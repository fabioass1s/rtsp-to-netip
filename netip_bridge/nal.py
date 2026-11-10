"""Utilitários para NAL units H.264/H.265 (Annex-B e formato com prefixo de tamanho)."""

from __future__ import annotations

H264 = "h264"
HEVC = "hevc"

START_CODE = b"\x00\x00\x00\x01"
AUD = {H264: 9, HEVC: 35}
PARAM_SETS = {H264: {7, 8}, HEVC: {32, 33, 34}}  # SPS/PPS e VPS/SPS/PPS


def nal_type(nal: bytes, codec: str) -> int:
    return nal[0] & 0x1F if codec == H264 else (nal[0] >> 1) & 0x3F


def is_vcl(nal_t: int, codec: str) -> bool:
    return 1 <= nal_t <= 5 if codec == H264 else nal_t < 32


def is_irap(nal_t: int, codec: str) -> bool:
    return nal_t == 5 if codec == H264 else 16 <= nal_t <= 23


def is_annexb(data: bytes) -> bool:
    return data[:3] == b"\x00\x00\x01" or data[:4] == START_CODE


def split_annexb(data: bytes) -> list[bytes]:
    nals = []
    pos = data.find(b"\x00\x00\x01")
    while pos >= 0:
        start = pos + 3
        nxt = data.find(b"\x00\x00\x01", start)
        # zeros finais pertencem ao próximo start code de 4 bytes
        nal = data[start : nxt if nxt >= 0 else len(data)].rstrip(b"\x00")
        if nal:
            nals.append(nal)
        pos = nxt
    return nals


def split_length_prefixed(data: bytes, length_size: int = 4) -> list[bytes]:
    nals = []
    pos = 0
    while pos + length_size <= len(data):
        size = int.from_bytes(data[pos : pos + length_size], "big")
        pos += length_size
        if size <= 0 or pos + size > len(data):
            break
        nals.append(data[pos : pos + size])
        pos += size
    return nals


def join_annexb(nals: list[bytes]) -> bytes:
    return b"".join(START_CODE + n for n in nals)


def parse_extradata(extradata: bytes | None, codec: str) -> tuple[list[bytes], int | None]:
    """Extrai os parameter sets do extradata.

    Retorna (NALs, tamanho do prefixo). O tamanho é None quando os pacotes vêm em
    Annex-B (caso do RTSP) e 1-4 quando vêm no formato avcC/hvcC.
    """
    if not extradata:
        return [], None
    if is_annexb(extradata):
        return split_annexb(extradata), None
    try:
        if codec == H264 and extradata[0] == 1:
            return _parse_avcc(extradata)
        if codec == HEVC and extradata[0] == 1 and len(extradata) >= 23:
            return _parse_hvcc(extradata)
    except IndexError:
        pass
    return [], None


def _read_sized(data: bytes, pos: int) -> tuple[bytes, int]:
    size = int.from_bytes(data[pos : pos + 2], "big")
    return data[pos + 2 : pos + 2 + size], pos + 2 + size


def _parse_avcc(data: bytes) -> tuple[list[bytes], int]:
    length_size = (data[4] & 0x03) + 1
    nals = []
    pos = 6
    for _ in range(data[5] & 0x1F):
        nal, pos = _read_sized(data, pos)
        nals.append(nal)
    count = data[pos]
    pos += 1
    for _ in range(count):
        nal, pos = _read_sized(data, pos)
        nals.append(nal)
    return [n for n in nals if n], length_size


def _parse_hvcc(data: bytes) -> tuple[list[bytes], int]:
    length_size = (data[21] & 0x03) + 1
    nals = []
    pos = 23
    for _ in range(data[22]):
        count = int.from_bytes(data[pos + 1 : pos + 3], "big")
        pos += 3
        for _ in range(count):
            nal, pos = _read_sized(data, pos)
            nals.append(nal)
    return [n for n in nals if n], length_size
