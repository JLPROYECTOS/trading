"""Motor de señales: revisa los pares con margen al cerrar cada vela, filtra por simulación
y crea propuestas que el usuario autoriza en la app. Nunca envía órdenes."""
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
LOOKBACK = 1000


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


class Engine:
    def __init__(self, notify=None):
        self.notify = notify or (lambda *a, **k: None)
        self.status = {"state": "arrancando", "last_scan": None, "next_scan": None, "pairs": 0,
                       "detected": 0, "proposed": 0, "error": None, "discarded": []}
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
            interval = cfg["SIGNAL_INTERVAL"]
            if cfg.get("ENGINE", "on").lower() != "on":
                self.status["state"] = "apagado (ENGINE=off)"
            else:
                period = bybit.MINUTES.get(interval, 60) * 60
                bar = int(time.time() // period)
                if bar != self._last_bar and time.time() - bar * period > 20:
                    try:
                        self.scan(cfg)
                        self._last_bar = bar
                        self.status["error"] = None
                    except Exception as e:  # noqa
                        self.status["error"] = str(e)
                        traceback.print_exc()
                nxt = (bar + 1) * period + 20
                self.status["next_scan"] = iso(datetime.fromtimestamp(nxt, timezone.utc))
                self.status["state"] = "vigilando"
            self._wake.wait(max(30, float(cfg["SCAN_EVERY_MIN"]) * 60))
            self._wake.clear()

    # ------------------------------------------------------------------
    def scan(self, cfg):
        self.status["state"] = "revisando"
        quote, interval = cfg["QUOTE"], cfg["SIGNAL_INTERVAL"]
        fee = float(cfg["FEE_PCT"]) / 100
        steps = max(1, int(float(cfg["HORIZON_H"]) * 60 / bybit.MINUTES[interval]))
        insts, tks = bybit.instruments(cfg), bybit.tickers(cfg)
        universe = [s for s, i in insts.items()
                    if i["quoteCoin"] == quote and i.get("marginTrading", "none") != "none"
                    and i.get("status") == "Trading"
                    and float(tks.get(s, {}).get("turnover24h") or 0) >= float(cfg["MIN_TURNOVER"])]
        self.status.update(pairs=len(universe), detected=0)
        discarded, equity = [], None

        with LOCK:
            lst = load_signals()
            busy = {s["symbol"] for s in lst if s["status"] in ("pending", "sending")}

        for sym in sorted(universe):
            bars = bybit.klines(cfg, sym, interval, LOOKBACK)
            time.sleep(0.05)
            for rule, side in st.detect(bars):
                self.status["detected"] += 1
                entry = bars[-1]["c"]
                a = st.atr(bars)
                base = {"symbol": sym, "rule": rule, "side": side, "time": iso(now())}
                if not a:
                    continue
                stop, target = st.levels(entry, side, a, float(cfg["SL_ATR"]), float(cfg["TP_R"]))
                sim = st.simulate(bars, entry, stop, target, side, steps, fee)
                sim.update(st.rule_history(bars, rule, side, steps, fee, float(cfg["SL_ATR"]), float(cfg["TP_R"])))
                sim["days"] = round(len(bars) * bybit.MINUTES[interval] / 1440)
                min_n = int(cfg.get("MIN_HIST_N", 8))
                ok = (sim["ev_r"] >= float(cfg["MIN_EV_R"]) and sim["hist_n"] >= min_n and sim["hist_ev_r"] > 0)
                if not ok:
                    discarded.append(dict(base, reason="Filtro: simulación %+.2fR · historia de la regla %+.2fR en %d casos" % (
                        sim["ev_r"], sim["hist_ev_r"], sim["hist_n"])))
                    continue
                if sym in busy:
                    discarded.append(dict(base, reason="Ya hay una señal pendiente en este par"))
                    continue
                if equity is None:
                    equity = bybit.wallet(cfg)["equity"]
                inst = insts[sym]
                sz, why = st.size(equity, entry, stop, fee, float(cfg["RISK_PCT"]), float(cfg["MAX_LEVERAGE"]),
                                  inst["lotSizeFilter"]["basePrecision"], float(inst["lotSizeFilter"].get("minOrderAmt") or 0))
                if not sz:
                    discarded.append(dict(base, reason=why))
                    continue
                self._propose(cfg, inst, rule, side, entry, stop, target, sz, equity, fee, sim, a)
                busy.add(sym)

        self.status["discarded"] = (discarded + self.status["discarded"])[:30]
        self.status["last_scan"] = iso(now())

    def _propose(self, cfg, inst, rule, side, entry, stop, target, sz, equity, fee, sim, atr_v):
        tick = inst["priceFilter"]["tickSize"]
        # stop y objetivo redondeados "hacia fuera" (sin acercar el stop a la entrada)
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
            "sim": sim, "env": cfg["BYBIT_ENV"],
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
