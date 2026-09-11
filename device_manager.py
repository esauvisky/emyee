# device_manager.py
from typing import List, Optional
import socket
from yeelight import discover_bulbs, Bulb
from loguru import logger
from light_device import LightDevice
from lan_reset import reset_device

def get_real_local_ip_for(target_ip: str) -> str:
    """Resolve a interface de rede real que chega na lâmpada (ignora docker/loopback)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, 55443))
        return s.getsockname()[0]
    except Exception:
        return "0.0.0.0"
    finally:
        s.close()

class DeviceManager:
    def __init__(self, effect="smooth", auto_on=False, enable_music_mode=True, tokens: dict = None):
        self.effect = effect
        self.auto_on = auto_on
        self.enable_music_mode = enable_music_mode
        self.tokens = tokens or {}

    def connect_to_ips(self, ips: List[str], port: int = 55443) -> List[LightDevice]:
        logger.info("Connecting directly to {} bulb(s): {}", len(ips), ips)
        devices: List[LightDevice] = []
        for ip in ips:
            if ip in self.tokens:
                reset_device(ip, self.tokens[ip])
            try:
                bulb = Bulb(ip, port, effect=self.effect, auto_on=True)
                music_mode_active = False
                if self.enable_music_mode:
                    local_ip = get_real_local_ip_for(ip)
                    try:
                        bulb.start_music(ip=local_ip)
                        logger.info(f"Music mode enabled on {ip} via interface {local_ip}")
                        music_mode_active = True
                    except Exception as me:
                        logger.warning(f"Music mode failed on {ip} ({me}). Falling back to standard mode.")
                    if not music_mode_active:
                        try:
                            bulb.start_music()
                            logger.info(f"Music mode enabled on {ip}")
                            music_mode_active = True
                        except Exception as fallback_error:
                            logger.warning(f"Music mode fallback failed on {ip} ({fallback_error}). Using standard mode.")
                light_device = LightDevice(bulb)
                light_device.music_mode = music_mode_active
                devices.append(light_device)
                logger.info(f"Initialized LightDevice at {ip}:{port}")
            except Exception as e:
                logger.error(f"Failed to initialize bulb at {ip}:{port}: {e}")
        logger.info(f"Connected to {len(devices)} LightDevice(s).")
        return devices

    def discover_devices(self) -> List[LightDevice]:
        logger.info("Discovering bulbs...")
        bulbs_info = discover_bulbs()
        devices: List[LightDevice] = []
        seen_ips = set()

        for bulb_info in bulbs_info:
            ip = bulb_info["ip"]
            if ip in seen_ips:
                logger.debug(f"Ignoring duplicate discovery result for {ip}")
                continue
            seen_ips.add(ip)
            port = bulb_info.get("port", 55443)
            if ip in self.tokens:
                reset_device(ip, self.tokens[ip])
            try:
                bulb = Bulb(ip, port, effect=self.effect, auto_on=True)

                music_mode_active = False
                if self.enable_music_mode:
                    local_ip = get_real_local_ip_for(ip)
                    try:
                        # passa o IP correto explicitamente pra evitar bind no docker
                        bulb.start_music(ip=local_ip)
                        logger.info(f"Music mode enabled on {ip} via interface {local_ip}")
                        music_mode_active = True
                    except Exception as me:
                        logger.warning(f"Music mode failed on {ip} ({me}). Falling back to standard mode.")

                    if not music_mode_active:
                        try:
                            bulb.start_music()
                            logger.info(f"Music mode enabled on {ip}")
                            music_mode_active = True
                        except Exception as fallback_error:
                            logger.warning(f"Music mode fallback failed on {ip} ({fallback_error}). Using standard mode.")
                light_device = LightDevice(bulb)
                light_device.music_mode = music_mode_active
                devices.append(light_device)
                logger.info(f"Initialized LightDevice at {ip}:{port}")
            except Exception as e:
                logger.error(f"Failed to initialize bulb at {ip}:{port}: {e}")

        logger.info(f"Found and initialized {len(devices)} LightDevice(s).")
        return devices
