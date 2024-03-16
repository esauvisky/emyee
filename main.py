#!/usr/bin/env python3
import asyncio
import os
from dotenv import load_dotenv
from loguru import logger
from spotify_listener import SpotifyChangesListener
from light_controller import LightsController
from device_manager import DeviceManager
from utils import setup_logging

def main():
    load_dotenv(".env")
    user_id = os.getenv('USER_ID')
    client_id = os.getenv('CLIENT_ID')
    client_secret = os.getenv('CLIENT_SECRET')
    setup_logging("DEBUG")

    device_manager = DeviceManager()
    devices = device_manager.discover_devices()

    events_queue = asyncio.Queue(1)

    spotify_listener = SpotifyChangesListener(user_id, client_id, client_secret, events_queue)
    light_controller = LightsController(devices, events_queue)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        asyncio.gather(
            spotify_listener.listen(),
            light_controller.control_lights()
        )
    )

if __name__ == '__main__':
    main()
