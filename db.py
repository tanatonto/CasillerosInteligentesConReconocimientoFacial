import os
import numpy as np
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def guardar_usuario(nombre, telefono, vector):
    sb.table("usuarios").insert({
        "nombre": nombre,
        "telefono": telefono,
        "vector": vector.flatten().tolist(),
    }).execute()


def cargar_usuarios():
    res = sb.table("usuarios").select("id,nombre,telefono,vector").execute()
    usuarios = []
    for u in res.data:
        usuarios.append({
            "id": u["id"],
            "nombre": u["nombre"],
            "telefono": u["telefono"],
            "vector": np.array(u["vector"], dtype=np.float32).reshape(1, 128),
        })
    return usuarios