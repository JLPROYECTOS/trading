"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const nf = (x, d = 2) => Number(x).toLocaleString("es-ES", {minimumFractionDigits: d, maximumFractionDigits: d});
const usd = x => "$" + nf(x, 2);
const dec = t => { const s = String(t); const i = s.indexOf("."); return i < 0 ? 0 : s.replace(/0+$/, "").length - i - 1; };
const fp = (x, t) => nf(x, Math.max(0, dec(t)));
const pct = x => (x >= 0 ? "+" : "") + nf(x, 2) + " %";
const pc = x => Math.round(x * 100) + " %";
const hm = iso => iso ? new Date(iso).toLocaleTimeString("es-ES", {hour: "2-digit", minute: "2-digit"}) : "—";
const dt = iso => iso ? new Date(iso).toLocaleString("es-ES", {day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit"}) : "";

let S = null, sigKey = "", sheetId = null, pin = "", deferredInstall = null;

async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Requested-With": "trading-app"},
                               body: JSON.stringify(body || {}), credentials: "same-origin"});
  if (r.status === 401 && path !== "/api/login") showLogin();
  return r.json();
}

function showLogin() { $("#login").hidden = false; setTimeout(() => $("#login-pass").focus(), 50); }
$("#login-form").addEventListener("submit", async e => {
  e.preventDefault();
  const j = await post("/api/login", {password: $("#login-pass").value});
  if (j.ok) { $("#login").hidden = true; $("#login-pass").value = ""; load(); }
  else toast(j.error, false);
});

function toast(msg, ok = true, ms = 5000) {
  const t = $("#toast");
  t.textContent = msg; t.style.background = ok ? "var(--up)" : "var(--down)"; t.style.display = "block";
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", ms);
}

/* ------------------------------------------------------------------ render */

function render() {
  const real = S.real;
  $("#env").textContent = real ? (S.env === "eu" ? "REAL · EU" : "REAL") : S.env.toUpperCase();
  $("#env").className = "env " + (real ? "real" : "demo");

  const w = S.wallet;
  if (w && w.ok) {
    $("#equity").textContent = usd(w.data.equity);
    $("#equity-sub").textContent = "Disponible " + usd(w.data.available) + " · apalancamiento máx. " + S.config.MAX_LEVERAGE + "x";
  } else {
    $("#equity").textContent = "—";
    $("#equity-sub").textContent = S.configured ? (w ? w.error : "") : "Sin claves de Bybit";
  }

  const n = [];
  if (!S.configured) n.push("Faltan las claves de Bybit en el archivo <b>.env</b> del PC.");
  if (!S.pin_set) n.push("Define <b>APP_PIN</b> en el .env del PC. Sin PIN no se puede autorizar.");
  if (!S.push.available) n.push("Las notificaciones no están instaladas en el PC (<code>pip install -r requirements.txt</code>).");
  else if (!S.push.devices) n.push("Todavía no has activado las notificaciones en ningún dispositivo. Pulsa <b>🔔 Activar notificaciones</b> abajo.");
  if (S.engine && S.engine.error) n.push("Motor: " + esc(S.engine.error));
  $("#alerts").innerHTML = n.map(x => `<div class="notice">${x}</div>`).join("");

  const key = S.pending.map(s => s.id).join(",");
  if (key !== sigKey) { sigKey = key; renderSignals(); }
  updateLive();

  renderWallet(); renderOrders(); renderHistory(); renderEngine();
  const c = S.config;
  $("#cfg").innerHTML = `Velas de ${c.SIGNAL_INTERVAL} min · stop ${c.SL_ATR} ATR · objetivo ${c.TP_R}R · riesgo ${c.RISK_PCT} % por operación ·
    apalancamiento máx. ${c.MAX_LEVERAGE}x · solo señales con ≥ ${c.MIN_EV_R}R esperado · caducan a los ${c.EXPIRES_MIN} min ·
    notificaciones en ${S.push.devices} dispositivo(s)`;
}

function renderSignals() {
  const box = $("#signals");
  if (!S.pending.length) {
    const e = S.engine || {};
    box.innerHTML = `<div class="empty"><div class="ic">🛰️</div><h3>Sin señales ahora</h3>
      <div class="muted">El motor vigila ${e.pairs || "los"} pares con margen y te avisará cuando una señal pase el filtro de simulación.</div></div>`;
    return;
  }
  box.innerHTML = `<h2>Pendiente de tu autorización</h2>` + S.pending.map(sigCard).join("");
}

function sigCard(s) {
  const long = s.side === "long", t = s.tick, sim = s.sim;
  const slp = (s.stop / s.entry - 1) * 100, tpp = (s.target / s.entry - 1) * 100;
  return `<article class="signal ${long ? "long" : "short"}" data-id="${esc(s.id)}">
    <div class="sig-head">
      <div><div class="pair">${esc(s.base)}<span> / ${esc(s.quote)}</span></div>
        <div class="rule">${esc(s.rule_name)} · velas de ${esc(s.interval)} min</div></div>
      <div class="side">${long ? "LARGO ↑" : "CORTO ↓"}<small>${nf(s.leverage, 1)}x</small></div>
    </div>
    <p class="summary">${esc(s.summary)}</p>
    <div class="now" data-now="${esc(s.id)}">Precio ahora: —</div>
    <div class="ladder" data-ladder="${esc(s.id)}">
      <div class="track"></div>
      <div class="lv">
        <div><span class="t">${long ? "Stop" : "Objetivo"}</span><b>${fp(long ? s.stop : s.target, t)}</b><span class="${long ? "neg" : "pos"}">${pct(long ? slp : tpp)}</span></div>
        <div><span class="t">Entrada</span><b>${fp(s.entry, t)}</b></div>
        <div><span class="t">${long ? "Objetivo" : "Stop"}</span><b>${fp(long ? s.target : s.stop, t)}</b><span class="${long ? "pos" : "neg"}">${pct(long ? tpp : slp)}</span></div>
      </div>
    </div>
    <div class="stats">
      <div class="stat"><div class="t">Prob. objetivo</div><div class="n pos">${pc(sim.p_target)}</div></div>
      <div class="stat"><div class="t">Prob. stop</div><div class="n neg">${pc(sim.p_stop)}</div></div>
      <div class="stat"><div class="t">Resultado esperado</div><div class="n">${sim.ev_r >= 0 ? "+" : ""}${nf(sim.ev_r, 2)}R</div></div>
      <div class="stat"><div class="t">Esta regla antes</div><div class="n">${pc(sim.hist_target)}</div><div class="t">llegó al objetivo · ${sim.hist_n} veces en ${sim.days} días · ${sim.hist_ev_r >= 0 ? "+" : ""}${nf(sim.hist_ev_r, 2)}R</div></div>
    </div>
    <div class="money">
      <div class="row"><span class="k">Tamaño</span><span class="v">${esc(s.qty)} ${esc(s.base)} · ${usd(s.notional)}</span></div>
      <div class="row"><span class="k">Si salta el stop</span><span class="v neg">−${usd(s.loss)} (${nf(s.loss / s.equity * 100, 1)} % del capital)</span></div>
      <div class="row"><span class="k">Si llega al objetivo</span><span class="v pos">+${usd(s.gain)}</span></div>
      <div class="row"><span class="k">Apalancamiento</span><span class="v">${nf(s.leverage, 2)}x (limitado por ${esc(s.limited_by)})</span></div>
      <div class="row"><span class="k">Liquidación aprox.</span><span class="v">${s.liquidation ? fp(s.liquidation, t) : "—"}</span></div>
    </div>
    <details class="why"><summary>¿Por qué esta señal?</summary>
      <p>${esc(s.rule_text)}</p>
      <p>Stop a ${esc(S.config.SL_ATR)}× el rango medio (ATR) y objetivo a ${esc(S.config.TP_R)} veces el riesgo. Simulación: ${sim.runs} escenarios con velas reales de los últimos ${sim.days} días. Además, esta regla se activó ${sim.hist_n} veces en ese periodo: llegó al objetivo el ${pc(sim.hist_target)} y al stop el ${pc(sim.hist_stop)}. Solo se notifican señales con ambos resultados positivos. Si el stop y el objetivo caen en la misma vela, cuenta como stop. No incluye intereses del préstamo.</p>
      <p>El SL y el TP se envían a Bybit junto con la orden.</p>
    </details>
    <div class="timer"><span>Caduca en</span><b data-exp="${esc(s.expires)}">—</b></div>
    <button class="authorize" data-auth="${esc(s.id)}">AUTORIZAR</button>
    <button class="dismiss" data-rej="${esc(s.id)}">Descartar</button>
  </article>`;
}

function updateLive() {
  for (const s of S.pending) {
    const price = S.prices[s.symbol];
    const now = document.querySelector(`[data-now="${s.id}"]`);
    if (now && price) {
      const mv = (price / s.entry - 1) * 100;
      now.textContent = `Precio ahora: ${fp(price, s.tick)} ${s.quote} (${pct(mv)} desde la señal)`;
    }
    const lad = document.querySelector(`[data-ladder="${s.id}"] .track`);
    if (!lad) continue;
    const vals = [s.stop, s.target, s.entry, price || s.entry].map(Number);
    let lo = Math.min(...vals), hi = Math.max(...vals); const pad = (hi - lo) * 0.06; lo -= pad; hi += pad;
    const pos = v => ((v - lo) / (hi - lo) * 100).toFixed(2) + "%";
    const a = Math.min(s.stop, s.entry), b = Math.max(s.stop, s.entry);
    const c = Math.min(s.target, s.entry), d = Math.max(s.target, s.entry);
    lad.innerHTML = `<div class="zone red" style="left:${pos(a)};width:calc(${pos(b)} - ${pos(a)})"></div>
      <div class="zone green" style="left:${pos(c)};width:calc(${pos(d)} - ${pos(c)})"></div>
      <div class="mark" style="left:${pos(s.entry)}"></div>
      ${price ? `<div class="cur" style="left:${pos(price)}"></div>` : ""}`;
  }
  tick();
}

function tick() {
  document.querySelectorAll("[data-exp]").forEach(el => {
    const sec = Math.max(0, Math.round((new Date(el.dataset.exp) - Date.now()) / 1000));
    el.textContent = sec ? Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0") : "caducada";
  });
}

function renderWallet() {
  const w = S.wallet;
  if (!w) return $("#wallet").innerHTML = `<div class="muted">Conecta tus claves de Bybit.</div>`;
  if (!w.ok) return $("#wallet").innerHTML = `<div class="muted">${esc(w.error)}</div>`;
  $("#wallet").innerHTML = w.data.coins.length ? w.data.coins.map(c => `<div class="row">
      <span>${esc(c.coin)}${c.debt ? ` <span class="neg">· debe ${nf(c.debt, 8)}</span>` : ""}</span>
      <span class="v">${nf(c.amount, 8)} <span class="muted">· ${usd(c.usd)}</span></span></div>`).join("")
    : `<div class="muted">Sin saldo.</div>`;
}

function renderOrders() {
  const o = S.orders;
  if (!o) return $("#orders").innerHTML = `<div class="muted">—</div>`;
  if (!o.ok) return $("#orders").innerHTML = `<div class="muted">${esc(o.error)}</div>`;
  $("#orders").innerHTML = o.data.length ? o.data.map(x => `<div class="row">
      <span>${x.side === "Buy" ? "Compra" : "Venta"} ${esc(x.qty)} ${esc(x.symbol)}</span>
      <span class="v muted">${esc(x.filter === "tpslOrder" ? "TP/SL" : x.type)}${Number(x.trigger) ? " · se activa a " + esc(x.trigger) : Number(x.price) ? " · " + esc(x.price) : ""}</span></div>`).join("")
    : `<div class="muted">Ninguna.</div>`;
}

const ST = {filled: "EJECUTADA", sent: "ENVIADA", failed: "FALLÓ", not_filled: "NO EJECUTADA", rejected: "DESCARTADA",
            expired: "CADUCADA", sending: "ENVIANDO", checking: "COMPROBANDO"};
function renderHistory() {
  const h = S.history;
  $("#history").innerHTML = h.length ? h.map(s => `<div class="hist">
      <div><div><b>${s.side === "long" ? "Largo" : "Corto"} ${esc(s.base)}</b> <span class="muted">· ${esc(s.rule_name)} · ${nf(s.leverage, 1)}x</span></div>
        <div class="m">${dt(s.approved_at || s.rejected_at || s.created)}${s.fill_price ? " · " + esc(s.fill_qty) + " a " + esc(s.fill_price) : ""}</div>
        ${s.warning ? `<div class="m neg">${esc(s.warning)}</div>` : ""}${s.error ? `<div class="m">${esc(s.error)}</div>` : ""}</div>
      <span class="st ${esc(s.status)}">${ST[s.status] || esc(s.status)}</span></div>`).join("")
    : `<div class="empty-s">Sin operaciones todavía.</div>`;
}

function renderEngine() {
  const e = S.engine;
  if (!e) return;
  const d = e.discarded || [];
  $("#engine").innerHTML = `
    <div class="row"><span class="muted">Estado</span><span class="v">${esc(e.state)}</span></div>
    <div class="row"><span class="muted">Última revisión</span><span class="v">${hm(e.last_scan)}</span></div>
    <div class="row"><span class="muted">Próxima</span><span class="v">${hm(e.next_scan)}</span></div>
    <div class="row"><span class="muted">Pares vigilados</span><span class="v">${e.pairs}</span></div>
    <div class="row"><span class="muted">Patrones vistos / propuestos</span><span class="v">${e.detected} / ${e.proposed || 0}</span></div>
    ${d.length ? `<details class="why"><summary>Descartadas recientemente (${d.length})</summary>${d.map(x =>
      `<div class="row"><span>${esc(x.symbol)} · ${x.side === "long" ? "largo" : "corto"} · ${hm(x.time)}</span><span class="v muted">${esc(x.reason)}</span></div>`).join("")}</details>` : ""}`;
}

/* ------------------------------------------------------------------ acciones */

document.addEventListener("click", async ev => {
  const a = ev.target.closest("[data-auth]"), r = ev.target.closest("[data-rej]");
  if (a) openSheet(a.dataset.auth);
  if (r) {
    const j = await post("/api/reject", {id: r.dataset.rej});
    toast(j.ok ? j.message : j.error, j.ok); load();
  }
});

function openSheet(id) {
  const s = S.pending.find(x => x.id === id);
  if (!s) return;
  sheetId = id; pin = ""; $("#pin-text").value = "";
  $("#sheet-sum").textContent = `${s.side === "long" ? "LARGO" : "CORTO"} ${s.base} ${nf(s.leverage, 1)}x · SL ${fp(s.stop, s.tick)} · TP ${fp(s.target, s.tick)}` +
    (S.real ? " · DINERO REAL" : "");
  drawDots(); $("#sheet-bg").hidden = false;
}
function closeSheet() { $("#sheet-bg").hidden = true; sheetId = null; pin = ""; }
function drawDots() {
  const n = Math.max(4, pin.length);
  $("#dots").innerHTML = Array.from({length: n}, (_, i) => `<i class="${i < pin.length ? "" : "ph"}"></i>`).join("");
}
$("#keypad").innerHTML = ["1","2","3","4","5","6","7","8","9","","0","⌫"].map(k =>
  k ? `<button data-k="${k}" aria-label="${k === "⌫" ? "borrar" : k}">${k}</button>` : "<span></span>").join("");
$("#keypad").addEventListener("click", e => {
  const b = e.target.closest("[data-k]"); if (!b) return;
  pin = b.dataset.k === "⌫" ? pin.slice(0, -1) : (pin + b.dataset.k).slice(0, 16);
  drawDots();
});
$("#kb-toggle").onclick = () => { const i = $("#pin-text"); i.hidden = !i.hidden; $("#keypad").hidden = !i.hidden ? true : false; $("#dots").hidden = !i.hidden; if (!i.hidden) i.focus(); };
$("#cancel").onclick = closeSheet;
$("#sheet-bg").addEventListener("click", e => { if (e.target.id === "sheet-bg") closeSheet(); });
$("#confirm").onclick = async () => {
  const code = $("#pin-text").hidden ? pin : $("#pin-text").value;
  if (!code) return toast("Escribe tu PIN.", false);
  const btn = $("#confirm"); btn.disabled = true; btn.textContent = "EJECUTANDO…";
  try {
    const j = await post("/api/approve", {id: sheetId, pin: code});
    if (j.ok) { closeSheet(); toast(j.message, true, 9000); if (navigator.vibrate) navigator.vibrate(80); }
    else { toast(j.error, false, 9000); pin = ""; drawDots(); $("#pin-text").value = ""; if (!/PIN/.test(j.error)) closeSheet(); }
  } catch (e) { toast("Sin conexión con el PC.", false); }
  btn.disabled = false; btn.textContent = "CONFIRMAR Y EJECUTAR";
  sigKey = ""; load();
};

/* ------------------------------------------------------------------ push / instalar */

function b64ToU8(b) {
  const p = "=".repeat((4 - b.length % 4) % 4), s = atob((b + p).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(s, c => c.charCodeAt(0));
}
$("#btn-push").onclick = async () => {
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !window.isSecureContext)
    return toast("Aquí no se pueden activar. Abre la app por su dirección https de Tailscale.", false, 8000);
  const perm = await Notification.requestPermission();
  if (perm !== "granted") return toast("Has bloqueado las notificaciones. Actívalas en los ajustes del navegador.", false, 8000);
  const {key} = await (await fetch("/api/push/key")).json();
  if (!key) return toast("Falta instalar las notificaciones en el PC.", false);
  const reg = await navigator.serviceWorker.ready;
  let sub = await reg.pushManager.getSubscription();
  if (!sub) sub = await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: b64ToU8(key)});
  const j = await post("/api/push/subscribe", {subscription: sub.toJSON()});
  toast(j.ok ? j.message : j.error, j.ok); load();
};
// Si el servidor se reinició y perdió las suscripciones, este dispositivo se vuelve a registrar solo.
let resubDone = false;
async function resubscribe() {
  if (resubDone || !S || !S.push.available || S.push.devices || !("serviceWorker" in navigator)) return;
  if (!window.isSecureContext || Notification.permission !== "granted") return;
  resubDone = true;
  try {
    const sub = await (await navigator.serviceWorker.ready).pushManager.getSubscription();
    if (sub) await post("/api/push/subscribe", {subscription: sub.toJSON()});
  } catch (_) {}
}
$("#btn-test").onclick = async () => { const j = await post("/api/push/test"); toast(j.ok ? j.message : j.error, j.ok); };
$("#btn-scan").onclick = async () => { const j = await post("/api/scan"); toast(j.ok ? j.message : j.error, j.ok); setTimeout(load, 8000); };
window.addEventListener("beforeinstallprompt", e => { e.preventDefault(); deferredInstall = e; $("#btn-install").hidden = false; });
$("#btn-install").onclick = async () => { if (!deferredInstall) return; deferredInstall.prompt(); await deferredInstall.userChoice; deferredInstall = null; $("#btn-install").hidden = true; };

/* ------------------------------------------------------------------ ciclo */

async function load() {
  try {
    const r = await fetch("/api/state", {cache: "no-store", credentials: "same-origin"});
    if (r.status === 401) return showLogin();
    S = await r.json(); render(); resubscribe();
  }
  catch (e) { $("#alerts").innerHTML = `<div class="notice">No hay conexión con el PC. ¿Está encendido y con la app abierta?</div>`; }
}
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
setInterval(() => { if (!document.hidden && !sheetId) load(); }, 5000);
setInterval(tick, 1000);
load();
