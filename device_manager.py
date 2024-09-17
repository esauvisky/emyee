from typing import List
from yeelight import discover_bulbs, Bulb
from loguru import logger
from light_device import LightDevice  # Import the new LightDevice class

class DeviceManager:
    def __init__(self, effect="smooth", auto_on=False):
        """
        Initializes the DeviceManager with default settings for bulbs.

        :param effect: The effect to use when changing the bulb's state. Default is "smooth".
        :param auto_on: Whether to automatically turn on the bulb if it's off when setting states. Default is False.
        """
        self.effect = effect
        self.auto_on = auto_on

    def discover_devices(self) -> List[LightDevice]:
        """
        Discovers Yeelight bulbs on the local network.

        :return: A list of initialized LightDevice objects ready for use.
        """
        logger.info("Discovering bulbs...")
        bulbs_info = discover_bulbs()
        devices = []

        for bulb_info in bulbs_info:
            ip = bulb_info["ip"]
            port = bulb_info.get("port", 55443)  # Default port for Yeelight bulbs

            try:
                bulb = Bulb(ip, port, effect=self.effect, auto_on=self.auto_on)
                # Initialize LightDevice with the Bulb instance
                light_device = LightDevice(bulb)
                bulb.start_music()
                devices.append(light_device)
                logger.info(f"Initialized LightDevice at {ip}:{port}")
            except Exception as e:
                logger.error(f"Failed to initialize bulb at {ip}:{port}: {e}")

        logger.info(f"Found and initialized {len(devices)} LightDevice(s).")
        return devices
