"""Captura de streams RTSP (PyAV/FFmpeg) e distribuição dos quadros no formato de mídia NetIP.

Cada StreamSource roda a captura numa thread própria e entrega quadros já
empacotados (cabeçalho XM 0x1FC/0x1FD/0x1FA + payload) aos assinantes, que
vivem no loop asyncio. Vários clientes NetIP do mesmo stream compartilham uma
única conexão RTSP com a câmera.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import av

from . import nal, protocol

log = logging.getLogger(__name__)


class UnsupportedStream(Exception):
    pass


def mask_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.password is None:
        return url
    netloc = f"{parts.username}:***@{parts.netloc.rsplit('@', 1)[1]}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def guess_fps(stream) -> int:
    for rate in (stream.average_rate, stream.guessed_rate, getattr(stream.codec_context, "framerate", None)):
        try:
            value = float(rate)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if 1 <= value <= 120:
            return round(value)
    return 25


@dataclass
class StreamInfo:
    codec: str | None = None
    width: int = 0
    height: int = 0
    fps: int = 25
    audio: bool = False
    online: bool = False


class VideoPacker:
    """Converte pacotes H.264/H.265 vindos do FFmpeg em quadros de mídia NetIP."""

    def __init__(self, codec: str, extradata: bytes | None, fps: int, width: int, height: int):
        self.codec = codec
        self.xm_codec = protocol.CODEC_H264 if codec == nal.H264 else protocol.CODEC_H265
        params, self.length_size = nal.parse_extradata(extradata, codec)
        self.params = {nal.nal_type(n, codec): n for n in params}
        self.fps = fps
        self.width = width
        self.height = height
        self._pending: list[bytes] = []

    def pack(self, data: bytes, keyflag: bool) -> tuple[bytes, bool, bytes] | None:
        """Retorna (quadro XM, é quadro-chave, payload Annex-B) ou None se não houver imagem."""
        codec = self.codec
        if self.length_size:
            nals = nal.split_length_prefixed(data, self.length_size)
        else:
            nals = nal.split_annexb(data)
        nals = self._pending + [n for n in nals if nal.nal_type(n, codec) != nal.AUD[codec]]
        self._pending = []
        types = [nal.nal_type(n, codec) for n in nals]
        for n, t in zip(nals, types):
            if t in nal.PARAM_SETS[codec]:
                self.params[t] = n
        if not any(nal.is_vcl(t, codec) for t in types):
            # só SPS/PPS/SEI: guarda para o próximo pacote com imagem
            self._pending = nals[-16:]
            return None

        is_key = keyflag or any(nal.is_irap(t, codec) for t in types)
        if is_key:
            present = set(types)
            missing = [self.params[t] for t in sorted(self.params) if t not in present]
            payload = nal.join_annexb(missing + nals)
            return protocol.video_iframe(payload, self.xm_codec, self.fps, self.width, self.height), True, payload
        return protocol.video_pframe(nal.join_annexb(nals)), False, b""


class AudioTranscoder:
    """Converte o áudio da câmera para G.711 A-law 8 kHz mono, formato de áudio do NetIP."""

    FRAME_BYTES = 320  # 40 ms

    def __init__(self, stream):
        ctx = stream.codec_context
        self.passthrough = ctx.name == "pcm_alaw" and ctx.sample_rate == 8000 and ctx.layout.name == "mono"
        if not self.passthrough:
            self.resampler = av.AudioResampler(format="s16", layout="mono", rate=8000)
            self.encoder = av.CodecContext.create("pcm_alaw", "w")
            self.encoder.sample_rate = 8000
            self.encoder.format = "s16"
            self.encoder.layout = "mono"
            self.encoder.time_base = Fraction(1, 8000)
        self.description = f"{ctx.name} {ctx.sample_rate} Hz"
        self._buf = bytearray()

    def feed(self, packet) -> list[bytes]:
        if self.passthrough:
            self._buf += bytes(packet)
        else:
            for frame in packet.decode():
                for resampled in self.resampler.resample(frame):
                    resampled.pts = None
                    for out in self.encoder.encode(resampled):
                        self._buf += bytes(out)
        chunks = []
        while len(self._buf) >= self.FRAME_BYTES:
            chunks.append(bytes(self._buf[: self.FRAME_BYTES]))
            del self._buf[: self.FRAME_BYTES]
        return chunks


class Subscriber:
    def __init__(self, maxsize: int):
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize)
        self.waiting_key = True
        self.dropped = 0

    def offer(self, frame: bytes, is_key: bool) -> None:
        if self.waiting_key:
            if not is_key:
                return
            self.waiting_key = False
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            # cliente lento: descarta o atraso e recomeça no próximo quadro-chave
            while not self.queue.empty():
                self.queue.get_nowait()
            self.dropped += 1
            self.waiting_key = not is_key
            if is_key:
                self.queue.put_nowait(frame)


class StreamSource:
    def __init__(
        self,
        name: str,
        url_provider: Callable[[], str],
        *,
        transport: str = "tcp",
        audio: bool = False,
        fps: int | None = None,
        gop_cache: bool = True,
        idle_timeout: float = 20.0,
        always_on: bool = False,
        queue_size: int = 300,
    ):
        self.name = name
        self.info = StreamInfo()
        self.always_on = always_on
        self._url_provider = url_provider
        self._transport = transport
        self._audio = audio
        self._fps = fps
        self._gop_cache = gop_cache
        self._gop_limit = queue_size // 2
        self._idle_timeout = idle_timeout
        self._queue_size = queue_size
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: set[Subscriber] = set()
        self._gop: list[tuple[bytes, bool]] = []
        self._last_key: tuple[bytes, str] | None = None
        self._gen = 0
        self._stop_evt: threading.Event | None = None
        self._idle_handle: asyncio.TimerHandle | None = None

    # ------------------------------------------------------------ lado asyncio
    @property
    def viewers(self) -> int:
        return len(self._subs)

    def start(self) -> None:
        if self._stop_evt is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._gen += 1
        self._stop_evt = threading.Event()
        threading.Thread(
            target=self._run, args=(self._gen, self._stop_evt), name=f"src-{self.name}", daemon=True
        ).start()

    def stop(self) -> None:
        if self._stop_evt is not None:
            self._stop_evt.set()
            self._stop_evt = None
        self._gen += 1
        self._gop.clear()
        self._last_key = None
        self.info.online = False

    def subscribe(self) -> Subscriber:
        if self._idle_handle:
            self._idle_handle.cancel()
            self._idle_handle = None
        sub = Subscriber(self._queue_size)
        for frame, is_key in self._gop:
            sub.offer(frame, is_key)
        self._subs.add(sub)
        self.start()
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subs.discard(sub)
        if not self._subs and not self.always_on and self._loop and not self._idle_handle:
            self._idle_handle = self._loop.call_later(self._idle_timeout, self._stop_if_idle)

    def _stop_if_idle(self) -> None:
        self._idle_handle = None
        if not self._subs and not self.always_on and self._stop_evt is not None:
            log.info("[%s] sem espectadores, fechando RTSP", self.name)
            self.stop()

    async def snapshot(self, timeout: float = 10.0) -> bytes | None:
        """JPEG do quadro-chave mais recente (abre o stream temporariamente, se preciso)."""
        if self._last_key is None:
            sub = self.subscribe()
            try:
                await asyncio.wait_for(sub.queue.get(), timeout)
            except asyncio.TimeoutError:
                return None
            finally:
                self.unsubscribe(sub)
        if self._last_key is None:
            return None
        payload, codec = self._last_key
        return await asyncio.to_thread(keyframe_to_jpeg, payload, codec)

    def _dispatch(self, gen: int, frame: bytes, is_key: bool, is_video: bool) -> None:
        if gen != self._gen:
            return
        if self._gop_cache:
            if is_video and is_key:
                self._gop = [(frame, True)]
            elif self._gop:
                if len(self._gop) < self._gop_limit:
                    self._gop.append((frame, False))
                else:
                    self._gop = []  # GOP longo demais para o cache
        key = is_video and is_key
        for sub in self._subs:
            sub.offer(frame, key)

    def _on_reset(self, gen: int) -> None:
        if gen != self._gen:
            return
        self._gop.clear()
        for sub in self._subs:
            sub.waiting_key = True

    def _post(self, gen: int, fn, *args) -> None:
        try:
            self._loop.call_soon_threadsafe(fn, gen, *args)
        except RuntimeError:
            pass  # loop encerrado

    # ------------------------------------------------------- thread de captura
    def _run(self, gen: int, stop_evt: threading.Event) -> None:
        backoff = 1.0
        while not stop_evt.is_set():
            started = time.monotonic()
            try:
                self._pull(gen, stop_evt)
                if not stop_evt.is_set():
                    log.warning("[%s] a câmera encerrou o stream", self.name)
            except UnsupportedStream as exc:
                log.error("[%s] %s", self.name, exc)
                backoff = 30.0
            except Exception as exc:  # noqa: BLE001 - qualquer falha de rede/FFmpeg vira reconexão
                if not stop_evt.is_set():
                    log.warning("[%s] erro no stream: %s", self.name, exc)
            self.info.online = False
            self._post(gen, self._on_reset)
            if time.monotonic() - started > 60:
                backoff = 1.0
            if stop_evt.wait(backoff):
                break
            backoff = min(backoff * 2, 30.0)

    def _pull(self, gen: int, stop_evt: threading.Event) -> None:
        url = self._url_provider()
        options = {}
        if url.lower().startswith(("rtsp://", "rtsps://")):
            if self._transport != "auto":
                options["rtsp_transport"] = self._transport
            if not self._audio:
                options["allowed_media_types"] = "video"
        log.info("[%s] conectando em %s", self.name, mask_url(url))
        container = av.open(url, options=options, timeout=(15.0, 10.0))
        try:
            if not container.streams.video:
                raise UnsupportedStream("a fonte não tem trilha de vídeo")
            vstream = container.streams.video[0]
            ctx = vstream.codec_context
            if ctx.name not in (nal.H264, nal.HEVC):
                raise UnsupportedStream(f"codec de vídeo '{ctx.name}' não suportado; configure a câmera para H.264 ou H.265")
            fps = self._fps or guess_fps(vstream)
            packer = VideoPacker(ctx.name, ctx.extradata, fps, ctx.width, ctx.height)

            transcoder = None
            astream = container.streams.audio[0] if self._audio and container.streams.audio else None
            if astream is not None:
                try:
                    transcoder = AudioTranscoder(astream)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[%s] áudio desativado: %s", self.name, exc)
                    astream = None

            self.info.codec, self.info.width, self.info.height, self.info.fps = ctx.name, ctx.width, ctx.height, fps
            self.info.audio = transcoder is not None
            self.info.online = True
            log.info(
                "[%s] conectado: %s %dx%d %d fps%s",
                self.name, ctx.name.upper(), ctx.width, ctx.height, fps,
                f", áudio {transcoder.description} -> G.711A" if transcoder else "",
            )

            streams = [vstream] + ([astream] if astream is not None else [])
            for packet in container.demux(*streams):
                if stop_evt.is_set():
                    return
                if packet.size == 0:
                    continue
                if packet.stream.index == vstream.index:
                    packed = packer.pack(bytes(packet), packet.is_keyframe)
                    if packed is None:
                        continue
                    frame, is_key, raw = packed
                    if is_key:
                        self._last_key = (raw, ctx.name)
                    self._post(gen, self._dispatch, frame, is_key, True)
                elif transcoder is not None:
                    try:
                        chunks = transcoder.feed(packet)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("[%s] falha no áudio, desativando: %s", self.name, exc)
                        transcoder = None
                        continue
                    for chunk in chunks:
                        self._post(gen, self._dispatch, protocol.audio_frame(chunk), False, False)
        finally:
            container.close()


def keyframe_to_jpeg(payload: bytes, codec: str) -> bytes | None:
    decoder = av.CodecContext.create(codec, "r")
    frames = list(decoder.decode(av.Packet(payload)))
    try:
        frames += decoder.decode(None)
    except av.error.FFmpegError:
        pass
    if not frames:
        return None
    frame = frames[0]
    encoder = av.CodecContext.create("mjpeg", "w")
    encoder.width = frame.width
    encoder.height = frame.height
    encoder.pix_fmt = "yuvj420p"
    encoder.time_base = Fraction(1, 25)
    packets = encoder.encode(frame.reformat(format="yuvj420p")) + encoder.encode(None)
    return b"".join(bytes(p) for p in packets) or None
