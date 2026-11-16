# Casilleros inteligentes con reconocimiento facial

Sistema embebido para administrar 12 casilleros sin llaves: una Raspberry Pi 4 reconoce el rostro del usuario, le asigna un casillero y controla su cerradura eléctrica, con pantalla táctil, base de datos en la nube y panel de pruebas.

## Descripción

En espacios compartidos (instituciones educativas, gimnasios, oficinas) los casilleros suelen cerrarse con llaves o candados que se pierden, se prestan o se copian, y el encargado no sabe qué compartimentos están libres u ocupados. Este proyecto reemplaza la llave por la identidad del usuario.

**Cómo funciona**

1. La pantalla táctil de 7" (800×480) ofrece dos opciones: **Registrarme** y **Abrir mi casillero**.
2. Para registrarse, la persona escribe su nombre y su celular (10 dígitos, formato `09XXXXXXXX`) y la cámara toma tres capturas de su rostro. Con OpenCV (detector YuNet y reconocedor SFace) se calcula un vector facial de 128 dimensiones que se guarda en Supabase. Se rechaza un rostro que ya esté registrado con otro nombre.
3. Para abrir, la cámara compara el rostro con los registrados. Tras tres coincidencias consecutivas, la pantalla muestra «Bienvenido» con el nombre y le asigna el primer casillero libre, o el que ya posee. Si ya tiene uno, puede solo abrirlo o abrirlo y dejarlo libre.
4. Cada casillero maneja los estados **libre, asignado, abierto y ocupado**. Cada acceso, rechazo o alerta queda en un historial de eventos.
5. La Raspberry activa el relé de la cerradura durante 2 s; la puerta se abre por resorte y un final de carrera confirma la apertura y el cierre. Una puerta forzada o abierta más de un minuto genera una alerta.
6. Si la Raspberry no encuentra una red Wi-Fi conocida, crea su propia red de configuración con una página web y guarda hasta dos redes en la memoria no volátil de la microSD.

### Estado del proyecto

| Módulo | Estado |
|---|---|
| Registro y reconocimiento facial (YuNet + SFace) | Listo |
| Base de datos en Supabase (usuarios, casilleros, eventos) | Listo |
| Interfaz táctil de 800×480 con teclado propio | Listo |
| Gestor de Wi-Fi con red propia de configuración | Listo |
| Traductor y calibración del táctil USB | En pruebas |
| Modo quiosco con arranque automático | En pruebas |
| Control real de relés y finales de carrera | Pendiente (hoy simulado en `hardware.py`) |
| Panel de administrador con contraseña | Pendiente |

## Objetivos

**General:** diseñar e implementar un sistema embebido de casilleros inteligentes que identifique a los usuarios mediante reconocimiento facial, asigne un casillero y controle el acceso a 12 cerraduras desde una Raspberry Pi 4 con pantalla táctil.

**Específicos**

- Implementar el registro y el reconocimiento facial con OpenCV (YuNet y SFace), calibrando el umbral de similitud para evitar falsos positivos entre personas parecidas.
- Desarrollar la lógica de asignación y los estados de los 12 casilleros, con historial de eventos y alertas en una base de datos.
- Diseñar una interfaz táctil en modo quiosco para registrar usuarios y abrir casilleros.
- Controlar las cerraduras mediante módulos de relés y supervisar cada puerta con finales de carrera leídos por un expansor MCP23017.
- Integrar una conexión Wi-Fi configurable, con las redes guardadas en memoria no volátil y arranque automático del sistema.

## Tecnologías utilizadas

- **Hardware:** Raspberry Pi 4, pantalla HDMI de 7" con táctil USB, cámara web USB, 2 módulos de 8 relés (bobinas de 5 V), cerraduras eléctricas de 12 V, finales de carrera y MCP23017 (I²C).
- **Software:** Python 3, OpenCV (YuNet y SFace), Flask, HTML y JavaScript (interfaz mostrada en Chromium en modo quiosco), NetworkManager y `systemd`.
- **Base de datos:** Supabase (PostgreSQL).
- **Herramientas:** Visual Studio Code (Remote-SSH), Raspberry Pi Connect y GitHub.

## Instalación

Pasos para Raspberry Pi OS (Bookworm, 64 bits) con la cámara USB conectada.

### 1. Clonar e instalar dependencias

```bash
git clone https://github.com/<usuario>/<repositorio>.git
cd <repositorio>
python3 -m venv venv
source venv/bin/activate
pip install opencv-python numpy flask supabase python-dotenv
```

### 2. Descargar los modelos

```bash
mkdir -p modelos && cd modelos
wget https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
wget https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx
cd ..
```

### 3. Crear las tablas en Supabase

En **SQL Editor** de tu proyecto de Supabase ejecuta:

```sql
create table if not exists usuarios (
  id uuid primary key default gen_random_uuid(),
  nombre text not null,
  telefono text not null unique,
  vector float8[] not null,
  created_at timestamptz default now()
);

create table if not exists casilleros (
  numero int primary key check (numero between 1 and 12),
  estado text not null default 'libre'
    check (estado in ('libre','asignado','abierto','ocupado')),
  usuario_id uuid references usuarios(id) on delete set null,
  actualizado timestamptz default now()
);

insert into casilleros (numero)
select generate_series(1, 12)
on conflict (numero) do nothing;

create table if not exists eventos (
  id bigint generated always as identity primary key,
  created_at timestamptz default now(),
  tipo text not null,
  usuario_id uuid references usuarios(id) on delete set null,
  casillero int,
  detalle text
);

alter table usuarios enable row level security;
alter table casilleros enable row level security;
alter table eventos enable row level security;

grant usage on schema public to service_role;
grant select, insert, update, delete on public.usuarios to service_role;
grant select, insert, update, delete on public.casilleros to service_role;
grant select, insert, update, delete on public.eventos to service_role;
grant usage, select on all sequences in schema public to service_role;

-- Libera el casillero cuando se elimina a su usuario
create or replace function liberar_casillero_de_usuario()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  update public.casilleros
     set estado = 'libre', usuario_id = null, actualizado = now()
   where usuario_id = old.id;
  return old;
end;
$$;

drop trigger if exists trg_liberar_casillero on public.usuarios;
create trigger trg_liberar_casillero
before delete on public.usuarios
for each row execute function liberar_casillero_de_usuario();
```

### 4. Configurar las credenciales

Crea un archivo `.env` en la raíz del proyecto:

```
SUPABASE_URL=https://TU-PROYECTO.supabase.co
SUPABASE_KEY=tu_clave_service_role
```

> **Importante:** la clave `service_role` da acceso total a la base de datos. No la subas a GitHub ni la compartas. Agrega a `.gitignore`:
>
> ```
> .env
> venv/
> wifi_config.json
> wifi_error.log
> touch_config.json
> __pycache__/
> ```

### 5. Ejecutar

```bash
python app.py
```

- Interfaz táctil: `http://127.0.0.1:5000`
- Simulador de puertas (mientras no hay cableado): `http://127.0.0.1:5000/sim`

### 6. Arranque automático 

Servicio `systemd` para `app.py` en `/etc/systemd/system/casilleros.service`:

```ini
[Unit]
Description=Aplicacion de casilleros inteligentes
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<usuario>
WorkingDirectory=/home/<usuario>/proyecto
ExecStart=/home/<usuario>/proyecto/venv/bin/python -u /home/<usuario>/proyecto/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now casilleros.service
```

El gestor de Wi-Fi (`wifi_manager.py`) y el traductor del táctil (`touch_hid.py`) se instalan igual, como servicios que se ejecutan con permisos de administrador. Para abrir la interfaz a pantalla completa, Chromium se lanza en modo quiosco (`--kiosk`) apuntando a `http://127.0.0.1:5000` desde el `autostart` de labwc.

### Parámetros que conviene ajustar

En `app.py`:

| Constante | Valor | Descripción |
|---|---|---|
| `UMBRAL` | 0.55 | Similitud mínima para aceptar un rostro |
| `MARGEN` | 0.10 | El mejor candidato debe superar al segundo por este margen |
| `COINCIDENCIAS` | 3 | Coincidencias consecutivas necesarias para abrir |
| `MUESTRAS` | 3 | Capturas de rostro al registrarse |

El umbral recomendado por OpenCV (0.363) permitió que un hermano fuera reconocido como el usuario registrado; por eso se subió y se agregó el margen.

## Estructura del proyecto

```
.
├── app.py               Servidor Flask, cámara y reconocimiento facial
├── casilleros.py        Estados y asignación de casilleros, historial
├── db.py                Conexión con Supabase
├── hardware.py          Relés y finales de carrera (hoy simulados)
├── wifi_manager.py      Gestor de Wi-Fi con red de configuración propia
├── touch_hid.py         Traductor del táctil USB (0eef:0005) a uinput
├── calibrar_touch.py    Calibración de 9 puntos del táctil
├── kiosco.sh            Abre Chromium en modo quiosco
├── modelos/             YuNet y SFace (.onnx)
└── static/
    ├── index.html       Interfaz táctil
    └── sim.html         Simulador de puertas
```

## Privacidad y limitaciones

- El sistema guarda el **vector facial** (128 números) y el nombre y teléfono del usuario en Supabase. Son datos personales sensibles: se recomienda obtener consentimiento informado y permitir el borrado a solicitud, conforme a la Ley Orgánica de Protección de Datos Personales de Ecuador.
- No hay **prueba de vida**: una fotografía del rostro podría intentar suplantar a un usuario.
- Con personas muy parecidas (hermanos, gemelos) el reconocimiento puede confundirse; el umbral y el margen deben calibrarse con pruebas reales en el lugar de instalación.
- Se recomienda una llave mecánica de emergencia para recuperar pertenencias si el sistema falla.

## Autores

- Juan Intriago (Mecatrónica)
- Jafet Carriel (Mecatrónica)

Laboratorio de Sistemas Embebidos, paralelo 106. Profesor: Ronald Solis.
