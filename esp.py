import socket
import machine
import neopixel


np = neopixel.NeoPixel(machine.Pin(5), 60)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(('', 42424))


while True:
    line, _ = sock.recvfrom(180)

    if len(line) < 180:
        continue

    for i in range(60):
        np[i] = (line[i * 3], line[i * 3 + 1], line[i * 3 + 2])

    np.write()
