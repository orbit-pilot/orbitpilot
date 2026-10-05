"""
Modelo local del MANDO REMOTO ORBIT para la interfaz del comma.

Tres cosas viven aqui, y ninguna de ellas dibuja:

  1. `CommandStateView` — lectura del PLANO DE ESTADO. El subsistema de mando publica
     el mensaje cereal `orbitCommandState` a 10 Hz (seccion 5 del diseno) con modo,
     verbo activo, mascara de gates, ultima fase de ACK, armado de banco y reloj
     sincronizado. La UI lo LEE de ahi y NO de Params: `Params.put` en este arbol es
     mkstemp+fsync y el plano cambia diez veces por segundo. Leerlo por frame desde
     disco es exactamente el patron que provoco commIssue.

  2. `BenchGuard` — el vigilante del ARMADO DE BANCO. Corre en `UIStateSP.update`, es
     decir en CADA frame de la UI y no solo mientras el panel esta abierto: un armado
     que solo caduca cuando el usuario mira la pantalla no caduca.

  3. Privacidad e interruptores de autoridad — las escrituras de Params/ficheros que
     hace la pantalla fisica. Todas devuelven la lista de lo que fallo en vez de
     tragarse la excepcion: un boton de panico que falla mudo es peor que no tenerlo.

REGLA DE TIPOS DE PARAMS (la que mas ha mordido en este arbol): `put()` exige el tipo
NATIVO de la clave. `put("1")` sobre una clave INT lanza TypeError, y si esa llamada
esta dentro de un try/except global se pierde ademas todo lo que venia detras. Por eso
`OrbitBenchExpiry` se escribe como TEXTO (es STRING) y `OrbitCommandMode` /
`SteerTorqueMode` como int.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

from openpilot.common.basedir import BASEDIR
from openpilot.common.params import Params, UnknownKeyName
from openpilot.common.swaglog import cloudlog
from openpilot.orbit import telemetria_grupos as grupos

# --------------------------------------------------------------------------- params
# Registrados en common/params_keys.h. Los cuatro del mando son
# CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION: jamas PERSISTENT (seccion 4.1).
PARAM_COMMAND_MODE = "OrbitCommandMode"    # INT 0..3
PARAM_BENCH_ARMED = "OrbitBenchArmed"      # BOOL
PARAM_BENCH_EXPIRY = "OrbitBenchExpiry"    # STRING, epoch ms como TEXTO
PARAM_DISARM_ALL = "OrbitDisarmAll"        # BOOL, disparo one-shot
PARAM_STEER_MODE = "SteerTorqueMode"       # INT 0..3
# Autorizacion PRESENCIAL del selector de volante. NO es el armado de banco: armar el banco
# habilita los verbos fisicos POR MQTT a cualquiera que publique en el broker, y elegir
# "Jetson" delante del coche no puede significar eso. Se limpia al arrancar el manager y al
# pasar a offroad (flags del param), y lo retira esta misma pantalla al salir del modo.
PARAM_STEER_LOCAL = "OrbitSteerModeLocal"  # BOOL

# Cuenta ORBIT del dueño. Los escribe mqtt_comandos al recibir el enroll_ack
# (retenido) del backend; la UI solo los lee.
PARAM_OWNER = "OrbitOwner"                 # STRING: nombre o email
PARAM_OWNER_ROLE = "OrbitOwnerRole"        # STRING: user | developer | superadmin
ROL_ETIQUETA = {"user": "usuario", "developer": "desarrollador", "superadmin": "superadmin"}


def rol_etiqueta(rol) -> str:
  """Etiqueta en pantalla del rol ORBIT del dueño; '' si no se conoce."""
  if isinstance(rol, bytes):
    rol = rol.decode(errors="ignore")
  return ROL_ETIQUETA.get(str(rol or "").strip().lower(), "")

# Interruptor maestro de privacidad. TODAVIA NO ESTA REGISTRADO en common/params_keys.h
# (ese fichero no se toca desde aqui), asi que cada acceso va envuelto en UnknownKeyName
# y el corte real se aplica ademas por los caminos que YA se honran en caliente
# (ver `set_privacy_mute`). Cuando alguien registre la clave, el publicador solo tiene
# que leerla y el interruptor pasa a ser un unico punto de verdad.
PARAM_PRIVACY_MUTE = "OrbitPrivacyMute"    # BOOL (pendiente de registrar)

# Canales cereal de POSICION del emisor MQTT. mqtt_envio_general._canal_habilitado los
# relee cada 5 s y excluye el canal solo si el toggle esta EXPLICITAMENTE a False, asi
# que escribir False aqui corta la emision de posicion en caliente y sin red.
POSITION_CHANNEL_PARAMS = ("gpsLocation_toggle", "gpsLocationExternal_toggle")

# Camara -> app (MQTT). Lo escribe orbit/camera_sender.py; aqui se escribe para dejar el
# envio apagado. OJO: camera_sender solo lee este fichero en su __init__, asi que el
# corte de esta via NO es en caliente (ver el informe de esta tarea).
CAMERA_CONFIG_FILE = "/data/orbit_camera_config.json"

# Camara -> Jetson (ZMQ). camera_sender comprueba JetsonConfigChanged en cada vuelta de
# su bucle y rehace el cliente ZMQ, asi que esta via SI se corta en caliente.
PARAM_JETSON_CONFIG_CHANGED = "JetsonConfigChanged"

# Estado local del interruptor de privacidad. Fichero y no Param porque la clave maestra
# aun no existe y porque hay que recordar QUE habia antes para poder restaurarlo.
PRIVACY_STATE_FILE = "/data/orbit_privacy.json"

# --------------------------------------------------------------------------- armado
# Seccion 4.1: el modo banco se arma SOLO desde la pantalla fisica y caduca a los 300 s.
BENCH_TTL_S = 300.0
# Por debajo de esto se renueva el arrendamiento del selector local (ver BenchGuard).
# Desarme automatico por velocidad. Es el mismo umbral que el diseno exige para el
# control fisico de banco (5 km/h).
BENCH_DISARM_SPEED_MS = 5.0 / 3.6

# Por que se armo. Vive en RAM del proceso de UI A PROPOSITO: es lo unico que distingue
# "el conductor toco la pantalla" de "llego un mensaje", y MQTT no puede alcanzarlo.
ARM_REASON_BENCH = "bench"   # boton explicito ARMAR BANCO -> se desarma al moverse


# ESCRITURAS BLOQUEANTES A PROPOSITO.
# `Params.put` por defecto es putNonBlocking: encola y vuelve. Para el estado de
# autoridad eso es inaceptable por dos motivos. (1) La UI lee inmediatamente despues
# para pintar el resultado, y con la escritura en vuelo pinta el valor viejo: el usuario
# pulsa DESARMAR y ve "ARMADO". (2) Cualquier TypeError del hilo asincrono no vuelve
# nunca, asi que la lista de fallos que se ensena seria mentira. Son acciones puntuales
# de usuario (armar, desarmar, restablecer), no camino de 100 Hz: un fsync aqui no le
# hace dano a nadie. OJO: block=True hace la escritura sincrona, pero Params NO lanza
# si el disco falla; lo que se captura de verdad es el error de TIPO/clave, que es el
# fallo real que este arbol ha tenido.
_BLOQUEANTE = True


def _now_ms() -> int:
  """Epoch en MILISEGUNDOS. Reloj de pared a proposito: OrbitBenchExpiry viaja en epoch
  porque es lo que la app puede leer. El PLAZO, en cambio, lo mide el plano de estado en
  monotono (`command_state.py` convierte una sola vez); un salto del reloj de pared no
  puede alargar un armado.

  time_ns y no time(): `time.time` esta prohibido por ruff en este arbol.
  """
  return time.time_ns() // 1_000_000


class _ParamsHolder:
  """Instancia perezosa de Params compartida por el modulo."""
  _p: Params | None = None

  @classmethod
  def get(cls) -> Params | None:
    if cls._p is None:
      try:
        cls._p = Params()
      except Exception:
        cloudlog.exception("[Orbit/UI] no se pudo abrir Params")
        return None
    return cls._p


def params() -> Params | None:
  return _ParamsHolder.get()


# ============================================================================ lectura
class CommandStateView:
  """Instantanea legible del mensaje cereal `orbitCommandState`.

  `update(sm)` se llama con el SubMaster de la UI. Si el servicio no esta vivo, el
  subsistema de mando no esta publicando: eso NO se pinta como "todo en verde", se
  pinta como enlace caido, que es lo que es.
  """

  MODE_LABELS = {
    "observer": "OBSERVADOR",
    "copilot": "COPILOTO",
    "maneuver": "MANIOBRA",
    "bench": "BANCO",
  }

  PHASE_LABELS = {
    "none": "-",
    "received": "RECIBIDO",
    "accepted": "ACEPTADO",
    "rejected": "RECHAZADO",
    "executing": "EJECUTANDO",
    "applied": "APLICADO",
    "failed": "FALLO",
    "expired": "CADUCADO",
    "superseded": "SUSTITUIDO",
  }

  # Fases en las que el ultimo comando termino mal. Se pintan en rojo.
  PHASE_BAD = ("rejected", "failed", "expired")

  SERVICE = "orbitCommandState"

  def __init__(self):
    self._limpiar()

  def _limpiar(self) -> None:
    # No se conserva el ultimo valor conocido: un plano muerto no describe el coche
    # de ahora, y pintar un estado rancio como si fuera vivo es justo el fallo que
    # este panel existe para evitar.
    self.available = False
    self.mode = "observer"
    self.active_verb = ""
    self.last_ack_phase = "none"
    self.last_reason = ""
    self.bench_armed = False
    self.clock_synced = False
    self.gates = 0
    self.link_fresh = False
    # seq: ultimo seq APLICADO (solo avanza con ordenes que pasaron modo y gates y van a
    # ejecutarse). deadline_mono: fin de la ventana del actuador remoto en time.monotonic()
    # de SISTEMA (command_state.py); 0.0 = ningun actuador remoto vivo.
    self.seq = 0
    self.deadline_mono = 0.0

  def update(self, sm) -> None:
    try:
      alive = bool(sm.alive[self.SERVICE]) and bool(sm.valid[self.SERVICE])
    except (KeyError, TypeError):
      # El servicio no esta en el SubMaster de este build: no hay plano que leer.
      self._limpiar()
      return

    if not alive:
      self._limpiar()
      return

    try:
      st = sm[self.SERVICE]
      self.mode = str(st.mode)
      self.active_verb = str(st.activeVerb or "")
      self.last_ack_phase = str(st.lastAckPhase)
      self.last_reason = str(st.lastReason or "")
      self.bench_armed = bool(st.benchArmed)
      self.clock_synced = bool(st.clockSynced)
      self.gates = int(st.gates)
      # Bit linkFresh del enum Gate de cereal/custom.capnp (1 << 8).
      self.link_fresh = bool(self.gates & (1 << 8))
      self.seq = int(st.seq)
      self.deadline_mono = float(st.deadlineMono)
      self.available = True
    except (KeyError, AttributeError, ValueError, TypeError):
      self._limpiar()

  @property
  def actuator_live(self) -> bool:
    """Ventana de actuador remoto abierta AHORA (deadman de la seccion 5). Mismo reloj
    que el publicador: CLOCK_MONOTONIC de sistema, comparable entre procesos."""
    return self.available and self.deadline_mono > time.monotonic()

  # ------------------------------------------------------------------- etiquetas
  @property
  def mode_label(self) -> str:
    return self.MODE_LABELS.get(self.mode, self.mode.upper())

  @property
  def phase_label(self) -> str:
    return self.PHASE_LABELS.get(self.last_ack_phase, self.last_ack_phase.upper())

  @property
  def phase_is_bad(self) -> bool:
    return self.last_ack_phase in self.PHASE_BAD

  @property
  def link_ok(self) -> bool:
    """Salud del enlace de mando: hay plano publicando Y el gate de frescura esta verde."""
    return self.available and self.link_fresh


# ============================================================================ armado
def bench_expiry_ms() -> int:
  """Caducidad del armado en epoch ms, 0 si no hay."""
  p = params()
  if p is None:
    return 0
  try:
    raw = p.get(PARAM_BENCH_EXPIRY)
    return int(str(raw).strip()) if raw not in (None, "") else 0
  except (UnknownKeyName, TypeError, ValueError):
    return 0


def bench_armed() -> bool:
  p = params()
  if p is None:
    return False
  try:
    return bool(p.get_bool(PARAM_BENCH_ARMED))
  except UnknownKeyName:
    return False


def bench_remaining_s() -> float:
  """Segundos que le quedan al armado. <= 0 significa caducado."""
  expiry = bench_expiry_ms()
  if expiry <= 0:
    return 0.0
  return max(0.0, (expiry - _now_ms()) / 1000.0)


# Cache del armado para los widgets. bench_armed()/bench_expiry_ms() abren ficheros de
# Params, y un lambda de titulo o una cuenta atras los llamarian 60 veces por segundo.
# Se cachea la CADUCIDAD (que casi nunca cambia) y los segundos restantes se calculan en
# RAM, para que la cuenta atras siga siendo exacta con una lectura de disco cada medio
# segundo. Toda escritura del armado invalida la cache, asi que la UI nunca ensena
# "ARMADO" un instante despues de que el usuario haya pulsado DESARMAR.
_SNAPSHOT_TTL_S = 0.5
_snapshot = {"mono": -1.0, "armed": False, "expiry_ms": 0}


def _invalidar_snapshot() -> None:
  _snapshot["mono"] = -1.0


def bench_snapshot() -> tuple[bool, float]:
  """(armado, segundos restantes) con lectura de Params limitada a 2 Hz.

  Es lo que deben usar los widgets; `bench_armed()` y `bench_remaining_s()` van sin
  cache y son para el vigilante y para los caminos de accion del usuario.
  """
  ahora = time.monotonic()
  if _snapshot["mono"] < 0.0 or (ahora - _snapshot["mono"]) > _SNAPSHOT_TTL_S:
    _snapshot["mono"] = ahora
    _snapshot["armed"] = bench_armed()
    _snapshot["expiry_ms"] = bench_expiry_ms()
  expiry = int(_snapshot["expiry_ms"])
  restante = max(0.0, (expiry - _now_ms()) / 1000.0) if expiry > 0 else 0.0
  return bool(_snapshot["armed"]) and restante > 0.0, restante


def arm_bench(ttl_s: float = BENCH_TTL_S) -> list[str]:
  """Arma el modo banco desde la pantalla fisica. Devuelve la lista de fallos.

  ORDEN DE ESCRITURA: primero la caducidad y despues el bool. Al reves habria una
  ventana en la que el plano ve `OrbitBenchArmed` a True sin caducidad; ese caso lo
  trata como NO armado (fail-closed) y ademas deja un error en el log cada vez.
  """
  p = params()
  if p is None:
    return [PARAM_BENCH_EXPIRY, PARAM_BENCH_ARMED]

  fallos: list[str] = []
  _invalidar_snapshot()
  expiry_ms = _now_ms() + int(ttl_s * 1000)
  try:
    # STRING: epoch ms como TEXTO. El porque de que no sea INT esta anotado en la
    # propia clave, en common/params_keys.h.
    p.put(PARAM_BENCH_EXPIRY, str(expiry_ms), _BLOQUEANTE)
  except Exception:
    cloudlog.exception(f"[Orbit/UI] no se pudo escribir {PARAM_BENCH_EXPIRY}")
    fallos.append(PARAM_BENCH_EXPIRY)
    return fallos  # sin caducidad no se arma: el armado sin plazo es un actuador pegado

  try:
    p.put_bool(PARAM_BENCH_ARMED, True, _BLOQUEANTE)
  except Exception:
    cloudlog.exception(f"[Orbit/UI] no se pudo escribir {PARAM_BENCH_ARMED}")
    fallos.append(PARAM_BENCH_ARMED)
  return fallos


def set_steer_local(activo: bool) -> list[str]:
  """Concede o retira la autorizacion presencial del selector de volante.

  block=True: controlsd la lee con cache de 0.5 s y la pantalla pinta el estado justo
  despues de escribir; con la escritura en vuelo pintaria el valor viejo.
  """
  fallos: list[str] = []
  p = params()
  if p is None:
    return [PARAM_STEER_LOCAL]
  try:
    p.put_bool(PARAM_STEER_LOCAL, bool(activo), _BLOQUEANTE)
  except Exception:
    cloudlog.exception(f"[Orbit/UI] no se pudo escribir {PARAM_STEER_LOCAL}")
    fallos.append(PARAM_STEER_LOCAL)
  return fallos


def steer_local_activo() -> bool:
  p = params()
  if p is None:
    return False
  try:
    return bool(p.get_bool(PARAM_STEER_LOCAL))
  except Exception:
    return False


def disarm_bench() -> list[str]:
  """Desarma el banco. Independientes: si falla el bool, la caducidad se borra igual."""
  p = params()
  if p is None:
    return [PARAM_BENCH_ARMED]

  fallos: list[str] = []
  _invalidar_snapshot()
  try:
    p.put_bool(PARAM_BENCH_ARMED, False, _BLOQUEANTE)
  except Exception:
    cloudlog.exception(f"[Orbit/UI] no se pudo desarmar {PARAM_BENCH_ARMED}")
    fallos.append(PARAM_BENCH_ARMED)
  try:
    p.remove(PARAM_BENCH_EXPIRY)
  except Exception:
    cloudlog.exception(f"[Orbit/UI] no se pudo borrar {PARAM_BENCH_EXPIRY}")
    fallos.append(PARAM_BENCH_EXPIRY)
  return fallos


def disarm_all() -> list[str]:
  """DESARMAR TODO. El unico control que nunca se bloquea (seccion 2: bajar autoridad
  siempre se acepta).

  Cinco escrituras INDEPENDIENTES, cada una con su try y su log. No es una lista de
  cortesia: `OrbitDisarmAll` lo consume el plano de estado, y si el hilo ORBIT esta
  caido -- que es justo cuando mas falta hace un boton de panico -- nadie lo leeria.
  Por eso la pantalla baja ademas la autoridad que puede bajar ella sola: armado de
  banco, modo del mando y fuente de torque del volante.
  """
  p = params()
  if p is None:
    return ["Params"]

  fallos: list[str] = []

  # 1. El disparo del contrato: lo consume CommandStateStore.consume_disarm_request().
  try:
    p.put_bool(PARAM_DISARM_ALL, True, _BLOQUEANTE)
  except Exception:
    cloudlog.exception(f"[Orbit/UI] disarm_all: fallo {PARAM_DISARM_ALL}")
    fallos.append(PARAM_DISARM_ALL)

  # 2-3. Armado de banco (las dos claves, por separado).
  fallos.extend(disarm_bench())

  # 4. Modo del mando a observador (INT: put exige int nativo).
  try:
    p.put(PARAM_COMMAND_MODE, 0, _BLOQUEANTE)
  except Exception:
    cloudlog.exception(f"[Orbit/UI] disarm_all: fallo {PARAM_COMMAND_MODE}")
    fallos.append(PARAM_COMMAND_MODE)

  # 5. Fuente de torque del volante de vuelta al modelo Comma.
  try:
    p.put(PARAM_STEER_MODE, 0, _BLOQUEANTE)
  except Exception:
    cloudlog.exception(f"[Orbit/UI] disarm_all: fallo {PARAM_STEER_MODE}")
    fallos.append(PARAM_STEER_MODE)

  return fallos


class BenchGuard:
  """Vigilante del armado de banco. Se llama desde `UIStateSP.update`, o sea en cada
  frame de la UI y con independencia de la pantalla que se este mirando.

  POR QUE EL MOTIVO DEL ARMADO VIVE EN RAM. El diseno exige dos cosas que se pisan si
  el armado es un unico bool sin contexto:

    * el armado de banco se desarma solo al superar los 5 km/h (seccion 4.1), y
    * el selector de modo de volante tiene que seguir funcionando desde la pantalla
      fisica, incluso circulando, porque tocar la pantalla es una accion presencial.

  controlsd exige `OrbitBenchArmed` vigente para los modos 1 (JETSON) y 2 (TEST MAX),
  asi que la unica forma de que el selector local vuelva a funcionar es que el propio
  selector arme. Lo que distingue ese armado del otro es QUIEN lo pidio, y eso no puede
  vivir en un Param: cualquiera que publique en el broker escribiria el Param. Vive en
  la RAM de este proceso, al que MQTT no llega.

  Consecuencias, todas deliberadas:
    * motivo `steer` (el conductor eligio JETSON en la pantalla): no se desarma por
      velocidad y el plazo se renueva mientras el modo siga elegido. Un mensaje MQTT que
      ponga SteerTorqueMode=1 NO produce motivo `steer`, asi que no arma ni renueva
      nada: el camino remoto sigue exigiendo banco.
    * motivo `steer` con modo 2 (TEST MAX, par a tope fijo): SI se desarma por
      velocidad. Es una prueba de banco parado, y al desarmarse el volante vuelve al
      modelo Comma, que es la salida segura.
    * motivo `bench` (boton explicito) o motivo DESCONOCIDO (la UI se reinicio y perdio
      el contexto): se desarma por velocidad. Fail-closed.
  """

  # 1 Hz basta: el plazo es de 300 s y el desarme por velocidad no necesita mas
  # resolucion que la de un segundo. Cada tick son lecturas de Params (ficheros).
  TICK_S = 1.0
  # Re-afirmacion del silencio de privacidad. La app puede volver a encender la camara
  # por MQTT; mientras el publicador no honre el interruptor maestro, esto lo reimpone.
  PRIVACY_TICK_S = 5.0

  def __init__(self):
    self._reason: str | None = None
    self._next_tick = 0.0
    self._next_privacy = 0.0
    self._started_prev = False

  def note_local_arm(self, reason: str) -> None:
    """La pantalla fisica acaba de armar. `reason` es ARM_REASON_*."""
    self._reason = reason

  def note_disarm(self) -> None:
    self._reason = None

  @property
  def reason(self) -> str | None:
    return self._reason

  def update(self, started: bool, v_ego: float) -> None:
    """`started` = onroad. `v_ego` en m/s. Nunca lanza: corre en el bucle de la UI."""
    try:
      self._update(started, v_ego)
    except Exception:
      cloudlog.exception("[Orbit/UI] BenchGuard fallo")

  def _update(self, started: bool, v_ego: float) -> None:
    ahora = time.monotonic()

    # Flanco onroad -> offroad. Las dos claves del armado estan registradas como
    # CLEAR_ON_OFFROAD_TRANSITION y manager las borra en ese mismo flanco
    # (system/manager/manager.py, clear_all en la rama `not started and started_prev`),
    # asi que este desarme es redundante A PROPOSITO: lo que manager NO puede tocar es
    # el motivo en RAM de este proceso, y el desarme explicito hace que la pantalla y
    # el plano coincidan en el mismo tick en vez de depender del orden de dos procesos.
    if self._started_prev and not started:
      if self._reason is not None or bench_armed():
        cloudlog.warning("[Orbit/UI] paso a offroad: se desarma el banco")
        disarm_bench()
      if steer_local_activo():
        set_steer_local(False)
      self._reason = None
    self._started_prev = started

    if ahora >= self._next_privacy:
      self._next_privacy = ahora + self.PRIVACY_TICK_S
      reassert_privacy()

    if ahora < self._next_tick:
      return
    self._next_tick = ahora + self.TICK_S

    # --- autorizacion PRESENCIAL del selector de volante (independiente del banco)
    steer_mode_local = read_steer_mode()
    if steer_local_activo():
      if steer_mode_local not in (1, 2):
        # Se volvio a COMMA (o a COMMA+JETSON, que no necesita autorizacion): retirar.
        set_steer_local(False)
      elif steer_mode_local == 2 and v_ego > BENCH_DISARM_SPEED_MS:
        cloudlog.warning("[Orbit/UI] TEST MAX con el coche en movimiento: se retira la autorizacion local")
        set_steer_local(False)
        try:
          params().put(PARAM_STEER_MODE, 0, _BLOQUEANTE)
        except Exception:
          cloudlog.exception("[Orbit/UI] no se pudo volver a modo COMMA")

    if not bench_armed():
      self._reason = None
      return

    # Caducidad. El plano tambien la comprueba, pero si el hilo ORBIT esta caido nadie
    # dejaria el bool a False y la pantalla seguiria diciendo "ARMADO".
    if bench_remaining_s() <= 0.0:
      cloudlog.warning("[Orbit/UI] el armado de banco caduco")
      disarm_bench()
      self._reason = None
      return

    # motivo `bench` o desconocido -> fail-closed.
    if v_ego > BENCH_DISARM_SPEED_MS:
      cloudlog.warning("[Orbit/UI] el coche se mueve: se desarma el banco")
      disarm_bench()
      self._reason = None


def read_steer_mode() -> int:
  p = params()
  if p is None:
    return 0
  try:
    raw = p.get(PARAM_STEER_MODE)
    return int(raw) if raw is not None else 0
  except (UnknownKeyName, TypeError, ValueError):
    return 0


# ======================================================================== privacidad
def _read_json(path: str) -> dict:
  try:
    with open(path) as f:
      data = json.load(f)
    return data if isinstance(data, dict) else {}
  except (OSError, ValueError):
    return {}


def _write_json(path: str, data: dict) -> bool:
  """Escritura atomica (tempfile + os.replace)."""
  directory = os.path.dirname(path) or "."
  try:
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
      with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
      os.replace(tmp, path)
    except Exception:
      if os.path.exists(tmp):
        os.remove(tmp)
      raise
  except OSError:
    cloudlog.exception(f"[Orbit/UI] no se pudo escribir {path}")
    return False
  return True


def _jetson_config_path() -> str:
  for path in (os.path.join(BASEDIR, "orbit", "config_jetson.json"),
               "/data/openpilot/orbit/config_jetson.json"):
    if os.path.exists(path):
      return path
  return os.path.join(BASEDIR, "orbit", "config_jetson.json")


def privacy_muted() -> bool:
  return bool(_read_json(PRIVACY_STATE_FILE).get("muted", False))


def set_privacy_mute(on: bool) -> list[str]:
  """Interruptor maestro LOCAL de privacidad: corta posicion y camara.

  Funciona sin red y con el movil apagado porque no habla con nadie: escribe los
  mismos ajustes que ya gobiernan a los emisores en el propio dispositivo.

  Tres vias, y solo dos de ellas surten efecto en caliente:
    * POSICION -> los toggles de canal `gpsLocation*_toggle`. mqtt_envio_general los
      relee cada 5 s y excluye el canal si estan a False. EN CALIENTE.
    * CAMARA -> JETSON (ZMQ) -> `jetson_enabled` de orbit/config_jetson.json mas el
      flag JetsonConfigChanged, que camera_sender comprueba en cada vuelta de su bucle
      y que le hace rehacer (o soltar) el cliente ZMQ. EN CALIENTE.
    * CAMARA -> APP (MQTT) -> `image_sending_enabled` de /data/orbit_camera_config.json.
      camera_sender solo lee ese fichero en su __init__, asi que esta via se aplica al
      siguiente arranque. Es la unica pieza que falta y esta reportada.

  Se escribe ademas `OrbitPrivacyMute` como punto de verdad unico para cuando la clave
  este registrada; hoy lanza UnknownKeyName y se ignora a proposito.

  Devuelve la lista de vias que fallaron.
  """
  fallos: list[str] = []
  estado = _read_json(PRIVACY_STATE_FILE)
  restore = estado.get("restore") if isinstance(estado.get("restore"), dict) else {}

  p = params()

  # --- posicion
  if on:
    if not estado.get("muted", False):
      # Solo se toma la foto la PRIMERA vez que se silencia: silenciar dos veces
      # seguidas no puede guardar "estaba apagado" como valor a restaurar.
      restore = dict(restore)
      for key in POSITION_CHANNEL_PARAMS:
        try:
          # `activo`, no get_bool: un toggle SIN CONFIGURAR (None) esta encendido, pero
          # get_bool lo lee como False. Guardar ese False dejaba el GPS v1 apagado para
          # siempre al quitar el mute, mientras el v2 volvia a emitir.
          restore[key] = grupos.activo(p, key) if p is not None else True
        except Exception:
          restore[key] = True
  for key in POSITION_CHANNEL_PARAMS:
    try:
      if p is None:
        raise RuntimeError("sin Params")
      p.put_bool(key, False if on else bool(restore.get(key, True)), _BLOQUEANTE)
    except Exception:
      cloudlog.exception(f"[Orbit/UI] privacidad: fallo {key}")
      fallos.append(key)

  # --- camara -> Jetson (ZMQ), en caliente
  jetson_path = _jetson_config_path()
  cfg = _read_json(jetson_path)
  if cfg:
    if on and not estado.get("muted", False):
      restore = dict(restore)
      restore["jetson_enabled"] = bool(cfg.get("jetson_enabled", False))
    cfg["jetson_enabled"] = False if on else bool(restore.get("jetson_enabled", False))
    cfg["_version"] = str(_now_ms())
    if _write_json(jetson_path, cfg):
      try:
        if p is not None:
          p.put_bool(PARAM_JETSON_CONFIG_CHANGED, True, _BLOQUEANTE)
      except Exception:
        cloudlog.exception("[Orbit/UI] privacidad: fallo JetsonConfigChanged")
        fallos.append(PARAM_JETSON_CONFIG_CHANGED)
    else:
      fallos.append("config_jetson.json")

  # --- camara -> app (MQTT); se aplica al siguiente arranque de camera_sender
  cam = _read_json(CAMERA_CONFIG_FILE)
  if on and not estado.get("muted", False):
    restore = dict(restore)
    restore["image_sending_enabled"] = bool(cam.get("image_sending_enabled", False))
  cam["image_sending_enabled"] = False if on else bool(restore.get("image_sending_enabled", False))
  if not _write_json(CAMERA_CONFIG_FILE, cam):
    fallos.append("orbit_camera_config.json")

  # --- punto de verdad unico (pendiente de registrar la clave)
  try:
    if p is not None:
      p.put_bool(PARAM_PRIVACY_MUTE, bool(on), _BLOQUEANTE)
  except UnknownKeyName:
    pass  # esperado hoy: la clave no esta en common/params_keys.h
  except Exception:
    cloudlog.exception(f"[Orbit/UI] privacidad: fallo {PARAM_PRIVACY_MUTE}")

  estado = {"muted": bool(on), "restore": restore, "ts_ms": _now_ms()}
  if not _write_json(PRIVACY_STATE_FILE, estado):
    fallos.append(PRIVACY_STATE_FILE)

  return fallos


def reassert_privacy() -> None:
  """Reimpone el silencio si alguien lo deshizo por detras.

  El envio de camara a la app se puede reactivar por MQTT (`apply_config`), y los
  toggles de canal son ficheros que cualquiera con acceso al dispositivo puede tocar.
  Mientras los publicadores no lean el interruptor maestro, esta re-afirmacion
  periodica es lo que hace que "no emitir" signifique no emitir. Solo escribe cuando
  encuentra una divergencia real: en reposo son lecturas, no escrituras.
  """
  if not privacy_muted():
    return

  p = params()
  divergente = False

  for key in POSITION_CHANNEL_PARAMS:
    try:
      if p is not None and p.get_bool(key):
        divergente = True
    except Exception:
      pass

  if _read_json(CAMERA_CONFIG_FILE).get("image_sending_enabled", False):
    divergente = True
  if _read_json(_jetson_config_path()).get("jetson_enabled", False):
    divergente = True

  if divergente:
    cloudlog.warning("[Orbit/UI] privacidad: emision reactivada por detras, se vuelve a silenciar")
    set_privacy_mute(True)
