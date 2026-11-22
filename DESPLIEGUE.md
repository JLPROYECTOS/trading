# Publicar la app en internet (sin tu PC)

**GitHub no sirve como servidor:** Bybit EU bloquea los servidores de GitHub (están en EE. UU.;
probado el 22-11-2026: HTTP 403). El servidor tiene que estar **en Europa**. GitHub solo guarda el código.

Elige UNA opción. En las dos: **las claves se pegan en el panel del servidor, nunca en GitHub.**

Necesitarás inventar:
- `APP_PASSWORD`: contraseña larga para entrar en la app (mínimo 12 caracteres). La app está en internet.
- `APP_PIN`: PIN para autorizar cada orden.

---

## Paso 0 (las dos opciones): subir el código a GitHub

1. Entra en https://github.com/JLPROYECTOS/trading
2. **Add file → Upload files**.
3. Arrastra **todo el contenido** de la carpeta `trading` del Escritorio (no la carpeta en sí: lo de dentro,
   incluida la carpeta `.github` si la ves; si no la ves, no pasa nada).
4. **Commit changes**.

No contiene claves: están en la carpeta `datos-trading` (al lado de `trading`), que **no** debes subir.

---

## Opción A — Render (fácil, ~7 $/mes)

1. Crea cuenta en https://render.com (con tu GitHub).
2. **New → Blueprint** → elige el repo `JLPROYECTOS/trading`. Render lee `render.yaml`:
   servicio en **Frankfurt**, con disco para los datos.
3. Te pedirá los valores secretos: `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `APP_PIN`, `APP_PASSWORD`.
4. **Apply**. En 2–3 minutos tendrás una dirección `https://senales-trading-xxxx.onrender.com`.

## Opción B — Oracle Cloud (gratis siempre, más pasos)

1. Crea cuenta en https://www.oracle.com/cloud/free/ y elige como región de inicio **Germany Central (Frankfurt)**.
2. **Create a VM instance** → imagen **Ubuntu** → forma "Always Free" → descarga la clave SSH → crear.
3. En la red de la VM (**Virtual Cloud Network → Security List**) añade reglas de entrada TCP **80** y **443** desde `0.0.0.0/0`.
4. Conéctate a la VM (botón **Cloud Shell** o SSH) y ejecuta:
   ```
   curl -fsSL https://raw.githubusercontent.com/JLPROYECTOS/trading/main/deploy/oracle-setup.sh | sudo bash
   ```
5. `sudo nano /etc/trading.env` → pega tus claves, PIN y contraseña → Ctrl+O, Enter, Ctrl+X.
6. `sudo systemctl restart trading`. El script te muestra tu dirección: `https://<ip>.sslip.io`.

---

## Instalar en Android

1. Abre la dirección `https://…` en **Chrome** del móvil → entra con tu contraseña.
2. Menú ⋮ → **Instalar aplicación**.
3. Abre la app → **Ajustes → 🔔 Activar notificaciones** → Permitir → "Enviar notificación de prueba".

## Después

- Ya no necesitas la app del PC: ciérrala para no tener dos motores.
- Cambiar ajustes (riesgo, apalancamiento, velas…): variables del servidor (ver `config.example.env`).
- Si Bybit rechaza la clave desde el servidor, revisa que no esté restringida a la IP de tu casa.
