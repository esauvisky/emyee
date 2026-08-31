"""
LAN control reset for Yeelight bulbs via miIO protocol.

Forces port 55443 open by cycling LAN control off→on before discovery.

Tokens file format  (tokens.json):
    {
        "192.168.1.46": "32-hex-char-token",
        "192.168.1.47": "..."
    }
"""
import hashlib
import json
import socket
import time

from loguru import logger

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False


def _md5(data: bytes) -> bytes:
    return hashlib.md5(data).digest()


def _encrypt(token: bytes, plaintext: bytes) -> bytes:
    key = _md5(token)
    iv  = _md5(key + token)
    enc = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend()).encryptor()
    pad = 16 - len(plaintext) % 16
    return enc.update(plaintext + bytes([pad] * pad)) + enc.finalize()


def _decrypt(token: bytes, ciphertext: bytes) -> bytes:
    key = _md5(token)
    iv  = _md5(key + token)
    dec = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend()).decryptor()
    raw = dec.update(ciphertext) + dec.finalize()
    return raw[:-raw[-1]]


def _miio(ip: str, token_hex: str, method: str, params, timeout: float = 3.0) -> dict:
    token = bytes.fromhex(token_hex)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        # Hello handshake to get device ID and clock stamp
        s.sendto(bytes.fromhex('21310020' + 'ff' * 28), (ip, 54321))
        resp, _ = s.recvfrom(1024)
        did   = int.from_bytes(resp[8:12],  'big')
        stamp = int.from_bytes(resp[12:16], 'big')

        payload   = json.dumps({'id': int(time.time()), 'method': method, 'params': params}).encode()
        encrypted = _encrypt(token, payload)
        length    = 32 + len(encrypted)

        header       = bytearray(32)
        header[0:2]  = b'\x21\x31'
        header[2:4]  = length.to_bytes(2, 'big')
        header[8:12] = did.to_bytes(4, 'big')
        header[12:16] = (stamp + 1).to_bytes(4, 'big')
        header[16:32] = _md5(header[:16] + token + encrypted)

        s.sendto(bytes(header) + encrypted, (ip, 54321))
        data, _ = s.recvfrom(1024)
        return json.loads(_decrypt(token, data[32:]).decode('utf-8', errors='ignore'))
    finally:
        s.close()


def load_tokens(path: str = 'tokens.json') -> dict:
    """Load {ip: token_hex} from a plain-text JSON file."""
    with open(path) as f:
        return json.load(f)


def reset_lan_control(tokens: dict, pause: float = 1.5) -> None:
    """Cycle LAN control off→on for every IP in *tokens* to force port 55443 open."""
    if not _CRYPTO_OK:
        logger.warning("cryptography package not installed — skipping LAN reset")
        return

    for ip, token in tokens.items():
        try:
            _miio(ip, token, 'set_ps', ['cfg_lan_ctrl', '0'])
            logger.debug("LAN ctrl off: {}", ip)
            time.sleep(0.2)
            _miio(ip, token, 'set_ps', ['cfg_lan_ctrl', '1'])
            logger.info("LAN ctrl reset OK: {}", ip)
        except Exception as e:
            logger.warning("LAN reset failed for {}: {}", ip, e)

    logger.info("Waiting {:.1f}s for bulbs to reopen port 55443...", pause)
    time.sleep(pause)
