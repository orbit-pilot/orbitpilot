#!/usr/bin/env python3
"""Descriptor unico de la telemetria ORBIT v1 sobre el namespace v2.

Implementa la seccion 7 (telemetria v1) y la 3.1 (namespace) de
docs/superpowers/specs/2026-08-23-orbit-mando-remoto-v2-design.md.

POR QUE EXISTE ESTE FICHERO
---------------------------
Hoy `orbit/canales.json` declara 8 canales cereal con `keys_importantes` VACIO en los
ocho, y `MQTTEnvioGeneral.enviar_datos_importantes()` interpreta la lista vacia como
"manda el `to_dict()` entero". El mecanismo de lista blanca existe desde el primer dia y
no se ha usado nunca. Estimacion con el mismo camino de serializacion del firmware
(`to_dict` -> saneado -> `json.dumps`): 4550 B por ciclo a 1 Hz = 16,4 MB/h, mas 1,74
MB/h del heartbeat que republica `carState` ENTERO cada 3 s solo para decir "sigo vivo".
Y es cota INFERIOR: los campos con valor por defecto ocupan menos que los reales.

Aqui la lista blanca es POR CAMPO, con tipo, unidad, redondeo y valor por defecto
declarados. El descriptor es la fuente de verdad: de el sale lo que publica el coche Y lo
que el backend y la app esperan recibir (`descriptor_backend()`), igual que
`command_spec.py` genera el descriptor de capacidades. Si el contrato vive en dos sitios,
en un mes discrepan -- que es exactamente el defecto que ya costo el 100 % de los envios
de tres verbos de mando.

MODULO PURO: no importa cereal, ni Params, ni paho. Se puede importar desde un test, desde
un generador de documentacion o desde el backend sin arrastrar el arbol de openpilot. Lee
los mensajes por `getattr` sobre rutas punteadas, asi que en los tests basta un objeto
cualquiera con los mismos atributos.

REGLAS TRANSVERSALES (seccion 7)
--------------------------------
- `dongle_id` NO viaja en el payload: va en el topic. El backend toma la identidad del
  topic y descarta el payload que discrepe, asi que repetirlo son bytes que ademas
  invitan a que alguien se fie del campo equivocado. Por el mismo motivo tampoco viaja
  el nombre del canal.
- Redondeo EXPLICITO por campo. Un `Float32` de velocidad en JSON son 18 caracteres de
  ruido decimal ("12.340000152587891") para una senal que se lee con una cifra decimal.
- `isfinite` falso -> `null`. `json.dumps` emite por defecto los literales NaN/Infinity,
  que no son JSON valido (RFC 8259): un solo NaN revienta el parser del backend y se
  pierde el mensaje ENTERO, de forma intermitente y sin rastro porque el publish sale
  con rc=0.
- Omision por DEFECTO DECLARADO, no delta contra el mensaje anterior. Un campo cuyo valor
  coincide con su `defecto` no se emite y el consumidor lo reconstruye con el defecto que
  viene en `descriptor_backend()`. Es estatico a proposito: la telemetria va en QoS 0, y
  un delta contra el mensaje anterior deja al consumidor con un estado equivocado en
  cuanto se pierde un paquete -- y no se entera.
"""
import hashlib
import json
import math
import secrets
import time
from dataclasses import dataclass, field
from types import MappingProxyType

# Version del contrato de namespace (la del sobre de mando, seccion 3.2).
CONTRACT_VERSION = 2
# Version del ESQUEMA de telemetria. Es independiente del namespace: el sobre puede
# seguir siendo v2 mientras el contenido de los canales evoluciona. Viaja en cada
# mensaje ("sv") porque un consumidor que no sabe que esquema esta leyendo acaba
# interpretando un campo nuevo como el viejo.
SCHEMA_TELEMETRIA = 1

# Namespace congelado de la seccion 3.1. No existe ningun topic sin <dongle> en la ruta.
TOPIC_TEL = "orbit/v2/tel/{}/{}"


def topic_telemetria(dongle: str, canal: str) -> str:
  return TOPIC_TEL.format(dongle, canal)


def ahora_epoch_ms() -> int:
  """Instante de PARED en epoch milisegundos ENTEROS (seccion 3.2: no hay ISO-8601 en el cable).

  `time.time` esta prohibida por la configuracion de ruff de este arbol; ademas el
  redondeo se hace sobre nanosegundos y no sobre un float que a 1,7e12 ms ya perdio
  resolucion. Ningun plazo interno de este modulo se mide con este reloj: para eso van
  los `ahora_mono` que recibe `MotorTelemetria`.
  """
  return time.time_ns() // 1_000_000


# --------------------------------------------------------------------------- saneado

def sanea_no_finitos(obj):
  """Sustituye recursivamente los float no finitos (NaN/inf) por None.

  Vive aqui, en el modulo puro, porque lo necesitan por igual el emisor v2, el puente v1
  y los tests. `mqtt_envio_general` lo reexporta para no tener dos implementaciones de la
  misma regla (que es como se acaba saneando un camino y el otro no).
  """
  if isinstance(obj, float):
    return obj if math.isfinite(obj) else None
  if isinstance(obj, dict):
    return {k: sanea_no_finitos(v) for k, v in obj.items()}
  if isinstance(obj, (list, tuple)):
    return [sanea_no_finitos(v) for v in obj]
  return obj


# Cifras significativas con las que un IEEE-754 binary32 (el Float32 de cereal) va y
# vuelve sin cambiar de valor. Redondear a MENOS pierde datos; a mas solo escribe ruido.
SIG_FLOAT32 = 9


def compacta_floats(obj, sig: int = SIG_FLOAT32):
  """Recorta los floats a `sig` cifras significativas, recursivamente.

  Casi todo lo que viaja en la telemetria es Float32, pero json.dumps escribe la expansion
  DOBLE del valor: `steeringAngleDeg` sale como "-3.4567890167236328" (20 caracteres) para
  una senal cuya precision real son 7 cifras. A 9 cifras significativas el valor
  round-trips exacto -- no se pierde nada que el Float32 tuviera -- y el JSON encoge cerca
  de la mitad. Es el punto (4) del apartado de telemetria de la auditoria.

  El numero de decimales nunca baja de 0: con `sig` cifras y un valor grande, `round`
  empezaria a redondear la parte ENTERA (un epoch en ms perderia los ultimos digitos).
  """
  if isinstance(obj, float):
    if not math.isfinite(obj) or obj == 0.0:
      return obj
    dec = max(0, sig - int(math.floor(math.log10(abs(obj)))) - 1)
    return round(obj, dec)
  if isinstance(obj, dict):
    return {k: compacta_floats(v, sig) for k, v in obj.items()}
  if isinstance(obj, (list, tuple)):
    return [compacta_floats(v, sig) for v in obj]
  return obj


# --------------------------------------------------------------------------- perfiles

PERFIL_AHORRO = "ahorro"
PERFIL_NORMAL = "normal"
PERFIL_DIAG = "diagnostico"
PERFILES = (PERFIL_AHORRO, PERFIL_NORMAL, PERFIL_DIAG)
_NIVEL = MappingProxyType({PERFIL_AHORRO: 0, PERFIL_NORMAL: 1, PERFIL_DIAG: 2})

# El perfil DIAGNOSTICO se apaga SOLO, en el propio dispositivo, a los 15 minutos
# (seccion 7). No depende de que el backend o la app se acuerden de bajarlo: un
# diagnostico olvidado encendido es la forma mas facil de gastarle los datos al usuario.
DIAG_TTL_S = 15 * 60.0


def perfil_valido(nombre) -> str | None:
  return nombre if isinstance(nombre, str) and nombre in _NIVEL else None


def nivel(perfil: str) -> int:
  return _NIVEL.get(perfil, _NIVEL[PERFIL_NORMAL])


def degrada_por_red(perfil: str, metered: bool) -> str:
  """Degradacion automatica por `deviceState.networkMetered` (seccion 7).

  Con la red marcada como de pago se baja SIEMPRE a AHORRO, tambien desde DIAGNOSTICO:
  el caso que importa es justo el del que enciende el diagnostico para mirar un problema
  y se olvida con el coche en itinerancia.
  """
  if metered and nivel(perfil) > _NIVEL[PERFIL_AHORRO]:
    return PERFIL_AHORRO
  return perfil


# --------------------------------------------------------------------------- descriptor

_SIN_DEFECTO = object()
# Sentinela de "no hay valor": lo devuelve `coacciona` cuando la senal no esta presente en
# el mensaje. NO es lo mismo que None: None es el `null` explicito de un float no finito
# (seccion 7), que si viaja. Un campo AUSENTE se omite y el consumidor lo reconstruye con
# su defecto declarado; un campo a null dice "esta senal existe y ahora mismo no vale".
_AUSENTE = object()

# Tope de longitud de los campos de texto libre (nombre de via, textos de alerta). Sin
# tope, un roadName largo o un alertText2 con la lista de procesos caidos hace que un
# canal "pequeno" pese mas que todo el resto junto.
MAX_TEXTO = 64


@dataclass(frozen=True)
class Campo:
  """Una senal de la lista blanca.

  `origen` es una ruta punteada dentro de la fuente (`cruiseState.speed`), o "" si el
  valor lo produce una funcion de CALCULOS. `factor` se aplica ANTES del redondeo, para
  que `dec` cuente decimales de la unidad publicada y no de la interna.
  """
  nombre: str
  fuente: str
  origen: str
  tipo: str                     # float | int | bool | enum | str | list[str]
  unidad: str = ""
  dec: int = 0                  # decimales del redondeo (solo tipo float)
  factor: float = 1.0
  agg: str = ""                 # "", max, min, mean, len
  defecto: object = _SIN_DEFECTO
  perfil: str = PERFIL_NORMAL   # perfil MINIMO en el que se emite el campo
  desc: str = ""

  def a_dict(self) -> dict:
    d = {
      "nombre": self.nombre,
      "tipo": self.tipo,
      "unidad": self.unidad,
      "origen": f"{self.fuente}.{self.origen}" if self.origen else f"{self.fuente}:calculado",
      "perfil_min": self.perfil,
      "desc": self.desc,
    }
    if self.tipo == "float":
      d["dec"] = self.dec
    if self.agg:
      d["agg"] = self.agg
    if self.defecto is not _SIN_DEFECTO:
      # El consumidor RECONSTRUYE con esto los campos que no llegan (ver la nota de
      # omision por defecto declarado en la cabecera del modulo).
      d["defecto"] = self.defecto
      d["omitido_si_defecto"] = True
    return d


@dataclass(frozen=True)
class Canal:
  """Un canal de la tabla de la seccion 7.

  `disparo`:
    periodico   -- cadencia fija por perfil
    cambio      -- se publica cuando el payload cambia, mas keepalive
    evento      -- flancos de onroadEvents/onroadEventsSP (payload propio, no lista blanca)
    adaptativo  -- decimacion por curvatura y calidad de fix (canal pos)
    sesion      -- start/end de viaje (canal trip)
  `periodo_s` es, en los canales de cambio, el periodo MINIMO de evaluacion (suelo de
  ritmo), no la cadencia de publicacion. `math.inf` = canal apagado en ese perfil.
  """
  nombre: str
  disparo: str
  fuentes: tuple[str, ...]
  requiere: tuple[str, ...]
  periodo_s: MappingProxyType
  keepalive_s: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
  campos: tuple[Campo, ...] = ()
  desc: str = ""

  def periodo(self, perfil: str) -> float:
    return self.periodo_s.get(perfil, math.inf)

  def keepalive(self, perfil: str) -> float:
    return self.keepalive_s.get(perfil, math.inf)

  def campos_de(self, perfil: str) -> tuple[Campo, ...]:
    n = nivel(perfil)
    return tuple(c for c in self.campos if nivel(c.perfil) <= n)

  def a_dict(self) -> dict:
    return {
      "canal": self.nombre,
      "topic": TOPIC_TEL.format("<dongle>", self.nombre),
      "disparo": self.disparo,
      "fuentes": list(self.fuentes),
      "requiere": list(self.requiere),
      "periodo_s": {p: (None if math.isinf(self.periodo(p)) else self.periodo(p)) for p in PERFILES},
      "keepalive_s": {p: (None if math.isinf(self.keepalive(p)) else self.keepalive(p)) for p in PERFILES},
      "desc": self.desc,
      "campos": [c.a_dict() for c in self.campos],
    }


def _p(ahorro, normal, diag) -> MappingProxyType:
  return MappingProxyType({PERFIL_AHORRO: ahorro, PERFIL_NORMAL: normal, PERFIL_DIAG: diag})


# --------------------------------------------------------------------------- calculados

def _calc_ttc_s(fuentes) -> float | None:
  """Tiempo hasta colision con el lead, en segundos. Solo si nos ESTAMOS acercando.

  No sale de ningun campo de cereal: radarState publica dRel y vRel por separado y el
  cociente lo hacia cada consumidor por su cuenta (o no lo hacia). Se calcula aqui, una
  vez, con el mismo criterio para backend y app.
  """
  rs = fuentes.get("radarState")
  if rs is None:
    return None
  lead = _leer(rs, "leadOne")
  if lead is None or not bool(_leer(lead, "status")):
    return None
  d = _num(_leer(lead, "dRel"))
  v = _num(_leer(lead, "vRel"))
  if d is None or v is None or v >= -0.1:
    return None
  return d / (-v)


def _calc_procs_caidos(fuentes) -> list | None:
  """Procesos que deberian estar corriendo y no lo estan (seccion 7: 'procesos caidos').

  Se recorta a 6 nombres: si se han caido mas de seis, el problema no se diagnostica por
  la lista sino por el hecho, y la lista entera hace que un canal de 300 B pase de un KB.
  """
  ms = fuentes.get("managerState")
  if ms is None:
    return None
  fuera = []
  try:
    for p in _leer(ms, "processes") or []:
      if bool(_leer(p, "shouldBeRunning")) and not bool(_leer(p, "running")):
        fuera.append(str(_leer(p, "name")))
  except Exception:
    return None
  return sorted(fuera)[:6]


CALCULOS = MappingProxyType({
  "ttc_s": _calc_ttc_s,
  "procs_caidos": _calc_procs_caidos,
})


# --------------------------------------------------------------------------- canales

MS_A_KMH = 3.6
RAD_A_DEG = 180.0 / math.pi

CANAL_VEHICLE = Canal(
  nombre="vehicle",
  disparo="periodico",
  fuentes=("carState",),
  requiere=("carState",),
  periodo_s=_p(2.0, 0.5, 0.25),
  desc="Estado del vehiculo: velocidad, marcha, volante, pedales, intermitentes, puertas, cinturon, standstill",
  campos=(
    Campo("speed_kph", "carState", "vEgo", "float", "km/h", 1, MS_A_KMH, defecto=0.0, perfil=PERFIL_AHORRO, desc="Velocidad estimada"),
    Campo("speed_cluster_kph", "carState", "vEgoCluster", "float", "km/h", 1, MS_A_KMH, defecto=0.0, desc="Velocidad que muestra el cuadro del coche"),
    Campo("accel_ms2", "carState", "aEgo", "float", "m/s2", 2, defecto=0.0, desc="Aceleracion longitudinal"),
    Campo("yaw_rate_dps", "carState", "yawRate", "float", "deg/s", 1, RAD_A_DEG, defecto=0.0, desc="Velocidad de guinada"),
    Campo("steer_deg", "carState", "steeringAngleDeg", "float", "deg", 1, defecto=0.0, desc="Angulo de volante"),
    Campo("steer_rate_dps", "carState", "steeringRateDeg", "float", "deg/s", 1, defecto=0.0, perfil=PERFIL_DIAG, desc="Velocidad de giro del volante"),
    # SIN defecto a proposito: hasta 2026-09 era de perfil diagnostico y un firmware anterior
    # no lo manda nunca en normal. Si 0.0 se omitiera, la app no podria reconstruirlo sin
    # inventarse un 0.0 para ese firmware viejo: asi viaja siempre y ausente = no se sabe.
    Campo("steer_torque_driver", "carState", "steeringTorque", "float", "Nm", 1, desc="Par que mete el conductor"),
    Campo("gear", "carState", "gearShifter", "enum", desc="Posicion de la palanca"),
    # Los intermitentes son de las senales mas pedidas y hoy NO salen del coche: van en
    # carState, que se publica entero, pero el consumidor no los tenia documentados en
    # ningun contrato. Aqui son campos de primera clase del canal.
    Campo("blink_left", "carState", "leftBlinker", "bool", defecto=False, perfil=PERFIL_AHORRO, desc="Intermitente izquierdo"),
    Campo("blink_right", "carState", "rightBlinker", "bool", defecto=False, perfil=PERFIL_AHORRO, desc="Intermitente derecho"),
    Campo("standstill", "carState", "standstill", "bool", defecto=False, perfil=PERFIL_AHORRO, desc="Vehiculo detenido"),
    Campo("gas_pressed", "carState", "gasPressed", "bool", defecto=False, desc="Acelerador pisado por el conductor"),
    Campo("brake_pressed", "carState", "brakePressed", "bool", defecto=False, desc="Freno pisado por el conductor"),
    Campo("steer_pressed", "carState", "steeringPressed", "bool", defecto=False, desc="Conductor con las manos forzando el volante"),
    Campo("door_open", "carState", "doorOpen", "bool", defecto=False, perfil=PERFIL_AHORRO, desc="Alguna puerta abierta"),
    Campo("seatbelt_off", "carState", "seatbeltUnlatched", "bool", defecto=False, perfil=PERFIL_AHORRO, desc="Cinturon sin abrochar"),
    Campo("parking_brake", "carState", "parkingBrake", "bool", defecto=False, desc="Freno de mano puesto"),
    Campo("cruise_on", "carState", "cruiseState.enabled", "bool", defecto=False, perfil=PERFIL_AHORRO, desc="Control de crucero activo"),
    Campo("cruise_avail", "carState", "cruiseState.available", "bool", defecto=False, desc="Control de crucero disponible"),
    Campo("cruise_set_kph", "carState", "vCruiseCluster", "float", "km/h", 0, defecto=0.0, desc="Velocidad de consigna que muestra el cuadro"),
    Campo("fuel_pct", "carState", "fuelGauge", "float", "%", 0, 100.0, defecto=0.0, desc="Nivel de combustible o bateria"),
    Campo("charging", "carState", "charging", "bool", defecto=False, desc="Vehiculo cargando"),
    Campo("esp_active", "carState", "espActive", "bool", defecto=False, desc="Control de estabilidad interviniendo"),
    Campo("can_valid", "carState", "canValid", "bool", defecto=True, perfil=PERFIL_AHORRO, desc="Bus CAN sano"),
  ),
)

CANAL_OPENPILOT = Canal(
  nombre="openpilot",
  disparo="cambio",
  fuentes=("selfdriveState", "selfdriveStateSP", "liveCalibration"),
  requiere=("selfdriveState",),
  periodo_s=_p(2.0, 1.0, 0.5),
  keepalive_s=_p(300.0, 60.0, 15.0),
  desc="Estado de openpilot: engaged, MADS, alerta activa, calibracion, modelo",
  campos=(
    Campo("enabled", "selfdriveState", "enabled", "bool", defecto=False, perfil=PERFIL_AHORRO, desc="openpilot habilitado"),
    Campo("active", "selfdriveState", "active", "bool", defecto=False, perfil=PERFIL_AHORRO, desc="openpilot actuando sobre el coche"),
    Campo("state", "selfdriveState", "state", "enum", perfil=PERFIL_AHORRO, desc="Maquina de estados de openpilot"),
    Campo("engageable", "selfdriveState", "engageable", "bool", defecto=False, desc="Se puede enganchar ahora"),
    Campo("experimental", "selfdriveState", "experimentalMode", "bool", defecto=False, desc="Modo experimental"),
    Campo("personality", "selfdriveState", "personality", "enum", desc="Distancia de seguimiento"),
    Campo("alert_status", "selfdriveState", "alertStatus", "enum", desc="Severidad de la alerta en pantalla"),
    Campo("alert_text1", "selfdriveState", "alertText1", "str", defecto="", desc="Primera linea de la alerta"),
    Campo("alert_text2", "selfdriveState", "alertText2", "str", defecto="", perfil=PERFIL_DIAG, desc="Segunda linea de la alerta"),
    Campo("mads_state", "selfdriveStateSP", "mads.state", "enum", desc="Estado de MADS"),
    Campo("mads_enabled", "selfdriveStateSP", "mads.enabled", "bool", defecto=False, desc="MADS habilitado"),
    Campo("mads_active", "selfdriveStateSP", "mads.active", "bool", defecto=False, desc="MADS actuando"),
    Campo("mads_available", "selfdriveStateSP", "mads.available", "bool", defecto=False, perfil=PERFIL_DIAG, desc="MADS disponible"),
    Campo("cal_status", "liveCalibration", "calStatus", "enum", perfil=PERFIL_AHORRO, desc="Estado de la calibracion"),
    Campo("cal_pct", "liveCalibration", "calPerc", "int", "%", defecto=0, desc="Progreso de la calibracion"),
  ),
)

CANAL_HEALTH = Canal(
  nombre="health",
  disparo="periodico",
  fuentes=("deviceState", "pandaStates", "managerState"),
  requiere=("deviceState",),
  periodo_s=_p(60.0, 10.0, 5.0),
  desc="Salud del dispositivo: temperaturas, CPU, memoria, disco, red, panda, procesos caidos",
  campos=(
    Campo("started", "deviceState", "started", "bool", defecto=False, perfil=PERFIL_AHORRO, desc="Dispositivo onroad"),
    Campo("cpu_pct", "deviceState", "cpuUsagePercent", "int", "%", agg="max", defecto=0, desc="Uso de CPU (nucleo mas cargado)"),
    Campo("mem_pct", "deviceState", "memoryUsagePercent", "int", "%", defecto=0, desc="Uso de memoria"),
    Campo("gpu_pct", "deviceState", "gpuUsagePercent", "int", "%", defecto=0, perfil=PERFIL_DIAG, desc="Uso de GPU"),
    Campo("disk_free_pct", "deviceState", "freeSpacePercent", "int", "%", perfil=PERFIL_AHORRO, desc="Espacio libre en disco"),
    Campo("cpu_temp_c", "deviceState", "cpuTempC", "float", "C", 1, agg="max", desc="Temperatura de CPU (nucleo mas caliente)"),
    Campo("max_temp_c", "deviceState", "maxTempC", "float", "C", 1, perfil=PERFIL_AHORRO, desc="Temperatura maxima del dispositivo"),
    Campo("thermal", "deviceState", "thermalStatus", "enum", perfil=PERFIL_AHORRO, desc="Estado termico"),
    Campo("net_type", "deviceState", "networkType", "enum", desc="Tipo de red"),
    Campo("net_strength", "deviceState", "networkStrength", "enum", desc="Calidad de la senal"),
    # Se emite SIEMPRE (sin defecto declarado): es el campo por el que el propio
    # dispositivo decide degradar el perfil, y el consumidor tiene que poder explicar
    # por que la telemetria bajo de ritmo sin adivinarlo.
    Campo("net_metered", "deviceState", "networkMetered", "bool", perfil=PERFIL_AHORRO, desc="Red de pago por datos"),
    Campo("power_w", "deviceState", "powerDrawW", "float", "W", 1, defecto=0.0, perfil=PERFIL_DIAG, desc="Consumo del dispositivo"),
    Campo("panda_type", "pandaStates", "0.pandaType", "enum", perfil=PERFIL_DIAG, desc="Modelo de panda"),
    Campo("panda_safety", "pandaStates", "0.safetyModel", "enum", desc="Modelo de safety cargado en el panda"),
    Campo("panda_controls_allowed", "pandaStates", "0.controlsAllowed", "bool", defecto=False, desc="El panda deja actuar"),
    Campo("panda_fault", "pandaStates", "0.faultStatus", "enum", defecto="none", desc="Fallo del panda"),
    Campo("panda_harness", "pandaStates", "0.harnessStatus", "enum", perfil=PERFIL_DIAG, desc="Estado del arnes"),
    Campo("procs_caidos", "managerState", "", "list[str]", defecto=[], perfil=PERFIL_AHORRO, desc="Procesos que deberian correr y no corren"),
  ),
)

CANAL_PERCEPTION = Canal(
  nombre="perception",
  disparo="periodico",
  fuentes=("radarState", "longitudinalPlan", "drivingModelData", "carState", "controlsState"),
  requiere=("radarState",),
  periodo_s=_p(math.inf, 0.5, 0.25),
  desc="Percepcion: lead, TTC, carriles, angulo muerto, plan longitudinal",
  campos=(
    Campo("lead", "radarState", "leadOne.status", "bool", defecto=False, desc="Hay vehiculo delante"),
    Campo("lead_dist_m", "radarState", "leadOne.dRel", "float", "m", 1, defecto=0.0, desc="Distancia al vehiculo de delante"),
    Campo("lead_v_rel_ms", "radarState", "leadOne.vRel", "float", "m/s", 1, defecto=0.0, desc="Velocidad relativa del lead"),
    Campo("lead_v_ms", "radarState", "leadOne.vLead", "float", "m/s", 1, defecto=0.0, perfil=PERFIL_DIAG, desc="Velocidad absoluta del lead"),
    Campo("lead_prob", "radarState", "leadOne.modelProb", "float", "", 2, defecto=0.0, perfil=PERFIL_DIAG, desc="Confianza del modelo en el lead"),
    Campo("ttc_s", "radarState", "", "float", "s", 1, desc="Tiempo hasta colision (solo si nos acercamos)"),
    Campo("bsm_left", "carState", "leftBlindspot", "bool", defecto=False, desc="Angulo muerto izquierdo ocupado"),
    Campo("bsm_right", "carState", "rightBlindspot", "bool", defecto=False, desc="Angulo muerto derecho ocupado"),
    Campo("lane_left_m", "drivingModelData", "laneLineMeta.leftY", "float", "m", 2, defecto=0.0, desc="Distancia a la linea izquierda"),
    Campo("lane_right_m", "drivingModelData", "laneLineMeta.rightY", "float", "m", 2, defecto=0.0, desc="Distancia a la linea derecha"),
    # SIN defecto, como steer_torque_driver: eran de perfil diagnostico y un firmware anterior
    # no las manda en normal. Con defecto, un 0 % (carretera sin lineas) no viajaria y la app
    # no podria distinguirlo de "firmware viejo"; asi viajan siempre y ausente = no se sabe.
    Campo("lane_left_prob", "drivingModelData", "laneLineMeta.leftProb", "float", "", 2,
          desc="Confianza en la linea izquierda"),
    Campo("lane_right_prob", "drivingModelData", "laneLineMeta.rightProb", "float", "", 2,
          desc="Confianza en la linea derecha"),
    Campo("lane_change", "drivingModelData", "meta.laneChangeState", "enum", defecto="off", desc="Estado del cambio de carril"),
    Campo("lane_change_dir", "drivingModelData", "meta.laneChangeDirection", "enum", defecto="none", desc="Sentido del cambio de carril"),
    Campo("a_target_ms2", "longitudinalPlan", "aTarget", "float", "m/s2", 2, defecto=0.0, desc="Aceleracion objetivo del plan"),
    Campo("long_source", "longitudinalPlan", "longitudinalPlanSource", "enum", desc="Que manda en el plan longitudinal"),
    Campo("should_stop", "longitudinalPlan", "shouldStop", "bool", defecto=False, desc="El plan pide detenerse"),
    Campo("fcw", "longitudinalPlan", "fcw", "bool", defecto=False, desc="Aviso de colision frontal"),
    Campo("curvature", "controlsState", "curvature", "float", "1/m", 4, defecto=0.0, perfil=PERFIL_DIAG, desc="Curvatura actual"),
    Campo("desired_curvature", "controlsState", "desiredCurvature", "float", "1/m", 4, defecto=0.0, perfil=PERFIL_DIAG, desc="Curvatura deseada"),
  ),
)

CANAL_ROAD = Canal(
  nombre="road",
  disparo="cambio",
  fuentes=("liveMapDataSP",),
  requiere=("liveMapDataSP",),
  periodo_s=_p(10.0, 2.0, 1.0),
  keepalive_s=_p(math.inf, 600.0, 120.0),
  desc="Via: limite de velocidad, proximo limite y nombre de la via",
  campos=(
    Campo("speed_limit_kph", "liveMapDataSP", "speedLimit", "int", "km/h", factor=MS_A_KMH,
          defecto=0, perfil=PERFIL_AHORRO, desc="Limite de velocidad vigente"),
    Campo("speed_limit_valid", "liveMapDataSP", "speedLimitValid", "bool", defecto=False, perfil=PERFIL_AHORRO, desc="El limite vigente es fiable"),
    Campo("next_limit_kph", "liveMapDataSP", "speedLimitAhead", "int", "km/h", factor=MS_A_KMH, defecto=0, desc="Proximo limite de velocidad"),
    Campo("next_limit_valid", "liveMapDataSP", "speedLimitAheadValid", "bool", defecto=False, desc="El proximo limite es fiable"),
    Campo("next_limit_dist_m", "liveMapDataSP", "speedLimitAheadDistance", "int", "m", defecto=0, desc="Distancia al proximo limite"),
    Campo("road_name", "liveMapDataSP", "roadName", "str", defecto="", desc="Nombre de la via"),
  ),
)

CANAL_POS = Canal(
  nombre="pos",
  disparo="adaptativo",
  fuentes=("gpsLocationExternal", "gpsLocation"),
  requiere=(),
  # En un canal adaptativo `periodo_s` es el SUELO de ritmo: nunca se publica mas rapido.
  periodo_s=_p(5.0, 1.0, 0.5),
  # ...y el keepalive es el techo: aunque el coche este parado y sin cambio de rumbo, se
  # manda una posicion cada tanto para que la traza no tenga huecos sin explicacion.
  keepalive_s=_p(120.0, 30.0, 10.0),
  desc="Traza GPS con decimacion adaptativa por curvatura y calidad de fix",
  campos=(
    Campo("lat", "gps", "latitude", "float", "deg", 6, perfil=PERFIL_AHORRO, desc="Latitud"),
    Campo("lon", "gps", "longitude", "float", "deg", 6, perfil=PERFIL_AHORRO, desc="Longitud"),
    Campo("alt_m", "gps", "altitude", "float", "m", 0, defecto=0.0, desc="Altitud"),
    Campo("speed_kph", "gps", "speed", "float", "km/h", 1, MS_A_KMH, defecto=0.0, perfil=PERFIL_AHORRO, desc="Velocidad sobre el suelo"),
    Campo("bearing_deg", "gps", "bearingDeg", "float", "deg", 0, defecto=0.0, perfil=PERFIL_AHORRO, desc="Rumbo"),
    Campo("acc_m", "gps", "horizontalAccuracy", "float", "m", 1, defecto=0.0, desc="Precision horizontal declarada"),
    Campo("sats", "gps", "satelliteCount", "int", defecto=0, desc="Satelites en solucion"),
    Campo("fix", "gps", "hasFix", "bool", defecto=True, perfil=PERFIL_AHORRO, desc="Hay fix GPS"),
  ),
)

# Los canales `event` y `trip` no se construyen con la lista blanca: su payload es propio
# (una lista de flancos tipados y un resumen de viaje). Se declaran igual para que
# aparezcan en el descriptor que consume el backend, con `campos` describiendo la forma.
CANAL_EVENT = Canal(
  nombre="event",
  disparo="evento",
  fuentes=("onroadEvents", "onroadEventsSP"),
  requiere=(),
  periodo_s=_p(1.0, 0.5, 0.25),
  desc="Flancos de onroadEvents/onroadEventsSP, tipados con el codigo estable del descriptor",
  campos=(
    Campo("ev", "onroadEvents", "", "list[obj]", perfil=PERFIL_AHORRO,
          desc="Lista de flancos: {cod:int, nom:str, sev:enum, on:bool}. cod=0 => evento que este firmware no conoce"),
  ),
)

CANAL_TRIP = Canal(
  nombre="trip",
  disparo="sesion",
  fuentes=("deviceState", "carState", "selfdriveState"),
  requiere=(),
  periodo_s=_p(0.0, 0.0, 0.0),
  desc="Apertura y cierre de viaje, con resumen al cerrar",
  campos=(
    Campo("ev", "trip", "", "enum", perfil=PERFIL_AHORRO, desc="start | end"),
    Campo("dur_s", "trip", "", "int", "s", perfil=PERFIL_AHORRO, desc="Duracion del viaje (solo en end)"),
    Campo("mov_s", "trip", "", "int", "s", perfil=PERFIL_AHORRO, desc="Tiempo en movimiento (solo en end)"),
    Campo("dist_km", "trip", "", "float", "km", 2, perfil=PERFIL_AHORRO, desc="Distancia recorrida (solo en end)"),
    Campo("v_max_kph", "trip", "", "float", "km/h", 1, perfil=PERFIL_AHORRO, desc="Velocidad maxima (solo en end)"),
    Campo("v_med_kph", "trip", "", "float", "km/h", 1, perfil=PERFIL_AHORRO, desc="Velocidad media en movimiento (solo en end)"),
    Campo("engaged_s", "trip", "", "int", "s", perfil=PERFIL_AHORRO, desc="Tiempo con openpilot actuando (solo en end)"),
    Campo("engaged_pct", "trip", "", "int", "%", perfil=PERFIL_AHORRO, desc="Porcentaje del viaje con openpilot actuando (solo en end)"),
    Campo("n_desenganches", "trip", "", "int", perfil=PERFIL_AHORRO, desc="Veces que openpilot dejo de actuar en movimiento (solo en end)"),
    Campo("n_eventos", "trip", "", "int", perfil=PERFIL_AHORRO, desc="Eventos de aviso o superiores (solo en end)"),
  ),
)

CANALES = MappingProxyType({c.nombre: c for c in (
  CANAL_VEHICLE, CANAL_OPENPILOT, CANAL_HEALTH, CANAL_EVENT,
  CANAL_PERCEPTION, CANAL_ROAD, CANAL_TRIP, CANAL_POS,
)})

# Servicios cereal que hay que suscribir para alimentar los canales. `gps` no es un
# servicio: es el alias interno del canal pos, que se resuelve al primero de
# gpsLocationExternal / gpsLocation que este vivo (ver MotorTelemetria._fuente_gps).
SERVICIOS = tuple(sorted({f for c in CANALES.values() for f in c.fuentes if f != "gps"}))


# --------------------------------------------------------------------------- eventos tipados

# Codigo ORBIT estable de cada evento. ESTA TABLA ES EL CONTRATO, no el orden del enum de
# cereal. El ordinal de `OnroadEvent.EventName` cambia entre rebases de openpilot (basta
# con que upstream inserte un evento en medio) y publicar el ordinal significa que el
# mismo numero pasa a querer decir otra cosa sin que nadie toque el backend: los historicos
# quedan mal etiquetados hacia atras y las reglas de la app disparan con el evento
# equivocado. Eso ya mordio en este proyecto.
#
# Reglas de mantenimiento:
#   - un codigo asignado NO se reutiliza ni se renumera JAMAS, aunque el evento
#     desaparezca de cereal;
#   - los eventos nuevos se anaden al final de su bloque (1..199 openpilot, 201.. sunnypilot);
#   - un evento que este firmware no conoce se publica con cod=0 y su nombre, para que el
#     backend lo registre en vez de perderlo.
EVENTOS_CODIGO = MappingProxyType({
  "canError": 1,
  "steerUnavailable": 2,
  "wrongGear": 3,
  "doorOpen": 4,
  "seatbeltNotLatched": 5,
  "espDisabled": 6,
  "wrongCarMode": 7,
  "steerTempUnavailable": 8,
  "reverseGear": 9,
  "buttonCancel": 10,
  "buttonEnable": 11,
  "pedalPressed": 12,
  "preEnableStandstill": 13,
  "gasPressedOverride": 14,
  "steerOverride": 15,
  "cruiseDisabled": 16,
  "speedTooLow": 17,
  "outOfSpace": 18,
  "overheat": 19,
  "calibrationIncomplete": 20,
  "calibrationInvalid": 21,
  "calibrationRecalibrating": 22,
  "controlsMismatch": 23,
  "pcmEnable": 24,
  "pcmDisable": 25,
  "radarFault": 26,
  "brakeHold": 27,
  "parkBrake": 28,
  "manualRestart": 29,
  "joystickDebug": 30,
  "longitudinalManeuver": 31,
  "steerTempUnavailableSilent": 32,
  "resumeRequired": 33,
  "driverDistracted1": 34,
  "driverDistracted2": 35,
  "driverDistracted3": 36,
  "driverUnresponsive1": 37,
  "driverUnresponsive2": 38,
  "driverUnresponsive3": 39,
  "belowSteerSpeed": 40,
  "lowBattery": 41,
  "accFaulted": 42,
  "sensorDataInvalid": 43,
  "commIssue": 44,
  "commIssueAvgFreq": 45,
  "tooDistracted": 46,
  "posenetInvalid": 47,
  "soundsUnavailableDEPRECATED": 48,
  "preLaneChangeLeft": 49,
  "preLaneChangeRight": 50,
  "laneChange": 51,
  "lowMemory": 52,
  "stockAeb": 53,
  "ldw": 54,
  "carUnrecognized": 55,
  "invalidLkasSetting": 56,
  "speedTooHigh": 57,
  "laneChangeBlocked": 58,
  "relayMalfunction": 59,
  "stockFcw": 60,
  "startup": 61,
  "startupNoCar": 62,
  "startupNoControl": 63,
  "startupNoSecOcKey": 64,
  "startupMaster": 65,
  "fcw": 66,
  "steerSaturated": 67,
  "belowEngageSpeed": 68,
  "noGps": 69,
  "wrongCruiseMode": 70,
  "modeldLagging": 71,
  "deviceFalling": 72,
  "fanMalfunction": 73,
  "cameraMalfunction": 74,
  "cameraFrameRate": 75,
  "processNotRunning": 76,
  "dashcamMode": 77,
  "selfdriveInitializing": 78,
  "usbError": 79,
  "cruiseMismatch": 80,
  "canBusMissing": 81,
  "selfdrivedLagging": 82,
  "resumeBlocked": 83,
  "steerTimeLimit": 84,
  "vehicleSensorsInvalid": 85,
  "locationdTemporaryError": 86,
  "locationdPermanentError": 87,
  "paramsdTemporaryError": 88,
  "paramsdPermanentError": 89,
  "actuatorsApiUnavailable": 90,
  "espActive": 91,
  "personalityChanged": 92,
  "aeb": 93,
  "radarTempUnavailable": 94,
  "steerDisengage": 95,
  "userBookmark": 96,
  "excessiveActuation": 97,
  "audioFeedback": 98,
  "stockLkas": 99,
  "lateralManeuver": 100,
  # --- sunnypilot (onroadEventsSP) ---
  "lkasEnable": 201,
  "lkasDisable": 202,
  "manualSteeringRequired": 203,
  "manualLongitudinalRequired": 204,
  "silentLkasEnable": 205,
  "silentLkasDisable": 206,
  "silentBrakeHold": 207,
  "silentWrongGear": 208,
  "silentReverseGear": 209,
  "silentDoorOpen": 210,
  "silentSeatbeltNotLatched": 211,
  "silentParkBrake": 212,
  "controlsMismatchLateral": 213,
  "hyundaiRadarTracksConfirmed": 214,
  "experimentalModeSwitched": 215,
  "wrongCarModeAlertOnly": 216,
  "pedalPressedAlertOnly": 217,
  "laneTurnLeft": 218,
  "laneTurnRight": 219,
  "speedLimitPreActive": 220,
  "speedLimitActive": 221,
  "speedLimitChanged": 222,
  "speedLimitPending": 223,
  "e2eChime": 224,
  "laneChangeBlockedLeft": 225,
  "laneChangeBlockedRight": 226,
})

EVENTOS_POR_CODIGO = MappingProxyType({v: k for k, v in EVENTOS_CODIGO.items()})

# Severidad publicada, de menos a mas. Sale de los flancos booleanos que trae CADA
# instancia del evento en cereal, no de una tabla fija: el mismo `steerTempUnavailable`
# llega como aviso o como desenganche segun el estado, y aplanarlo a un valor fijo por
# nombre pierde justo la parte que importa.
SEVERIDADES = ("info", "aviso", "noentry", "override", "disable", "critico")

# Flanco de severidad a partir del cual un evento cuenta en el resumen del viaje.
SEV_MIN_RESUMEN = "aviso"

# Donde vive la LISTA de eventos dentro de cada servicio. Los dos no tienen la misma forma:
# `onroadEvents` ES una List(OnroadEvent) y `onroadEventsSP` es un STRUCT con un campo
# `events` dentro. Iterar el struct como si fuera lista lanza TypeError, y en un bucle
# defensivo eso significa que los ~26 eventos de sunnypilot no se publican NUNCA sin que
# nadie se entere. Por eso la forma se declara aqui y hay un test que la comprueba.
FUENTES_EVENTO = MappingProxyType({
  "onroadEvents": "",
  "onroadEventsSP": "events",
})


def codigo_evento(nombre) -> int:
  """Codigo ORBIT del evento. 0 = este firmware no lo conoce (rebase mas nuevo que la tabla)."""
  return EVENTOS_CODIGO.get(nombre, 0) if isinstance(nombre, str) else 0


def severidad_evento(ev) -> str:
  """Severidad de UNA instancia de OnroadEvent, derivada de sus flancos."""
  if bool(_leer(ev, "immediateDisable")):
    return "critico"
  if bool(_leer(ev, "softDisable")) or bool(_leer(ev, "userDisable")):
    return "disable"
  if bool(_leer(ev, "overrideLateral")) or bool(_leer(ev, "overrideLongitudinal")):
    return "override"
  if bool(_leer(ev, "noEntry")):
    return "noentry"
  if bool(_leer(ev, "warning")):
    return "aviso"
  return "info"


# --------------------------------------------------------------------------- lectura y coercion

def _leer(obj, ruta: str):
  """Recorre una ruta punteada por getattr (o por indice si el tramo es un entero).

  Devuelve None ante cualquier fallo. Es a proposito: la telemetria NUNCA puede tumbar el
  hilo que la produce por un campo que este build de cereal no tiene, y el consumidor ya
  sabe (por el descriptor) que un campo ausente se reconstruye con su defecto.
  """
  cur = obj
  if not ruta:
    return cur
  for tramo in ruta.split("."):
    if cur is None:
      return None
    try:
      if tramo.isdigit():
        cur = cur[int(tramo)]
      else:
        cur = getattr(cur, tramo)
    except Exception:
      return None
  return cur


def _num(v) -> float | None:
  try:
    f = float(v)
  except (TypeError, ValueError):
    return None
  return f if math.isfinite(f) else None


def _agrega(valores, modo: str):
  try:
    vals = [x for x in (_num(v) for v in valores) if x is not None]
  except TypeError:
    return None
  if not vals:
    return None
  if modo == "max":
    return max(vals)
  if modo == "min":
    return min(vals)
  if modo == "mean":
    return sum(vals) / len(vals)
  if modo == "len":
    return float(len(vals))
  return vals[0]


def coacciona(campo: Campo, bruto):
  """Aplica agregacion, factor, redondeo y tipo declarados. Devuelve el valor de cable.

  Un float no finito sale como None (null en el cable) EN VEZ de desaparecer: la seccion 7
  lo pide explicitamente, y ademas un null dice "esta senal existe y ahora mismo no vale"
  mientras que un campo ausente dice "vale su defecto", que no es lo mismo.
  """
  if bruto is None:
    return _AUSENTE
  if campo.agg:
    bruto = _agrega(bruto, campo.agg)
    if bruto is None:
      return _AUSENTE
  t = campo.tipo
  if t == "bool":
    return bool(bruto)
  if t == "enum":
    return str(bruto)
  if t == "str":
    return str(bruto)[:MAX_TEXTO]
  if t == "list[str]":
    try:
      return [str(x)[:MAX_TEXTO] for x in bruto]
    except TypeError:
      return _AUSENTE
  n = _num(bruto)
  if n is None:
    return None       # no finito -> null EXPLICITO en el cable (seccion 7)
  n *= campo.factor
  if t == "int":
    return int(round(n))
  return round(n, campo.dec)


def extraer(canal: Canal, fuentes: dict, perfil: str) -> dict:
  """Construye el diccionario de datos de un canal aplicando la lista blanca del perfil."""
  datos: dict = {}
  for campo in canal.campos_de(perfil):
    if campo.origen == "" and campo.nombre in CALCULOS:
      try:
        bruto = CALCULOS[campo.nombre](fuentes)
      except Exception:
        bruto = None
    else:
      fuente = fuentes.get(campo.fuente)
      if fuente is None:
        continue    # fuente no viva: el campo se OMITE (no es lo mismo que null)
      bruto = _leer(fuente, campo.origen)
      if bruto is None and campo.origen:
        continue
    valor = coacciona(campo, bruto)
    if valor is _AUSENTE:
      continue
    if campo.defecto is not _SIN_DEFECTO and valor == campo.defecto:
      continue    # omision por defecto DECLARADO (estatica, no delta contra el anterior)
    datos[campo.nombre] = valor
  return datos


# --------------------------------------------------------------------------- sobre

def sobre(seq: int, ts_ms: int, trip_id, datos: dict) -> dict:
  """Sobre de telemetria. Ni `dongle_id` ni nombre de canal: los dos van en el topic.

  `seq` es monotono POR CANAL. La telemetria va en QoS 0 (seccion 3.1), asi que el
  consumidor no tiene ninguna otra forma de saber que se ha perdido una muestra ni de
  detectar que dos llegaron desordenadas.
  """
  s = {
    "v": CONTRACT_VERSION,
    "sv": SCHEMA_TELEMETRIA,
    "ts_ms": int(ts_ms),
    "seq": int(seq),
    "d": datos,
  }
  if trip_id:
    s["trip"] = trip_id
  return s


def descriptor_backend() -> dict:
  """Descriptor completo que consumen backend y app. Es lo que hace que el contrato sea uno.

  Lleva todo lo necesario para reconstruir un mensaje sin mirar el firmware: canales,
  cadencia por perfil, campos con tipo/unidad/redondeo/defecto y la tabla de codigos de
  evento. Se sirve tal cual (o se congela en un fichero generado) en el otro repo.
  """
  return {
    "v": CONTRACT_VERSION,
    "sv": SCHEMA_TELEMETRIA,
    "topic": TOPIC_TEL.format("<dongle>", "<canal>"),
    "perfiles": list(PERFILES),
    "diag_ttl_s": DIAG_TTL_S,
    "sobre": {
      "v": "int: version del namespace",
      "sv": "int: version del esquema de telemetria",
      "ts_ms": "int: epoch ms de pared",
      "seq": "int: monotono por canal, detecta perdida y desorden en QoS 0",
      "trip": "str|ausente: id del viaje abierto",
      "d": "obj: datos del canal",
    },
    "canales": {nombre: c.a_dict() for nombre, c in CANALES.items()},
    "eventos": dict(EVENTOS_CODIGO),
    "severidades": list(SEVERIDADES),
  }


def firma_telemetria() -> str:
  """Huella estable del contrato. El test cruzado del otro repo compara ESTA cadena.

  Cambiar un nombre de campo, un tipo, una unidad o un codigo de evento cambia la huella;
  cambiar una descripcion o una cadencia no deberia romper a nadie, pero aqui entra todo
  a proposito: mas vale un test que obliga a mirar que una divergencia silenciosa.
  """
  crudo = json.dumps(descriptor_backend(), sort_keys=True, separators=(",", ":"))
  return hashlib.sha256(crudo.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- decimacion de pos

# Cambio de rumbo a partir del cual una muestra es imprescindible: es la "curvatura" de la
# seccion 7. Sin este criterio una rotonda o una curva cerrada se reconstruye como una
# linea recta entre los dos unicos puntos que cayeron dentro del intervalo de tiempo.
UMBRAL_RUMBO_DEG = 8.0
# Distancia minima entre muestras por perfil (m).
DIST_MIN_M = MappingProxyType({PERFIL_AHORRO: 150.0, PERFIL_NORMAL: 50.0, PERFIL_DIAG: 20.0})
# Precision horizontal a partir de la cual el fix es malo: se triplica la distancia minima
# (mandar mas puntos de una posicion mala no da mas traza, da mas ruido y mas datos).
ACC_MALA_M = 25.0
# ...y a partir de aqui el fix no vale para nada: solo se manda el keepalive, para que el
# consumidor sepa que seguimos vivos y sin posicion util.
ACC_INUTIL_M = 100.0
# Velocidad por encima de la cual se considera que el coche se mueve (m/s).
V_MOVIMIENTO_MS = 0.5


def _dist_m(lat1, lon1, lat2, lon2) -> float:
  """Distancia entre dos puntos por equirectangular. A escala de metros el error frente a
  haversine es despreciable y no cuesta dos trigonometricas inversas por muestra."""
  x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2.0))
  y = math.radians(lat2 - lat1)
  return 6371000.0 * math.hypot(x, y)


def _delta_rumbo(a, b) -> float:
  d = abs((a - b) % 360.0)
  return min(d, 360.0 - d)


# --------------------------------------------------------------------------- motor

class MotorTelemetria:
  """Decide QUE canal toca publicar, con QUE campos y CUANDO. No publica: devuelve mensajes.

  Es puro y sin I/O a proposito. El hilo del emisor (`mqtt_envio_general`) le pasa las
  fuentes ya leidas del SubMaster y un reloj monotono, y recibe una lista de
  `(canal, sobre)` lista para `json.dumps` + `publish`. Asi todo el comportamiento
  interesante -- cadencias, on-change, decimacion de la traza, apertura y cierre de viaje,
  caducidad del perfil de diagnostico -- se prueba sin broker, sin cereal y sin coche.

  Todos los plazos se miden con el reloj MONOTONO que entra por parametro. El epoch solo
  se usa para SELLAR (`ts_ms`), que es lo unico comparable fuera de este proceso.
  """

  def __init__(self, perfil: str = PERFIL_NORMAL, ahora: float = 0.0):
    self.perfil_pedido = perfil_valido(perfil) or PERFIL_NORMAL
    self.perfil = self.perfil_pedido
    self._diag_desde = ahora if self.perfil_pedido == PERFIL_DIAG else None
    # Bandera de un solo uso: el llamante la consume para reescribir el Param del perfil
    # cuando el diagnostico caduca solo. Sin esto la UI seguiria diciendo "diagnostico"
    # mientras el dispositivo ya emite en normal.
    self.diag_expirado = False

    self._seq = dict.fromkeys(CANALES, 0)
    self._ultimo_envio: dict[str, float] = {}
    self._ultimo_hash: dict[str, str] = {}
    # Sello ANTERIOR de cada canal on-change sellado en el tick en curso. Existe solo para
    # poder deshacerlo si el mensaje no llega a salir del dispositivo: ver `revertir`.
    self._hash_previo: dict[str, str | None] = {}
    self._t_prev = ahora

    # Estado del canal event: conjunto de (nombre, severidad) activos y flancos pendientes.
    self._ev_activos: set = set()
    self._ev_pendientes: list = []

    # Estado del canal pos.
    self._pos_ultima = None    # (lat, lon, rumbo)

    # Estado de viaje.
    self.trip_id = None
    self._trip = None
    self._onroad = False
    self._activo_prev = False

  # ------------------------------------------------------------------ perfil

  def pedir_perfil(self, nombre, ahora: float) -> str:
    """Cambia el perfil pedido. Devuelve el perfil pedido resultante (no el efectivo)."""
    p = perfil_valido(nombre)
    if p is None:
      return self.perfil_pedido
    self.perfil_pedido = p
    self._diag_desde = ahora if p == PERFIL_DIAG else None
    return p

  def _resolver_perfil(self, ahora: float, metered: bool) -> str:
    if self.perfil_pedido == PERFIL_DIAG and self._diag_desde is not None and (ahora - self._diag_desde) >= DIAG_TTL_S:
      # Apagado del diagnostico EN EL PROPIO DISPOSITIVO (seccion 7): no depende de que
      # el backend o la app se acuerden de bajarlo.
      self.perfil_pedido = PERFIL_NORMAL
      self._diag_desde = None
      self.diag_expirado = True
    return degrada_por_red(self.perfil_pedido, metered)

  # ------------------------------------------------------------------ utilidades internas

  def _due(self, canal: str, ahora: float, periodo: float) -> bool:
    if math.isinf(periodo):
      return False
    ultimo = self._ultimo_envio.get(canal)
    return ultimo is None or (ahora - ultimo) >= periodo

  def _mensaje(self, canal: str, ahora: float, ts_ms: int, datos: dict) -> tuple:
    self._seq[canal] = self._seq.get(canal, 0) + 1
    self._ultimo_envio[canal] = ahora
    return (canal, sobre(self._seq[canal], ts_ms, self.trip_id, datos))

  def revertir(self, canal: str) -> bool:
    """Deshace el sello de estado de un canal ON-CHANGE cuyo mensaje NO llego a salir.

    `tick` escribe `_ultimo_hash` ANTES de que nadie publique, porque el motor no publica:
    devuelve mensajes. Si el publish falla y el sello se queda puesto, el canal da el
    estado por entregado y se calla hasta el keepalive. Con los numeros del descriptor eso
    es `openpilot` hasta 60 s en NORMAL pintando "actuando" un coche ya desenganchado,
    `road` hasta 600 s, y en perfil AHORRO el keepalive de `road` es INFINITO
    (`keepalive_s=_p(math.inf, ...)`): no se reenvia NUNCA.

    Se llama SOLO cuando la muestra no se ha podido conservar de ninguna otra forma (el
    spool tampoco la acepto). Si el spool si la guardo, el sello se MANTIENE: esa muestra
    se reenviara con su propio `seq` y revertir ademas la duplicaria.

    Lo que NO se deshace, a proposito:
      - el `seq`, que es monotono por canal justo para que el consumidor detecte en QoS 0
        que se perdio una muestra; taparlo seria mentirle;
      - `_ultimo_envio`, para que el reintento espere al siguiente periodo del canal en
        vez de repetirse en el tick de golpe.

    Los canales que NO son on-change (`vehicle`, `perception`, `health`, `pos`) no sellan
    estado: para ellos esto es un no-op y devuelve False. `event` y `trip` son mensajes
    UNICOS -- un rc!=0 los borra de la historia -- y por eso su red es el spool, no esto.
    """
    if canal not in self._hash_previo:
      return False
    anterior = self._hash_previo.pop(canal)
    if anterior is None:
      self._ultimo_hash.pop(canal, None)
    else:
      self._ultimo_hash[canal] = anterior
    return True

  def invalidar_sello(self, canal: str) -> bool:
    """Olvida el sello de un canal ON-CHANGE cuya muestra se perdio DESPUES de este tick.

    `revertir` solo vale DENTRO del tick que sello, porque `_hash_previo` se limpia al
    empezar cada uno. Pero hay perdidas que no se conocen hasta mucho despues: la muestra
    la acepto la cola persistente (spool.guardar() devolvio True, asi que el llamante NO
    revirtio) y murio luego al evictar RAM, al rechazar el lote con el disco al tope, al
    podar el disco o al apagarse el spool en caliente. En todas ellas el estado quedo
    sellado y nunca salio: el canal se calla hasta el keepalive, que para `openpilot` son
    60 s en NORMAL y para `road` es INFINITO en AHORRO. Este es el cierre de esos caminos.

    Aqui NO se restaura el sello anterior -- ya no existe, lo limpio el tick -- sino que se
    olvida entero, que es lo honesto cuando no se sabe que llego a entregarse. Lo que se
    paga es como mucho un mensaje de mas: el canal vuelve a proponer su estado en su
    siguiente periodo aunque no haya cambiado.

    Devuelve True solo si habia sello que olvidar. En los canales que no son on-change
    (`vehicle`, `perception`, `health`, `pos`) es un no-op declarado, igual que `revertir`,
    y en `event`/`trip` tampoco hay nada que hacer: son mensajes UNICOS y perderlos no se
    deshace desde aqui (el spool lo cuenta en `perdidas_criticas`).
    """
    self._hash_previo.pop(canal, None)
    return self._ultimo_hash.pop(canal, None) is not None

  @staticmethod
  def _huella(datos: dict) -> str:
    return hashlib.sha1(json.dumps(datos, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

  @staticmethod
  def _fuente_gps(fuentes: dict):
    """Elige la fuente de posicion: la externa si tiene fix, si no la interna.

    gpsLocationExternal (ublox/qcom, 10 Hz) y gpsLocation (1 Hz) coexisten segun el
    hardware y la version. Hoy `canales.json` publica LAS DOS enteras, que ademas de
    duplicar el coste deja al consumidor decidiendo cual se cree.
    """
    for nombre in ("gpsLocationExternal", "gpsLocation"):
      f = fuentes.get(nombre)
      if f is not None and bool(_leer(f, "hasFix")):
        return f
    for nombre in ("gpsLocationExternal", "gpsLocation"):
      f = fuentes.get(nombre)
      if f is not None:
        return f
    return None

  # ------------------------------------------------------------------ viaje

  def _abrir_viaje(self, ahora: float) -> None:
    self.trip_id = secrets.token_hex(6)
    self._trip = {"t0": ahora, "dur": 0.0, "mov": 0.0, "dist": 0.0, "vmax": 0.0,
                  "eng": 0.0, "des": 0, "ev": 0}

  def _resumen_viaje(self) -> dict:
    t = self._trip or {}
    dur = t.get("dur", 0.0)
    mov = t.get("mov", 0.0)
    dist_km = t.get("dist", 0.0) / 1000.0
    v_med = (t.get("dist", 0.0) / mov * MS_A_KMH) if mov > 1.0 else 0.0
    return {
      "ev": "end",
      "dur_s": int(round(dur)),
      "mov_s": int(round(mov)),
      "dist_km": round(dist_km, 2),
      "v_max_kph": round(t.get("vmax", 0.0) * MS_A_KMH, 1),
      "v_med_kph": round(v_med, 1),
      "engaged_s": int(round(t.get("eng", 0.0))),
      "engaged_pct": int(round(100.0 * t.get("eng", 0.0) / dur)) if dur > 1.0 else 0,
      "n_desenganches": int(t.get("des", 0)),
      "n_eventos": int(t.get("ev", 0)),
    }

  def _acumular_viaje(self, dt: float, fuentes: dict) -> None:
    if self._trip is None or dt <= 0.0:
      return
    self._trip["dur"] += dt
    cs = fuentes.get("carState")
    v = _num(_leer(cs, "vEgo")) if cs is not None else None
    if v is not None:
      if v > V_MOVIMIENTO_MS:
        self._trip["mov"] += dt
        self._trip["dist"] += v * dt
      self._trip["vmax"] = max(self._trip["vmax"], v)
    ss = fuentes.get("selfdriveState")
    activo = bool(_leer(ss, "active")) if ss is not None else False
    if activo:
      self._trip["eng"] += dt
    # Un desenganche solo cuenta si el coche se estaba moviendo: apagar openpilot al
    # aparcar no es una intervencion y contarlo hace inutil la metrica.
    if self._activo_prev and not activo and (v or 0.0) > V_MOVIMIENTO_MS:
      self._trip["des"] += 1
    self._activo_prev = activo

  # ------------------------------------------------------------------ eventos

  def _recoger_eventos(self, fuentes: dict) -> None:
    """Calcula los flancos de onroadEvents/onroadEventsSP y los deja pendientes de envio."""
    presentes = False
    activos: set = set()
    for nombre, ruta in FUENTES_EVENTO.items():
      fuente = fuentes.get(nombre)
      if fuente is None:
        continue
      presentes = True
      lista = _leer(fuente, ruta)
      if lista is None:
        continue
      try:
        for ev in lista:
          n = _leer(ev, "name")
          if n is None:
            continue
          activos.add((str(n), severidad_evento(ev)))
      except TypeError:
        continue
    if not presentes:
      # Sin fuente viva NO se deducen bajadas: hacerlo publicaria un "off" de todos los
      # eventos cada vez que se pierde un ciclo de onroadEvents.
      return
    for nom, sev in sorted(activos - self._ev_activos):
      self._ev_pendientes.append({"cod": codigo_evento(nom), "nom": nom, "sev": sev, "on": True})
      if self._trip is not None and SEVERIDADES.index(sev) >= SEVERIDADES.index(SEV_MIN_RESUMEN):
        self._trip["ev"] += 1
    for nom, sev in sorted(self._ev_activos - activos):
      self._ev_pendientes.append({"cod": codigo_evento(nom), "nom": nom, "sev": sev, "on": False})
    self._ev_activos = activos

  # ------------------------------------------------------------------ pos

  @staticmethod
  def _pos_cruda(gps) -> dict:
    """Valores de posicion SIN pasar por la lista blanca.

    La decimacion decide sobre fisica (metros, grados de rumbo, metros de error), no sobre
    la representacion del cable. Mirar el payload ya construido seria un error sutil: un
    rumbo de 0 grados coincide con su defecto declarado y se omite, asi que el criterio de
    curvatura se quedaria ciego justo cuando el coche va recto y empieza a girar.
    """
    return {
      "lat": _num(_leer(gps, "latitude")),
      "lon": _num(_leer(gps, "longitude")),
      "rumbo": _num(_leer(gps, "bearingDeg")),
      "acc": _num(_leer(gps, "horizontalAccuracy")),
      "fix": bool(_leer(gps, "hasFix")),
    }

  def _pos_debida(self, canal: Canal, ahora: float, crudo: dict) -> bool:
    ultimo = self._ultimo_envio.get(canal.nombre)
    keepalive = canal.keepalive(self.perfil)
    if ultimo is None:
      return True
    transcurrido = ahora - ultimo
    if transcurrido >= keepalive:
      return True                      # techo: la traza no tiene huecos sin explicacion
    if transcurrido < canal.periodo(self.perfil):
      return False                     # suelo de ritmo
    if not crudo["fix"]:
      return False                     # sin fix solo sale el keepalive
    acc = crudo["acc"]
    if acc is not None and acc > ACC_INUTIL_M:
      return False
    lat, lon = crudo["lat"], crudo["lon"]
    if lat is None or lon is None or self._pos_ultima is None:
      return True
    lat0, lon0, rumbo0 = self._pos_ultima
    rumbo = crudo["rumbo"]
    if rumbo is not None and rumbo0 is not None and _delta_rumbo(rumbo, rumbo0) >= UMBRAL_RUMBO_DEG:
      return True                      # curvatura: la muestra que salva la curva
    minimo = DIST_MIN_M.get(self.perfil, 50.0)
    if acc is not None and acc > ACC_MALA_M:
      minimo *= 3.0
    return _dist_m(lat0, lon0, lat, lon) >= minimo

  # ------------------------------------------------------------------ tick

  def tick(self, ahora: float, fuentes: dict, ts_ms: int | None = None) -> list:
    """Un ciclo. `fuentes` = {servicio cereal: lector o None}. Devuelve [(canal, sobre)].

    No hace I/O, no captura excepciones del llamante y no bloquea: el hilo que la llama es
    el mismo que publica, y el emisor corre en el proceso de telemetria, no en controlsd.
    """
    if ts_ms is None:
      ts_ms = ahora_epoch_ms()
    dt = max(0.0, min(ahora - self._t_prev, 5.0))   # tope: tras una pausa larga no se integra basura
    self._t_prev = ahora
    # `revertir` solo puede deshacer lo sellado en ESTE tick: un sello de hace tres ciclos
    # ya se dio por bueno y deshacerlo republicaria un estado viejo.
    self._hash_previo.clear()

    ds = fuentes.get("deviceState")
    metered = bool(_leer(ds, "networkMetered")) if ds is not None else False
    self.perfil = self._resolver_perfil(ahora, metered)

    fuentes = dict(fuentes)
    fuentes["gps"] = self._fuente_gps(fuentes)

    salida: list = []

    # --- viaje: apertura y cierre mandan sobre todo lo demas, porque el trip_id que
    # --- llevan los sobres del resto del ciclo depende de ellos.
    onroad = bool(_leer(ds, "started")) if ds is not None else self._onroad
    recien_abierto = onroad and not self._onroad
    if recien_abierto:
      self._abrir_viaje(ahora)
      salida.append(self._mensaje("trip", ahora, ts_ms, {"ev": "start"}))
    self._onroad = onroad
    if onroad and not recien_abierto:
      # En el tick que ABRE el viaje no se acumula: ese `dt` es tiempo de ANTES del viaje
      # (offroad, o el hueco desde que arranco el proceso) y se colaria entero en dur_s.
      self._acumular_viaje(dt, fuentes)

    # --- eventos
    self._recoger_eventos(fuentes)
    if self._ev_pendientes and self._due("event", ahora, CANAL_EVENT.periodo(self.perfil)):
      salida.append(self._mensaje("event", ahora, ts_ms, {"ev": self._ev_pendientes}))
      self._ev_pendientes = []

    # --- canales periodicos y on-change
    for canal in (CANAL_VEHICLE, CANAL_PERCEPTION, CANAL_HEALTH, CANAL_OPENPILOT, CANAL_ROAD):
      periodo = canal.periodo(self.perfil)
      if not self._due(canal.nombre, ahora, periodo):
        continue
      if any(fuentes.get(f) is None for f in canal.requiere):
        continue
      datos = extraer(canal, fuentes, self.perfil)
      if canal.disparo == "cambio":
        h = self._huella(datos)
        vencido = (ahora - self._ultimo_envio.get(canal.nombre, -math.inf)) >= canal.keepalive(self.perfil)
        if h == self._ultimo_hash.get(canal.nombre) and not vencido:
          continue
        # Se guarda el sello ANTERIOR antes de pisarlo: el llamante puede tener que
        # deshacerlo si el publish no sale (ver `revertir`).
        self._hash_previo[canal.nombre] = self._ultimo_hash.get(canal.nombre)
        self._ultimo_hash[canal.nombre] = h
      elif not datos:
        continue
      # OJO al orden: en un canal on-change un payload VACIO es informacion (todo volvio a
      # su defecto declarado) y hay que publicarlo. Descartarlo por vacio, como se hace en
      # los periodicos, dejaria al consumidor pintando el ultimo estado NO por defecto --
      # justo el caso de openpilot pasando de "actuando" a "apagado".
      salida.append(self._mensaje(canal.nombre, ahora, ts_ms, datos))

    # --- pos (decimacion adaptativa)
    gps = fuentes.get("gps")
    if gps is not None and not math.isinf(CANAL_POS.periodo(self.perfil)):
      crudo = self._pos_cruda(gps)
      datos = extraer(CANAL_POS, fuentes, self.perfil)
      if not crudo["fix"] and crudo["lat"] == 0.0 and crudo["lon"] == 0.0:
        # (0,0) sin fix no es una posicion: es lo que manda el receptor mientras no la tiene
        # (el ublox del comma 4 en un garaje). Publicarlo pinta el coche en el golfo de
        # Guinea. El keepalive sigue saliendo con fix=false: "vivo y sin posicion".
        datos.pop("lat", None)
        datos.pop("lon", None)
      if datos and self._pos_debida(CANAL_POS, ahora, crudo):
        if crudo["lat"] is not None and crudo["lon"] is not None:
          self._pos_ultima = (crudo["lat"], crudo["lon"], crudo["rumbo"])
        salida.append(self._mensaje("pos", ahora, ts_ms, datos))

    # --- cierre de viaje al final: el resumen se sella con el mismo trip_id que el resto
    if not onroad and self._trip is not None:
      salida.append(self._mensaje("trip", ahora, ts_ms, self._resumen_viaje()))
      self._trip = None
      self.trip_id = None
      # Los eventos de la sesion se olvidan SIN publicar bajadas: el cierre de viaje ya
      # dice que la sesion termino, y 20 flancos "off" en el mismo instante son ruido.
      self._ev_activos = set()
      self._ev_pendientes = []
      self._activo_prev = False

    return salida
