"""
Logica PURA de los ajustes ORBIT, compartida por las dos pantallas del comma:

  * UI grande (comma 3 / 3X): sunnypilot/layouts/settings/orbit_panel.py y
    orbit_sub_layouts/ (steer_mode, server_settings, advanced_settings),
    widgets/orbit_enroll_dialog.py.
  * UI mici (comma 4, 536x240): sunnypilot/mici/layouts/orbit*.py y
    sunnypilot/mici/widgets/orbit_*.py.

Aqui no se pinta nada (no se importa pyray): textos, validaciones y escrituras de Params
viven en UN sitio para que las dos pantallas hagan exactamente lo mismo, y para que los
tests corran sin ventana ni build (selfdrive/ui/tests/test_orbit_mici_ajustes.py).

Las funciones que escriben reciben `params` como argumento (la UI pasa ui_state.params)
para poder probarlas con un Params falso.
"""
import functools
import json
import os
import re
import tempfile
import time
from collections.abc import Callable

from openpilot.common.basedir import BASEDIR
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.ui.widgets import orbit_mando as mando
from openpilot.system.ui.lib.multilang import tr


def _ahora_ms() -> int:
  # Reloj de PARED: OrbitLastPublish, OrbitEnrollExpiry y los payloads MQTT viajan en
  # epoch. time_ns y no time(): `time.time` esta prohibido por ruff en este arbol.
  return time.time_ns() // 1_000_000


# ======================================================================== a salvo
def a_salvo(etiqueta: str, aviso: Callable[[str], None] | None = None):
  """Decorador: la funcion NUNCA deja salir una excepcion al bucle de la UI.

  El bucle de render no envuelve `widget.render` en ningun try y los callbacks de los
  botones corren dentro de el: una excepcion mata el proceso `ui` entero (bucle de
  reinicios si depende de un estado que sobrevive en Params, como el armado). Ver la
  historia completa en orbit_panel._a_salvo.

  `aviso(etiqueta)` se llama tras el fallo en las acciones del usuario: cada pantalla
  lo cuenta con su propio dialogo. Los refrescos van sin aviso (solo log).
  """
  def decorador(fn):
    @functools.wraps(fn)
    def envoltura(*args, **kwargs):
      try:
        return fn(*args, **kwargs)
      except Exception:
        cloudlog.exception(f"[Orbit/UI] {etiqueta}: excepcion no controlada")
        if aviso is not None:
          try:
            aviso(etiqueta)
          except Exception:
            cloudlog.exception("[Orbit/UI] no se pudo mostrar el aviso de fallo")
        return None
    return envoltura
  return decorador


# ======================================================================== servidor
_ETIQUETA_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def validar_host(texto: str) -> str:
  """'' si `texto` vale como direccion del broker; si no, el motivo para la pantalla.

  Solo rechaza lo CLARAMENTE invalido: una IPv4 con 4 numeros de 0 a 255, o un nombre
  de maquina de etiquetas [A-Za-z0-9-] separadas por puntos (max. 253). Ni esquema ni
  puerto: config_broker guarda el host y el puerto va en su propia clave, asi que
  "1.2.3.4:1883" se guardaba entero como host y el broker nunca conectaba.
  """
  host = (texto or "").strip()
  if not host:
    return tr("la dirección está vacía")
  if "://" in host:
    return tr("sin http:// ni otro esquema")
  if any(c.isspace() for c in host):
    return tr("sin espacios")
  if ":" in host or "/" in host:
    return tr("sin puerto ni ruta")
  if len(host) > 253:
    return tr("máximo 253 caracteres")
  etiquetas = host.split(".")
  if all(e.isascii() and e.isdigit() for e in etiquetas):
    if len(etiquetas) != 4 or any(int(e) > 255 for e in etiquetas):
      return tr("4 números de 0 a 255 con puntos")
    return ""
  if not all(_ETIQUETA_HOST.match(e) for e in etiquetas):
    return tr("solo letras, números, '-' y '.'")
  return ""


# ======================================================================== enlace / cuenta
LINK_STALE_SECONDS = 30.0


def edad_publicacion(raw, ahora_s: float) -> float:
  """Segundos desde el ultimo publish MQTT (OrbitLastPublish, epoch s); inf si no hay dato."""
  try:
    if raw:
      return max(0.0, ahora_s - float(raw))
  except (TypeError, ValueError):
    pass
  return float("inf")


def enlace_orbit(params) -> bool:
  """ENLACE: OrbitConnected y un publish de menos de 30 s.

  El heartbeat MQTT escribe OrbitLastPublish cada ~3 s; sin dato fresco el enlace se da
  por caido aunque OrbitConnected quedara en True (el proceso pudo morir sin escribir el
  False de despedida).
  """
  try:
    conectado = bool(params.get_bool("OrbitConnected"))
    return conectado and edad_publicacion(params.get("OrbitLastPublish"), time.time_ns() / 1e9) <= LINK_STALE_SECONDS
  except Exception:
    return False


def etiqueta_cuenta(owner, rol) -> str:
  """'Adrian • superadmin' (primer nombre, 12 car. max, y el rol si se conoce); '' sin dueno.

  '•' y no '·': el punto medio no esta en las fuentes .fnt del comma.
  """
  owner = owner.strip() if isinstance(owner, str) else ""
  if not owner:
    return ""
  nombre = owner.split()[0][:12]
  rol_txt = mando.rol_etiqueta(rol)
  return f"{nombre} • {rol_txt}" if rol_txt else nombre


def url_enrolamiento(dongle_id, codigo) -> str:
  """Carga del QR de vinculacion. El backend y la app parsean exactamente este formato."""
  return f"orbit://enroll?d={dongle_id or ''}&c={codigo or ''}"


def texto_caducidad(expiry_raw, ahora_ms: int) -> str:
  """'caduca en M:SS' a partir de OrbitEnrollExpiry (epoch ms); '' si no hay caducidad."""
  try:
    expiry_ms = int(expiry_raw or 0)
  except (TypeError, ValueError):
    expiry_ms = 0
  if expiry_ms <= 0:
    return ""
  restante = max(0, int(expiry_ms / 1000 - ahora_ms / 1000))
  minutos, segundos = divmod(restante, 60)
  return f"caduca en {minutos}:{segundos:02d}"


def desvincular(params) -> list[str]:
  """Desvinculo LOCAL: las mismas escrituras en las dos pantallas. Devuelve los fallos.

  Se borra tambien el rol (contrato C2) y se pide un codigo nuevo (OrbitEnrollRegen):
  OrbitPairingCode se borro al vincular y, sin esto, el QR salia con `c=` vacio hasta
  que caducaba el codigo en memoria del hilo MQTT.
  """
  fallos: list[str] = []
  pasos = (
    ("OrbitClaimed", lambda: params.put_bool("OrbitClaimed", False, True)),
    (mando.PARAM_OWNER, lambda: params.remove(mando.PARAM_OWNER)),
    (mando.PARAM_OWNER_ROLE, lambda: params.remove(mando.PARAM_OWNER_ROLE)),
    ("OrbitEnrollRegen", lambda: params.put_bool("OrbitEnrollRegen", True, True)),
  )
  for clave, paso in pasos:
    try:
      paso()
    except Exception:
      cloudlog.exception(f"[Orbit/UI] desvincular: fallo {clave}")
      fallos.append(clave)
  return fallos


# ======================================================================== mando
def lineas_mando(st) -> list[tuple[str, str, str]]:
  """Las tres lineas del estado del mando (solo con el plano vivo: `st.available`).

  (etiqueta, valor, tono) con tono 'ok' | 'aviso' | 'peligro' | 'normal'. Cada pantalla
  pone el color. Mismo criterio que la tarjeta de mando de la UI grande: nada rancio en
  verde, el enlace caido en rojo, el ultimo comando en rojo si acabo mal.
  """
  modo = (tr("MODO"), st.mode_label, "ok" if st.mode == "observer" else "aviso")

  if not st.link_ok:
    enlace = (tr("ENLACE"), tr("CAIDO"), "peligro")
  elif not st.clock_synced:
    # Sin reloj sincronizado el router rechaza todo verbo de banco: decirlo evita el
    # "el coche no responde y no se por que".
    enlace = (tr("ENLACE"), tr("OK - reloj sin sincronizar"), "aviso")
  else:
    enlace = (tr("ENLACE"), tr("OK"), "ok")

  if st.last_ack_phase != "none":
    texto = f"{st.active_verb or '-'}  -  {st.phase_label}"
    if st.last_reason:
      texto += f"  ({st.last_reason})"
  else:
    texto = tr("ninguno")
  ultimo = (tr("ULTIMO COMANDO"), texto, "peligro" if st.phase_is_bad else "normal")
  return [modo, enlace, ultimo]


# ======================================================================== banco
def texto_armar_banco() -> str:
  # El texto dice que habilita mandos REMOTOS porque es exactamente lo que hace, y es el
  # dato que decide si armar o no. Elegir el modo de volante en la pantalla no necesita
  # esto: eso va por OrbitSteerModeLocal y no abre nada por MQTT.
  return tr("Armar el MODO BANCO durante 5 minutos?\n\n" +
            "Mientras este armado, los verbos de control fisico (torque del volante, pulso " +
            "de direccion, control directo) pasan a ser ejecutables DE FORMA REMOTA desde " +
            "la app. Armalo solo con el coche parado y contigo delante.\n\n" +
            "No hace falta para elegir el modo de volante en esta pantalla.\n\n" +
            "Se desarma solo al agotarse los 5 minutos, al superar los 5 km/h y al pasar " +
            "a offroad.")


def texto_estado_banco(armado: bool, restante_s: float) -> str:
  if armado:
    return tr("ARMADO") + f" - {int(restante_s)}s " + tr("restantes")
  return tr("Desarmado. Los verbos de banco se rechazan.")


# ======================================================================== volante
# Indice del boton -> valor de SteerTorqueMode. El orden replica el Qt original:
#   ['COMMA', 'COMMA+JETSON', 'JETSON', 'TEST MAX'] -> 0, 3, 1, 2
# (etiquetas cortas: 'MODELO COMMA' desbordaba los 320 px del boton de la UI grande)
MODE_BUTTONS = ["COMMA", "COMMA+JETSON", "JETSON", "TEST MAX"]
INDEX_TO_MODE = {0: 0, 1: 3, 2: 1, 3: 2}
MODE_TO_INDEX = {v: k for k, v in INDEX_TO_MODE.items()}
MODE_NAMES = {0: "MODELO COMMA", 1: "JETSON", 2: "TEST MAX", 3: "COMMA+JETSON"}

# Modos cuyo override de torque controlsd solo aplica con autorizacion vigente.
MODES_REQUIRING_BENCH = (1, 2)

TORQUE_STALE_SECONDS = 3.0

PARAM_APPLY_TARGET = "JetsonObstacleApplyTarget"


def texto_estado_volante(modo: int, objetivo, armado: bool, restante_s: float, local: bool) -> str:
  """Estado REAL del volante, no el boton pulsado.

  Sin esto el selector se queda marcando JETSON, el volante lo lleva el modelo Comma
  porque no hay autorizacion, y nada en pantalla lo explica. Para los modos 1 y 2
  controlsd acepta CUALQUIERA de las dos: el banco armado u OrbitSteerModeLocal (`local`,
  lo que escribe el selector de esta pantalla). Mirar solo el banco decia "sin efecto"
  justo despues de elegir JETSON aqui, con la Jetson llevando el volante.
  """
  if modo == 0:
    return tr("torque del modelo interno")
  if modo == 3:
    tgt_label = tr("TORQUE") if objetivo == "torque" else tr("CURVATURA")
    return tr("esquive en") + f" {tgt_label}"
  if local:
    return tr("ACTIVO, elegido en pantalla")
  if armado:
    return tr("ACTIVO, banco armado") + f" ({int(restante_s)}s)"
  return tr("SIN EFECTO: banco no armado, manda COMMA")


def confirmacion_modo(modo: int) -> tuple[str, str]:
  """(texto, boton de confirmar) del cambio al modo `modo`."""
  if modo == 0:
    return (tr("Volver al MODELO COMMA (recomendado).\n\n" +
               "El volante usara el torque calculado por el modelo interno de openpilot. " +
               "Esta es la opcion mas segura y probada."),
            tr("Cambiar a MODELO COMMA"))
  if modo == 1:
    return (tr("ATENCION\n\n" +
               "Vas a delegar el control del volante a la JETSON (PilotNet). " +
               "El volante obedecera al torque que calcule la red neuronal externa.\n\n" +
               "Es una autorizacion PRESENCIAL: dura hasta volver a COMMA o pasar a offroad. " +
               "No arma el banco ni habilita mandos remotos.\n\n" +
               "Asegurate de que la Jetson esta conectada y enviando torque por ZMQ, " +
               "de estar en un entorno controlado y de tener las manos sobre el volante.\n\n" +
               "Deseas continuar?"),
            tr("SI, usar JETSON"))
  if modo == 2:
    return (tr("PELIGRO - MODO DE PRUEBA\n\n" +
               "Este modo fija el torque del volante al MAXIMO hacia la DERECHA de forma continua. " +
               "SOLO sirve para verificar la interceptacion del torque.\n\n" +
               "Es una autorizacion PRESENCIAL y no arma el banco. Se retira sola al superar " +
               "los 5 km/h (vuelve a COMMA) y al pasar a offroad.\n\n" +
               "USALO SOLO EN PRUEBAS CONTROLADAS, CON LAS MANOS EN EL VOLANTE. " +
               "NO LO USES EN VIA PUBLICA.\n\n" +
               "Deseas continuar?"),
            tr("SI, ACTIVAR TEST MAX"))
  return (tr("Activar COMMA + JETSON.\n\n" +
             "El volante usara el torque del MODELO COMMA (comportamiento normal). " +
             "Si la Jetson detecta un obstaculo, aplicara temporalmente un esquive lateral.\n\n" +
             "No necesita armado de banco.\n\n" +
             "Requisitos: Jetson conectada y enviando alertas por ZMQ, y modelo de " +
             "deteccion de obstaculos cargado."),
          tr("SI, activar COMMA+JETSON"))


def texto_esquive_curvatura() -> tuple[str, str, str]:
  """Paso 1 de COMMA+JETSON: (texto, CURVATURA, otra opcion)."""
  return (tr("COMMA + JETSON - Como debe esquivar la Jetson?\n\n" +
             "CURVATURA suma un offset a la curvatura deseada. Comportamiento historico, " +
             "mas suave y predecible (RECOMENDADO).\n\n" +
             "Pulsa CURVATURA para usarla, o Otra opcion para elegir TORQUE (beta)."),
          tr("CURVATURA (recomendado)"), tr("Otra opcion (TORQUE)"))


def texto_esquive_torque(cancelar: str = "Pulsa Cancelar para no cambiar nada.") -> tuple[str, str]:
  """Paso 2 de COMMA+JETSON: (texto, confirmar TORQUE).

  `cancelar` dice como se cancela en cada pantalla: el comma 4 no tiene boton Cancelar,
  se desliza hacia abajo.
  """
  return (tr("COMMA + JETSON - Usar TORQUE para esquivar? (BETA)\n\n" +
             "TORQUE pisa directamente el torque del volante mientras dura el esquive. " +
             "Reaccion mas fuerte e inmediata.\n\n") + tr(cancelar),
          tr("SI, usar TORQUE"))


def _dongle(params) -> str | None:
  dongle = params.get("DongleId")
  return dongle if dongle else None


def aplicar_modo_volante(params, modo: int) -> None:
  """Commit del selector de volante: modo, autorizacion presencial y buzon MQTT.

  SteerTorqueMode es INT: put(str) lanza TypeError. block=True: la autorizacion de la
  linea siguiente y el texto de estado leen este valor de inmediato.

  AUTORIZACION PRESENCIAL, no armado de banco: OrbitBenchArmed es lo que el router mira
  para dejar pasar los verbos FISICOS por MQTT, asi que elegir JETSON delante del coche
  no puede armarlo (ver la cabecera de steer_mode.py). OrbitSteerModeLocal lo acepta
  controlsd para los modos 1 y 2 y NO habilita ningun verbo remoto. Va DESPUES del modo.
  """
  params.put(mando.PARAM_STEER_MODE, modo, True)
  mando.set_steer_local(modo in MODES_REQUIRING_BENCH)

  dongle = _dongle(params)
  if dongle:
    payload = {
      "dongle_id": dongle,
      "steer_torque_mode": modo,
      "source": "comma_ui",
      "timestamp": str(_ahora_ms()),
    }
    params.put("SteerTorqueModeMqttPayload", json.dumps(payload))


def fijar_objetivo_esquive(params, objetivo: str) -> None:
  """Como esquiva la Jetson en COMMA+JETSON: 'curvature' (recomendado) o 'torque' (beta)."""
  params.put(PARAM_APPLY_TARGET, objetivo)
  dongle = _dongle(params)
  if dongle:
    payload = {
      "dongle_id": dongle,
      "apply_target": objetivo,
      "source": "comma_ui",
      "ts": str(_ahora_ms()),
    }
    params.put("JetsonObstacleApplyTargetMqttPayload", json.dumps(payload))


# ======================================================================== valores seguros
def texto_valores_seguros() -> str:
  return tr("Restablecer los params ORBIT de conduccion a valores seguros?\n\n" +
            "- Modo de volante: COMMA\n" +
            "- Armado de banco y modo del mando: desarmados\n" +
            "- Frenado de emergencia remoto: OFF\n" +
            "- Cambios de carril forzados pendientes: borrados\n" +
            "- Pulso de direccion remoto: borrado\n" +
            "- Alertas de comunicacion: visibles\n\n" +
            "No toca la configuracion de servidor, telemetria ni Jetson (IPs/puertos), " +
            "ni el interruptor de privacidad.")


def aplicar_valores_seguros(params) -> list[str]:
  """Siete grupos de escrituras INDEPENDIENTES, cada una con su try y su log.

  Antes era un unico try/except global: si la primera fallaba (`put("SteerTorqueMode", 0)`,
  la mas propensa: un put con el tipo equivocado lanza TypeError) las demas ni se
  intentaban y el usuario no se enteraba. Un boton de panico que falla mudo es peor que
  no tener boton. Devuelve las claves que fallaron, ordenadas y sin repetir.
  """
  fallos: list[str] = []

  def _paso(clave, fn):
    try:
      fn()
    except Exception:
      cloudlog.exception(f"[Orbit/UI] valores seguros: fallo {clave}")
      fallos.append(clave)

  # block=True: la escritura por defecto es asincrona y el resultado que se pinta justo
  # despues seria el valor viejo (ver la nota de orbit_mando._BLOQUEANTE).
  # 1. fuente de torque del volante (INT: put exige int nativo)
  _paso(mando.PARAM_STEER_MODE, lambda: params.put(mando.PARAM_STEER_MODE, 0, True))
  # 2. frenado remoto
  _paso("brutebreak_active", lambda: params.put_bool("brutebreak_active", False, True))
  # 3. cambios de carril forzados pendientes
  _paso("ForceLaneChangeLeft", lambda: params.put_bool("ForceLaneChangeLeft", False, True))
  _paso("ForceLaneChangeRight", lambda: params.put_bool("ForceLaneChangeRight", False, True))
  # 4. pulso de direccion remoto
  _paso("orbit_steering_pulse", lambda: params.remove("orbit_steering_pulse"))
  # 5. autoridad del mando: armado de banco y modo, por si el hilo ORBIT esta caido
  fallos.extend(mando.disarm_bench())
  _paso(mando.PARAM_COMMAND_MODE, lambda: params.put(mando.PARAM_COMMAND_MODE, 0, True))
  # 6. HUD de adelantamiento: sic_adelantar es PERSISTENT y su overlay ya no existe;
  #    apagarlo aqui evita dejar un flag encendido que nadie puede volver a apagar.
  _paso("sic_adelantar", lambda: params.put_bool("sic_adelantar", False, True))
  # 7. volcado de mensajes MQTT a /tmp (era PERSISTENT|BACKUP y sobrevivia a reinicios)
  _paso("modo_debug", lambda: params.put_bool("modo_debug", False, True))
  return sorted(set(fallos))


# ======================================================================== jetson
# Valores por defecto, los del Qt viejo.
CONFIG_DEFAULTS = {
  "jetson_enabled": False,
  "jetson_ip": "192.168.1.50",
  "comma_ip": "127.0.0.1",
  "jetson_img_port": 5555,
  "jetson_torque_port": 5556,
  "jpeg_quality": 80,
}

# Campos de red editables en pantalla: (clave, es_entero, (min, max) | None, paso | None).
CAMPOS_JETSON = (
  ("jetson_ip", False, None, None),
  ("comma_ip", False, None, None),
  ("jetson_img_port", True, None, None),
  ("jetson_torque_port", True, None, None),
  ("jpeg_quality", True, (10, 100), 10),
)


def ruta_config_jetson() -> str:
  """orbit/config_jetson.json en BASEDIR, con /data/openpilot de respaldo."""
  candidatas = (os.path.join(BASEDIR, "orbit", "config_jetson.json"),
                "/data/openpilot/orbit/config_jetson.json")
  for ruta in candidatas:
    if os.path.exists(ruta):
      return ruta
  return candidatas[0]  # se crea al guardar


def cargar_config_jetson(ruta: str) -> dict:
  config = dict(CONFIG_DEFAULTS)
  try:
    with open(ruta) as f:
      data = json.load(f)
    if isinstance(data, dict):
      config.update(data)
  except (OSError, ValueError):
    pass
  return config


def guardar_config_jetson(ruta: str, config: dict, params) -> bool:
  """Escritura atomica (tempfile + os.replace) con `_version`, JetsonConfigChanged y payload MQTT.

  Modifica `config` (anade `_version`). False si no se pudo escribir el fichero; en ese
  caso no se levanta el flag ni se publica nada.
  """
  version_ms = _ahora_ms()
  config["_version"] = str(version_ms)
  directorio = os.path.dirname(ruta)
  try:
    os.makedirs(directorio, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directorio, suffix=".tmp")
    try:
      with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=4, sort_keys=True)
      os.replace(tmp, ruta)
    except Exception:
      if os.path.exists(tmp):
        os.remove(tmp)
      raise
  except OSError:
    return False

  params.put_bool("JetsonConfigChanged", True)
  dongle = _dongle(params)
  if dongle:
    payload = {
      "dongle_id": dongle,
      "jetson_enabled": bool(config.get("jetson_enabled", False)),
      "jetson_ip": str(config.get("jetson_ip", "")),
      "comma_ip": str(config.get("comma_ip", "")),
      "jetson_img_port": int(config.get("jetson_img_port", 5555)),
      "jetson_torque_port": int(config.get("jetson_torque_port", 5556)),
      "jpeg_quality": int(config.get("jpeg_quality", 80)),
      "source": "comma_ui",
      "timestamp": str(_ahora_ms()),
      "_version": str(version_ms),
    }
    params.put("JetsonConfigMqttPayload", json.dumps(payload))
  return True


def parsear_campo(texto: str, es_entero: bool, rango: tuple[int, int] | None = None, paso: int | None = None):
  """Valor a guardar, o None si la entrada se ignora (vacia o no numerica)."""
  texto = (texto or "").strip()
  if not texto:
    return None
  if not es_entero:
    return texto
  try:
    valor = int(texto)
  except ValueError:
    return None
  if paso:
    valor = int(round(valor / paso) * paso)
  if rango:
    valor = max(rango[0], min(rango[1], valor))
  return valor


def texto_torque(torque_raw, ts_raw, ahora_s: float) -> str:
  """JetsonTorque con signo, o '-' si no hay dato o tiene mas de 3 s (sello de pared)."""
  if not torque_raw:
    return "-"
  try:
    if not ts_raw or ahora_s - float(ts_raw) > TORQUE_STALE_SECONDS:
      return "-"
    return f"{float(torque_raw):+.2f}"
  except (TypeError, ValueError):
    return "-"


def texto_obstaculo(raw) -> str:
  """SI/NO a partir del JSON {"obstacle": bool, ...} de JetsonObstaclePulse."""
  if raw:
    try:
      payload = json.loads(raw)
      if isinstance(payload, dict) and payload.get("obstacle"):
        return tr("SI")
    except (TypeError, ValueError):
      pass
  return tr("NO")


# ======================================================================== texto en tarjetas
def _partir_palabras(frase: str, max_car: int) -> list[str]:
  """Parte una frase larga en trozos PAREJOS (no uno lleno y una palabra suelta)."""
  n = max(1, -(-len(frase) // max_car))
  objetivo = len(frase) / n
  piezas: list[str] = []
  actual = ""
  for palabra in frase.split():
    candidato = f"{actual} {palabra}" if actual else palabra
    if actual and (len(candidato) > max_car or (len(actual) >= objetivo and len(piezas) < n - 1)):
      piezas.append(actual)
      actual = palabra
    else:
      actual = candidato
  if actual:
    piezas.append(actual)
  return piezas


def trocear(texto: str, max_car: int = 90) -> list[str]:
  """Parte un texto largo en trozos de <= max_car para las tarjetas de la pantalla pequena.

  Un parrafo nunca se mezcla con otro. Dentro de el se juntan frases enteras (o lineas, si
  el parrafo es una lista con saltos de linea, que se conservan) y solo se parte por
  palabras una frase que por si sola no cabe. Nunca parte una palabra ni pierde texto.
  """
  trozos: list[str] = []
  for parrafo in texto.split("\n\n"):
    parrafo = parrafo.strip()
    if not parrafo:
      continue
    es_lista = "\n" in parrafo
    unidades = parrafo.split("\n") if es_lista else re.split(r"(?<=[.?!])\s+", parrafo)
    sep = "\n" if es_lista else " "
    actual = ""
    for unidad in unidades:
      for pieza in _partir_palabras(unidad, max_car):
        candidato = f"{actual}{sep}{pieza}" if actual else pieza
        if len(candidato) > max_car and actual:
          trozos.append(actual)
          actual = pieza
        else:
          actual = candidato
    if actual:
      trozos.append(actual)
  return trozos
