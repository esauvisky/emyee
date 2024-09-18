import asyncio
from yeelight import Bulb
from loguru import logger

from custom_lock import CustomLock  # Import the CustomLock class
from utils import CONTROLLER_TICK

class LightDevice:
    def __init__(self, bulb: Bulb):
        """
        Initializes the LightDevice with a Yeelight Bulb instance.

        :param bulb: An instance of yeelight.Bulb.
        """
        self.bulb = bulb
        self.ip = bulb._ip
        self.model = bulb.get_capabilities()["model"]  # type: ignore
        self.lock = CustomLock()

    async def set_brightness(self, brightness: int, duration: float = 0.05):
        """
        Asynchronously sets the brightness of the bulb.
        Ignores the request if the device is currently handling another state change.

        :param brightness: Brightness level (0-100).
        :param duration: Duration of the transition in seconds.
        """
        if self.lock.locked():
            logger.trace(f"Brightness change ignored for {self.ip} because it's currently locked.")
            return

        await self.lock.acquire()
        try:
            logger.trace(f"Setting brightness to {brightness}% over {duration}s for {self.ip}")
            await asyncio.to_thread(
                self.bulb.set_brightness, brightness, duration=int(duration * 1000)
            )
            logger.trace(f"Brightness set to {brightness}% for {self.ip}")
        except asyncio.CancelledError:
            logger.trace(f"Brightness change task was cancelled for {self.ip}.")
            raise
        except Exception as e:
            logger.error(f"Failed to set brightness for {self.ip}: {e}")
        finally:
            await asyncio.sleep(CONTROLLER_TICK)  # Adjust sleep as needed
            self.lock.release()

    async def set_hsv(self, hue: int, saturation: int, brightness: int, duration: float = 0.05):
        """
        Asynchronously sets the HSV color of the bulb.
        Overrides any ongoing state change by cancelling it and forcing the new state.

        :param hue: Hue value (0-359).
        :param saturation: Saturation level (0-100).
        :param brightness: Brightness level (0-100).
        :param duration: Duration of the transition in seconds.
        """
        if self.lock.locked():
            logger.trace(f"HSV change attempting to override current state change for {self.ip}.")
            holder_task = self.lock.holder
            if holder_task and not holder_task.done():
                logger.trace(f"Cancelling current state change task for {self.ip}.")
                holder_task.cancel()
                try:
                    await holder_task
                except asyncio.CancelledError:
                    logger.trace(f"Cancelled task holding the lock for {self.ip}.")
                except Exception as e:
                    logger.error(f"Error while cancelling task for {self.ip}: {e}")

        await self.lock.acquire()
        try:
            logger.trace(f"Setting HSV to ({hue}, {saturation}, {brightness}) over {duration}s for {self.ip}")
            await asyncio.to_thread(
                self.bulb.set_hsv, hue, saturation, brightness, duration=int(duration * 1000)
            )
            logger.trace(f"HSV set to ({hue}, {saturation}, {brightness}) for {self.ip}")
        except asyncio.CancelledError:
            logger.trace(f"HSV change task was cancelled for {self.ip}.")
            raise
        except Exception as e:
            logger.error(f"Failed to set HSV for {self.ip}: {e}")
        finally:
            await asyncio.sleep(CONTROLLER_TICK)  # Adjust sleep as needed
            self.lock.release()

    async def turn_on(self, duration: float = 0.05):
        """
        Asynchronously turns on the bulb.
        Ignores the request if the device is currently handling another state change.

        :param duration: Duration of the transition in seconds.
        """
        if self.lock.locked():
            logger.trace(f"Turn on ignored for {self.ip} because it's currently locked.")
            return

        await self.lock.acquire()
        try:
            logger.trace(f"Turning on bulb {self.ip} over {duration}s")
            await asyncio.to_thread(
                self.bulb.turn_on, duration=int(duration * 1000)
            )
            logger.trace(f"Bulb {self.ip} turned on")
        except asyncio.CancelledError:
            logger.trace(f"Turn on task was cancelled for {self.ip}.")
            raise
        except Exception as e:
            logger.error(f"Failed to turn on bulb {self.ip}: {e}")
        finally:
            self.lock.release()

    async def turn_off(self, duration: float = 0.05):
        """
        Asynchronously turns off the bulb.
        Ignores the request if the device is currently handling another state change.

        :param duration: Duration of the transition in seconds.
        """
        if self.lock.locked():
            logger.trace(f"Turn off ignored for {self.ip} because it's currently locked.")
            return

        await self.lock.acquire()
        try:
            logger.trace(f"Turning off bulb {self.ip} over {duration}s")
            await asyncio.to_thread(
                self.bulb.turn_off, duration=int(duration * 1000)
            )
            logger.trace(f"Bulb {self.ip} turned off")
        except asyncio.CancelledError:
            logger.trace(f"Turn off task was cancelled for {self.ip}.")
            raise
        except Exception as e:
            logger.error(f"Failed to turn off bulb {self.ip}: {e}")
        finally:
            self.lock.release()
