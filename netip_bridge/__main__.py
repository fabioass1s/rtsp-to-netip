"""Linha de comando: run (conversor), discover (câmeras ONVIF) e client (teste NetIP)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("libav").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_run(args) -> int:
    from .app import run_app
    from .config import ConfigError, load_config

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        _setup_logging("INFO")
        logging.error("configuração inválida: %s", exc)
        return 2
    _setup_logging(os.environ.get("LOG_LEVEL") or cfg.log_level)
    try:
        asyncio.run(run_app(cfg))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_discover(args) -> int:
    from .onvif import OnvifCamera, OnvifError, discover

    _setup_logging("WARNING")
    cams = discover(args.timeout)
    if not cams:
        print("nenhuma câmera ONVIF respondeu (o multicast precisa de network_mode: host no Docker)")
        return 1
    for cam in cams:
        print(f"\n{cam['ip']}  {cam.get('name', '')} {cam.get('hardware', '')}\n  ONVIF: {cam['xaddr']}")
        if not args.username:
            continue
        from urllib.parse import urlsplit

        parts = urlsplit(cam["xaddr"])
        onvif = OnvifCamera(parts.hostname or cam["ip"], parts.port or 80, args.username, args.password)
        try:
            onvif.connect()
            for prof in onvif.get_profiles():
                uri = onvif.get_stream_uri(prof.token)
                print(f"  perfil {prof.token}: {prof.encoding} {prof.width}x{prof.height} {prof.fps} fps -> {uri}")
        except OnvifError as exc:
            print(f"  erro ONVIF: {exc}")
    return 0


def cmd_client(args) -> int:
    from .client import NetIPError, run_client

    try:
        return run_client(args)
    except (NetIPError, OSError) as exc:
        print(f"erro: {exc}")
        return 1


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")  # acentos corretos em logs redirecionados
    parser = argparse.ArgumentParser(prog="netip_bridge", description="Conversor RTSP/ONVIF -> NetIP (Xiongmai/DVRIP)")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="inicia o conversor (padrão)")
    default_config = "/config/config.yaml" if os.path.isfile("/config/config.yaml") else "config/config.yaml"
    run.add_argument("--config", default=os.environ.get("NETIP_CONFIG", default_config))

    disc = sub.add_parser("discover", help="procura câmeras ONVIF na rede")
    disc.add_argument("--timeout", type=float, default=3.0)
    disc.add_argument("--username", default="")
    disc.add_argument("--password", default="")

    cli = sub.add_parser("client", help="testa um dispositivo NetIP (este conversor ou uma câmera real)")
    cli.add_argument("--host", default="127.0.0.1")
    cli.add_argument("--port", type=int, default=34567)
    cli.add_argument("--user", default="admin")
    cli.add_argument("--password", default="")
    cli.add_argument("--channel", type=int, default=0)
    cli.add_argument("--stream", choices=["main", "sub"], default="main")
    cli.add_argument("--seconds", type=float, default=5.0, help="tempo de captura de vídeo (0 = não capturar)")
    cli.add_argument("--out", help="arquivo para salvar o vídeo bruto (.h264/.h265)")
    cli.add_argument("--separate", action="store_true", help="usa conexão separada para a mídia (como NVR/CMS)")
    cli.add_argument("--snapshot", help="salva um JPEG do canal neste arquivo")
    cli.add_argument("--ptz", help="envia um comando PTZ por 1 s (ex.: DirectionLeft, ZoomTile)")

    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["run"])
    if args.command == "run":
        return cmd_run(args)
    if args.command == "discover":
        return cmd_discover(args)
    return cmd_client(args)


if __name__ == "__main__":
    sys.exit(main())
