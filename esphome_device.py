"""ESPHome light backend driven over the native API (port 6053)."""

import asyncio
from colorsys import hsv_to_rgb
from typing import Optional

from aioesphomeapi import APIClient, ColorMode, LightColorCapability, LightInfo
from loguru import logger

from light_device import BaseLightDevice, DesiredState

DEFAULT_PORT = 6053
# light_command is fire-and-forget over a persistent TCP connection, so the
# only real ceiling is how fast the ESP can chew through frames. Well under
# the Yeelight non-music limit and tighter than Tasmota's HTTP round-trip.
MIN_COMMAND_INTERVAL = 0.02
CONNECT_TIMEOUT = 20.0
# ESPHome interpolates brightness linearly and only then applies gamma_correct
# (2.8 by default on the Athom firmware), so the bottom of the range is crushed:
# 12% sent is ~0.3% light, and any ramp out of a trough looks back-loaded no
# matter how long it is. Pre-compensate with a power law so the net curve is a
# mild 2.8/OUTPUT_GAMMA ≈ 1.27: 1% → 12%, 10% → 35%, 50% → 73%, 0 stays off.
OUTPUT_GAMMA = 2.2


class UnsupportedLightError(RuntimeError):
    pass


class ESPHomeDevice(BaseLightDevice):
    """An ESPHome light (RGB or RGBCW) spoken to over the native API.

    Same surface as LightDevice so LightsController can drive a mixed fleet.
    Unlike Tasmota, ESPHome honours a per-command transition, so `duration`
    is passed straight through as transition_length instead of being dropped.
    """

    def __init__(
        self,
        ip: str,
        port: int = DEFAULT_PORT,
        noise_psk: Optional[str] = None,
        password: Optional[str] = None,
        min_command_interval: float = MIN_COMMAND_INTERVAL,
    ):
        super().__init__(ip)
        self.port = port
        self.min_command_interval = min_command_interval
        self.music_mode = True
        self._noise_psk = noise_psk
        self._password = password
        # APIClient grabs the running loop in __init__, so it cannot be built
        # here — connect() constructs it on the loop the worker will run on.
        self._client: Optional[APIClient] = None
        self._key: Optional[int] = None
        self._color_mode: Optional[int] = None
        self._white_modes: list[int] = []

    # ---- Connection ----

    async def connect(self) -> "ESPHomeDevice":
        """Open the API connection and latch onto the device's light entity.

        Must be awaited on the same loop the worker will later run on — the
        aioesphomeapi connection is bound to the loop it was opened on.
        """
        self._client = APIClient(self.ip, self.port, self._password, noise_psk=self._noise_psk)
        # Noise (or an open API) authenticates during the handshake; a
        # ConnectRequest on top of that gets no reply and just hangs.
        await asyncio.wait_for(self._client.connect(login=False), CONNECT_TIMEOUT)

        info = await self._client.device_info()
        self.model = info.name or "unknown"

        entities, _ = await self._client.list_entities_services()
        lights = [e for e in entities if isinstance(e, LightInfo)]
        if not lights:
            await self._client.disconnect()
            raise UnsupportedLightError(f"{self.ip} is an ESPHome device ({self.model}) but exposes no light entity")
        light = lights[0]
        self._key = light.key

        modes = list(light.supported_color_modes)
        if ColorMode.RGB in modes:
            self._color_mode = int(ColorMode.RGB)
        else:
            # Anything else this controller can meaningfully colour-shift.
            rgb_capable = [m for m in modes if (int(m) & int(ColorMode.RGB)) == int(ColorMode.RGB)]
            if not rgb_capable:
                await self._client.disconnect()
                raise UnsupportedLightError(f"{self.ip} light {light.name!r} has no RGB colour mode: {modes}")
            self._color_mode = int(rgb_capable[0])
        tunable = int(LightColorCapability.COLOR_TEMPERATURE | LightColorCapability.COLD_WARM_WHITE)
        self._white_modes = [
            int(m) for m in modes
            if int(m) & tunable and not int(m) & int(LightColorCapability.RGB)
        ]

        logger.info(
            f"ESPHome {self.model} at {self.ip} — firmware {info.esphome_version}, "
            f"light {light.name!r}, colour mode {ColorMode(self._color_mode).name}"
        )
        return self

    async def stop(self, final: Optional[DesiredState] = None):
        await super().stop(final)
        await self._client.disconnect()

    # ---- Backend hooks ----

    def _post_command_delay(self) -> float:
        return self.min_command_interval

    def _visible(self, brightness: float) -> float:
        b = max(0.0, min(100.0, brightness))
        return 100.0 * (b / 100) ** (1 / OUTPUT_GAMMA)

    async def _apply(self, desired: DesiredState):
        # light_command is fire-and-forget (no ack), so there is no round trip
        # to time — estimated_latency keeps its default rather than collapsing
        # to zero and pulling every dispatch_at forward.
        if desired.mode == "hsv":
            hue = desired.hue % 360
            sat = max(0.0, min(100.0, desired.saturation))
            bri = self._visible(desired.brightness)
            logger.trace(f"[{self.ip}] apply HSV ({hue:.1f},{sat:.1f},{bri:.1f}) over {desired.duration:.3f}s")
            self._client.light_command(
                key=self._key,
                state=True,
                brightness=bri / 100,
                color_mode=self._color_mode,
                color_brightness=1.0,
                rgb=hsv_to_rgb(hue / 360, sat / 100, 1.0),
                transition_length=desired.duration,
            )

        elif desired.mode == "brightness":
            bri = self._visible(desired.brightness)
            logger.trace(f"[{self.ip}] apply brightness {bri:.1f} over {desired.duration:.3f}s")
            # state=True on every command: brightness 0 would otherwise latch
            # the entity off and a later brightness-only command would not
            # bring it back, leaving a pulse trough permanently dark.
            self._client.light_command(
                key=self._key,
                state=True,
                brightness=bri / 100,
                transition_length=desired.duration,
            )

        elif desired.mode == "ct":
            if not self._white_modes:
                raise RuntimeError(f"{self.ip} has no white colour mode separate from RGB to set {desired.kelvin}K in")
            bri = self._visible(desired.brightness)
            logger.trace(f"[{self.ip}] apply {desired.kelvin}K at {bri:.1f} over {desired.duration:.3f}s")
            self._client.light_command(
                key=self._key,
                state=True,
                brightness=bri / 100,
                color_mode=self._white_modes[0],
                color_temperature=1_000_000 / desired.kelvin,
                transition_length=desired.duration,
            )

        elif desired.mode == "on":
            logger.trace(f"[{self.ip}] turn_on over {desired.duration:.3f}s")
            self._client.light_command(key=self._key, state=True, transition_length=desired.duration)

        elif desired.mode == "off":
            logger.trace(f"[{self.ip}] turn_off over {desired.duration:.3f}s")
            self._client.light_command(key=self._key, state=False, transition_length=desired.duration)
