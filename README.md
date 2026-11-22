# Señales de Trading (Bybit EU)

App web instalable en Android que:

1. **Vigila** los pares con margen de Bybit EU (contra USDC) al cerrar cada vela.
2. **Detecta** tres patrones: ruptura con volumen (largo), rebote de sobreventa (largo) y rotura a la baja con volumen (corto).
3. **Filtra**: solo avisa si la simulación (4.000 escenarios con velas reales) **y** el histórico de esa misma regla en ese par dan un resultado esperado positivo tras comisiones.
4. **Notifica** al móvil con un resumen: entrada, **stop loss**, **take profit**, tamaño, apalancamiento, pérdida máxima y probabilidades.
5. **Ejecuta** la orden en Bybit (con SL y TP incluidos) **solo cuando pulsas AUTORIZAR y escribes tu PIN**.

> ⚠️ No es asesoramiento financiero. Las señales son reglas mecánicas y el pasado no garantiza nada.
> Con apalancamiento puedes perder todo lo que pongas en la operación.

> **Para usarla como app web sin el PC, sigue [DESPLIEGUE.md](DESPLIEGUE.md)** (Render u Oracle Cloud, en Europa).

## Cómo está montado

```
Android (app instalada) ──HTTPS privado (Tailscale)──▶ tu PC: server.py ──▶ API de Bybit EU
                                                         ├─ engine.py   (motor de señales)
                                                         └─ ..\datos-trading\.env (claves + PIN, nunca en GitHub)
```

- El servidor solo escucha en `127.0.0.1`. Desde el móvil se entra por **Tailscale** (red privada cifrada).
- Las claves y el PIN viven en `datos-trading\.env` (carpeta al lado de `trading`), **fuera** del repositorio.
- El PC tiene que estar encendido y con la app arrancada para recibir señales.

## Instalación en el PC (Windows)

1. Python 3.10+ instalado.
2. `pip install -r requirements.txt` (para las notificaciones push).
3. Crea la clave de API en bybit.eu: lectura + Spot Trade, **sin Withdraw**.
4. Rellena `datos-trading\.env`, en la carpeta al lado de `trading` (se crea sola la primera vez; ver `config.example.env`).
5. Arranca: doble clic en `start.cmd`, o `python app\server.py`.
6. Abre http://127.0.0.1:8765 en el PC para comprobarlo.

## Instalarla en Android (Tailscale)

1. Instala **Tailscale** en el PC (tailscale.com/download) y en el móvil (Google Play). Entra con la misma cuenta en los dos.
2. En el panel de Tailscale (login.tailscale.com/admin/dns) activa **MagicDNS** y **HTTPS Certificates**.
3. En el PC, en una terminal: `tailscale serve --bg 8765`
   Te dará una dirección tipo `https://tu-pc.tu-red.ts.net`.
4. En el móvil, abre esa dirección en **Chrome** → menú ⋮ → **Instalar aplicación** (o "Añadir a pantalla de inicio").
5. Abre la app instalada → **Ajustes → 🔔 Activar notificaciones** → permitir. Prueba con "Enviar notificación de prueba".

## Configuración (`.env`)

| clave | por defecto | qué hace |
| --- | --- | --- |
| `BYBIT_ENV` | `eu` | `eu` = Bybit EU (dinero real). También `live`, `demo`, `testnet`. |
| `APP_PIN` | — | PIN para autorizar cada orden. |
| `MAX_LEVERAGE` | `4` | Apalancamiento máximo (margen spot). |
| `RISK_PCT` | `10` | % del capital que se pierde como máximo si salta el stop. |
| `SIGNAL_INTERVAL` | `60` | Velas en minutos (15, 60, 240…). |
| `SL_ATR` / `TP_R` | `1.5` / `2` | Stop a 1,5 ATR; objetivo a 2 veces el riesgo. |
| `MIN_EV_R` | `0.10` | Resultado esperado mínimo de la simulación para avisar. |
| `MIN_HIST_N` | `8` | Veces mínimas que la regla debe haberse dado antes. |
| `EXPIRES_MIN` | `15` | Minutos para autorizar antes de que caduque. |
| `ENGINE` | `on` | `off` para parar el motor. |

## Seguridad

- Cada orden necesita **tu PIN** (5 fallos → bloqueo de 5 min).
- Antes de enviar, la app comprueba que el precio no se ha movido más de medio riesgo ni ha tocado el SL/TP.
- La orden es límite IOC con un 0,3 % de margen: si el precio se escapa, no se ejecuta.
- Tras ejecutar, verifica que Bybit tiene el SL y el TP; si no, te avisa al momento.
- Solo acepta peticiones desde la propia app (cabecera + origen) y desde `localhost` o tu dominio `.ts.net`.

## Subir a GitHub

El repositorio no contiene claves (`.gitignore` excluye `.env` y datos). Con git:

```
git init
git add .
git commit -m "App de señales"
git branch -M main
git remote add origin https://github.com/JLPROYECTOS/trading.git
git push -u origin main
```

Sin git: en github.com/JLPROYECTOS/trading → **Add file → Upload files** y arrastra el contenido de esta carpeta.
