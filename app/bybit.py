"""Cliente mínimo de la API V5 de Bybit (pública y privada). Solo biblioteca estándar."""
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# Datos y claves: carpeta "datos-trading" junto a la carpeta "trading" (fuera del repositorio).
DATA_DIR = os.environ.get("TRADING_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "datos-trading")
ENV_FILE = os.path.join(DATA_DIR, ".env")
UA = "trading-signal-app/2.0"
HOSTS = {"eu": "https://api.bybit.eu", "live": "https://api.bybit.com",
         "demo": "https://api-demo.bybit.com", "testnet": "https://api-testnet.bybit.com"}
PUBLIC_HOSTS = {"eu": "https://api.bybit.eu", "live": "https://api.bybit.com",
                "demo": "https://api.bybit.com", "testnet": "https://api-testnet.bybit.com"}
REAL_MONEY = {"eu", "live"}

DEFAULTS = {
    "BYBIT_ENV": "eu",
    "APP_PORT": "8765",
    "QUOTE": "USDC",          # moneda con la que se opera
    "MAX_LEVERAGE": "4",      # apalancamiento máximo de las propuestas (margen spot)
    "RISK_PCT": "10",         # % del capital que se pierde como máximo si salta el stop
    "SIGNAL_INTERVAL": "60",  # velas (min) en las que se buscan señales
    "SCAN_EVERY_MIN": "5",    # cada cuánto mira el motor si hay vela nueva
    "MIN_TURNOVER": "20000",  # volumen 24h mínimo del par (en QUOTE)
    "MIN_EV_R": "0.10",       # resultado esperado mínimo (en R) para notificar
    "MIN_HIST_N": "8",        # veces mínimas que la regla debe haberse dado en el histórico
    "SL_ATR": "1.5",          # stop a 1,5 ATR
    "TP_R": "2",              # objetivo a 2R
    "HORIZON_H": "24",        # horizonte de la simulación
    "FEE_PCT": "0.1",         # comisión por lado
    "EXPIRES_MIN": "15",      # minutos para autorizar una señal
    "ENGINE": "on",
}

ENV_TEMPLATE = """# Configuración (NO subir a GitHub). Ver config.example.env para todas las opciones.
BYBIT_ENV=eu
BYBIT_API_KEY=
BYBIT_API_SECRET=
APP_PIN=
"""


class BybitError(Exception):
    pass


ENV_KEYS = set(DEFAULTS) | {"BYBIT_API_KEY", "BYBIT_API_SECRET", "APP_PIN", "APP_PASSWORD", "ALLOWED_HOSTS", "HOST", "PORT"}


def load_env():
    """Lee datos-trading/.env (PC) y, encima, las variables de entorno (servidor en la nube)."""
    cloud = bool(os.environ.get("BYBIT_API_KEY"))
    if not cloud and not os.path.exists(ENV_FILE):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(ENV_FILE, "w", encoding="utf-8") as fh:
                fh.write(ENV_TEMPLATE)
        except OSError:
            pass
    cfg = dict(DEFAULTS)
    try:
        with open(ENV_FILE, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    for k in ENV_KEYS:
        if os.environ.get(k):
            cfg[k] = os.environ[k].strip()
    if cfg.get("PORT"):
        cfg["APP_PORT"] = cfg["PORT"]
    if cfg["BYBIT_ENV"].lower() not in HOSTS:
        cfg["BYBIT_ENV"] = "eu"
    cfg["BYBIT_ENV"] = cfg["BYBIT_ENV"].lower()
    return cfg


def _http(req):
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read()[:200].decode("utf-8", "replace")
        if e.code == 401:
            raise BybitError("Bybit rechazó la clave (401). Revisa BYBIT_API_KEY/SECRET y BYBIT_ENV.")
        raise BybitError("Bybit HTTP %s %s" % (e.code, body))
    except (urllib.error.URLError, OSError) as e:
        raise BybitError("No se pudo conectar con Bybit: %s" % e)
    if data.get("retCode") != 0:
        raise BybitError("Bybit: %s (código %s)" % (data.get("retMsg"), data.get("retCode")))
    return data["result"]


def public(cfg, path, **params):
    url = PUBLIC_HOSTS[cfg["BYBIT_ENV"]] + path + "?" + urllib.parse.urlencode(params)
    return _http(urllib.request.Request(url, headers={"User-Agent": UA}))


def private(cfg, method, path, params=None):
    key, sec = cfg.get("BYBIT_API_KEY", ""), cfg.get("BYBIT_API_SECRET", "")
    if not key or not sec:
        raise BybitError("Faltan BYBIT_API_KEY / BYBIT_API_SECRET en %s" % ENV_FILE)
    ts, rw = str(int(time.time() * 1000)), "10000"
    base = HOSTS[cfg["BYBIT_ENV"]]
    if method == "GET":
        payload = urllib.parse.urlencode(params or {})
        url, body = base + path + ("?" + payload if payload else ""), None
    else:
        payload = json.dumps(params or {}, separators=(",", ":"))
        url, body = base + path, payload.encode("utf-8")
    sign = hmac.new(sec.encode(), (ts + key + rw + payload).encode(), hashlib.sha256).hexdigest()
    headers = {"X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": rw,
               "X-BAPI-SIGN": sign, "X-BAPI-SIGN-TYPE": "2",
               "Content-Type": "application/json", "User-Agent": UA}
    return _http(urllib.request.Request(url, data=body, headers=headers, method=method))


# ---------------------------------------------------------------- mercado

MINUTES = {"1": 1, "3": 3, "5": 5, "15": 15, "30": 30, "60": 60, "120": 120,
           "240": 240, "360": 360, "720": 720, "D": 1440}


def klines(cfg, symbol, interval, limit):
    """Velas de la más antigua a la más reciente, SOLO cerradas."""
    out, end = [], None
    while len(out) < limit:
        n = min(1000, limit - len(out))
        p = {"category": "spot", "symbol": symbol, "interval": interval, "limit": n}
        if end:
            p["end"] = end
        rows = public(cfg, "/v5/market/kline", **p)["list"]
        if not rows:
            break
        out.extend(rows)
        end = int(rows[-1][0]) - 1
        if len(rows) < n:
            break
    bars = [dict(t=int(r[0]), o=float(r[1]), h=float(r[2]), l=float(r[3]), c=float(r[4]), v=float(r[5]))
            for r in out[:limit]]
    bars.reverse()
    if bars and bars[-1]["t"] + MINUTES[interval] * 60000 > time.time() * 1000:
        bars = bars[:-1]
    return bars


def instruments(cfg):
    return {i["symbol"]: i for i in public(cfg, "/v5/market/instruments-info", category="spot", limit=1000)["list"]}


def tickers(cfg):
    return {t["symbol"]: t for t in public(cfg, "/v5/market/tickers", category="spot")["list"]}


# ---------------------------------------------------------------- cuenta

def wallet(cfg):
    res = private(cfg, "GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    acct = (res.get("list") or [{}])[0]
    coins = []
    for c in acct.get("coin", []):
        wb = float(c.get("walletBalance") or 0)
        debt = float(c.get("borrowAmount") or 0)
        if wb or debt:
            coins.append({"coin": c["coin"], "amount": wb, "debt": debt, "usd": float(c.get("usdValue") or 0)})
    coins.sort(key=lambda c: -abs(c["usd"]))
    return {"equity": float(acct.get("totalEquity") or 0),
            "available": float(acct.get("totalAvailableBalance") or 0),
            "coins": coins}


def open_orders(cfg):
    res = private(cfg, "GET", "/v5/order/realtime", {"category": "spot", "limit": 50})
    return [{"symbol": o["symbol"], "side": o["side"], "type": o["orderType"], "qty": o["qty"],
             "price": o.get("price"), "trigger": o.get("triggerPrice"), "status": o["orderStatus"],
             "filter": o.get("orderFilter"), "id": o["orderId"]} for o in res.get("list", [])]
