"""Grupos de telemetria que el usuario enciende y apaga desde el menu ORBIT (submenu Telemetria).

QUE ES UN GRUPO. Lo que el conductor entiende ("Posicion", "Vehiculo"...), no lo que existe
por dentro. Cada dispositivo emite DOS generaciones de telemetria a la vez y un mismo dato vive
en las dos, asi que apagar "Posicion" tiene que apagar los dos caminos o la app seguiria
recibiendo coordenadas por el que se olvido:

  * v1 (legacy, telemetry_mqtt/<dongle>/<canal>): el param `<canal>_toggle`, que YA existia y
    que el backend y la app ya escriben por cfg/desired (config_v2.py). Aqui se reutiliza, no se
    duplica: una sola fuente de verdad por canal.
  * v2 (orbit/v2/tel/<dongle>/<canal>): el param `tel2_<canal>_toggle`, nuevo. Antes los canales
    v2 no tenian interruptor propio (solo perfil y privacidad).

REGLA DE LECTURA (la misma que `_canal_habilitado` del emisor): un canal solo esta apagado si su
param es EXPLICITAMENTE False. Sin configurar (None), a True o ante un error de lectura, esta
encendido. `get_bool` NO sirve para esto: devuelve False tambien para un param que nunca se ha
escrito, y una pantalla que lo use pinta OFF mientras el canal emite.

QUE NO ES UN GRUPO: presencia, enrolamiento, capacidades, configuracion y la camara. Sin presencia
el dispositivo desaparece de la app y sin enrolamiento no se puede reclamar; la camara tiene su
propio interruptor y su propio consentimiento.

Modulo PURO (sin pyray, sin cereal): lo importan el emisor (mqtt_envio_general, events_mqtt) y las
dos pantallas del comma, y se prueba sin build.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Grupo:
  clave: str             # identificador estable (tests, logs)
  titulo: str
  descripcion: str
  v1: tuple[str, ...]    # canales legacy (param `<canal>_toggle`)
  v2: tuple[str, ...]    # canales v2 (param `tel2_<canal>_toggle`)
  posicion: bool = False  # lo gobierna tambien el interruptor de privacidad


GRUPOS: tuple[Grupo, ...] = (
  Grupo("posicion", "Posicion (GPS)",
        "Latitud, longitud, rumbo y velocidad GPS. El interruptor de privacidad la apaga siempre.",
        v1=("gpsLocation", "gpsLocationExternal"), v2=("pos",), posicion=True),
  Grupo("vehiculo", "Vehiculo",
        "Velocidad, marcha, volante, pedales, intermitentes y crucero.",
        v1=("carState", "carControl"), v2=("vehicle",)),
  Grupo("percepcion", "Percepcion",
        "Coche delante, carriles y angulo muerto. Es la que mas datos consume.",
        v1=("radarState", "drivingModelData"), v2=("perception",)),
  Grupo("estado", "Estado de openpilot",
        "Alertas, calibracion y estado de los controles.",
        v1=("controlsState", "liveCalibration"), v2=("openpilot",)),
  Grupo("carretera", "Carretera",
        "Nombre de la via y limites de velocidad.",
        v1=(), v2=("road",)),
  Grupo("salud", "Salud del dispositivo",
        "Temperatura, CPU, disco, red y panda.",
        v1=(), v2=("health",)),
  Grupo("eventos", "Eventos y viaje",
        "Avisos del coche y resumen de cada viaje.",
        v1=(), v2=("event", "trip")),
)

PARAM_AHORRO_MOVIL = "OrbitAhorroRedMovil"

# Todos los canales v2 que tienen toggle propio.
CANALES_V2 = tuple(c for g in GRUPOS for c in g.v2)


def param_v1(canal: str) -> str:
  return f"{canal}_toggle"


def param_v2(canal: str) -> str:
  return f"tel2_{canal}_toggle"


def params_de(grupo: Grupo) -> tuple[str, ...]:
  return tuple(param_v1(c) for c in grupo.v1) + tuple(param_v2(c) for c in grupo.v2)


def activo(params, clave: str) -> bool:
  """True salvo que `clave` este EXPLICITAMENTE a False (ver REGLA DE LECTURA)."""
  try:
    if params.get(clave) is None:
      return True
    return bool(params.get_bool(clave))
  except Exception:
    return True


def grupo_activo(params, grupo: Grupo) -> bool:
  """Un grupo esta ON solo si TODOS sus canales lo estan.

  Si la app apago un solo canal v1 por cfg/desired, el grupo no se puede pintar como "todo
  encendido": ON ofreceria apagarlo cuando lo que falta es encenderlo. Pulsar el toggle lo
  deja entero en ON o entero en OFF."""
  return all(activo(params, k) for k in params_de(grupo))


def fijar_grupo(params, grupo: Grupo, encendido: bool) -> list[str]:
  """Escribe todos los params del grupo. Devuelve los que fallaron (lista vacia = ok)."""
  fallos = []
  for k in params_de(grupo):
    try:
      params.put_bool(k, bool(encendido))
    except Exception:
      fallos.append(k)
  return fallos


def canales_v2_apagados(params) -> frozenset[str]:
  """Canales v2 cuyo toggle esta explicitamente a False."""
  return frozenset(c for c in CANALES_V2 if not activo(params, param_v2(c)))


def ahorro_movil_activo(params) -> bool:
  """Degradar a perfil AHORRO cuando la red movil esta marcada como de pago.

  APAGADO por defecto (param sin configurar = False): `GsmMetered` vale "1" de fabrica, asi que
  con el ahorro automatico encendido TODO comma con SIM caia a AHORRO nada mas salir de casa
  y la app perdia percepcion, alertas, marcha y pedales justo conduciendo. Encenderlo es una
  decision del dueno, que es quien paga los datos."""
  try:
    return bool(params.get_bool(PARAM_AHORRO_MOVIL))
  except Exception:
    return False
