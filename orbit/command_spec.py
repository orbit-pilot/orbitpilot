#!/usr/bin/env python3
"""Tabla declarativa de verbos del mando remoto ORBIT v2.

Implementa las secciones 3 (contrato), 4 (modos y precondiciones) y 6 (catalogo
de verbos) de docs/superpowers/specs/2026-08-23-orbit-mando-remoto-v2-design.md.

COMMANDS es la UNICA fuente de verdad de que verbos existen, que modo exigen,
que gates hay que tener en verde y que argumentos se aceptan. El router, el
descriptor de capacidades (orbit/v2/caps/<dongle>) y el centro de ayuda de la app
se generan de aqui: si una precondicion vive en dos sitios, en un mes discrepan y
la app acaba pintando un boton que el coche no obedece -- que es el defecto mas
repetido del sistema actual (seccion 3.5 del diseno).

Este modulo es PURO: no importa cereal, ni Params, ni paho. Asi se puede importar
desde un test, desde un generador de documentacion o desde el backend sin arrastrar
el arbol de openpilot entero.
"""
import time
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag
from types import MappingProxyType

# Version del contrato que viaja en el campo `v` del sobre (seccion 3.2).
CONTRACT_VERSION = 2

# Namespace congelado de la seccion 3.1. No existe ningun topic sin <dongle>.
TOPIC_CMD = "orbit/v2/cmd/{}"
TOPIC_ACK = "orbit/v2/ack/{}"
TOPIC_CAPS = "orbit/v2/caps/{}"


def ahora_epoch_ms() -> int:
  """Instante de pared en epoch MILISEGUNDOS ENTEROS (seccion 3.2: no hay ISO-8601 en el cable).

  Se usa time.time_ns() y no time.time() por dos motivos: (1) time.time esta prohibido
  por la configuracion de ruff de este arbol, y (2) el redondeo a entero se hace sobre
  nanosegundos, sin pasar por un float que a 1.7e12 ms ya no tiene resolucion de
  microsegundo. Este reloj SOLO vale para sellar y comparar con el emisor; ningun plazo
  interno (TTL, deadman, hold) se mide con el -- para eso esta ahora_mono().
  """
  return time.time_ns() // 1_000_000


def ahora_mono() -> float:
  """Reloj MONOTONO en segundos. Todos los plazos internos se miden con este (seccion 3.2).

  CLOCK_MONOTONIC es de sistema, no de proceso: un deadline publicado por el hilo ORBIT
  en cereal lo puede comparar controlsd con su propio time.monotonic(). Ojo al integrar:
  NO es lo mismo que nanos_since_boot()/logMonoTime (CLOCK_BOOTTIME, que si cuenta el
  tiempo suspendido). Quien lea deadlineMono tiene que usar time.monotonic().
  """
  return time.monotonic()


class Gate(IntFlag):
  """Mascara de precondiciones (seccion 4.2).

  Los bits son 1 << ordinal del enum Gate de cereal/custom.capnp (struct
  OrbitCommandState). La correspondencia esta fijada por un test: si alguien anade un
  gate en el .capnp sin anadirlo aqui, o al reves, el test falla. Un desfase silencioso
  aqui significa publicar una mascara que el consumidor interpreta al reves, es decir
  ejecutar una maniobra creyendo que el gate estaba verde.
  """
  ENGAGED = 1 << 0
  LAT_ACTIVE = 1 << 1
  LONG_ACTIVE = 1 << 2
  SPEED_RANGE = 1 << 3
  DRIVER_IDLE = 1 << 4
  DRIVER_PRESENT = 1 << 5
  CALIBRATED = 1 << 6
  NOT_DEGRADED = 1 << 7
  LINK_FRESH = 1 << 8
  CLOCK_SYNCED = 1 << 9


# Nombre de cada gate en cereal/custom.capnp. Sirve al test de correspondencia y al
# descriptor de capacidades (la app pinta el motivo con el mismo nombre que el firmware).
GATE_CEREAL_NAMES = MappingProxyType({
  Gate.ENGAGED: "engaged",
  Gate.LAT_ACTIVE: "latActive",
  Gate.LONG_ACTIVE: "longActive",
  Gate.SPEED_RANGE: "speedRange",
  Gate.DRIVER_IDLE: "driverIdle",
  Gate.DRIVER_PRESENT: "driverPresent",
  Gate.CALIBRATED: "calibrated",
  Gate.NOT_DEGRADED: "notDegraded",
  Gate.LINK_FRESH: "linkFresh",
  Gate.CLOCK_SYNCED: "clockSynced",
})


def gate_reason(gate: Gate) -> str:
  """Codigo de motivo GATE_<nombre> de la seccion 3.3."""
  return f"GATE_{gate.name}"


def gates_list(mask: int) -> list[str]:
  """Nombres de los gates presentes en una mascara, en orden de bit (para ACK y caps)."""
  return [g.name for g in Gate if mask & g]


class Mode(IntEnum):
  """Los cuatro modos de la seccion 4.1.

  El valor entero es el mismo que el del param OrbitCommandMode y el mismo ordinal que
  el enum Mode de cereal/custom.capnp. OrbitCommandMode esta registrado como
  CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION: jamas PERSISTENT (el precedente
  de SteerTorqueMode persistente es exactamente por que manager.py tuvo que anadir un
  fail-safe de arranque).
  """
  OBSERVER = 0
  COPILOT = 1
  MANEUVER = 2
  BENCH = 3


MODE_CEREAL_NAMES = MappingProxyType({
  Mode.OBSERVER: "observer",
  Mode.COPILOT: "copilot",
  Mode.MANEUVER: "maneuver",
  Mode.BENCH: "bench",
})

# Nombre del modo TAL Y COMO VIAJA EN EL SOBRE ("mode": "maniobra", seccion 3.2). El
# enum de cereal va en ingles y el sobre en castellano, asi que hay dos tablas y no una:
# la de arriba es la de cereal y esta es la del cable. El backend escribe estas mismas
# cadenas y el test de contrato cruzado (orbit/test/test_contrato_cruzado.py) compara
# verbo a verbo que las dos partes exijan el mismo modo.
MODE_WIRE_NAMES = MappingProxyType({
  Mode.OBSERVER: "observador",
  Mode.COPILOT: "copiloto",
  Mode.MANEUVER: "maniobra",
  Mode.BENCH: "banco",
})

# El sobre de la seccion 3.2 lleva el modo en castellano ("mode": "maniobra"), el enum de
# cereal en ingles y el param como entero. Se aceptan las tres formas de ENTRADA y se
# normaliza a Mode: un contrato que solo acepta una de las tres se rompe con el primer
# cliente que no sea el nuestro, y el sintoma seria un NACK MODE incomprensible.
MODE_ALIASES = MappingProxyType({
  "observador": Mode.OBSERVER, "observer": Mode.OBSERVER, "0": Mode.OBSERVER,
  "copiloto": Mode.COPILOT, "copilot": Mode.COPILOT, "1": Mode.COPILOT,
  "maniobra": Mode.MANEUVER, "maneuver": Mode.MANEUVER, "2": Mode.MANEUVER,
  "banco": Mode.BENCH, "bench": Mode.BENCH, "3": Mode.BENCH,
})


def parse_mode(valor) -> Mode | None:
  """Normaliza el campo `mode` del sobre (o el param OrbitCommandMode) a Mode.

  Devuelve None si no se reconoce. bool NO es un modo aunque sea subclase de int:
  aceptar True como Mode.COPILOT es exactamente la clase de coercion silenciosa que
  hizo que bool('false') disparara un cambio de carril.
  """
  if isinstance(valor, bool):
    return None
  if isinstance(valor, int):
    try:
      return Mode(valor)
    except ValueError:
      return None
  if isinstance(valor, str):
    return MODE_ALIASES.get(valor.strip().lower())
  return None


@dataclass(frozen=True)
class ArgSpec:
  """Tipo y rango de UN argumento. La validacion es estricta: no hay coercion.

  `tipo` es bool, int, float o str. Un int vale donde se espera float (JSON manda 2 y no
  2.0), pero un bool NO vale como numero y una cadena NO vale como bool: la coercion de
  cadenas a bool es el bug que hoy dispara el cambio de carril con el payload 'false'.
  """
  tipo: type
  requerido: bool = True
  minimo: float | None = None
  maximo: float | None = None
  opciones: tuple = ()
  por_defecto: object = None
  unidad: str = ""

  def a_dict(self) -> dict:
    d: dict = {"type": self.tipo.__name__, "required": self.requerido}
    if self.minimo is not None:
      d["min"] = self.minimo
    if self.maximo is not None:
      d["max"] = self.maximo
    if self.opciones:
      d["choices"] = list(self.opciones)
    if self.por_defecto is not None:
      d["default"] = self.por_defecto
    if self.unidad:
      d["unit"] = self.unidad
    return d


@dataclass(frozen=True)
class CommandSpec:
  """Una fila de la tabla de la seccion 6."""
  verb: str
  mode_min: Mode
  gates: Gate = Gate(0)
  args_schema: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
  ttl_ms: int | None = None            # None = sin TTL (solo disarm_all, seccion 6)
  limits: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
  handler_name: str = ""
  # Documentacion que viaja al descriptor de capacidades y de ahi al centro de ayuda.
  descripcion: str = ""
  # Verbo que BAJA autoridad. Solo disarm_all hoy. Ver la excepcion explicita del router.
  baja_autoridad: bool = False
  # Verbo que ABRE UNA VENTANA DE ACTUADOR, es decir que deja algo moviendose hasta que
  # vence su TTL. Solo estos escriben `deadlineMono` en el plano de estado.
  #
  # POR QUE HACE FALTA DISTINGUIRLO. El plano de estado tiene UN solo deadline para todo
  # el subsistema. Si un verbo que no mueve nada lo escribiera, su TTL alargaria la
  # ventana del actuador que estuviera vivo: un healthcheck (TTL 30 s) llegado un segundo
  # despues de un lane_change (TTL 3 s) le regalaria 30 segundos de autoridad que nadie
  # pidio. Aumentar la autoridad de una orden por el efecto colateral de otra es
  # exactamente lo que la seccion 2 prohibe.
  arma_actuador: bool = True
  # Verbo cuyo RESULTADO REAL lo decide un consumidor en otro proceso, no el handler.
  #
  # El handler solo escribe un Param; quien decide de verdad es el consumidor: desire_helper
  # para lane_change (que puede rechazar un cambio de carril por nueve motivos propios:
  # velocidad, latActive, angulo muerto, pedales, cinturon...) y card para cruise_delta
  # (gates del ciclo, topes y su propio presupuesto por minuto). Anunciar `applied` en
  # cuanto el handler retorna es anunciar "hecho" cuando lo unico cierto es que hay un flag
  # en disco. Para estos verbos el router se queda esperando a OrbitCmdResult y cierra el
  # ACK con lo que diga el consumidor, o con NO_RESULT si no contesta a tiempo.
  cierra_consumidor: bool = False
  # Solo con cierra_consumidor: el router CONSERVA activeVerb/cmdId en el plano de estado
  # hasta el veredicto, porque el consumidor firma con el cmdId que lee de ahi.
  #
  # No es gratis: mientras el plano anuncia un verbo, allows() da BUSY a cualquier otro
  # (OrbitAuthority.busy_with_other), y controlsd lo reevalua en CADA ciclo de un
  # assisted_decel en marcha. Por eso solo lo pide lane_change, que lo suelta en cuanto
  # desire_helper anuncia la maniobra. cruise_delta firma sin id y el router lo
  # correlaciona por verbo: conservarlo cortaba en seco una deceleracion asistida en curso
  # con cada pulsacion de +/-.
  conserva_plano: bool = False

  @property
  def requiere_armado_banco(self) -> bool:
    """Modo banco = armado FISICO en la pantalla del comma (seccion 4.1).

    Se deriva del modo minimo y no de una lista aparte para que no puedan divergir:
    anadir un verbo de banco sin acordarse de anadirlo a la lista de armado seria un
    verbo fisico alcanzable por MQTT, que es justo lo que la seccion 1 declara imposible.
    """
    return self.mode_min >= Mode.BENCH

  def a_dict(self) -> dict:
    return {
      "verb": self.verb,
      "mode_min": int(self.mode_min),
      "mode": MODE_CEREAL_NAMES[self.mode_min],
      "gates": gates_list(int(self.gates)),
      "ttl_ms": self.ttl_ms,
      "args": {nombre: a.a_dict() for nombre, a in self.args_schema.items()},
      "limits": dict(self.limits),
      "bench_arm_required": self.requiere_armado_banco,
      "lowers_authority": self.baja_autoridad,
      "arms_actuator": self.arma_actuador,
      "description": self.descripcion,
    }


def _args(**kwargs) -> MappingProxyType:
  return MappingProxyType(dict(kwargs))


def _limits(**kwargs) -> MappingProxyType:
  return MappingProxyType(dict(kwargs))


# Gates comunes de maniobra (seccion 6, fila lane_change).
_GATES_MANIOBRA = (Gate.ENGAGED | Gate.LAT_ACTIVE | Gate.DRIVER_IDLE | Gate.DRIVER_PRESENT |
                   Gate.SPEED_RANGE | Gate.LINK_FRESH)


COMMANDS: MappingProxyType = MappingProxyType({spec.verb: spec for spec in (

  # ---------------------------------------------------------------- modo observador
  CommandSpec(
    verb="disarm_all",
    arma_actuador=False,
    mode_min=Mode.OBSERVER,
    gates=Gate(0),
    args_schema=_args(),
    ttl_ms=None,
    handler_name="handle_disarm_all",
    baja_autoridad=True,
    descripcion="Devuelve todos los actuadores remotos a neutro. Unico verbo sin modo, sin gate y sin TTL.",
  ),

  CommandSpec(
    verb="set_mode",
    arma_actuador=False,
    mode_min=Mode.OBSERVER,
    gates=Gate(0),
    # `target_mode` y no `mode`: el sobre YA lleva un campo `mode` con otro significado
    # (el modo minimo que el emisor cree exigido, seccion 3.2). Dos campos con el mismo
    # nombre y distinto significado en el mismo mensaje es la clase de ambiguedad que
    # produjo el bloqueante de nombres de argumentos.
    #
    # BANCO NO ESTA EN LAS OPCIONES, y esa ausencia es la regla: el modo banco solo se
    # alcanza con armado FISICO en la pantalla del comma (seccion 4.1). Si estuviera
    # aqui, un mensaje MQTT abriria el modo de los verbos fisicos, que es exactamente lo
    # que la seccion 1 declara imposible. Se rechaza con RANGE, no en el handler.
    args_schema=_args(target_mode=ArgSpec(tipo=str, opciones=("observador", "copiloto", "maniobra"))),
    # TTL corto: subir de modo es subir autoridad. Una orden de "ponte en maniobra" que
    # llega 30 segundos tarde describe una situacion que ya no existe.
    ttl_ms=5_000,
    limits=_limits(
      # CADUCIDAD DEL MODO, en segundos. No es el TTL del sobre (que solo dice cuanto
      # vale la orden en vuelo): es cuanto dura el modo una vez concedido. Sin esto,
      # "maniobra" se queda encendido para siempre y el gate de modo deja de significar
      # nada. Lo tiene que aplicar el plano de estado; el router solo lo declara.
      expira_s={"copiloto": 900.0, "maniobra": 120.0},
      banco_inalcanzable=True,
      # Subir de modo exige un gesto explicito del usuario en la app (§10.2: nada
      # critico a un solo toque). Eso se hace cumplir donde hay sesion y donde hay
      # dedo -- backend y app --; aqui se DECLARA para que el descriptor de
      # capacidades y el centro de ayuda digan lo mismo que hace la interfaz.
      exige_gesto_explicito=True,
    ),
    handler_name="handle_set_mode",
    descripcion="Cambia el modo de mando: observador, copiloto o maniobra. El modo banco solo se alcanza con armado fisico en la pantalla del comma.",
  ),

  # ------------------------------------------------------------------ modo copiloto
  CommandSpec(
    verb="healthcheck",
    arma_actuador=False,
    mode_min=Mode.COPILOT,
    ttl_ms=30_000,
    handler_name="handle_healthcheck",
    descripcion="Pide un informe de diagnostico del dispositivo.",
  ),
  CommandSpec(
    verb="location_now",
    arma_actuador=False,
    mode_min=Mode.COPILOT,
    ttl_ms=10_000,
    handler_name="handle_location_now",
    descripcion="Publica una unica posicion. El GPS continuo esta apagado por defecto.",
  ),
  CommandSpec(
    verb="cruise_delta",
    mode_min=Mode.COPILOT,
    gates=Gate.ENGAGED | Gate.LONG_ACTIVE,
    args_schema=_args(delta_kph=ArgSpec(tipo=float, minimo=-5.0, maximo=5.0, unidad="kph")),
    ttl_ms=2_000,
    # +-20 km/h por minuto: el tope por orden no sirve de nada si se pueden encadenar
    # veinte ordenes de +5 en dos segundos.
    limits=_limits(rate={"campo": "delta_kph", "presupuesto": 20.0, "ventana_s": 60.0}),
    handler_name="handle_cruise_delta",
    descripcion="Sube o baja la velocidad de crucero. Unifica los cuatro caminos actuales (speed_up, speed_down, control, speed).",
    # El handler solo deja orbit_speed_increase/decrease en disco; quien mueve (o no) la
    # consigna es el consumidor de card (orbit_speed_ultra_simple). Medido en el coche: 17
    # de 78 `applied` no movieron la consigna, porque el ACK salia al escribir el flag y el
    # rechazo del consumidor solo llegaba al log. El ACK lo cierra su veredicto, que va sin
    # id: sin conserva_plano, a proposito (ver el campo).
    cierra_consumidor=True,
  ),
  CommandSpec(
    verb="cruise_button",
    arma_actuador=False,
    mode_min=Mode.COPILOT,
    gates=Gate.ENGAGED,
    # SOLO 'cancel'. 'resume' y 'set' SUBEN autoridad (reenganchan o fijan consigna) y
    # hoy no tienen consumidor en este arbol: declararlos seria pintar en la app dos
    # botones que el coche rechaza con UNSUPPORTED_VERB. Se anaden cuando exista el
    # camino, no antes.
    args_schema=_args(button=ArgSpec(tipo=str, opciones=("cancel",))),
    ttl_ms=2_000,
    handler_name="handle_cruise_button",
    descripcion=("Cancelar crucero. Es el boton de panico: BAJA autoridad (desengancha) en vez de "
                 + "aumentarla, y esta implementado en las 12 marcas, a diferencia de una frenada remota."),
  ),
  CommandSpec(
    verb="follow_distance",
    arma_actuador=False,
    mode_min=Mode.COPILOT,
    gates=Gate.ENGAGED,
    args_schema=_args(personality=ArgSpec(tipo=int, minimo=0, maximo=2)),
    ttl_ms=5_000,
    handler_name="handle_follow_distance",
    descripcion="Distancia de seguimiento (0 agresiva, 1 estandar, 2 relajada).",
  ),
  CommandSpec(
    verb="mads",
    arma_actuador=False,
    mode_min=Mode.COPILOT,
    args_schema=_args(enabled=ArgSpec(tipo=bool)),
    ttl_ms=10_000,
    handler_name="handle_toggle_mads",
    descripcion="Modo MADS (direccion asistida independiente del crucero).",
  ),
  CommandSpec(
    verb="experimental",
    arma_actuador=False,
    mode_min=Mode.COPILOT,
    args_schema=_args(enabled=ArgSpec(tipo=bool)),
    ttl_ms=10_000,
    handler_name="handle_toggle_experimental",
    descripcion="Modo experimental (control longitudinal por el modelo).",
  ),
  CommandSpec(
    verb="dec",
    arma_actuador=False,
    mode_min=Mode.COPILOT,
    args_schema=_args(enabled=ArgSpec(tipo=bool)),
    ttl_ms=10_000,
    handler_name="handle_toggle_dec",
    descripcion="Dynamic Experimental Control.",
  ),
  CommandSpec(
    verb="nnlc",
    arma_actuador=False,
    mode_min=Mode.COPILOT,
    args_schema=_args(enabled=ArgSpec(tipo=bool)),
    ttl_ms=10_000,
    handler_name="handle_toggle_nnlc",
    descripcion="Control lateral por red neuronal.",
  ),
  CommandSpec(
    verb="openpilot_enable",
    arma_actuador=False,
    mode_min=Mode.COPILOT,
    # standstill se comprueba en el handler contra carState.standstill; no es un bit del
    # enum Gate de cereal y no se inventa uno aqui para no desalinear la mascara.
    args_schema=_args(enabled=ArgSpec(tipo=bool)),
    ttl_ms=10_000,
    limits=_limits(requiere_standstill=True),
    handler_name="handle_openpilot_enable",
    descripcion="Interruptor de openpilot del dueno. Solo con el coche detenido.",
  ),
  CommandSpec(
    verb="device_admin",
    arma_actuador=False,
    mode_min=Mode.COPILOT,
    args_schema=_args(action=ArgSpec(tipo=str, opciones=("reboot", "poweroff", "restart_services"))),
    ttl_ms=30_000,
    limits=_limits(offroad_para=("reboot", "poweroff")),
    handler_name="handle_device_admin",
    descripcion="Administracion del dispositivo. reboot y poweroff solo con el coche apagado.",
  ),

  # ------------------------------------------------------------------ modo maniobra
  CommandSpec(
    verb="lane_change",
    mode_min=Mode.MANEUVER,
    gates=_GATES_MANIOBRA,
    args_schema=_args(direction=ArgSpec(tipo=str, opciones=("left", "right"))),
    ttl_ms=3_000,
    limits=_limits(v_min_kph=40.0, v_max_kph=130.0, encadenable=False),
    handler_name="handle_lane_change",
    descripcion="Un unico cambio de carril. No se encadena: una maniobra por orden.",
    # El handler solo arma ForceLaneChange*; quien acepta o rechaza es desire_helper, que
    # aplica sus propios gates en el ciclo en que actua. El ACK lo cierra su resultado,
    # firmado con el cmdId que lee del plano.
    cierra_consumidor=True,
    conserva_plano=True,
  ),
  CommandSpec(
    verb="assisted_decel",
    mode_min=Mode.MANEUVER,
    gates=Gate.ENGAGED | Gate.LONG_ACTIVE | Gate.DRIVER_IDLE,
    # NO HAY ARGUMENTO hold_ms, y su ausencia es deliberada. Lo declaraban el firmware y
    # el backend, pero el ejecutor (MQTTComandos._h_assisted_decel) escribe solo
    # brutebreak_intensidad y brutebreak_active: el hold real es la constante fija
    # ORBIT_DECEL_HOLD_MAX_S = 1.5 s de selfdrive/controls/controlsd.py, comprobada al
    # escribir esto. Un argumento del contrato que nadie lee es una promesa falsa: la app
    # ensenaria un control de duracion que no cambia nada.
    #
    # Quien quiera una frenada MAS CORTA que ese tope acorta el `ttl_ms` DEL SOBRE, que
    # si se respeta de punta a punta: el router lo convierte en `deadline_mono`, el plano
    # lo publica como deadlineMono y controlsd exige esa ventana abierta en cada ciclo de
    # 100 Hz (OrbitCommandLink.allows -> window_open). Alargarla no se puede, y es lo
    # correcto: el tope lo pone el coche.
    args_schema=_args(
      accel=ArgSpec(tipo=float, minimo=-2.5, maximo=-1.0, unidad="m/s2"),
    ),
    ttl_ms=1_500,
    # Rango acotado y hold corto: sustituye a brutebreak, que aceptaba [-10,-1] sin modo
    # ni TTL. Una frenada que llega con segundos de latencia, ordenada por alguien que no
    # ve lo que ve el coche, no evita un peligro: lo crea (decision por defecto, seccion 0).
    limits=_limits(hold_max_ms=1500, jerk_max=2.5),
    handler_name="handle_assisted_decel",
    descripcion="Deceleracion asistida acotada, con rampa de jerk y cancelacion por el conductor.",
  ),

  # --------------------------------------------------------------------- modo banco
  # Declarados para que la app sepa que existen y por que estan bloqueados, pero su
  # handler rechaza mientras no haya armado FISICO en la pantalla del comma. Nunca son
  # alcanzables solo por MQTT (seccion 1, control compensatorio 2).
  CommandSpec(
    verb="torque_mode",
    mode_min=Mode.BENCH,
    gates=Gate.ENGAGED,
    args_schema=_args(
      # 1..3, no 0..3. El 0 (volver al modelo del comma) BAJA autoridad y se atiende como
      # disarm_all -- lo traduce asi el puente v1 (_v1_steer_torque_mode) --, asi que
      # aceptarlo aqui era declarar dos caminos para lo mismo, y uno de ellos exigiendo
      # armado fisico de banco: apagar no puede depender de que el banco siga armado.
      mode=ArgSpec(tipo=int, minimo=1, maximo=3),
      # Solo tiene sentido con mode=3 (COMMA+JETSON): dice DONDE se aplica el esquive,
      # sumando offset a la curvatura deseada o pisando el par. Va en el esquema y no
      # como campo suelto porque validar_args RECHAZA los argumentos desconocidos: sin
      # declararlo, un sobre v2 con apply_target seria TYPE, y sin el un mode=3 no se
      # puede ejecutar (el handler no sabria donde aplicarlo).
      apply_target=ArgSpec(tipo=str, requerido=False, opciones=("curvature", "torque")),
    ),
    # 15 s de VENTANA DE ACTUADOR, no 300 s.
    #
    # Los 300 s de la seccion 4.1 son la caducidad del ARMADO FISICO de banco
    # (OrbitBenchExpiry), que es otra cosa: dice cuanto tiempo se admite operar el banco
    # despues de tocar la pantalla del comma. El TTL de ESTE verbo es el deadman del
    # actuador (seccion 5, "centenas de ms"), y con un solo deadline en el plano de
    # estado un torque_mode de 300 s dejaba una ventana de cinco minutos abierta para
    # cualquier consumidor que solo mire deadlineMono. Cinco minutos de autoridad remota
    # sin que nadie renueve nada no es un deadman: es un actuador pegado.
    #
    # 15 s es holgado para un operador de banco (renovar la orden es un mensaje, y el
    # armado fisico sigue vivo sus 300 s) y corto para un enlace caido.
    ttl_ms=15_000,
    limits=_limits(clamp=(-1.0, 1.0), dead_zone=0.05,
                   # mode 0 = volver al modelo del comma: BAJA autoridad y por eso no
                   # llega aqui, se atiende como disarm_all (seccion 2).
                   apply_target_obligatorio_en=(3,)),
    handler_name="handle_torque_mode",
    descripcion="Modo de torque del volante (1 Jetson, 2 TEST MAX, 3 COMMA+JETSON). Solo banco, con armado fisico. Volver a 0 es disarm_all.",
  ),
  CommandSpec(
    verb="steering_pulse",
    mode_min=Mode.BENCH,
    gates=Gate.ENGAGED | Gate.SPEED_RANGE,
    args_schema=_args(
      torque=ArgSpec(tipo=float, minimo=-1.0, maximo=1.0),
      duration_ms=ArgSpec(tipo=int, requerido=False, minimo=0, maximo=500, por_defecto=200, unidad="ms"),
    ),
    ttl_ms=500,
    limits=_limits(v_min_kph=0.0, v_max_kph=20.0, clamp=(-1.0, 1.0)),
    handler_name="handle_steering_pulse",
    descripcion="Pulso de direccion antes del limitador. Solo banco, con armado fisico y a baja velocidad.",
  ),
  CommandSpec(
    verb="physical_control",
    mode_min=Mode.BENCH,
    gates=Gate.SPEED_RANGE,
    args_schema=_args(
      axis=ArgSpec(tipo=str, opciones=("steer", "accel")),
      value=ArgSpec(tipo=float, minimo=-1.0, maximo=1.0),
    ),
    ttl_ms=200,
    limits=_limits(v_min_kph=0.0, v_max_kph=5.0, clamp=(-1.0, 1.0), solo_red_local=True),
    handler_name="handle_physical_control",
    descripcion="Control fisico directo (joystickd). Solo banco, red local, coche parado. Nunca sobre LTE ni en la app de usuario.",
  ),
)})


# Verbos que el diseno nombra pero que este firmware NO ofrece, con el motivo. Viaja en
# el descriptor de capacidades: la app no pinta el control y el centro de ayuda puede
# decir POR QUE, en vez de dejar un boton que no hace nada (seccion 3.5).
UNSUPPORTED: MappingProxyType = MappingProxyType({
  # Seccion 11: solo Ford, y solo acoplado a una maniobra real. Un intermitente remoto
  # sin maniobra es una senal falsa a los demas conductores.
  "blinker_standalone": "policy",
  # El verbo `blinker` esta en el catalogo del backend (declarado NO base, es decir
  # inalcanzable mientras el coche no lo publique en caps). Aparece aqui con su motivo
  # para que el rechazo del backend diga POR QUE y no un generico "no esta en las
  # capacidades declaradas": este firmware no tiene ejecutor de intermitentes.
  "blinker": "policy",
  # Seccion 6: "o se implementa la maquina de estados o se retira el HUD que miente".
  # Hasta que exista esa maquina de estados, aqui no hay verbo.
  "overtake": "not_implemented",
  # Seccion 6: la cruceta y los verbos v1 asociados se retiran.
  "forward": "removed_v1", "break": "removed_v1", "tright": "removed_v1", "tleft": "removed_v1",
  "speed": "removed_v1", "intervalos": "removed_v1", "brutebreak": "replaced_by_assisted_decel",
  # Seccion 15: exigiria tocar el firmware de panda.
  "dtc_read": "panda_signed",
})


# Verbos que solo tienen sentido en una marca concreta. La comprobacion real la hace el
# router contra carParams.brand y responde UNSUPPORTED_PLATFORM (seccion 3.3).
PLATFORM_ONLY: MappingProxyType = MappingProxyType({})


def get_spec(verb: str) -> CommandSpec | None:
  if not isinstance(verb, str):
    return None
  return COMMANDS.get(verb)


def spec_table_dict() -> dict:
  """Serializa la tabla entera. Es lo que consume el generador del centro de ayuda."""
  return {verb: spec.a_dict() for verb, spec in COMMANDS.items()}


# Nombre canonico del tipo de un argumento. `str` con opciones cerradas se llama "enum"
# porque es como lo declara el catalogo del backend: el test de contrato cruzado compara
# ESTAS cadenas, asi que la traduccion vive aqui y no repetida en cada test.
_TIPO_CANONICO = MappingProxyType({bool: "bool", int: "int", float: "float", str: "str"})


def firma_args(spec: CommandSpec) -> dict:
  """Firma normalizada de los argumentos de UN verbo: nombre -> tipo, rango y opciones.

  Es la unidad de comparacion del contrato. Se creo porque backend, app y firmware
  divergieron en los NOMBRES de los argumentos ('dir' vs 'direction', 'delta' vs
  'delta_kph', 'value' vs 'torque') y el sintoma fue que el 100 % de esos envios moria
  con TYPE "argumentos desconocidos" -- un fallo total que ningun test de una sola parte
  podia ver, porque cada lado era coherente consigo mismo.
  """
  fuera: dict = {}
  for nombre, a in spec.args_schema.items():
    tipo = _TIPO_CANONICO[a.tipo]
    if tipo == "str" and a.opciones:
      tipo = "enum"
    fuera[nombre] = {
      "type": tipo,
      "required": bool(a.requerido),
      "min": a.minimo,
      "max": a.maximo,
      "choices": list(a.opciones) if a.opciones else None,
      "default": a.por_defecto,
    }
  return fuera


def firma_contrato() -> dict:
  """Contrato completo en forma comparable: verbo -> modo del cable, TTL y firma de args."""
  return {
    verb: {
      "mode": MODE_WIRE_NAMES[spec.mode_min],
      "ttl_ms": spec.ttl_ms,
      "args": firma_args(spec),
    }
    for verb, spec in COMMANDS.items()
  }


def capabilities_payload(brand: str = "", platform: str = "", fw: str = "",
                         verbos_disponibles=None, extra: dict | None = None) -> dict:
  """Descriptor de capacidades para orbit/v2/caps/<dongle> (retenido, qos 1, seccion 3.5).

  `verbos_disponibles` es el conjunto de verbos con handler REGISTRADO. Un verbo de la
  tabla sin handler se publica en `unsupported` con motivo "no_handler": declararlo como
  soportado seria volver al boton que no hace nada.
  """
  soportados: dict = {}
  no_soportados = dict(UNSUPPORTED)
  for verb, spec in COMMANDS.items():
    if verbos_disponibles is not None and verb not in verbos_disponibles:
      no_soportados[verb] = "no_handler"
      continue
    soportados[verb] = spec.a_dict()
  payload = {
    "v": CONTRACT_VERSION,
    "schema_version": CONTRACT_VERSION,
    "ts_ms": ahora_epoch_ms(),
    "brand": brand,
    "platform": platform,
    "fw": fw,
    "modes": {MODE_CEREAL_NAMES[m]: int(m) for m in Mode},
    "gates": {g.name: GATE_CEREAL_NAMES[g] for g in Gate},
    "verbs": soportados,
    "unsupported": no_soportados,
  }
  if extra:
    payload.update(extra)
  return payload


# --------------------------------------------------------------------------- validacion

def validar_args(spec: CommandSpec, args) -> tuple[str | None, str, dict]:
  """Valida y normaliza los argumentos de un verbo.

  Devuelve (reason, detail, args_normalizados). reason es None si todo esta bien, o el
  codigo TYPE / RANGE de la seccion 3.3.
  """
  if args is None:
    args = {}
  if not isinstance(args, dict):
    return "TYPE", "args no es un objeto", {}

  desconocidos = [k for k in args if k not in spec.args_schema]
  if desconocidos:
    # Un argumento que no esta en el esquema se rechaza en vez de ignorarse: si el emisor
    # cree que manda 'dir' y el coche espera 'direction', ignorarlo ejecutaria la maniobra
    # con el valor por defecto en vez de decir que el sobre esta mal.
    return "TYPE", f"argumentos desconocidos: {sorted(desconocidos)}", {}

  fuera: dict = {}
  for nombre, aspec in spec.args_schema.items():
    if nombre not in args:
      if aspec.requerido:
        return "TYPE", f"falta el argumento obligatorio '{nombre}'", {}
      if aspec.por_defecto is not None:
        fuera[nombre] = aspec.por_defecto
      continue

    valor = args[nombre]
    esperado = aspec.tipo

    if esperado is bool:
      # Estricto a proposito: 'false', 0 y 1 NO son booleanos. El bug vivo hoy es que
      # bool('false') es True y dispara un cambio de carril.
      if not isinstance(valor, bool):
        return "TYPE", f"'{nombre}' debe ser booleano nativo, llego {type(valor).__name__}", {}
    elif esperado is int:
      if isinstance(valor, bool) or not isinstance(valor, int):
        return "TYPE", f"'{nombre}' debe ser entero, llego {type(valor).__name__}", {}
    elif esperado is float:
      # int vale donde se espera float (JSON manda 2, no 2.0). bool no.
      if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return "TYPE", f"'{nombre}' debe ser numero, llego {type(valor).__name__}", {}
      valor = float(valor)
    elif esperado is str:
      if not isinstance(valor, str):
        return "TYPE", f"'{nombre}' debe ser cadena, llego {type(valor).__name__}", {}
    else:
      return "INTERNAL", f"tipo no soportado en el esquema de '{nombre}'", {}

    if aspec.opciones and valor not in aspec.opciones:
      return "RANGE", f"'{nombre}'={valor!r} fuera de {list(aspec.opciones)}", {}
    if aspec.minimo is not None and valor < aspec.minimo:
      return "RANGE", f"'{nombre}'={valor} por debajo de {aspec.minimo}", {}
    if aspec.maximo is not None and valor > aspec.maximo:
      return "RANGE", f"'{nombre}'={valor} por encima de {aspec.maximo}", {}

    fuera[nombre] = valor

  return None, "", fuera


# ------------------------------------------------------------------- ACK (seccion 3.3)

class Phase:
  """Las cinco fases del ACK, mas los tres finales de error.

  Las cadenas son EXACTAMENTE los nombres del enum AckPhase de cereal/custom.capnp
  (struct OrbitCommandState): asi el publicador de estado puede asignarlas tal cual sin
  una tabla de traduccion que se desincronice. Un test lo fija.

      received ──> accepted ──> executing ──> applied
           │            │                        │
           └─> rejected └─> expired              └─> failed | superseded
  """
  NONE = "none"
  RECEIVED = "received"
  ACCEPTED = "accepted"
  REJECTED = "rejected"
  EXECUTING = "executing"
  APPLIED = "applied"
  FAILED = "failed"
  EXPIRED = "expired"
  SUPERSEDED = "superseded"


PHASES = (Phase.NONE, Phase.RECEIVED, Phase.ACCEPTED, Phase.REJECTED, Phase.EXECUTING,
          Phase.APPLIED, Phase.FAILED, Phase.EXPIRED, Phase.SUPERSEDED)

# Codigos de motivo cerrados de la seccion 3.3. Los GATE_<nombre> se generan con
# gate_reason() y no se listan aqui uno a uno.
REASONS = ("OK", "TYPE", "RANGE", "MODE", "DUPLICATE", "EXPIRED", "CLOCK",
           "UNSUPPORTED_PLATFORM", "UNSUPPORTED_VERB", "BUSY", "SUPERSEDED", "LINK", "INTERNAL")


def reason_valido(reason: str) -> bool:
  return reason in REASONS or (isinstance(reason, str) and reason.startswith("GATE_"))
