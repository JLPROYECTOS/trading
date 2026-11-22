#!/usr/bin/env python3
"""Servidor de la app de señales.

- Sirve la app web instalable (PWA) en http://127.0.0.1:8765
- Arranca el motor de señales (engine.py)
- Envía notificaciones push al móvil
- SOLO envía una orden a Bybit cuando el usuario pulsa AUTORIZAR y escribe su PIN.

Escucha solo en 127.0.0.1; para el móvil se publica con `tailscale serve` (HTTPS privado).
"""
import base64
import hmac
import json
import mimetypes
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bybit  # noqa: E402
import engine as eng  # noqa: E402
import strategy as st  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
VAPID_FILE = os.path.join(bybit.DATA_DIR, "vapid_private.pem")
SUBS_FILE = os.path.join(bybit.DATA_DIR, "push.json")
PIN_FAILS = {"n": 0, "total": 0, "until": 0.0}
LOGIN_FAILS = {"n": 0, "until": 0.0}
SESSIONS = {}  # token -> caducidad (epoch)
SESSION_DAYS = 60
CACHE = {}
ENGINE = None

try:
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid01
    from pywebpush import WebPushException, webpush
    PUSH_OK = True
except ImportError:
    PUSH_OK = False


class AppError(Exception):
    pass


# ------------------------------------------------------------------ push

def vapid_public():
    if not PUSH_OK:
        return None
    if not os.path.exists(VAPID_FILE) and os.environ.get("VAPID_PRIVATE_KEY"):
        os.makedirs(bybit.DATA_DIR, exist_ok=True)
        with open(VAPID_FILE, "w", encoding="utf-8") as fh:
            fh.write(os.environ["VAPID_PRIVATE_KEY"].replace("\\n", "\n"))
    if not os.path.exists(VAPID_FILE):
        os.makedirs(bybit.DATA_DIR, exist_ok=True)
        v = Vapid01()
        v.generate_keys()
        v.save_key(VAPID_FILE)
    v = Vapid01.from_file(VAPID_FILE)
    raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def load_subs():
    try:
        with open(SUBS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def save_subs(subs):
    os.makedirs(bybit.DATA_DIR, exist_ok=True)
    with open(SUBS_FILE, "w", encoding="utf-8") as fh:
        json.dump(subs, fh)


def notify(title, body, tag="signal", url="/"):
    if not PUSH_OK:
        return 0

    def run():
        subs, keep = load_subs(), []
        for s in subs:
            try:
                webpush(subscription_info=s, data=json.dumps({"title": title, "body": body, "tag": tag, "url": url}),
                        vapid_private_key=VAPID_FILE, vapid_claims={"sub": "mailto:noreply@example.com"}, ttl=900)
                keep.append(s)
            except WebPushException as e:
                code = getattr(e.response, "status_code", None)
                if code not in (404, 410):
                    keep.append(s)
                print("push error:", code, e)
            except Exception as e:  # noqa
                keep.append(s)
                print("push error:", e)
        if len(keep) != len(subs):
            save_subs(keep)
    threading.Thread(target=run, daemon=True).start()
    return len(load_subs())


# ------------------------------------------------------------------ estado

def cached(name, ttl, fn):
    c = CACHE.get(name)
    if c and time.time() - c[0] < ttl:
        return c[1]
    try:
        val = {"ok": True, "data": fn()}
    except bybit.BybitError as e:
        val = {"ok": False, "error": str(e)}
    CACHE[name] = (time.time(), val)
    return val


def state():
    cfg = bybit.load_env()
    with eng.LOCK:
        lst = eng.load_signals()
        if eng.expire_old(lst):
            eng.save_signals(lst)
    lst = [s for s in lst if s.get("v") == 2]
    pending = [s for s in lst if s["status"] == "pending"]
    prices = {}
    if pending:
        tk = cached("tickers", 4, lambda: bybit.tickers(cfg))
        if tk["ok"]:
            prices = {s["symbol"]: float(tk["data"][s["symbol"]]["lastPrice"]) for s in pending if s["symbol"] in tk["data"]}
    configured = bool(cfg.get("BYBIT_API_KEY") and cfg.get("BYBIT_API_SECRET"))
    out = {
        "env": cfg["BYBIT_ENV"], "real": cfg["BYBIT_ENV"] in bybit.REAL_MONEY,
        "configured": configured, "pin_set": bool(cfg.get("APP_PIN")),
        "config": {k: cfg[k] for k in ("QUOTE", "MAX_LEVERAGE", "RISK_PCT", "SIGNAL_INTERVAL", "MIN_EV_R",
                                       "SL_ATR", "TP_R", "HORIZON_H", "EXPIRES_MIN")},
        "push": {"available": PUSH_OK, "devices": len(load_subs())},
        "engine": ENGINE.status if ENGINE else None,
        "pending": pending, "prices": prices,
        "history": [s for s in lst if s["status"] != "pending"][-25:][::-1],
        "now": eng.iso(eng.now()),
    }
    if configured:
        out["wallet"] = cached("wallet", 15, lambda: bybit.wallet(cfg))
        out["orders"] = cached("orders", 15, lambda: bybit.open_orders(cfg))
    return out


# ------------------------------------------------------------------ acciones

def check_pin(cfg, pin):
    if not cfg.get("APP_PIN"):
        raise AppError("Define APP_PIN antes de autorizar.")
    if PIN_FAILS["total"] >= 15:
        raise AppError("Autorizaciones bloqueadas por demasiados PIN incorrectos. Reinicia el servidor para desbloquear.")
    if time.time() < PIN_FAILS["until"]:
        raise AppError("Demasiados PIN incorrectos. Espera %d min." % ((PIN_FAILS["until"] - time.time()) // 60 + 1))
    if not hmac.compare_digest(str(pin or "").encode(), cfg["APP_PIN"].encode()):
        PIN_FAILS["n"] += 1
        PIN_FAILS["total"] += 1
        if PIN_FAILS["total"] >= 15:
            notify("⚠️ Autorizaciones bloqueadas", "15 PIN incorrectos. Si no has sido tú, cambia el PIN y revisa tu cuenta.", tag="pin-lock")
        elif PIN_FAILS["n"] >= 5:
            PIN_FAILS.update(n=0, until=time.time() + 1800)
            notify("⚠️ PIN incorrecto 5 veces", "Autorizaciones bloqueadas 30 min.", tag="pin-lock")
        raise AppError("PIN incorrecto.")
    PIN_FAILS["n"] = 0


# ------------------------------------------------------------------ acceso (modo nube)

def password_required(cfg):
    return bool(cfg.get("APP_PASSWORD"))


def session_ok(handler):
    cfg = bybit.load_env()
    if not password_required(cfg):
        return True
    for part in (handler.headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "sess" and SESSIONS.get(v, 0) > time.time():
            return True
    return False


def login(password):
    cfg = bybit.load_env()
    if not password_required(cfg):
        return None
    if time.time() < LOGIN_FAILS["until"]:
        raise AppError("Demasiados intentos. Espera %d min." % ((LOGIN_FAILS["until"] - time.time()) // 60 + 1))
    if not hmac.compare_digest(str(password or "").encode(), cfg["APP_PASSWORD"].encode()):
        LOGIN_FAILS["n"] += 1
        if LOGIN_FAILS["n"] >= 5:
            LOGIN_FAILS.update(n=0, until=time.time() + 900)
        time.sleep(1)
        raise AppError("Contraseña incorrecta.")
    LOGIN_FAILS["n"] = 0
    import secrets as _s
    tok = _s.token_urlsafe(32)
    SESSIONS[tok] = time.time() + SESSION_DAYS * 86400
    return tok


def _update(sid, **fields):
    with eng.LOCK:
        lst = eng.load_signals()
        for s in lst:
            if s["id"] == sid:
                s.update(fields)
                out = s
        eng.save_signals(lst)
    return out


def approve(sid, pin):
    cfg = bybit.load_env()
    check_pin(cfg, pin)
    with eng.LOCK:
        lst = eng.load_signals()
        eng.expire_old(lst)
        s = next((x for x in lst if x["id"] == sid and x.get("v") == 2), None)
        if not s:
            raise AppError("Esa señal no existe.")
        if s["status"] != "pending":
            raise AppError("Esa señal ya está: %s." % s["status"])
        if s["env"] != cfg["BYBIT_ENV"]:
            raise AppError("La señal se creó para otro entorno (%s)." % s["env"])
        s["status"] = "checking"
        eng.save_signals(lst)

    try:
        price = float(bybit.public(cfg, "/v5/market/tickers", category="spot", symbol=s["symbol"])["list"][0]["lastPrice"])
    except bybit.BybitError as e:
        _update(sid, status="pending")
        raise AppError(str(e))
    long_ = s["side"] == "long"
    entry, sl, tp = s["entry"], float(s["stop"]), float(s["target"])
    if (long_ and (price <= sl or price >= tp)) or (not long_ and (price >= sl or price <= tp)):
        _update(sid, status="expired", error="El precio ya tocó el stop o el objetivo (%s)." % price)
        raise AppError("El precio (%s) ya ha tocado el stop o el objetivo. Señal anulada." % price)
    if abs(price - entry) > 0.5 * abs(entry - sl):
        _update(sid, status="expired", error="El precio se movió demasiado (%s → %s)." % (entry, price))
        raise AppError("El precio se ha movido demasiado desde la señal (%s → %s). Señal anulada." % (entry, price))

    slip = 0.003
    limit = st.round_level(price * (1 + slip), s["tick"], "up") if long_ else st.round_level(price * (1 - slip), s["tick"], "down")
    order = {"category": "spot", "symbol": s["symbol"], "side": "Buy" if long_ else "Sell",
             "orderType": "Limit", "price": limit, "qty": s["qty"], "timeInForce": "IOC",
             "isLeverage": 1 if (s["leverage"] > 1 or not long_) else 0,
             "takeProfit": s["target"], "stopLoss": s["stop"],
             "tpOrderType": "Market", "slOrderType": "Market", "orderLinkId": s["id"]}
    _update(sid, status="sending", approved_at=eng.iso(eng.now()), price_at_approval=price, order=order)
    try:
        res = bybit.private(cfg, "POST", "/v5/order/create", order)
    except bybit.BybitError as e:
        _update(sid, status="failed", error=str(e))
        raise AppError("Bybit no aceptó la orden: %s" % e)
    order_id = res.get("orderId")
    CACHE.pop("wallet", None)
    CACHE.pop("orders", None)

    # Verificación: ¿se ejecutó? ¿quedaron el SL y el TP?
    info = None
    for _ in range(4):
        time.sleep(1.2)
        try:
            for path in ("/v5/order/history", "/v5/order/realtime"):
                lst2 = bybit.private(cfg, "GET", path, {"category": "spot", "orderId": order_id}).get("list") or []
                if lst2:
                    info = lst2[0]
                    break
        except bybit.BybitError:
            pass
        if info and info.get("orderStatus") not in ("New", "PartiallyFilled", "Untriggered", "Created"):
            break
    filled = float((info or {}).get("cumExecQty") or 0)
    avg = float((info or {}).get("avgPrice") or 0)
    tpv, slv = (info or {}).get("takeProfit"), (info or {}).get("stopLoss")
    protected = bool(tpv and slv and float(tpv) > 0 and float(slv) > 0)
    if info is None:
        _update(sid, status="sent", order_id=order_id, warning="No se pudo verificar la orden. Revísala en Bybit.")
        return "Orden enviada (ID %s), pero no se pudo verificar. Revísala en Bybit." % order_id
    if filled <= 0:
        _update(sid, status="not_filled", order_id=order_id, error="No se ejecutó: el precio se movió más de un 0,3 %.")
        raise AppError("La orden no se ejecutó: el precio se movió más de un 0,3 %. No se ha comprado ni vendido nada.")
    fields = dict(status="filled", order_id=order_id, fill_qty=filled, fill_price=avg)
    if not protected:
        fields["warning"] = "La orden se ejecutó pero Bybit no muestra SL/TP. Ponlos tú en Bybit YA."
        _update(sid, **fields)
        notify("⚠️ Sin stop en %s" % s["symbol"], fields["warning"], tag="warn-" + sid)
        return "Ejecutada %s %s a %s, PERO sin SL/TP visibles. Ponlos en Bybit." % (filled, s["base"], avg)
    _update(sid, **fields)
    return "Ejecutada: %s %s a %s. Stop %s · objetivo %s." % (filled, s["base"], avg, s["stop"], s["target"])


def reject(sid):
    with eng.LOCK:
        lst = eng.load_signals()
        s = next((x for x in lst if x["id"] == sid), None)
        if not s or s["status"] != "pending":
            raise AppError("Esa señal ya no está pendiente.")
        s["status"] = "rejected"
        s["rejected_at"] = eng.iso(eng.now())
        eng.save_signals(lst)
    return "Señal descartada."


# ------------------------------------------------------------------ HTTP

CLOUD_SUFFIXES = (".ts.net", ".onrender.com", ".up.railway.app", ".sslip.io", ".nip.io")


def host_ok(h):
    name = (h or "").rsplit(":", 1)[0].lower()
    extra = [x.strip().lower() for x in (bybit.load_env().get("ALLOWED_HOSTS") or "").split(",") if x.strip()]
    return name in ("127.0.0.1", "localhost") or name in extra or name.endswith(CLOUD_SUFFIXES)


class Handler(BaseHTTPRequestHandler):
    server_version = "TradingApp"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not host_ok(self.headers.get("Host")):
            return self.send(403, {"error": "host no permitido"})
        path = urlsplit(self.path).path
        if path == "/healthz":
            return self.send(200, {"ok": True})
        if path.startswith("/api/") and path != "/api/push/key" and not session_ok(self):
            return self.send(401, {"ok": False, "login": True, "error": "Inicia sesión."})
        if path == "/api/state":
            return self.send(200, state(), extra={"Cache-Control": "no-store"})
        if path == "/api/push/key":
            return self.send(200, {"key": vapid_public()})
        rel = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB, rel))
        if not full.startswith(WEB + os.sep) or not os.path.isfile(full):
            return self.send(404, {"error": "no encontrado"})
        ctype = {".webmanifest": "application/manifest+json", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8"}.get(
            os.path.splitext(full)[1], mimetypes.guess_type(full)[0] or "application/octet-stream")
        with open(full, "rb") as fh:
            data = fh.read()
        cache = "no-cache" if rel in ("index.html", "sw.js", "app.js", "style.css") else "max-age=86400"
        self.send(200, data, ctype, {"Cache-Control": cache})

    def do_POST(self):
        origin = self.headers.get("Origin")
        if (not host_ok(self.headers.get("Host")) or self.headers.get("X-Requested-With") != "trading-app"
                or (origin and not host_ok(urlsplit(origin).netloc))):
            return self.send(403, {"ok": False, "error": "petición no permitida"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            path = urlsplit(self.path).path
            if path == "/api/login":
                tok = login(body.get("password"))
                if not tok:
                    return self.send(200, {"ok": True, "message": "Sin contraseña configurada."})
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").startswith("https") else ""
                return self.send(200, {"ok": True, "message": "Sesión iniciada."}, extra={
                    "Set-Cookie": "sess=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d%s" % (tok, SESSION_DAYS * 86400, secure)})
            if not session_ok(self):
                return self.send(401, {"ok": False, "login": True, "error": "Inicia sesión."})
            if path == "/api/approve":
                return self.send(200, {"ok": True, "message": approve(body.get("id"), body.get("pin"))})
            if path == "/api/reject":
                return self.send(200, {"ok": True, "message": reject(body.get("id"))})
            if path == "/api/scan":
                ENGINE.scan_now()
                return self.send(200, {"ok": True, "message": "Revisando el mercado ahora…"})
            if path == "/api/push/subscribe":
                sub = body.get("subscription") or {}
                if not sub.get("endpoint", "").startswith("https://"):
                    raise AppError("Suscripción no válida.")
                subs = [s for s in load_subs() if s.get("endpoint") != sub["endpoint"]] + [sub]
                save_subs(subs)
                return self.send(200, {"ok": True, "message": "Notificaciones activadas en este dispositivo."})
            if path == "/api/push/test":
                n = notify("Prueba de notificación", "Así te llegarán las señales. Toca para abrir la app.", tag="test")
                return self.send(200, {"ok": True, "message": "Enviada a %d dispositivo(s)." % n})
            self.send(404, {"ok": False, "error": "no encontrado"})
        except AppError as e:
            self.send(400, {"ok": False, "error": str(e)})
        except Exception as e:  # noqa
            traceback.print_exc()
            self.send(500, {"ok": False, "error": "Error interno: %s" % e})


def main():
    global ENGINE
    cfg = bybit.load_env()
    port = int(cfg.get("APP_PORT") or 8765)
    host = cfg.get("HOST") or "127.0.0.1"
    if host not in ("127.0.0.1", "localhost") and not password_required(cfg):
        print("ERROR: para escuchar en %s (internet) define APP_PASSWORD." % host)
        sys.exit(1)
    if PUSH_OK:
        vapid_public()
    ENGINE = eng.Engine(notify=notify)
    ENGINE.start()
    srv = ThreadingHTTPServer((host, port), Handler)
    print("App de señales en http://127.0.0.1:%d  (entorno: %s%s)" % (
        port, cfg["BYBIT_ENV"].upper(), ", DINERO REAL" if cfg["BYBIT_ENV"] in bybit.REAL_MONEY else ""))
    print("Notificaciones push: %s" % ("sí" if PUSH_OK else "no (pip install -r requirements.txt)"))
    print("Configuración: %s" % bybit.ENV_FILE)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
