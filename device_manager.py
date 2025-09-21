# device_manager.py
from typing import List
from yeelight import discover_bulbs, Bulb
from loguru import logger
from light_device import LightDevice

class DeviceManager:
    def __init__(self, effect="smooth", auto_on=False):
        self.effect = effect
        self.auto_on = auto_on

    def discover_devices(self) -> List[LightDevice]:
        logger.info("Discovering bulbs...")
        bulbs_info = discover_bulbs()
        devices: List[LightDevice] = []

        for bulb_info in bulbs_info:
            ip = bulb_info["ip"]
            port = bulb_info.get("port", 55443)
            try:
                bulb = Bulb(ip, port, effect=self.effect, auto_on=self.auto_on)
                bulb.start_music()
                light_device = LightDevice(bulb)
                # NOTE: do NOT start the worker here (no running loop yet)
                devices.append(light_device)
                logger.info(f"Initialized LightDevice at {ip}:{port}")
            except Exception as e:
                logger.error(f"Failed to initialize bulb at {ip}:{port}: {e}")

        logger.info(f"Found and initialized {len(devices)} LightDevice(s).")
        return devices
