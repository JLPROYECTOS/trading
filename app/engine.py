"""Motor de señales: revisa los pares con margen al cerrar cada vela, aplica filtros y gestor de
riesgo, filtra por simulación y crea propuestas que el usuario autoriza en la app.
Nunca envía órdenes.

Herramientas adaptadas de las skills de day trading:
  scanner      -> filtro de liquidez/spread
  entry        -> confirmación de tendencia en 1 h
  news         -> pausa por sobresalto del mercado (BTC)
  risk         -> máx. posiciones abiertas, operaciones/día y riesgo diario
  exit         -> stop mínimo frente a comisiones
  timemachine  -> simulación + historial de la regla
  alerts       -> aviso cuando se cierra una posición
"""
import json
import os
import secrets
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import bybit
import strategy as st

SIGNALS_FILE = os.path.join(bybit.DATA_DIR, "signals.json")
LOCK = threading.RLock()
IGNORE_COINS = {"USDC", "USDT", "EUR", "PLN"}


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def load_signals():
    try:
        with open(SIGNALS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def save_signals(lst):
    os.makedirs(bybit.DATA_DIR, exist_ok=True)
    tmp = SIGNALS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(lst[-300:], fh, indent=1, ensure_ascii=False)
    os.replace(tmp, SIGNALS_FILE)


def expire_old(lst):
    changed = False
    for s in lst:
        if s["status"] == "pending" and datetime.fromisoformat(s["expires"]) < now():
            s["status"] = "expired"
            changed = True
    return changed


# ------------------------------------------------------------------ gestor de riesgo

def open_positions(wallet, orders):
    """Posiciones abiertas: monedas (no estables) con valor > 1 $, deudas de margen u órdenes abiertas."""
    pos = set()
    for c in wallet["coins"]:
        if c["debt"] > 0:
            pos.add(c["coin"])
        elif c["coin"] not in IGNORE_COINS and abs(c["usd"]) > 1:
            pos.add(c["coin"])
    for o in orders:
        pos.add(o["symbol"])
    return pos


def today_trades(lst):
    day = now().date().isoformat()
    return [s for s in lst if s.get("status") in ("filled", "closed", "sent")
            and (s.get("approved_at") or "").startswith(day)]


def risk_check(cfg, lst, wallet, orders):
    """Devuelve (motivo de bloqueo o None, riesgo ya usado hoy en $)."""
    pos = open_positions(wallet, orders)
    if len(pos) >= int(cfg["MAX_OPEN"]):
        return "Posición abierta (%s): máx. %s a la vez" % (", ".join(sorted(pos)), cfg["MAX_OPEN"]), 0
    trades = today_trades(lst)
    if len(trades) >= int(cfg["MAX_TRADES_DAY"]):
        return "Límite de %s operaciones hoy alcanzado" % cfg["MAX_TRADES_DAY"], 0
    used = sum(float(s.get("loss") or 0) for s in trades)
    return None, used


class Engine:
    def __init__(self, notify=None):
        self.notify = notify or (lambda *a, **k: None)
        self.status = {"state": "arrancando", "last_scan": None, "next_scan": None, "pairs": 0,
                       "detected": 0, "proposed": 0, "error": None, "discarded": [], "paused": None}
        self._last_bar = None
        self._wake = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="engine").start()

    def scan_now(self):
        self._last_bar = None
        self._wake.set()

    def _loop(self):
        time.sleep(3)
        while True:
            cfg = bybit.load_env()
            period = bybit.MINUTES.get(cfg["SIGNAL_INTERVAL"], 5) * 60
            bar = int(time.time() // period)
            if cfg.get("ENGINE", "on").lower() != "on":
                self.status["state"] = "apagado (ENGINE=off)"
            elif bar != self._last_bar and time.time() - bar * period > 15:
                try:
                    self.scan(cfg)
                    self._last_bar = bar
                    self.status["error"] = None
                except Exception as e:  # noqa
                    self.status["error"] = str(e)
                    traceback.print_exc()
                self.status["state"] = "vigilando"
            nxt = (bar + 1) * period + 15
            self.status["next_scan"] = iso(datetime.fromtimestamp(nxt, timezone.utc))
            wait = min(float(cfg["SCAN_EVERY_MIN"]) * 60, max(5, nxt - time.time()))
            self._wake.wait(wait)
            self._wake.clear()

    # ------------------------------------------------------------------ revisión
    def scan(self, cfg):
        self.status["state"] = "revisando"
        quote, interval = cfg["QUOTE"], cfg["SIGNAL_INTERVAL"]
        fee = float(cfg["FEE_PCT"]) / 100
        min_stop = float(cfg["MIN_STOP_PCT"])
        steps = max(1, int(float(cfg["HORIZON_H"]) * 60 / bybit.MINUTES[interval]))
        insts, tks = bybit.instruments(cfg), bybit.tickers(cfg)
        universe = [s for s, i in insts.items()
                    if i["quoteCoin"] == quote and i.get("marginTrading", "none") != "none"
                    and i.get("status") == "Trading"
                    and float(tks.get(s, {}).get("turnover24h") or 0) >= float(cfg["MIN_TURNOVER"])]
        self.status.update(pairs=len(universe), detected=0)
        discarded = []

        # Cuenta: posiciones abiertas, aviso de cierres y gestor de riesgo
        wallet = orders = None
        if cfg.get("BYBIT_API_KEY"):
            wallet, orders = bybit.wallet(cfg), bybit.open_orders(cfg)
            self._monitor_closes(cfg, wallet)
        with LOCK:
            lst = load_signals()
        busy = {s["symbol"] for s in lst if s["status"] in ("pending", "checking", "sending")}
        blocked, used_risk = (risk_check(cfg, lst, wallet, orders) if wallet else (None, 0))

        # Sobresalto del mercado (BTC)
        shock = None
        btc = "BTC" + quote
        if btc in insts:
            b = bybit.klines(cfg, btc, "5", 4)
            if len(b) >= 3:
                mv = (b[-1]["c"] / b[-3]["o"] - 1) * 100
                if abs(mv) > float(cfg["SHOCK_PCT"]):
                    shock = "BTC se ha movido %+.2f%% en 15 min: mercado agitado" % mv
        self.status["paused"] = blocked or shock

        trend_cache = {}
        for sym in sorted(universe):
            t = tks.get(sym, {})
            bid, ask = float(t.get("bid1Price") or 0), float(t.get("ask1Price") or 0)
            spread = (ask - bid) / ask * 100 if bid and ask else 99
            if spread > float(cfg["MAX_SPREAD_PCT"]):
                continue
            bars = bybit.klines(cfg, sym, interval, int(cfg["LOOKBACK"]))
            time.sleep(0.03)
            for rule, side in st.detect(bars[-260:]):
                self.status["detected"] += 1
                base = {"symbol": sym, "rule": rule, "side": side, "time": iso(now())}

                def drop(reason):
                    discarded.append(dict(base, reason=reason))

                if self.status["paused"]:
                    drop("En pausa: " + self.status["paused"])
                    continue
                if sym in busy:
                    drop("Ya hay una señal pendiente en este par")
                    continue
                if sym not in trend_cache:
                    trend_cache[sym] = st.trend(bybit.klines(cfg, sym, cfg["TREND_INTERVAL"], 120))
                tr = trend_cache[sym]
                if (side == "long" and tr != "up") or (side == "short" and tr != "down"):
                    drop("Tendencia de 1 h %s: no confirma el %s" % (
                        {"up": "alcista", "down": "bajista", "flat": "lateral"}[tr], "largo" if side == "long" else "corto"))
                    continue
                entry, a = bars[-1]["c"], st.atr(bars[-200:])
                if not a:
                    continue
                stop, target = st.levels(entry, side, a, float(cfg["SL_ATR"]), float(cfg["TP_R"]), min_stop)
                sim = st.simulate(bars, entry, stop, target, side, steps, fee, runs=3000)
                sim.update(st.rule_history(bars, rule, side, steps, fee, float(cfg["SL_ATR"]), float(cfg["TP_R"]), min_stop))
                sim["days"] = round(len(bars) * bybit.MINUTES[interval] / 1440, 1)
                ok = (sim["ev_r"] >= float(cfg["MIN_EV_R"]) and sim["hist_n"] >= int(cfg["MIN_HIST_N"])
                      and sim["hist_ev_r"] > 0)
                if not ok:
                    drop("Filtro: simulación %+.2fR · historia %+.2fR en %d casos" % (
                        sim["ev_r"], sim["hist_ev_r"], sim["hist_n"]))
                    continue
                if not wallet:
                    drop("Sin claves de Bybit")
                    continue
                equity = wallet["equity"]
                inst = insts[sym]
                sz, why = st.size(equity, entry, stop, fee, float(cfg["RISK_PCT"]), float(cfg["MAX_LEVERAGE"]),
                                  inst["lotSizeFilter"]["basePrecision"], float(inst["lotSizeFilter"].get("minOrderAmt") or 0))
                if not sz:
                    drop(why)
                    continue
                q = float(sz["qty"])
                loss = q * abs(entry - stop) + fee * q * (entry + stop)
                if used_risk + loss > equity * float(cfg["DAILY_RISK_PCT"]) / 100:
                    drop("Superaría el riesgo diario (%s %% del capital)" % cfg["DAILY_RISK_PCT"])
                    continue
                checks = ["Spread %.3f %% (máx. %s %%)" % (spread, cfg["MAX_SPREAD_PCT"]),
                          "Tendencia de 1 h %s" % ("alcista" if tr == "up" else "bajista"),
                          "Mercado tranquilo (BTC sin sobresaltos)",
                          "Riesgo: sin posiciones abiertas · %d/%s operaciones hoy" % (
                              len(today_trades(lst)), cfg["MAX_TRADES_DAY"]),
                          "Simulación %+.2fR y la regla ganó en su historial (%+.2fR)" % (sim["ev_r"], sim["hist_ev_r"])]
                self._propose(cfg, inst, rule, side, entry, stop, target, sz, equity, fee, sim, a, checks)
                busy.add(sym)
                used_risk += loss
                if wallet and len(busy) >= int(cfg["MAX_OPEN"]):
                    self.status["paused"] = "Ya hay una señal pendiente (máx. %s a la vez)" % cfg["MAX_OPEN"]

        self.status["discarded"] = (discarded + self.status["discarded"])[:40]
        self.status["last_scan"] = iso(now())

    # ------------------------------------------------------------------ aviso de cierre
    def _monitor_closes(self, cfg, wallet):
        coins = {c["coin"]: c for c in wallet["coins"]}
        changed = False
        with LOCK:
            lst = load_signals()
            for s in lst:
                if s.get("status") != "filled":
                    continue
                q = float(s.get("fill_qty") or s.get("qty") or 0)
                c = coins.get(s["base"], {"amount": 0, "debt": 0})
                if abs(c["amount"]) < 0.1 * q and c["debt"] < 0.1 * q:
                    pnl = wallet["equity"] - float(s.get("equity_before") or wallet["equity"])
                    s.update(status="closed", closed_at=iso(now()), pnl=pnl)
                    changed = True
                    self.notify(title="%s %s cerrada · %s%.2f $" % (
                        "Largo" if s["side"] == "long" else "Corto", s["base"], "+" if pnl >= 0 else "−", abs(pnl)),
                        body="Capital ahora: %.2f $. Toca para ver el historial." % wallet["equity"], tag="close-" + s["id"])
            if changed:
                save_signals(lst)

    # ------------------------------------------------------------------ propuesta
    def _propose(self, cfg, inst, rule, side, entry, stop, target, sz, equity, fee, sim, atr_v, checks):
        tick = inst["priceFilter"]["tickSize"]
        if side == "long":
            sl, tp = st.round_level(stop, tick, "down"), st.round_level(target, tick, "up")
        else:
            sl, tp = st.round_level(stop, tick, "up"), st.round_level(target, tick, "down")
        qty, notional, lev = sz["qty"], sz["notional"], sz["leverage"]
        q = float(qty)
        loss = q * abs(entry - float(sl)) + fee * q * (entry + float(sl))
        gain = q * abs(float(tp) - entry) - fee * q * (entry + float(tp))
        liq = st.liquidation(entry, side, lev)
        sig = {
            "v": 2, "id": "s" + datetime.now().strftime("%y%m%d%H%M") + secrets.token_hex(3),
            "status": "pending", "created": iso(now()),
            "expires": iso(now() + timedelta(minutes=int(cfg["EXPIRES_MIN"]))),
            "symbol": inst["symbol"], "base": inst["baseCoin"], "quote": inst["quoteCoin"],
            "rule": rule, "rule_name": st.RULE_NAMES[rule], "rule_text": st.RULES[rule],
            "side": side, "entry": entry, "stop": sl, "target": tp, "tick": tick,
            "qty": qty, "notional": notional, "leverage": lev, "limited_by": sz["limited_by"],
            "equity": equity, "loss": loss, "gain": gain, "liquidation": liq, "atr": atr_v,
            "interval": cfg["SIGNAL_INTERVAL"], "horizon_h": float(cfg["HORIZON_H"]),
            "sim": sim, "env": cfg["BYBIT_ENV"], "checks": checks,
        }
        verb = "Comprar" if side == "long" else "Vender en corto"
        sig["summary"] = "%s %s %s (≈%.2f %s) con %.1fx. Si salta el stop pierdes ≈%.2f %s; si llega al objetivo ganas ≈%.2f %s." % (
            verb, qty, inst["baseCoin"], notional, inst["quoteCoin"], lev, loss, inst["quoteCoin"], gain, inst["quoteCoin"])
        with LOCK:
            lst = load_signals()
            lst.append(sig)
            save_signals(lst)
        self.status["proposed"] = self.status.get("proposed", 0) + 1
        self.notify(
            title="%s %s · %s" % ("LARGO" if side == "long" else "CORTO", inst["baseCoin"], st.RULE_NAMES[rule]),
            body="Prob. objetivo %d%% · stop %d%% · %+.2fR. Toca para revisar y autorizar." % (
                sim["p_target"] * 100, sim["p_stop"] * 100, sim["ev_r"]),
            tag=sig["id"])
