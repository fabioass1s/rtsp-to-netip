"""Câmera falsa para testes: publica um padrão de teste via RTSP num servidor (ex.: MediaMTX).

    python tests/e2e/fake_camera.py --url rtsp://127.0.0.1:8554/main --size 1280x720 --fps 25 --audio
    python tests/e2e/fake_camera.py --url rtsp://127.0.0.1:8554/hevc --codec hevc --size 1920x1080 --fps 20
"""

from __future__ import annotations

import argparse
import time
from fractions import Fraction

import av


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--fps", type=int, default=25)
    ap.add_argument("--codec", choices=["h264", "hevc"], default="h264")
    ap.add_argument("--audio", action="store_true", help="inclui um tom de 440 Hz em AAC")
    args = ap.parse_args()
    width, height = (int(v) for v in args.size.split("x"))

    graph = f"testsrc2=size={args.size}:rate={args.fps}[out0]"
    if args.audio:
        graph += ";sine=frequency=440:sample_rate=48000[out1]"
    src = av.open(graph, format="lavfi")

    out = av.open(args.url, "w", format="rtsp", options={"rtsp_transport": "tcp"})
    gop = args.fps * 2
    if args.codec == "h264":
        vout = out.add_stream("libx264", rate=args.fps, options={"preset": "ultrafast", "tune": "zerolatency"})
    else:
        vout = out.add_stream(
            "libx265", rate=args.fps,
            options={"preset": "ultrafast", "tune": "zerolatency", "x265-params": f"keyint={gop}:log-level=error"},
        )
    vout.width, vout.height, vout.pix_fmt = width, height, "yuv420p"
    vout.codec_context.gop_size = gop
    aout = out.add_stream("aac", rate=48000) if args.audio else None

    started = time.monotonic()
    vcount = 0
    for packet in src.demux():
        for frame in packet.decode():
            if packet.stream.type == "video":
                frame = frame.reformat(format="yuv420p")
                frame.pts, frame.time_base = vcount, Fraction(1, args.fps)
                frame.pict_type = av.video.frame.PictureType.NONE  # senão o x264 força IDR em todo quadro
                vcount += 1
                delay = started + vcount / args.fps - time.monotonic()
                if delay > 0:
                    time.sleep(delay)  # tempo real, como uma câmera
                out.mux(vout.encode(frame))
            elif aout is not None:
                frame.pts = None
                out.mux(aout.encode(frame))


if __name__ == "__main__":
    main()
