import os
import re
import threading
import time
import unicodedata

import cv2
import numpy as np
from flask import Flask, Response, jsonify, request, send_from_directory

from db import cargar_usuarios, guardar_usuario
import casilleros as cs
import hardware

# DPM: Limitar OpenCV a 2 hilos evita picos de corriente en los 4 nucleos de la Pi 4
cv2.setNumThreads(2)

UMBRAL = 0.55     # similitud minima para aceptar (ajustar con pruebas)
MARGEN = 0.10     # el mejor debe superar al segundo por este margen
ANCHO, ALTO = 640, 360
MUESTRAS = 3
COINCIDENCIAS = 3       # coincidencias seguidas para abrir
TIEMPO_IDENT_S = 8
INACTIVIDAD_CAM_S = 10  # segundos sin uso para pasar la camara a estado SLEEP
TEL_RE = re.compile(r"^09\d{8}$")   # celular de Ecuador: 10 digitos, empieza con 09

detector = cv2.FaceDetectorYN.create(
    "modelos/face_detection_yunet_2023mar.onnx", "", (ANCHO, ALTO),
    score_threshold=0.8)
reconocedor = cv2.FaceRecognizerSF.create(
    "modelos/face_recognition_sface_2021dec.onnx", "")
lock_ia = threading.Lock()

# Cuadro con nombre que se dibuja sobre el video
overlay = None   # dict: box=(x,y,an,al), nombre, ok, hasta


class CamaraDPM:
    """Manejo de Poder Dinamico (DPM):
    - SLEEP: Camara liberada (0 mW en USB y 0% CPU) cuando nadie usa la interfaz.
    - IDLE: Captura regulada a ~15 FPS cuando se visualiza el video.
    """
    def __init__(self):
        self.cap = None
        self.frame = None
        self.ultimo_uso = 0.0
        self.clientes_video = 0
        self.lock = threading.Lock()
        self.evento_despertar = threading.Event()
        threading.Thread(target=self._bucle_dpm, daemon=True).start()

    def _abrir_hardware(self):
        cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, ANCHO)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, ALTO)
        cap.set(cv2.CAP_PROP_FPS, 15)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def despertar(self):
        self.ultimo_uso = time.time()
        if not self.evento_despertar.is_set():
            self.evento_despertar.set()
    
    def dormir(self):
      self.ultimo_uso = 0.0
      self.evento_despertar.clear()
      with self.lock:
          if self.cap is not None:
              self.cap.release()
              self.cap = None
          self.frame = None

    def _bucle_dpm(self):
        while True:
            # ESTADO SLEEP: bloquea el hilo sin consumir CPU hasta que alguien pida camara
            self.evento_despertar.wait()

            with self.lock:
                if self.cap is None or not self.cap.isOpened():
                    self.cap = self._abrir_hardware()

            ok, f = self.cap.read()
            if ok:
                self.frame = f
                # ESTADO IDLE: pausa de 65ms (~15 FPS) para bajar voltaje/frecuencia entre frames
                time.sleep(0.065)
            else:
                time.sleep(0.1)

            # Si no hay nadie viendo /video o ya paso el tiempo de inactividad -> volver a SLEEP
            inactivo = (time.time() - self.ultimo_uso) > INACTIVIDAD_CAM_S
            if inactivo:
                with self.lock:
                    if self.cap is not None:
                        self.cap.release()
                        self.cap = None
                    self.frame = None
                    self.evento_despertar.clear()

    def leer(self):
        self.despertar()
        # Si estaba en SLEEP, espera hasta 0.6s a que llegue el primer fotograma
        t0 = time.time()
        while self.frame is None and (time.time() - t0) < 0.6:
            time.sleep(0.04)
        return self.frame


cam = CamaraDPM()
usuarios = cargar_usuarios()
muestras = []
app = Flask(__name__, static_folder="static")


def sin_tildes(texto):
    """OpenCV no dibuja tildes ni ñ: las convertimos."""
    t = texto.replace("ñ", "n").replace("Ñ", "N")
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if not unicodedata.combining(c))


def obtener_vector(frame):
    """ESTADO RUN: Devuelve (vector, mensaje, caja). Exige exactamente un rostro."""
    if frame is None:
        return None, "Camara sin imagen", None
    with lock_ia:
        h, w = frame.shape[:2]
        detector.setInputSize((w, h))
        _, rostros = detector.detect(frame)
        if rostros is None:
            return None, "No veo ningun rostro", None
        if len(rostros) > 1:
            return None, "Debe haber una sola persona", None
        r = rostros[0]
        caja = tuple(int(v) for v in r[:4])
        alineado = reconocedor.alignCrop(frame, r)
        return reconocedor.feature(alineado), "", caja


def mejor_coincidencia(vector):
    """Para abrir casillero: exige umbral y margen sobre el segundo."""
    if not usuarios:
        return None, -1
    puntajes = sorted(
        ((reconocedor.match(u["vector"], vector, cv2.FaceRecognizerSF_FR_COSINE), u)
         for u in usuarios),
        key=lambda x: x[0], reverse=True)
    s1, u1 = puntajes[0]
    s2 = puntajes[1][0] if len(puntajes) > 1 else -1
    if s1 >= UMBRAL and (s1 - s2) >= MARGEN:
        return u1, s1
    return None, s1


def existe_rostro(vector):
    """Para registro: True si algun usuario supera el umbral (sin margen)."""
    mejor, sim_max = None, -1
    for u in usuarios:
        s = reconocedor.match(u["vector"], vector, cv2.FaceRecognizerSF_FR_COSINE)
        if s > sim_max:
            mejor, sim_max = u, s
    if mejor is not None and sim_max >= UMBRAL:
        return mejor
    return None


def refrescar_usuarios():
    global usuarios
    usuarios = cargar_usuarios()


def _auto_refresco():
    while True:
        time.sleep(60)  # Subido a 60s para reducir trafico de red en reposo
        try:
            refrescar_usuarios()
        except Exception as e:
            print("No se pudo refrescar usuarios:", e)


threading.Thread(target=_auto_refresco, daemon=True).start()


# ---------- Pagina y video ----------
@app.route("/")
def inicio():
    return send_from_directory("static", "index.html")


def dibujar_overlay(frame):
    """frame ya esta volteado (espejo). Dibuja el cuadro con el nombre."""
    ov = overlay
    if ov is None or time.time() > ov["hasta"]:
        return
    x, y, an, al = ov["box"]
    w = frame.shape[1]
    x = w - (x + an)                      # coordenada en la imagen espejo
    color = (0, 220, 0) if ov["ok"] else (0, 0, 255)
    cv2.rectangle(frame, (x, y), (x + an, y + al), color, 3)
    texto = sin_tildes(ov["nombre"])
    (tw, th), _ = cv2.getTextSize(texto, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
    ty = y - 10 if y - th - 14 > 0 else y + al + th + 12
    cv2.rectangle(frame, (x, ty - th - 6), (x + tw + 10, ty + 6), color, -1)
    cv2.putText(frame, texto, (x + 5, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                (255, 255, 255), 2)


@app.route("/video")
def video():
    def generar():
        t_inicio_stream = time.time()
        # Seguridad DPM: si el frontend olvida cerrar el stream, se corta a los 45s
        while (time.time() - t_inicio_stream) < 45:
            f = cam.leer()
            if f is not None:
                esp = cv2.flip(f, 1)
                dibujar_overlay(esp)
                ok, jpg = cv2.imencode(".jpg", esp, [cv2.IMWRITE_JPEG_QUALITY, 65])
                if ok:
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                           + jpg.tobytes() + b"\r\n")
            time.sleep(0.07)  # ~14 FPS en stream web (fluido y bajo consumo)
    return Response(generar(), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/api/camara/dormir", methods=["POST"])
def camara_dormir():
    muestras.clear()
    cam.dormir()
    return jsonify(ok=True)
    
# ---------- Registro ----------
@app.route("/api/registro/reiniciar", methods=["POST"])
def registro_reiniciar():
    muestras.clear()
    cam.despertar()
    return jsonify(ok=True)


@app.route("/api/registro/muestra", methods=["POST"])
def registro_muestra():
    vec, msg, _ = obtener_vector(cam.leer())
    if vec is None:
        return jsonify(ok=False, msg=msg, n=len(muestras))
    existente, _ = mejor_coincidencia(vec)
    if existente is not None:
        muestras.clear()
        cs.registrar_evento("registro_rostro_duplicado",
                            existente["id"], None,
                            "Intento de registrar un rostro ya existente")
        return jsonify(ok=False, duplicado=True,
                       msg="Este rostro ya esta registrado")
    muestras.append(vec.flatten())
    return jsonify(ok=True, n=len(muestras), total=MUESTRAS)


@app.route("/api/registro/guardar", methods=["POST"])
def registro_guardar():
    global usuarios
    d = request.get_json()
    nombre = d.get("nombre", "").strip()
    telefono = d.get("telefono", "").strip()

    if len(nombre) < 2:
        return jsonify(ok=False, msg="Nombre invalido")
    if not TEL_RE.match(telefono):
        muestras.clear()
        return jsonify(ok=False, msg="El telefono debe tener 10 digitos y empezar con 09")
    if len(muestras) < MUESTRAS:
        return jsonify(ok=False, msg="Faltan capturas")

    promedio = np.mean(muestras, axis=0).astype(np.float32)
    existente = existe_rostro(promedio.reshape(1, 128))
    if existente is not None:
        muestras.clear()
        return jsonify(ok=False, msg="Este rostro ya esta registrado")

    try:
        guardar_usuario(nombre, telefono, promedio)
    except Exception:
        muestras.clear()
        return jsonify(ok=False, msg="No se pudo guardar. Telefono ya registrado?")
    muestras.clear()
    usuarios = cargar_usuarios()
    return jsonify(ok=True)


# ---------- Abrir casillero ----------
@app.route("/api/identificar", methods=["POST"])
def identificar():
    global overlay
    cam.despertar()
    t0 = time.time()
    previo, seguidas, msg = None, 0, "No te pude reconocer"
    while time.time() - t0 < TIEMPO_IDENT_S:
        vec, m, caja = obtener_vector(cam.leer())
        if vec is None:
            msg = m
            seguidas = 0
            overlay = None
            time.sleep(0.15)  # Transicion RUN -> IDLE cuando no hay rostro
            continue
        u, sim = mejor_coincidencia(vec)
        print(f"[ID] similitud={sim:.2f} -> {u['nombre'] if u else 'nadie'}")
        if u is not None:
            overlay = {"box": caja, "nombre": u["nombre"], "ok": True,
                       "hasta": time.time() + 0.8}
        else:
            overlay = {"box": caja, "nombre": "Desconocido", "ok": False,
                       "hasta": time.time() + 0.8}

        if u is not None and u["id"] == previo:
            seguidas += 1
        elif u is not None:
            previo, seguidas = u["id"], 1
        else:
            previo, seguidas, msg = None, 0, "No te pude reconocer"

        if seguidas >= COINCIDENCIAS:
            overlay = {"box": caja, "nombre": u["nombre"], "ok": True,
                       "hasta": time.time() + 2.5}
            return jsonify(ok=True, usuario_id=u["id"], nombre=u["nombre"],
                           casillero=cs.casillero_de_usuario(u["id"]))
        # Pausa DPM entre inferencias exitosas para evitar pico termico y de corriente
        time.sleep(0.12)

    overlay = None
    cs.registrar_evento("acceso_denegado", detalle=msg)
    return jsonify(ok=False, msg=msg)


def proceso_apertura(numero, uid, nuevo, liberar):
    hardware.abrir_cerradura(numero)
    if not hardware.esperar_apertura(numero, 10):
        if nuevo:
            cs.cancelar_asignacion(numero, uid)
        else:
            cs.registrar_evento("puerta_no_abrio", uid, numero)
        return
    cs.puerta_abierta(numero, uid)
    segundos = hardware.esperar_cierre(numero)
    if segundos > 60:
        cs.registrar_evento("alerta_puerta_abierta", uid, numero,
                            f"Abierta {segundos} s")
    cs.puerta_cerrada(numero, uid, liberar)


@app.route("/api/abrir", methods=["POST"])
def abrir():
    d = request.get_json()
    uid = d["usuario_id"]
    liberar = d.get("modo") == "liberar"
    try:
        numero = cs.casillero_de_usuario(uid)
        nuevo = numero is None
        if nuevo:
            numero = cs.asignar(uid)
            if numero is None:
                return jsonify(ok=False, msg="No hay casilleros libres")
    except Exception as e:
        print("Error al abrir:", e)
        refrescar_usuarios()
        return jsonify(ok=False, msg="Usuario no valido. Intenta de nuevo")
    threading.Thread(target=proceso_apertura,
                     args=(numero, uid, nuevo, liberar), daemon=True).start()
    return jsonify(ok=True, casillero=numero)


# ---------- Simulador de puertas (solo mientras no hay ESP32) ----------
if hardware.SIMULACION:
    from db import sb

    @app.route("/sim")
    def sim_pagina():
        return send_from_directory("static", "sim.html")

    @app.route("/api/sim/estado")
    def sim_estado():
        puertas = [{"numero": n, "abierta": a,
                    "energizada": hardware.cerradura_energizada(n)}
                   for n, a in hardware.estado_puertas().items()]
        eventos = (sb.table("eventos")
                   .select("created_at,tipo,casillero,detalle")
                   .order("id", desc=True).limit(8).execute().data)
        return jsonify(puertas=puertas, casilleros=cs.estado_casilleros(),
                       eventos=eventos)

    @app.route("/api/sim/puerta", methods=["POST"])
    def sim_puerta():
        d = request.get_json()
        n, abrir_p = int(d["numero"]), bool(d["abierta"])
        if abrir_p and not hardware.cerradura_energizada(n):
            cs.registrar_evento("puerta_forzada", None, n,
                                "Abierta sin orden del sistema")
        hardware.set_puerta(n, abrir_p)
        return jsonify(ok=True)


if __name__ == "__main__":
    # El modo quiosco ya lo maneja kiosco.sh en labwc, no lanzamos doble Chromium aqui
    app.run(host="127.0.0.1", port=5000, threaded=True)