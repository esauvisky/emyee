import asyncio
from dataclasses import dataclass
from typing import Optional
from colorsys import hsv_to_rgb
from yeelight import Bulb
from loguru import logger

from utils import CONTROLLER_TICK


@dataclass
class DesiredState:
    mode: str
    hue: Optional[int] = None
    saturation: Optional[int] = None
    brightness: Optional[int] = None
    duration: float = 0.05


class LightDevice:
    def __init__(self, bulb: Bulb):
        self.bulb = bulb
        self.ip = bulb._ip
        try:
            caps = bulb.get_capabilities() or {}
            self.model = caps.get("model", "unknown")
        except Exception:
            self.model = "unknown"
        self.music_mode: bool = False
        self._latency_samples: list = []
        self._desired: Optional[DesiredState] = None
        self._update_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._worker_task: Optional[asyncio.Task] = None
        self._color_busy_until: float = 0.0           # monotonic deadline while HSV is flowing

    @property
    def estimated_latency(self) -> float:
        if not self._latency_samples:
            return 0.02
        return sum(self._latency_samples) / len(self._latency_samples)

    def _record_latency(self, rtt: float):
        self._latency_samples.append(rtt)
        if len(self._latency_samples) > 10:
            self._latency_samples.pop(0)

    def _color_busy(self) -> bool:
        return asyncio.get_event_loop().time() < self._color_busy_until

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        """Schedule background worker on the provided loop (or current event loop)."""
        if self._worker_task and not self._worker_task.done():
            return
        if loop is None:
            loop = asyncio.get_event_loop()
        self._worker_task = loop.create_task(self._worker(), name=f"LightWorker-{self.ip}")

    async def stop(self):
        self._stop_event.set()
        self._update_event.set()
        if self._worker_task:
            try:
                await self._worker_task
            except Exception as e:
                logger.error(f"Worker stop error for {self.ip}: {e}")

    # ---- Public API (called by controller) ----

    async def set_brightness(self, brightness: int, duration: float = 0.05):
        # If a color transition is currently flowing, ignore brightness updates
        if self._color_busy():
            logger.trace(f"[{self.ip}] skip brightness {brightness}% (color transition flowing)")
            return
        self._desired = DesiredState(mode="brightness", brightness=int(brightness), duration=float(duration))
        self._update_event.set()

    async def set_hsv(self, hue: int, saturation: int, brightness: int, duration: float = 0.05):
        # HSV supersedes pending brightness-only updates
        self._desired = DesiredState(mode="hsv",
                                     hue=int(hue),
                                     saturation=int(saturation),
                                     brightness=int(brightness),
                                     duration=float(duration))
        self._update_event.set()

    async def turn_on(self, duration: float = 0.05):
        self._desired = DesiredState(mode="on", duration=float(duration))
        self._update_event.set()

    async def turn_off(self, duration: float = 0.05):
        self._desired = DesiredState(mode="off", duration=float(duration))
        self._update_event.set()

    # ---- Worker loop ----

    async def _worker(self):
        logger.debug(f"[{self.ip}] worker started")
        try:
            while not self._stop_event.is_set():
                # Wait until there’s something to do
                await self._update_event.wait()
                self._update_event.clear()

                # Snapshot the latest desired state (coalesced)
                desired = self._desired
                self._desired = None
                if desired is None:
                    continue

                try:

                    loop_time = asyncio.get_event_loop().time
                    if desired.mode == "hsv":
                        logger.trace(f"[{self.ip}] apply HSV ({desired.hue},{desired.saturation},{desired.brightness}) "
                                     f"over {desired.duration:.3f}s")
                        # Mark busy window while color is transitioning
                        self._color_busy_until = loop_time() + desired.duration
                        try:
                            t0 = loop_time()
                            await asyncio.to_thread(
                                self.bulb.set_hsv,
                                int(desired.hue),
                                int(desired.saturation),
                                int(desired.brightness),
                                duration=int(desired.duration * 1000),
                            )
                            self._record_latency(loop_time() - t0)
                        except Exception as hsv_error:
                            logger.warning(
                                f"[{self.ip}] set_hsv failed ({hsv_error}), trying set_rgb fallback"
                            )
                            hue = int(desired.hue) % 360
                            sat = max(0, min(100, int(desired.saturation)))
                            val = max(0, min(100, int(desired.brightness)))
                            rgb = tuple(int(round(component * 255)) for component in hsv_to_rgb(hue / 360, sat / 100, val / 100))
                            try:
                                t0 = loop_time()
                                await asyncio.to_thread(
                                    self.bulb.set_rgb,
                                    *rgb,
                                    duration=int(desired.duration * 1000),
                                )
                                self._record_latency(loop_time() - t0)
                            except Exception as rgb_error:
                                logger.error(
                                    f"[{self.ip}] set_rgb fallback failed (set_hsv_error={hsv_error}, set_rgb_error={rgb_error})"
                                )
                                raise

                    elif desired.mode == "brightness":
                        logger.trace(f"[{self.ip}] apply brightness {desired.brightness}% over {desired.duration:.3f}s")
                        t0 = loop_time()
                        await asyncio.to_thread(
                            self.bulb.set_brightness,
                            int(desired.brightness),
                            duration=int(desired.duration * 1000),
                        )
                        self._record_latency(loop_time() - t0)

                    elif desired.mode == "on":
                        logger.trace(f"[{self.ip}] turn_on over {desired.duration:.3f}s")
                        t0 = loop_time()
                        await asyncio.to_thread(self.bulb.turn_on, duration=int(desired.duration * 1000))
                        self._record_latency(loop_time() - t0)

                    elif desired.mode == "off":
                        logger.trace(f"[{self.ip}] turn_off over {desired.duration:.3f}s")
                        t0 = loop_time()
                        await asyncio.to_thread(self.bulb.turn_off, duration=int(desired.duration * 1000))
                        self._record_latency(loop_time() - t0)

                except asyncio.CancelledError:
                    # If we ever cancel the worker, propagate
                    raise
                except Exception as e:
                    logger.error(f"[{self.ip}] apply error: {e}")

                # Music mode has no rate limit; standard mode allows ~1 cmd/s
                await asyncio.sleep(CONTROLLER_TICK if self.music_mode else 1.1)

        finally:
            logger.debug(f"[{self.ip}] worker stopped")
