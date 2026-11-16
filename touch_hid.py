#!/usr/bin/env python3
"""Traduce el tactil USB 0eef:0005 (protocolo propio) a un tactil virtual (uinput).
Con Manejo de Poder Dinamico (DPM): 0% CPU en estado SLEEP cuando no hay toque.
"""
import glob
import json
import os
import select
import sys
import time

from evdev import AbsInfo, UInput, ecodes as e

W, H = 800, 480
HID_ID = "HID_ID=0003:00000EEF:00000005"
CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "touch_config.json")
SOLTAR_S = 1.0      # respaldo: si no llegan datos por este tiempo, se considera soltado
TOLERANCIA_S = 0.15  # un "sin toque" mas corto que esto se ignora (parpadeo)


def buscar():
    for d in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(d + "/device/uevent") as f:
                u = f.read().upper()
        except OSError:
            continue
        if HID_ID in u:
            return "/dev/" + os.path.basename(d)
    return None


def esperar_dispositivo():
    avisado = False
    while True:
        dev = buscar()
        if dev:
            return dev
        if not avisado:
            print("Esperando el tactil 0eef:0005 ...", flush=True)
            avisado = True
        time.sleep(2)


def cargar_cfg():
    try:
        with open(CFG) as f:
            return json.load(f)
    except Exception:
        return {}


def punto(d):
    """None = reporte invalido, False = sin toque, (x, y) = toque."""
    if len(d) < 6 or d[0] != 0xAA:
        return None
    if d[1] != 1:
        return False
    return (d[2] << 8) | d[3], (d[4] << 8) | d[5]


def base(u, v, modelo):
    """Terminos del ajuste: afin (3) o cuadratico (6). u, v = crudo / 1000."""
    if modelo == "cuad":
        return [u, v, 1.0, u * v, u * u, v * v]
    return [u, v, 1.0]


def mapear(x, y, c):
    """Crudo -> pixeles de pantalla (0..W-1, 0..H-1)."""
    if "modelo" in c:  # calibracion de 9 puntos (calibrar_touch.py)
        f = base(x / 1000.0, y / 1000.0, c["modelo"])
        fx = sum(k * b for k, b in zip(c["cx"], f))
        fy = sum(k * b for k, b in zip(c["cy"], f))
        fx = min(max(fx, 0.0), 1.0)
        fy = min(max(fy, 0.0), 1.0)
        return round(fx * (W - 1)), round(fy * (H - 1))
    # sin calibrar: coordenadas crudas recortadas a la pantalla
    return min(max(int(x), 0), W - 1), min(max(int(y), 0), H - 1)


def probar(fd, cfg):
    print("Toca la pantalla (Ctrl+C para salir)")
    while True:
        if select.select([fd], [], [], None)[0]:
            p = punto(os.read(fd, 64))
            if isinstance(p, tuple):
                print("crudo", p, "-> pantalla", mapear(p[0], p[1], cfg))


# ------------------------------------------------------------ tactil virtual
def servicio(cfg):
    cap = {
        e.EV_KEY: [e.BTN_TOUCH],
        e.EV_ABS: [
            (e.ABS_X, AbsInfo(0, 0, W - 1, 0, 0, 0)),
            (e.ABS_Y, AbsInfo(0, 0, H - 1, 0, 0, 0)),
            (e.ABS_MT_SLOT, AbsInfo(0, 0, 1, 0, 0, 0)),
            (e.ABS_MT_TRACKING_ID, AbsInfo(0, 0, 65535, 0, 0, 0)),
            (e.ABS_MT_POSITION_X, AbsInfo(0, 0, W - 1, 0, 0, 0)),
            (e.ABS_MT_POSITION_Y, AbsInfo(0, 0, H - 1, 0, 0, 0)),
        ],
    }
    ui = UInput(cap, name="Tactil JMS123 (traducido)", input_props=[e.INPUT_PROP_DIRECT])
    print("Tactil virtual creado. Calibracion:", cfg.get("modelo", "ninguna"), flush=True)
    estado = {"tocando": False, "tid": 0, "pos": None}

    def tocar(x, y):
        if estado["tocando"] and estado["pos"] == (x, y):
            return
        ui.write(e.EV_ABS, e.ABS_MT_SLOT, 0)
        if not estado["tocando"]:
            estado["tid"] = (estado["tid"] + 1) % 65000
            ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, estado["tid"])
            ui.write(e.EV_KEY, e.BTN_TOUCH, 1)
            estado["tocando"] = True
        ui.write(e.EV_ABS, e.ABS_MT_POSITION_X, x)
        ui.write(e.EV_ABS, e.ABS_MT_POSITION_Y, y)
        ui.write(e.EV_ABS, e.ABS_X, x)
        ui.write(e.EV_ABS, e.ABS_Y, y)
        ui.syn()
        estado["pos"] = (x, y)

    def soltar():
        if estado["tocando"]:
            ui.write(e.EV_ABS, e.ABS_MT_SLOT, 0)
            ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
            ui.write(e.EV_KEY, e.BTN_TOUCH, 0)
            ui.syn()
            estado["tocando"] = False
            estado["pos"] = None

    try:
        while True:
            dev = esperar_dispositivo()
            fd = os.open(dev, os.O_RDONLY)
            ultimo = 0.0
            liberar_en = None
            try:
                while True:
                    espera = 0.05 if estado["tocando"] else None
                    listo, _, _ = select.select([fd], [], [], espera)

                    if listo:
                        p = punto(os.read(fd, 64))
                        if isinstance(p, tuple):
                            liberar_en = None
                            tocar(*mapear(p[0], p[1], cfg))
                            ultimo = time.time()
                        elif p is False and estado["tocando"] and liberar_en is None:
                            liberar_en = time.time() + TOLERANCIA_S

                    ahora = time.time()
                    if liberar_en is not None and ahora >= liberar_en:
                        soltar()
                        liberar_en = None
                    elif estado["tocando"] and (ahora - ultimo) > SOLTAR_S:
                        soltar()
            except OSError:
                soltar()
                time.sleep(1)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
    finally:
        ui.close()


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("Ejecuta con sudo")
    modo = sys.argv[1] if len(sys.argv) > 1 else ""
    cfg = cargar_cfg()
    try:
        if modo == "--probar":
            dev = esperar_dispositivo()
            fd = os.open(dev, os.O_RDONLY)
            try:
                probar(fd, cfg)
            finally:
                os.close(fd)
        else:
            servicio(cfg)
    except KeyboardInterrupt:
        pass
