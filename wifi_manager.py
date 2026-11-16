#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import threading
import time
from flask import Flask, request, render_template_string

AP_SSID = "Casilleros-G1"        # pon el numero de tu grupo
AP_PASS = "clave123"      # minimo 8 caracteres
AP_CON = "ap-casilleros"
IFACE = "wlan0"
REINTENTO_S = 180
BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "wifi_config.json")
LOG = os.path.join(BASE, "wifi_error.log")

redes = []
pendiente = None
mensaje = ""
servidor_activo = False


def sh(*args, timeout=60):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 1, "timeout"


def log(texto):
    linea = time.strftime("%H:%M:%S ") + texto
    print(linea)
    with open(LOG, "a") as f:
        f.write(linea + "\n")


# ---------- Memoria (redes guardadas en la microSD) ----------
def cargar():
    try:
        with open(CONFIG) as f:
            d = json.load(f)
    except Exception:
        d = {}
    return {"1": d.get("1"), "2": d.get("2")}


def guardar(ssid, clave):
    d = cargar()
    anteriores = [x for x in (d["1"], d["2"]) if x and x["ssid"] != ssid]
    nuevo = {
        "1": {"ssid": ssid, "clave": clave},
        "2": anteriores[0] if anteriores else None,
    }
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(nuevo, f)
    log(f"Red '{ssid}' guardada en el slot 1")


# ---------- Wi-Fi ----------
def conexion_actual():
    rc, out = sh("nmcli", "-t", "-g", "GENERAL.CONNECTION", "device", "show", IFACE)
    return out.strip() if rc == 0 and out.strip() != "--" else ""


def escanear(rescan=True):
    cmd = ["nmcli", "-t", "-f", "SSID,SIGNAL", "device", "wifi", "list",
           "ifname", IFACE, "--rescan", "yes" if rescan else "no"]
    rc, out = sh(*cmd)
    vistas = {}
    for linea in out.splitlines():
        if ":" not in linea:
            continue
        ssid, senal = linea.rsplit(":", 1)
        ssid = ssid.replace("\\:", ":").strip()
        if ssid and ssid != AP_SSID:
            try:
                vistas[ssid] = max(vistas.get(ssid, 0), int(senal))
            except ValueError:
                pass
    return [s for s, _ in sorted(vistas.items(), key=lambda x: -x[1])]


def esperar_antena(t=20):
    """Espera a que wlan0 salga del modo AP y quede disponible."""
    fin = time.time() + t
    while time.time() < fin:
        rc, out = sh("nmcli", "-t", "-g", "GENERAL.STATE", "device", "show", IFACE)
        if "unavailable" not in out and "unmanaged" not in out and rc == 0:
            return True
        time.sleep(1)
    return False


def reiniciar_antena():
    sh("nmcli", "connection", "down", AP_CON)
    sh("nmcli", "connection", "delete", AP_CON)
    sh("nmcli", "device", "disconnect", IFACE)
    sh("nmcli", "radio", "wifi", "off")
    time.sleep(2)
    sh("nmcli", "radio", "wifi", "on")
    time.sleep(3)
    esperar_antena()


def conectar(ssid, clave):
    global mensaje
    log(f"Conectando a '{ssid}'...")
    reiniciar_antena()

    visible = False
    for _ in range(4):
        sh("nmcli", "device", "wifi", "rescan", "ifname", IFACE)
        time.sleep(4)
        if ssid in escanear(rescan=False):
            visible = True
            break
    log(f"Red visible en el escaneo: {visible}")

    for intento in range(2):
        sh("nmcli", "connection", "delete", ssid)
        cmd = ["nmcli", "connection", "add", "type", "wifi", "ifname", IFACE,
               "con-name", ssid, "ssid", ssid]
        if clave:
            cmd += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", clave]
        if not visible:
            cmd += ["802-11-wireless.hidden", "yes"]
        rc, out = sh(*cmd)
        if rc != 0:
            log(f"Error al crear el perfil: {out}")
            mensaje = f"Error al crear el perfil: {out[:150]}"
            continue

        rc, out = sh("nmcli", "--wait", "40", "connection", "up", ssid,
                     "ifname", IFACE, timeout=60)
        if rc == 0:
            log("Conexion exitosa")
            return True
        log(f"Intento {intento + 1} fallo: {out}")
        mensaje = f"No se pudo conectar a '{ssid}': {out[:200]}"
        time.sleep(3)

    sh("nmcli", "connection", "delete", ssid)
    return False


def intentar_guardadas():
    d = cargar()
    for k in ("1", "2"):
        s = d[k]
        if s:
            log(f"Probando red guardada {k}: {s['ssid']}")
            if conectar(s["ssid"], s["clave"]):
                return True
    return False


def iniciar_ap():
    global redes
    sh("nmcli", "radio", "wifi", "on")
    sh("nmcli", "connection", "down", AP_CON)
    sh("nmcli", "connection", "delete", AP_CON)
    esperar_antena()
    redes = escanear()  # escanear ANTES de abrir el AP
    rc, out = sh("nmcli", "device", "wifi", "hotspot", "ifname", IFACE,
                 "con-name", AP_CON, "ssid", AP_SSID, "password", AP_PASS)
    sh("nmcli", "connection", "modify", AP_CON, "connection.autoconnect", "no")
    if rc != 0:
        log(f"No se pudo crear el AP: {out}")
    return rc == 0


# ---------- Pagina web ----------
app = Flask(__name__)

PAGINA = """
<!doctype html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Configurar Wi-Fi</title>
<style>
 body{font-family:sans-serif;max-width:420px;margin:30px auto;padding:0 16px}
 select,input,button{width:100%;padding:12px;margin:6px 0;font-size:16px;box-sizing:border-box}
 button{background:#c51d4a;color:#fff;border:0;border-radius:6px}
 .error{background:#fde8e8;padding:10px;border-radius:6px}
</style></head><body>
<h2>Casilleros inteligentes</h2>
<p>Elige la red Wi-Fi a la que se conectara el sistema.</p>
{% if mensaje %}<p class="error">{{ mensaje }}</p>{% endif %}
<form method="post" action="/conectar">
  <label>Redes encontradas</label>
  <select name="ssid">
    {% for r in redes %}<option value="{{ r }}">{{ r }}</option>{% endfor %}
  </select>
  <label>O escribe el nombre (red oculta)</label>
  <input name="ssid_manual" placeholder="Dejar vacio si usas la lista">
  <label>Contrasena</label>
  <input name="clave" type="text" autocapitalize="off" autocorrect="off"
         placeholder="Contrasena del Wi-Fi">
  <button type="submit">Conectar</button>
</form></body></html>
"""

RESPUESTA = """
<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:sans-serif;max-width:420px;margin:30px auto;padding:0 16px">
<h2>Probando conexion...</h2>
<p>El sistema cierra esta red para probar el Wi-Fi que elegiste (tarda hasta 1 minuto).</p>
<p>Si funciona, quedara guardado y esta red desaparecera.</p>
<p>Si falla, la red <b>{{ ap }}</b> volvera a aparecer: conectate de nuevo
y la pagina mostrara el motivo del error.</p></body></html>
"""


@app.route("/")
def inicio():
    return render_template_string(PAGINA, redes=redes, mensaje=mensaje)


@app.route("/conectar", methods=["POST"])
def conectar_web():
    global pendiente, mensaje
    ssid = (request.form.get("ssid_manual") or request.form.get("ssid") or "").strip()
    clave = request.form.get("clave", "").strip()
    if not ssid:
        return render_template_string(PAGINA, redes=redes, mensaje="Elige o escribe una red")
    mensaje = ""
    pendiente = (ssid, clave)
    return render_template_string(RESPUESTA, ap=AP_SSID)


def arrancar_servidor():
    global servidor_activo
    if servidor_activo:
        return
    servidor_activo = True
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=80, threaded=True),
        daemon=True,
    ).start()


# ---------- Logica principal ----------
def asegurar_wifi(forzar_ap=False):
    global pendiente, mensaje

    if not forzar_ap:
        actual = conexion_actual()
        if actual and actual != AP_CON:
            log(f"Ya conectada a: {actual}")
            return True
        if intentar_guardadas():
            return True

    arrancar_servidor()
    while True:
        if not iniciar_ap():
            time.sleep(5)
            continue
        log(f"AP '{AP_SSID}' activo. Abre http://10.42.0.1 desde el celular")

        t0 = time.time()
        while pendiente is None and time.time() - t0 < REINTENTO_S:
            time.sleep(0.5)

        if pendiente is not None:
            ssid, clave = pendiente
            pendiente = None
            if conectar(ssid, clave):
                guardar(ssid, clave)
                mensaje = ""
                return True
            # si falla, el bucle vuelve a abrir el AP y la pagina muestra 'mensaje'
        elif not forzar_ap and intentar_guardadas():
            return True


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("Ejecuta con sudo (el puerto 80 y nmcli lo requieren)")
    asegurar_wifi(forzar_ap="--forzar-ap" in sys.argv)
    log("Listo: la Pi tiene Wi-Fi")