import threading
import time

SIMULACION = True
VENTANA_S = 10   # segundos que la cerradura queda liberada (en real: pulso de 2 s)

_puertas = {n: False for n in range(1, 13)}   # True = puerta abierta
_pulso = {}                                   # ultimo pulso por casillero
_evento_puerta = threading.Condition()        # DPM: evita polling constante de CPU


def abrir_cerradura(numero):
    with _evento_puerta:
        _pulso[numero] = time.time()
    print(f"[SIM] Pulso al rele del casillero {numero}")
    return True


def cerradura_energizada(numero):
    return time.time() - _pulso.get(numero, 0) < VENTANA_S


def esperar_apertura(numero, timeout=10):
    """DPM SLEEP: Duerme el hilo (0% CPU) hasta que se abra la puerta o venza el timeout."""
    limite = time.time() + timeout
    with _evento_puerta:
        while not _puertas[numero]:
            restante = limite - time.time()
            if restante <= 0:
                return False
            _evento_puerta.wait(timeout=restante)
        return True


def esperar_cierre(numero, alerta_s=60):
    """DPM SLEEP: Bloquea sin consumir CPU hasta que la puerta se cierre."""
    t0 = time.time()
    with _evento_puerta:
        while _puertas[numero]:
            # Topamos la espera maxima a 300s por seguridad si dejan la puerta abierta
            restante = 300 - (time.time() - t0)
            if restante <= 0:
                break
            _evento_puerta.wait(timeout=restante)
    return int(time.time() - t0)


# --- solo para el simulador (o callback de recepcion del ESP32) ---
def estado_puertas():
    with _evento_puerta:
        return dict(_puertas)


def set_puerta(numero, abierta):
    with _evento_puerta:
        if _puertas.get(numero) != abierta:
            _puertas[numero] = abierta
            _evento_puerta.notify_all()  # Despierta al instante a los hilos en espera