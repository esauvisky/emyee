import asyncio
from yeelight import Bulb
from loguru import logger
from collections import deque  # Import deque for fixed-size queue

from custom_lock import CustomLock  # Import the CustomLock class
from utils import CONTROLLER_TICK


class LightDevice:
    def __init__(self, bulb: Bulb, latency_history_size: int = 5):
        """
        Initializes the LightDevice with a Yeelight Bulb instance.

        :param bulb: An instance of yeelight.Bulb.
        :param latency_history_size: Number of recent latency measurements to keep for averaging.
        """
        self.bulb = bulb
        self.ip = bulb._ip
        self.model = bulb.get_capabilities()["model"]  # type: ignore
        self.lock = CustomLock()
        self.latency_history = deque(maxlen=latency_history_size)  # Store last n latencies
        self.latency = 0  # Default latency in seconds (50ms)

    async def measure_latency(self, retries: int = 5) -> float:
        """
        Measures the round-trip time (RTT) to the bulb by sending a simple command.

        :param retries: Number of attempts to measure latency.
        :return: Average latency in seconds based on the history.
        """
        latencies = []
        for i in range(retries):
            start_time = asyncio.get_event_loop().time()
            try:
                # Send a simple get_prop command to measure latency
                await asyncio.to_thread(self.bulb.get_properties, ["power"])
                end_time = asyncio.get_event_loop().time()
                latency = end_time - start_time
                latencies.append(latency)
                self.latency_history.append(latency)  # Add to history
                logger.debug(f"Latency measurement {i+1}/{retries} for {self.ip}: {latency * 1000:.2f} ms")
                await asyncio.sleep(0.05)  # Small delay between measurements
            except Exception as e:
                logger.error(f"Latency measurement failed for {self.ip}: {e}")

        if self.latency_history:
            self.latency = sum(self.latency_history) / len(self.latency_history)
            logger.info(f"Updated average latency for {self.ip}: {self.latency * 1000:.2f} ms (based on {len(self.latency_history)} measurements)")
        else:
            logger.warning(f"No successful latency measurements for {self.ip}. Using default latency.")
            self.latency = 0.05  # Reset to default if no measurements

        return self.latency

    async def start_latency_monitor(self, interval: float = 60.0, retries: int = 5):
        """
        Starts a background task to periodically measure latency.

        :param interval: Time between latency measurements in seconds.
        :param retries: Number of attempts per measurement.
        """
        while True:
            await self.measure_latency(retries=retries)
            await asyncio.sleep(interval)

    async def initialize(self, latency_history_size: int = 5, monitor_interval: float = 60.0, retries: int = 5):
        """
        Initializes the device by measuring initial latency and starting the latency monitor.

        :param latency_history_size: Number of recent latency measurements to keep for averaging.
        :param monitor_interval: Time between latency measurements in seconds.
        :param retries: Number of attempts per latency measurement.
        """
        # Update deque size if different from default
        if latency_history_size != self.latency_history.maxlen:
            self.latency_history = deque(maxlen=latency_history_size)
        await self.measure_latency(retries=retries)
        asyncio.create_task(self.start_latency_monitor(interval=monitor_interval, retries=retries))

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
