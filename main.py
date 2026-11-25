#!/usr/bin/env python3
import argparse
import asyncio
import os
from dotenv import load_dotenv
from loguru import logger
from spotify_listener import SpotifyChangesListener
from light_controller import LightsController
from device_manager import DeviceManager
from light_device import DesiredState
from utils import setup_logging
from lan_reset import load_tokens
from web_app import WebApp

_EXIT_KELVIN = 2500
_EXIT_BRIGHTNESS = 100


def main():
    parser = argparse.ArgumentParser(description="Spotify-synced Yeelight controller")
    parser.add_argument(
        "--min-confidence", type=float, default=0.0, metavar="0-1",
        help="Minimum confidence for beats/bars/segments/tatums (default: 0)",
    )
    parser.add_argument(
        "--sync-offset", type=float, default=0.0, metavar="MS",
        help="Fire light commands this many ms early (+) or late (-) relative to beat/segment (default: 0)",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=20.0, metavar="MS",
        help="Progress event interval in ms — lower = more responsive, higher = less CPU (default: 20)",
    )
    parser.add_argument(
        "--no-music-mode", action="store_true",
        help="Disable Yeelight music mode (use if getting connection errors)",
    )
    parser.add_argument(
        "--tokens", metavar="FILE", default="tokens.json",
        help="Path to {ip: token_hex} JSON for LAN reset (default: tokens.json)",
    )
    parser.add_argument(
        "--no-lan-reset", action="store_true",
        help="Skip the LAN control off→on cycle before discovery",
    )
    parser.add_argument(
        "--web", metavar="HOST:PORT", default="0.0.0.0:8080",
        help="Where to serve the control web app (default: 0.0.0.0:8080)",
    )
    parser.add_argument(
        "--yeelight-discover", action="store_true",
        help="Discover Yeelight bulbs instead of ESPHome lights",
    )
    parser.add_argument(
        "--ip", metavar="IP", action="append", dest="ips",
        help="Connect directly to this bulb IP (skip discovery); repeat for multiple bulbs",
    )
    parser.add_argument(
        "--tasmota", metavar="IP", action="append", dest="tasmota_ips",
        help="Drive a Tasmota RGB light over HTTP at this IP; repeat for multiple lights",
    )
    parser.add_argument(
        "--esphome", metavar="IP", action="append", dest="esphome_ips",
        help="Drive only these ESPHome lights instead of discovering them over mDNS; repeat for multiple lights",
    )
    parser.add_argument(
        "--esphome-key", metavar="KEY",
        help="ESPHome API encryption key (noise PSK); defaults to $ESPHOME_KEY",
    )
    parser.add_argument(
        "--esphome-password", metavar="PASSWORD",
        help="ESPHome legacy API password; defaults to $ESPHOME_PASSWORD",
    )
    parser.add_argument(
        "--tasmota-no-sleep", action="store_true",
        help="Set Sleep 0 on each Tasmota light (halves command latency, more idle power, persisted to flash)",
    )
    parser.add_argument(
        "--atmospheric-pulse-on", type=int, metavar="N",
        help="Override the atmospheric DB's accented-beat count",
    )
    parser.add_argument(
        "--atmospheric-pulse-off", type=int, metavar="N",
        help="Override the atmospheric DB's subtle-beat count",
    )
    args = parser.parse_args()
    if args.atmospheric_pulse_on is not None and args.atmospheric_pulse_on < 1:
        parser.error("--atmospheric-pulse-on must be at least 1")
    if args.atmospheric_pulse_off is not None and args.atmospheric_pulse_off < 0:
        parser.error("--atmospheric-pulse-off cannot be negative")

    load_dotenv(".env")
    user_id = os.getenv('USER_ID')
    client_id = os.getenv('CLIENT_ID')
    client_secret = os.getenv('CLIENT_SECRET')

    setup_logging("DEBUG")

    tokens = {}
    if not args.no_lan_reset:
        if os.path.exists(args.tokens):
            tokens = load_tokens(args.tokens)
            logger.info(f"LAN reset enabled for {len(tokens)} bulb(s) — will reset each before connecting")
        else:
            logger.warning(f"tokens file not found at {args.tokens!r} — skipping LAN reset (use --no-lan-reset to suppress)")

    device_manager = DeviceManager(enable_music_mode=not args.no_music_mode, tokens=tokens)
    devices = []
    if args.ips:
        devices += device_manager.connect_to_ips(args.ips)
    elif args.yeelight_discover:
        devices += device_manager.discover_devices()
    if args.tasmota_ips:
        devices += device_manager.connect_to_tasmota(
            args.tasmota_ips, disable_wifi_sleep=args.tasmota_no_sleep
        )
    if args.esphome_ips or not (args.ips or args.yeelight_discover or args.tasmota_ips):
        devices += device_manager.connect_to_esphome(
            args.esphome_ips,
            noise_psk=args.esphome_key or os.getenv("ESPHOME_KEY"),
            password=args.esphome_password or os.getenv("ESPHOME_PASSWORD"),
        )

    events_queue = asyncio.Queue()
    spotify_listener = SpotifyChangesListener(user_id, client_id, client_secret, events_queue)
    spotify_listener.poll_interval = args.poll_interval / 1000

    light_controller = LightsController(devices, events_queue)
    light_controller.min_confidence = args.min_confidence
    light_controller.sync_offset = args.sync_offset / 1000
    light_controller.atmospheric_pulse_on = args.atmospheric_pulse_on
    light_controller.atmospheric_pulse_off = args.atmospheric_pulse_off

    logger.info(
        f"confidence≥{args.min_confidence} | "
        f"sync offset: {args.sync_offset:+.0f}ms | poll: {args.poll_interval:.0f}ms"
    )

    # After setup_logging: it replaces every log sink, the web app's included.
    web_app = WebApp(light_controller)
    host, port = args.web.rsplit(":", 1)

    loop = asyncio.get_event_loop()
    for d in devices:
        d.start(loop)

    final_state = None
    try:
        loop.run_until_complete(asyncio.gather(
            spotify_listener.listen(),
            light_controller.control_lights(),
            web_app.serve(host, int(port)),
        ))
    except KeyboardInterrupt:
        logger.info("Stopping...")
        final_state = DesiredState(mode="ct", kelvin=_EXIT_KELVIN, brightness=_EXIT_BRIGHTNESS, duration=0.3)
    finally:
        results = loop.run_until_complete(
            asyncio.gather(*(d.stop(final_state) for d in devices), return_exceptions=True)
        )
        for device, result in zip(devices, results):
            if isinstance(result, Exception):
                logger.error(f"[{device.ip}] stop failed: {result!r}")


if __name__ == '__main__':
    main()
