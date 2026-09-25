import asyncio
from dataclasses import dataclass
from typing import Optional
from colorsys import hsv_to_rgb
from yeelight import Bulb
from loguru import logger



@dataclass
class DesiredState:
    mode: str
    hue: Optional[float] = None
    saturation: Optional[float] = None
    brightness: Optional[float] = None
    duration: float = 0.05
    replaceable: bool = False
    dispatch_at: float | None = None


class BaseLightDevice:
    """Queue and scheduling shell shared by every light backend.

    Subclasses provide _apply() for their protocol and _post_command_delay()
    for their rate limit. Command coalescing, dispatch_at scheduling, stale
    command dropping and latency tracking all live here.
    """

    def __init__(self, ip: str, model: str = "unknown"):
        self.ip = ip
        self.model = model
        self.music_mode: bool = False
        self._latency_samples: list = []
        self._commands = asyncio.Queue()
        self._stop_event = asyncio.Event()
        self._worker_task: Optional[asyncio.Task] = None

    @property
    def estimated_latency(self) -> float:
        if not self._latency_samples:
            return 0.02
        return sum(self._latency_samples) / len(self._latency_samples)

    def _record_latency(self, rtt: float):
        self._latency_samples.append(rtt)
        if len(self._latency_samples) > 10:
            self._latency_samples.pop(0)

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        """Schedule background worker on the provided loop (or current event loop)."""
        if self._worker_task and not self._worker_task.done():
            return
        if loop is None:
            loop = asyncio.get_event_loop()
        self._worker_task = loop.create_task(self._worker(), name=f"LightWorker-{self.ip}")

    async def stop(self):
        self._stop_event.set()
        self._commands.put_nowait(None)
        if self._worker_task:
            try:
                await self._worker_task
            except Exception as e:
                logger.error(f"Worker stop error for {self.ip}: {e}")

    # ---- Public API (called by controller) ----

    async def set_brightness(self, brightness: float, duration: float = 0.05, dispatch_at: float | None = None):
        command = DesiredState(mode="brightness", brightness=float(brightness), duration=float(duration), replaceable=True, dispatch_at=dispatch_at)
        pending = list(self._commands._queue)
        kept = [item for item in pending if not (getattr(item, 'replaceable', False) and item.mode == "brightness")]
        removed = len(pending) - len(kept)
        if removed:
            self._commands._queue.clear()
            self._commands._queue.extend(kept)
            self._commands._unfinished_tasks -= removed
            if self._commands._unfinished_tasks == 0:
                self._commands._finished.set()
        self._commands.put_nowait(command)

    async def set_hsv(self, hue: float, saturation: float, brightness: float, duration: float = 0.05, dispatch_at: float | None = None):
        self._commands.put_nowait(DesiredState(mode="hsv",
                                               hue=float(hue),
                                               saturation=float(saturation),
                                               brightness=float(brightness),
                                               duration=float(duration), dispatch_at=dispatch_at))

    async def turn_on(self, duration: float = 0.05):
        self._commands.put_nowait(DesiredState(mode="on", duration=float(duration)))

    async def turn_off(self, duration: float = 0.05):
        self._commands.put_nowait(DesiredState(mode="off", duration=float(duration)))

    # ---- Backend hooks ----

    async def _apply(self, desired: DesiredState):
        raise NotImplementedError

    def _post_command_delay(self) -> float:
        """Seconds to wait after each command before pulling the next one."""
        return 0.0

    # ---- Worker loop ----

    async def _worker(self):
        logger.debug(f"[{self.ip}] worker started")
        try:
            while not self._stop_event.is_set():
                desired = await self._commands.get()
                if desired is None:
                    self._commands.task_done()
                    break

                if desired.dispatch_at is not None:
                    await asyncio.sleep(max(0.0, desired.dispatch_at - asyncio.get_event_loop().time()))
                if (
                    desired.replaceable
                    and desired.dispatch_at is not None
                    and asyncio.get_event_loop().time() > desired.dispatch_at + max(desired.duration, 0.05)
                ):
                    logger.debug(f"[{self.ip}] dropped stale brightness command")
                    self._commands.task_done()
                    continue

                try:
                    await self._apply(desired)
                except asyncio.CancelledError:
                    # If we ever cancel the worker, propagate
                    raise
                except Exception as e:
                    logger.error(f"[{self.ip}] apply error: {e}")

                self._commands.task_done()

                delay = self._post_command_delay()
                if delay > 0:
                    await asyncio.sleep(delay)

        finally:
            logger.debug(f"[{self.ip}] worker stopped")


class LightDevice(BaseLightDevice):
    def __init__(self, bulb: Bulb):
        try:
            caps = bulb.get_capabilities() or {}
            model = caps.get("model", "unknown")
        except Exception:
            model = "unknown"
        super().__init__(bulb._ip, model)
        self.bulb = bulb

    def _post_command_delay(self) -> float:
        # Music mode has no rate limit; standard mode allows ~1 cmd/s.
        return 0.0 if self.music_mode else 1.1

    async def _apply(self, desired: DesiredState):
        loop_time = asyncio.get_event_loop().time
        if desired.mode == "hsv":
            logger.trace(f"[{self.ip}] apply HSV ({desired.hue},{desired.saturation},{desired.brightness}) "
                         f"over {desired.duration:.3f}s")
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
