"""Reglas de señal, niveles (SL/TP), tamaño y simulación. Funciones puras, sin red."""
import math
import random
from decimal import Decimal, ROUND_DOWN

RULES = {
    "breakout": "Ruptura con volumen: cierre sobre el máximo de las 20 velas anteriores, volumen ≥ 2x la media y tendencia alcista (precio > EMA20 > EMA50).",
    "bounce": "Rebote de sobreventa: el RSI(14) venía de menos de 30 y lo cruza hacia arriba.",
    "breakdown": "Rotura a la baja con volumen: cierre bajo el mínimo de las 20 velas anteriores, volumen ≥ 2x la media y tendencia bajista (precio < EMA20 < EMA50).",
}
RULE_NAMES = {"breakout": "Ruptura + volumen", "bounce": "Rebote de sobreventa", "breakdown": "Rotura a la baja"}


# ------------------------------------------------------------ indicadores

def ema(vals, n):
    k, e, out = 2 / (n + 1), vals[0], []
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        g, l = g + max(d, 0), l + max(-d, 0)
    ag, al = g / n, l / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag, al = (ag * (n - 1) + max(d, 0)) / n, (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr(bars, n=14):
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(bars, bars[1:])]
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


# ----------------------------------------------------------------- reglas

def detect(bars):
    """Señales en la última vela cerrada. Devuelve lista de (regla, lado)."""
    if len(bars) < 60:
        return []
    closes = [b["c"] for b in bars]
    last, prev20 = bars[-1], bars[-21:-1]
    hi20, lo20 = max(b["h"] for b in prev20), min(b["l"] for b in prev20)
    e20, e50 = ema(closes, 20)[-1], ema(closes, 50)[-1]
    avgv = sum(b["v"] for b in prev20) / 20
    vol_ok = avgv > 0 and last["v"] >= 2 * avgv
    r, r_prev = rsi(closes), rsi(closes[:-1])
    out = []
    if last["c"] > hi20 and vol_ok and last["c"] > e20 > e50:
        out.append(("breakout", "long"))
    if r is not None and r_prev is not None and r_prev < 30 <= r:
        out.append(("bounce", "long"))
    if last["c"] < lo20 and vol_ok and last["c"] < e20 < e50:
        out.append(("breakdown", "short"))
    return out


def levels(entry, side, atr_v, sl_atr, tp_r):
    dist = sl_atr * atr_v
    if side == "long":
        return entry - dist, entry + tp_r * dist
    return entry + dist, entry - tp_r * dist


# ------------------------------------------------------------- simulación

def _hit(side, hi, lo, stop, target):
    if side == "long":
        if lo <= stop:
            return "stop"
        if hi >= target:
            return "target"
    else:
        if hi >= stop:
            return "stop"
        if lo <= target:
            return "target"
    return None


def simulate(bars, entry, stop, target, side, steps, fee, runs=4000, seed=7):
    """Montecarlo con velas reales barajadas + la misma regla sobre la historia real.
    Si stop y objetivo caen en la misma vela, cuenta el stop (conservador)."""
    d = 1 if side == "long" else -1
    loss_u = abs(entry - stop) + fee * (entry + stop)

    def r_of(exitp):
        return (d * (exitp - entry) - fee * (entry + exitp)) / loss_u

    rel = [(b["h"] / b["o"], b["l"] / b["o"], b["c"] / b["o"]) for b in bars if b["o"] > 0]
    rng = random.Random(seed)
    cnt = {"target": 0, "stop": 0, "open": 0}
    rs = []
    for _ in range(runs):
        p, res, exitp = entry, "open", None
        for _s in range(steps):
            h, l, c = rel[rng.randrange(len(rel))]
            hit = _hit(side, p * h, p * l, stop, target)
            if hit:
                res, exitp = hit, (stop if hit == "stop" else target)
                break
            p *= c
        cnt[res] += 1
        rs.append(r_of(exitp if exitp is not None else p))

    sr, tr = stop / entry, target / entry
    hist = {"target": 0, "stop": 0, "open": 0}
    hrs = []
    for i in range(len(bars) - steps - 1):
        p0 = bars[i]["c"]
        s_i, t_i, res, exit_rel = p0 * sr, p0 * tr, "open", None
        for b in bars[i + 1:i + 1 + steps]:
            hit = _hit(side, b["h"], b["l"], s_i, t_i)
            if hit:
                res, exit_rel = hit, (sr if hit == "stop" else tr)
                break
        if exit_rel is None:
            exit_rel = bars[i + steps]["c"] / p0
        hist[res] += 1
        hrs.append(r_of(entry * exit_rel))
    hn = len(hrs)
    return {
        "runs": runs, "p_target": cnt["target"] / runs, "p_stop": cnt["stop"] / runs, "p_open": cnt["open"] / runs,
        "ev_r": sum(rs) / runs,
        "hist_n": hn,
        "hist_target": hist["target"] / hn if hn else 0, "hist_stop": hist["stop"] / hn if hn else 0,
        "hist_ev_r": sum(hrs) / hn if hn else 0,
        "days": None,
    }


def rule_history(bars, rule, side, steps, fee, sl_atr, tp_r):
    """Cada vez que ESTA regla se activó en el pasado: mismos SL/TP (en ATR de ese momento),
    resultado real de las velas siguientes. Devuelve n, % objetivo, % stop y R medio."""
    cnt = {"target": 0, "stop": 0, "open": 0}
    rs = []
    for i in range(60, len(bars) - steps - 1):
        if (rule, side) not in detect(bars[:i + 1]):
            continue
        e = bars[i]["c"]
        a = atr(bars[max(0, i - 200):i + 1])
        if not a:
            continue
        sl, tp = levels(e, side, a, sl_atr, tp_r)
        res, exitp = "open", bars[i + steps]["c"]
        for b in bars[i + 1:i + 1 + steps]:
            hit = _hit(side, b["h"], b["l"], sl, tp)
            if hit:
                res, exitp = hit, (sl if hit == "stop" else tp)
                break
        d = 1 if side == "long" else -1
        loss_u = abs(e - sl) + fee * (e + sl)
        cnt[res] += 1
        rs.append((d * (exitp - e) - fee * (e + exitp)) / loss_u)
    n = len(rs)
    return {"hist_n": n, "hist_target": cnt["target"] / n if n else 0, "hist_stop": cnt["stop"] / n if n else 0,
            "hist_ev_r": sum(rs) / n if n else 0}


# ----------------------------------------------------------------- tamaño

def dround(x, step):
    d = (Decimal(repr(float(x))) / Decimal(step)).to_integral_value(ROUND_DOWN) * Decimal(step)
    return format(d.normalize(), "f")


def size(equity, entry, stop, fee, risk_pct, max_lev, base_step, min_amt):
    """Cantidad que arriesga como mucho risk_pct del capital y no pasa de max_lev x capital."""
    stop_frac = abs(entry - stop) / entry + 2 * fee
    by_risk = equity * risk_pct / 100 / stop_frac
    by_lev = equity * max_lev * 0.95
    notional = min(by_risk, by_lev)
    qty = dround(notional / entry, base_step)
    notional = float(qty) * entry
    if notional < min_amt or float(qty) <= 0:
        return None, "El tamaño posible (%.2f) no llega al mínimo del par (%.2f)." % (min(by_risk, by_lev), min_amt)
    return {"qty": qty, "notional": notional, "leverage": notional / equity if equity else 0,
            "limited_by": "riesgo" if by_risk <= by_lev else "apalancamiento"}, None


def liquidation(entry, side, lev):
    """Precio de liquidación aproximado (sin margen de mantenimiento ni intereses)."""
    if lev <= 1.0001:
        return None if side == "long" else entry * 2
    return entry * (1 - 1 / lev) if side == "long" else entry * (1 + 1 / lev)


def price_decimals(tick):
    return max(0, -Decimal(tick).normalize().as_tuple().exponent)


def fmt_price(x, tick):
    return dround(x, tick)


def round_level(x, tick, side_kind):
    """Redondea un nivel al tick (stop/objetivo) sin acercarlo a la entrada de forma relevante."""
    step = Decimal(tick)
    q = Decimal(repr(float(x))) / step
    q = q.to_integral_value(ROUND_DOWN if side_kind == "down" else "ROUND_UP")
    return format((q * step).normalize(), "f")


def isfinite(*xs):
    return all(x is not None and math.isfinite(x) for x in xs)
