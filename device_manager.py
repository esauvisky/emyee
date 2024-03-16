from yeelight import discover_bulbs, Bulb
from loguru import logger

class DeviceManager:
    def __init__(self, effect="smooth", auto_on=False):
        """
        Initializes the DeviceManager with default settings for bulbs.

        :param effect: The effect to use when changing the bulb's state. Default is "smooth".
        :param auto_on: Whether to automatically turn on the bulb if it's off when setting states. Default is False.
        """
        self.effect = effect
        self.auto_on = auto_on

    def discover_devices(self):
        """
        Discovers Yeelight bulbs on the local network.

        :return: A list of initialized Bulb objects ready for use.
        """
        logger.info("Discovering bulbs...")
        bulbs_info = discover_bulbs()
        devices = []

        for bulb_info in bulbs_info:
            ip = bulb_info["ip"]
            port = bulb_info.get("port", 55443)  # Default port for Yeelight bulbs

            try:
                bulb = Bulb(ip, port, effect=self.effect, auto_on=self.auto_on)
                if bulb.get_capabilities()["model"] != "ct_bulb":
                    bulb.start_music()
                    devices.append(bulb)
                    logger.info(f"Initialized bulb at {ip}:{port}")
            except Exception as e:
                logger.error(f"Failed to initialize bulb at {ip}:{port}: {e}")

        logger.info(f"Found and initialized {len(devices)} bulbs.")
        return devices
