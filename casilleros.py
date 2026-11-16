from datetime import datetime, timezone
from db import sb


def registrar_evento(tipo, usuario_id=None, casillero=None, detalle=None):
    sb.table("eventos").insert({
        "tipo": tipo,
        "usuario_id": usuario_id,
        "casillero": casillero,
        "detalle": detalle,
    }).execute()


def _set_estado(numero, estado, usuario_id=None):
    sb.table("casilleros").update({
        "estado": estado,
        "usuario_id": usuario_id,
        "actualizado": datetime.now(timezone.utc).isoformat(),
    }).eq("numero", numero).execute()


def casillero_de_usuario(usuario_id):
    """Devuelve el numero de casillero del usuario o None."""
    res = (sb.table("casilleros").select("numero")
           .eq("usuario_id", usuario_id).execute())
    return res.data[0]["numero"] if res.data else None


def primer_libre():
    res = (sb.table("casilleros").select("numero")
           .eq("estado", "libre").order("numero").limit(1).execute())
    return res.data[0]["numero"] if res.data else None


def asignar(usuario_id):
    """Asigna el primer casillero libre. Devuelve el numero o None."""
    numero = primer_libre()
    if numero is None:
        registrar_evento("sin_casilleros", usuario_id)
        return None
    _set_estado(numero, "asignado", usuario_id)
    registrar_evento("asignado", usuario_id, numero)
    return numero


def puerta_abierta(numero, usuario_id):
    _set_estado(numero, "abierto", usuario_id)
    registrar_evento("puerta_abierta", usuario_id, numero)


def puerta_cerrada(numero, usuario_id, dejar_libre):
    if dejar_libre:
        _set_estado(numero, "libre", None)
        registrar_evento("liberado", usuario_id, numero)
    else:
        _set_estado(numero, "ocupado", usuario_id)
        registrar_evento("puerta_cerrada", usuario_id, numero)


def cancelar_asignacion(numero, usuario_id):
    """La puerta no se abrio a tiempo: vuelve a libre."""
    _set_estado(numero, "libre", None)
    registrar_evento("asignacion_cancelada", usuario_id, numero,
                     "La puerta no se abrio en 10 s")


def estado_casilleros():
    return (sb.table("casilleros").select("numero,estado,usuario_id")
            .order("numero").execute().data)