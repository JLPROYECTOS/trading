"""Genera los iconos PNG de la app (solo biblioteca estándar)."""
import math
import os
import struct
import zlib

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "web", "icons")
LINE = [(0.20, 0.68), (0.40, 0.48), (0.56, 0.58), (0.80, 0.30)]
GREEN = (34, 197, 94)


def png(path, w, h, px):
    raw = b"".join(b"\x00" + bytes(px[y * w * 4:(y + 1) * w * 4]) for y in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(data)


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def make(size, mode):
    """mode: any (esquinas redondeadas), maskable (fondo completo, dibujo en zona segura), badge (blanco)."""
    s = size
    scale, off = (0.62, 0.19) if mode == "maskable" else (1.0, 0.0)
    pts = [((off + x * scale) * s, (off + y * scale) * s) for x, y in LINE]
    thick = s * (0.085 if mode == "badge" else 0.065) * (scale if mode == "maskable" else 1)
    dot_r = thick * 1.25
    rad = s * 0.22
    px = bytearray(s * s * 4)
    for y in range(s):
        for x in range(s):
            cx, cy = x + 0.5, y + 0.5
            # fondo
            if mode == "badge":
                r, g, b, a = 0, 0, 0, 0.0
            else:
                tt = y / s
                r, g, b = int(17 - 6 * tt), int(24 - 9 * tt), int(39 - 19 * tt)
                a = 1.0
                if mode == "any":
                    qx, qy = min(cx, s - cx), min(cy, s - cy)
                    if qx < rad and qy < rad:
                        dd = math.hypot(rad - qx, rad - qy)
                        a = max(0.0, min(1.0, rad - dd + 0.5))
            # rejilla tenue
            # línea
            d = min(seg_dist(cx, cy, *pts[i], *pts[i + 1]) for i in range(len(pts) - 1))
            la = max(0.0, min(1.0, thick / 2 - d + 0.5))
            dd = math.hypot(cx - pts[-1][0], cy - pts[-1][1])
            la = max(la, max(0.0, min(1.0, dot_r - dd + 0.5)))
            col = (255, 255, 255) if mode == "badge" else GREEN
            if la > 0:
                r = int(r * (1 - la) + col[0] * la)
                g = int(g * (1 - la) + col[1] * la)
                b = int(b * (1 - la) + col[2] * la)
                a = max(a, la) if mode == "badge" else a
            i = (y * s + x) * 4
            px[i:i + 4] = bytes((r, g, b, int(a * 255)))
    return px


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for name, size, mode in (("icon-192.png", 192, "any"), ("icon-512.png", 512, "any"),
                             ("icon-maskable-512.png", 512, "maskable"), ("badge-96.png", 96, "badge")):
        png(os.path.join(OUT, name), size, size, make(size, mode))
        print("ok", name)
