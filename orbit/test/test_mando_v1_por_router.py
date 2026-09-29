"""El mando v1 legacy pasa por los MISMOS gates que el v2 (seccion 13 de la migracion).

Estos tests existen por una razon concreta: mientras convivan los dos namespaces, el v1
es el camino que un atacante -- o un backend viejo -- va a usar. Si el dispatch v1
escribiera los Params del actuador por su cuenta, todo el contrato v2 (modo, gates, TTL,
idempotencia, ACK) seria decorativo: bastaria publicar en telemetry_config/<dongle>/left
para saltarselo entero. Aqui se comprueba, comando a comando, que NO existe ese atajo.

Lo que se fija:

  * un mando v1 con los gates en ROJO no toca ningun Param y contesta GATE_<nombre>;
  * un mando v1 con los gates en VERDE recorre las cinco fases del ACK y aplica;
  * la CANCELACION v1 (bajar autoridad) nunca se bloquea, ni por modo ni por gates;
  * los verbos retirados de la seccion 6 contestan UNSUPPORTED_VERB en vez de callarse;
  * el modo de torque exige armado FISICO de banco tambien por la puerta v1;
  * sin GateMonitor vivo (mascara rancia) NO se ejecuta nada.
"""
import json
import os

import pytest

from openpilot.orbit.command_gates import GateMonitor
from openpilot.orbit.command_spec import TOPIC_CAPS, TOPIC_CMD, Gate, Mode, Phase, ahora_epoch_ms
from openpilot.orbit.command_state import (PARAM_BENCH_ARMED, PARAM_BENCH_EXPIRY, PARAM_MODE,
                                           CommandStateStore)
from openpilot.orbit import mqtt_comandos
from openpilot.orbit.mqtt_comandos import MQTTComandos
from openpilot.system.hardware import HARDWARE

DONGLE = "0123456789abcdef"
V1 = f"telemetry_config/{DONGLE}"
V2 = TOPIC_CMD.format(DONGLE)


# --------------------------------------------------------------------------- dobles

class _ParamsFalsos:
  """Doble de Params: dict en memoria que respeta que una clave desconocida lance."""

  def __init__(self, valores=None, desconocidos=()):
    self.valores = dict(valores or {})
    self.desconocidos = set(desconocidos)

  def _check(self, key):
    if key in self.desconocidos:
      raise KeyError(key)  # equivalente a params_pyx.UnknownKeyName

  def get(self, key, *args, **kwargs):
    self._check(key)
    return self.valores.get(key)

  def get_bool(self, key, *args, **kwargs):
    self._check(key)
    return bool(self.valores.get(key, False))

  def put(self, key, value, *args, **kwargs):
    self._check(key)
    self.valores[key] = value

  def put_bool(self, key, value, *args, **kwargs):
    self._check(key)
    self.valores[key] = bool(value)

  def remove(self, key):
    self.valores.pop(key, None)


class _GatesFalsos:
  """Doble del GateMonitor con la superficie que usan el router y el descriptor."""

  def __init__(self, verde=True, clock=True, brand="ford", platform="FORD_FOCUS_MK4"):
    self.verde = verde
    self.clock_synced = clock
    self.brand = brand
    self.platform = platform
    self.stale = False
    self.mask = 0xFFFF if verde else 0

  def note_link(self, cuando_mono=None):
    pass

  def evaluate(self, spec, args=None):
    if self.verde:
      return True, []
    return False, [g for g in Gate if int(spec.gates) & g]


class _PlanoFalso:
  def __init__(self, gates, store):
    self.gates = gates
    self.store = store
    self.on_disarm = None

  def set_on_disarm(self, cb):
    self.on_disarm = cb


class _ClienteFalso:
  def __init__(self):
    self.publicados = []

  def publish(self, topic, payload, qos=0, retain=False):
    self.publicados.append((topic, payload, qos, retain))


class _Mensaje:
  def __init__(self, topic, payload, retain=False):
    self.topic = topic
    self.payload = payload.encode() if isinstance(payload, str) else payload
    self.retain = retain


# --------------------------------------------------------------------------- utiles

def _comandos(modo=Mode.MANEUVER, verde=True, clock=True, banco=False, params=None, gates=None):
  """MQTTComandos real (sin __init__: ni broker ni disco) con router real y plano falso."""
  valores = {PARAM_MODE: int(modo), PARAM_BENCH_ARMED: banco}
  if banco:
    valores[PARAM_BENCH_EXPIRY] = str(ahora_epoch_ms() + 300_000)
  p = params if params is not None else _ParamsFalsos(valores)
  for k, v in valores.items():
    p.valores.setdefault(k, v)

  store = CommandStateStore(params=p)
  store.refresh_params(forzar=True)
  gates = gates if gates is not None else _GatesFalsos(verde=verde, clock=clock)

  c = MQTTComandos.__new__(MQTTComandos)
  c.params = p
  c.DongleID = DONGLE
  c.dongle_valido = True
  c.conectado = True
  c.camera_sender = None
  c.plane = _PlanoFalso(gates, store)
  c.router = None
  c._fw = "0.0.0"
  c._caps_huella = None
  c.mqttc = _ClienteFalso()
  c.init_router()
  # El worker se para: los tests drenan la cola a mano para no depender de la carrera
  # entre el hilo del test y el del router.
  c.router.stop()
  return c


def _acks(c):
  """(fase, motivo) de cada ACK publicado, en orden."""
  fuera = []
  for topic, payload, _, _ in c.mqttc.publicados:
    if "/ack/" not in topic:
      continue
    d = json.loads(payload)
    fuera.append((d["phase"], d["reason"]))
  return fuera


def _entregar(c, topic, payload, retain=False):
  c.on_message(None, None, _Mensaje(topic, payload, retain))
  return c.router.drenar()


@pytest.fixture(autouse=True)
def _sin_globales_de_velocidad():
  """El ejecutor de cruise_delta toca las globales del modulo de velocidad (fast-path del
  mismo proceso). Se limpian para no contaminar otros tests."""
  yield
  import openpilot.orbit.orbit_speed_ultra_simple as sp
  sp.orbit_speed_increase = False
  sp.orbit_speed_decrease = False


# ------------------------------------------------ el v1 atraviesa los gates del router

def test_lane_change_v1_con_gates_en_rojo_no_toca_ningun_param():
  """LA prueba de esta tanda. Antes, este payload escribia ForceLaneChangeLeft
  directamente desde el callback de paho, sin mirar si openpilot estaba enganchado, a que
  velocidad iba el coche ni si habia alguien en el asiento."""
  c = _comandos(verde=False)
  ejecutados = _entregar(c, f"{V1}/left", '{"ForceLaneChangeLeft": true}')
  assert ejecutados == 0
  assert "ForceLaneChangeLeft" not in c.params.valores
  fases = _acks(c)
  assert (Phase.RECEIVED, "") in fases
  assert any(f == Phase.REJECTED and m.startswith("GATE_") for f, m in fases), fases


def test_lane_change_v1_recorre_las_mismas_fases_que_el_v2():
  """El camino legacy no tiene un ciclo de vida propio: pasa por el mismo router y se
  queda igualmente en EXECUTING esperando el veredicto de desire_helper."""
  c = _comandos(verde=True, modo=Mode.MANEUVER)
  assert _entregar(c, f"{V1}/left", '{"ForceLaneChangeLeft": true}') == 1
  assert c.params.valores["ForceLaneChangeLeft"] is True
  assert c.params.valores["ForceLaneChangeRight"] is False
  assert [f for f, _ in _acks(c)] == [Phase.RECEIVED, Phase.ACCEPTED, Phase.EXECUTING]


def test_lane_change_v1_en_modo_observador_da_MODE():
  """El modo tampoco se lo puede saltar el v1: el catalogo pide 'maniobra'."""
  c = _comandos(modo=Mode.OBSERVER, verde=True)
  assert _entregar(c, f"{V1}/right", '{"ForceLaneChangeRight": true}') == 0
  assert "ForceLaneChangeRight" not in c.params.valores
  assert (Phase.REJECTED, "MODE") in _acks(c)


def test_lane_change_v1_con_el_reloj_sin_sincronizar_da_CLOCK():
  c = _comandos(clock=False)
  assert _entregar(c, f"{V1}/left", '{"ForceLaneChangeLeft": true}') == 0
  assert "ForceLaneChangeLeft" not in c.params.valores
  assert (Phase.REJECTED, "CLOCK") in _acks(c)


def test_la_cadena_false_del_v1_sigue_sin_disparar_un_giro():
  """El bug historico: bool('false') es True. Ahora ademas 'false' significa CANCELAR."""
  c = _comandos(verde=True)
  _entregar(c, f"{V1}/left", 'false')
  assert c.params.valores.get("ForceLaneChangeLeft") is not True


def test_basura_v1_no_llega_ni_al_router():
  c = _comandos(verde=True)
  for basura in ('{"foo": 1}', 'lorem ipsum', '[]', '"x"', 'null', '0'):
    _entregar(c, f"{V1}/left", basura)
  assert "ForceLaneChangeLeft" not in c.params.valores
  assert _acks(c) == []          # ni siquiera se fabrica sobre: la lista blanca lo corta


# ---------------------------------------------------- bajar autoridad nunca se bloquea

def test_la_cancelacion_v1_es_disarm_all_y_no_la_paran_ni_el_modo_ni_los_gates():
  """Seccion 2: bajar autoridad siempre se acepta. Si la cancelacion viajara como
  'lane_change con false', un gate en rojo la rechazaria -- justo cuando mas falta hace."""
  p = _ParamsFalsos({"ForceLaneChangeLeft": True, "ForceLaneChangeRight": True,
                     "brutebreak_active": True, "SteerTorqueMode": 2})
  c = _comandos(modo=Mode.OBSERVER, verde=False, clock=False, params=p)
  assert _entregar(c, f"{V1}/left", 'false') == 1
  assert p.valores["ForceLaneChangeLeft"] is False
  assert p.valores["ForceLaneChangeRight"] is False
  assert p.valores["brutebreak_active"] is False
  assert p.valores["SteerTorqueMode"] == 0
  assert (Phase.APPLIED, "OK") in _acks(c)


def test_el_toggle_v1_cancela_si_el_sentido_opuesto_estaba_armado():
  p = _ParamsFalsos({"ForceLaneChangeRight": True})
  c = _comandos(verde=True, params=p)
  assert _entregar(c, f"{V1}/left", 'true') == 1
  # disarm_all solo escribe lo que estaba armado, asi que el sentido que nunca se armo
  # sigue sin existir en Params. Lo importante es que NO se armo ninguno de los dos.
  assert p.valores.get("ForceLaneChangeLeft") is not True
  assert p.valores["ForceLaneChangeRight"] is False


def test_el_toggle_v1_activa_si_no_habia_nada_armado():
  c = _comandos(verde=True)
  assert _entregar(c, f"{V1}/right", 'true') == 1
  assert c.params.valores["ForceLaneChangeRight"] is True


# --------------------------------------------------------------------- cruise_delta

def test_speed_up_v1_es_cruise_delta_y_gasta_presupuesto():
  """El tope por orden (+-5 km/h) no sirve de nada si se encadenan veinte ordenes en dos
  segundos: el presupuesto de +-20 km/h por minuto lo aplica el router, tambien al v1."""
  c = _comandos(modo=Mode.COPILOT, verde=True)
  aplicados = 0
  for _ in range(6):
    aplicados += _entregar(c, f"{V1}/speed_up", '{"speed_up": true}')
    c.params.valores["orbit_speed_increase"] = False
  assert aplicados == 4                      # 4 x 5 km/h = 20; la quinta agota el presupuesto
  assert (Phase.REJECTED, "RANGE") in _acks(c)


def test_speed_down_v1_manda_el_signo_correcto():
  c = _comandos(modo=Mode.COPILOT, verde=True)
  assert _entregar(c, f"{V1}/speed_down", '-1') == 1
  assert c.params.valores["orbit_speed_decrease"] is True
  assert "orbit_speed_increase" not in c.params.valores


def test_speed_up_v1_con_los_gates_en_rojo_no_mueve_el_crucero():
  c = _comandos(modo=Mode.COPILOT, verde=False)
  assert _entregar(c, f"{V1}/speed_up", '1') == 0
  assert "orbit_speed_increase" not in c.params.valores


# ------------------------------------------------------------------- assisted_decel

def test_brutebreak_v1_fuera_de_rango_se_acota_al_contrato():
  """v1 aceptaba [-10,-1] sin modo, sin gates y sin TTL. El contrato lo acota a
  [-2.5,-1.0]: se aplica MENOS autoridad de la pedida, nunca mas."""
  c = _comandos(modo=Mode.MANEUVER, verde=True)
  assert _entregar(c, f"{V1}/brutebreak", '{"brutebreak": true, "intensidad_frenado": -8.0}') == 1
  assert c.params.valores["brutebreak_intensidad"] == -2.5
  assert c.params.valores["brutebreak_active"] is True


def test_brutebreak_v1_dentro_de_rango_se_respeta():
  c = _comandos(modo=Mode.MANEUVER, verde=True)
  assert _entregar(c, f"{V1}/brutebreak", '{"brutebreak": true, "intensidad_frenado": -1.5}') == 1
  assert c.params.valores["brutebreak_intensidad"] == -1.5


def test_brutebreak_v1_en_modo_copiloto_da_MODE():
  c = _comandos(modo=Mode.COPILOT, verde=True)
  assert _entregar(c, f"{V1}/brutebreak", 'true') == 0
  assert "brutebreak_active" not in c.params.valores
  assert (Phase.REJECTED, "MODE") in _acks(c)


def test_brutebreak_v1_apagado_es_disarm_all():
  p = _ParamsFalsos({"brutebreak_active": True})
  c = _comandos(modo=Mode.OBSERVER, verde=False, params=p)
  assert _entregar(c, f"{V1}/brutebreak", 'false') == 1
  assert p.valores["brutebreak_active"] is False


# ------------------------------------------------------------ verbos retirados (§6)

@pytest.mark.parametrize("payload", ['{"forward": true}', '{"break": true}', 'tright', 'tleft'])
def test_la_cruceta_v1_contesta_UNSUPPORTED_VERB(payload):
  """Un boton que no responde es indistinguible de un coche que no esta: la cruceta
  retirada contesta con su nombre en vez de callarse."""
  c = _comandos(verde=True)
  assert _entregar(c, f"{V1}/control", payload) == 0
  assert (Phase.REJECTED, "UNSUPPORTED_VERB") in _acks(c)
  assert c.params.valores.get("orbit_speed_increase") is not True


def test_overtake_v1_activo_contesta_UNSUPPORTED_VERB():
  """El adelantamiento v1 armaba un cambio de carril a la izquierda sin maquina de
  estados: ni volvia al carril, ni miraba el trafico de frente, ni si habia carril."""
  c = _comandos(verde=True)
  assert _entregar(c, f"{V1}/overtake", '{"enabled": true}') == 0
  assert "ForceLaneChangeLeft" not in c.params.valores
  assert (Phase.REJECTED, "UNSUPPORTED_VERB") in _acks(c)


def test_overtake_v1_apagado_si_desarma():
  p = _ParamsFalsos({"sic_adelantar": True, "ForceLaneChangeLeft": True})
  c = _comandos(modo=Mode.OBSERVER, verde=False, params=p)
  assert _entregar(c, f"{V1}/overtake", '{"enabled": false}') == 1
  assert p.valores["ForceLaneChangeLeft"] is False
  assert p.valores["sic_adelantar"] is False


# -------------------------------------------------------------------- torque_mode

@pytest.mark.parametrize("modo", [1, 2, 3])
def test_steer_torque_mode_v1_exige_armado_fisico_de_banco(modo):
  """Los tres modos mueven el volante: el 1 se lo da a la Jetson, el 2 pone par maximo y
  el 3 deja sumar offsets de esquive. Los tres son modo BANCO."""
  c = _comandos(modo=Mode.BENCH, banco=False, verde=True)
  payload = json.dumps({"steer_torque_mode": modo, "source": "app", "apply_target": "curvature"})
  assert _entregar(c, f"{V1}/steer_torque_mode", payload) == 0
  assert c.params.valores.get("SteerTorqueMode") != modo
  assert (Phase.REJECTED, "MODE") in _acks(c)


def test_steer_torque_mode_v1_con_banco_armado_se_aplica():
  c = _comandos(modo=Mode.BENCH, banco=True, verde=True)
  payload = json.dumps({"steer_torque_mode": 2, "source": "app"})
  assert _entregar(c, f"{V1}/steer_torque_mode", payload) == 1
  assert c.params.valores["SteerTorqueMode"] == 2


def test_steer_torque_mode_v1_modo_0_es_disarm_all_y_no_exige_banco():
  """Si apagar el TEST MAX exigiera banco, un modo 2 armado por error no se podria
  apagar en remoto."""
  p = _ParamsFalsos({"SteerTorqueMode": 2})
  c = _comandos(modo=Mode.OBSERVER, banco=False, verde=False, params=p)
  payload = json.dumps({"steer_torque_mode": 0, "source": "app"})
  assert _entregar(c, f"{V1}/steer_torque_mode", payload) == 1
  assert p.valores["SteerTorqueMode"] == 0


def test_steer_torque_mode_v1_retenido_solo_acepta_el_modo_0():
  c = _comandos(modo=Mode.BENCH, banco=True, verde=True)
  payload = json.dumps({"steer_torque_mode": 2, "source": "app"})
  assert _entregar(c, f"{V1}/steer_torque_mode", payload, retain=True) == 0
  assert "SteerTorqueMode" not in c.params.valores
  assert _acks(c) == []          # ni ACK: el retenido se corta antes de fabricar sobre


def test_steer_torque_mode_v1_de_otro_dongle_se_descarta():
  c = _comandos(modo=Mode.BENCH, banco=True, verde=True)
  payload = json.dumps({"dongle_id": "otro", "steer_torque_mode": 2, "source": "app"})
  assert _entregar(c, f"{V1}/steer_torque_mode", payload) == 0
  assert "SteerTorqueMode" not in c.params.valores


def test_steer_torque_mode_v1_eco_del_propio_comma_se_ignora():
  c = _comandos(modo=Mode.BENCH, banco=True, verde=True)
  payload = json.dumps({"steer_torque_mode": 2, "source": "comma_ui"})
  assert _entregar(c, f"{V1}/steer_torque_mode", payload) == 0
  assert _acks(c) == []


def test_steer_torque_mode_v1_modo_3_sin_apply_target_se_descarta():
  c = _comandos(modo=Mode.BENCH, banco=True, verde=True)
  payload = json.dumps({"steer_torque_mode": 3, "source": "app"})
  assert _entregar(c, f"{V1}/steer_torque_mode", payload) == 0
  assert "SteerTorqueMode" not in c.params.valores


def test_steer_torque_mode_v1_modo_3_con_apply_target_se_aplica():
  c = _comandos(modo=Mode.BENCH, banco=True, verde=True)
  payload = json.dumps({"steer_torque_mode": 3, "source": "app", "apply_target": "torque"})
  assert _entregar(c, f"{V1}/steer_torque_mode", payload) == 1
  assert c.params.valores["SteerTorqueMode"] == 3
  assert c.params.valores["JetsonObstacleApplyTarget"] == "torque"


# ------------------------------------------------------- GateMonitor muerto o rancio

def test_un_gatemonitor_que_no_tica_deja_todo_en_rojo():
  """REGLA DURA: si el GateMonitor muere o sus datos estan rancios, los gates se
  consideran EN ROJO. Un GateMonitor real al que nadie ha llamado a update() nunca ha
  mirado el coche."""
  gm = GateMonitor(sm=None, servicios=())
  assert gm.stale
  c = _comandos(modo=Mode.MANEUVER, gates=gm)
  assert _entregar(c, f"{V1}/left", '{"ForceLaneChangeLeft": true}') == 0
  assert "ForceLaneChangeLeft" not in c.params.valores
  assert (Phase.REJECTED, "INTERNAL") in _acks(c)


def test_una_mascara_rancia_no_se_lee_aunque_estuviera_en_verde():
  """Se congela una mascara entera en verde y se declara vencida: no puede autorizar."""
  gm = GateMonitor(sm=None, servicios=(), mask_max_age_s=-1.0)
  gm._mask = 0xFFFF
  gm._ultimo_update_mono = 1.0
  assert gm.stale
  assert gm.mask == 0
  assert gm.clock_synced is False
  ok, fallados = gm.evaluate(type("S", (), {"gates": Gate.ENGAGED | Gate.LAT_ACTIVE, "limits": {}})())
  assert not ok and len(fallados) == 2


def test_el_plano_no_esta_sano_si_su_hilo_no_tica():
  from openpilot.orbit.command_state import CommandPlane
  plano = CommandPlane(params=_ParamsFalsos(), gates=GateMonitor(sm=None, servicios=()))
  assert not plano.healthy()          # ni siquiera arrancado
  assert not plano.is_alive()


# ------------------------------------------------------------------ namespace v2

def test_un_mando_v2_va_al_router_por_su_topic():
  c = _comandos(modo=Mode.MANEUVER, verde=True)
  sobre = json.dumps({"v": 2, "id": "abc", "seq": 1, "verb": "lane_change",
                      "args": {"direction": "right"}, "ts_ms": ahora_epoch_ms(),
                      "ttl_ms": 3000, "mode": "maniobra"})
  assert _entregar(c, V2, sobre) == 1
  assert c.params.valores["ForceLaneChangeRight"] is True


def test_un_mando_v2_retenido_se_descarta_sin_ack():
  c = _comandos(modo=Mode.MANEUVER, verde=True)
  sobre = json.dumps({"v": 2, "id": "abc", "seq": 1, "verb": "lane_change",
                      "args": {"direction": "right"}, "ts_ms": ahora_epoch_ms()})
  assert _entregar(c, V2, sobre, retain=True) == 0
  assert _acks(c) == []
  assert "ForceLaneChangeRight" not in c.params.valores


def test_un_mando_v2_de_otro_dongle_se_descarta():
  c = _comandos(modo=Mode.MANEUVER, verde=True)
  sobre = json.dumps({"v": 2, "id": "abc", "seq": 1, "verb": "lane_change",
                      "args": {"direction": "right"}, "ts_ms": ahora_epoch_ms()})
  assert _entregar(c, TOPIC_CMD.format("otrodongle"), sobre) == 0
  assert _acks(c) == []


def test_sin_router_ningun_mando_escribe_nada():
  """Fail-closed: si el router no se pudo construir, el mando queda MUDO. Lo contrario
  seria volver a tener un camino que escribe Params sin que nadie mire los gates."""
  c = _comandos(verde=True)
  c.router = None
  for topic, payload in ((f"{V1}/left", '{"ForceLaneChangeLeft": true}'),
                         (f"{V1}/speed_up", '1'),
                         (f"{V1}/brutebreak", 'true'),
                         (f"{V1}/steer_torque_mode", '{"steer_torque_mode": 2}'),
                         (f"{V1}/healthcheck", 'request'),
                         (V2, '{"v":2,"id":"x","verb":"lane_change","args":{"direction":"left"},"ts_ms":1}')):
    c.on_message(None, None, _Mensaje(topic, payload))
  for clave in ("ForceLaneChangeLeft", "orbit_speed_increase", "brutebreak_active",
                "SteerTorqueMode", "OrbitHealthcheckRequest"):
    assert clave not in c.params.valores, clave


# --------------------------------------------------------------------- capacidades

def test_las_capacidades_se_publican_retenidas_y_con_qos_1():
  c = _comandos(verde=True)
  assert c.maybe_publish_caps(forzar=True)
  caps = [(t, p, q, r) for t, p, q, r in c.mqttc.publicados if t == TOPIC_CAPS.format(DONGLE)]
  assert len(caps) == 1
  _, payload, qos, retain = caps[0]
  assert qos == 1 and retain is True
  d = json.loads(payload)
  assert d["v"] == 2
  assert d["brand"] == "ford"
  assert d["platform"] == "FORD_FOCUS_MK4"
  # Solo los verbos con ejecutor real; el resto va a unsupported con su motivo.
  assert set(d["verbs"]) == set(c._verbos())
  assert d["unsupported"]["overtake"] == "not_implemented"
  # cruise_button SI tiene ejecutor: es el boton de panico (cancelar crucero), el unico
  # mando grande que el diseno deja como accion de panico porque BAJA autoridad.
  assert "cruise_button" in d["verbs"]
  assert "cruise_button" not in d["unsupported"]
  # Y solo ofrece 'cancel': resume/set subirian autoridad y no tienen consumidor.
  assert d["verbs"]["cruise_button"]["args"]["button"]["choices"] == ["cancel"]
  # Modelo del comma (contrato C1): 'pc' en este PC, tici/tizi/mici en el coche.
  assert d["device_type"] == HARDWARE.get_device_type()
  if not os.path.isfile("/TICI"):
    assert d["device_type"] == "pc"


def test_si_no_se_puede_leer_el_modelo_las_capacidades_salen_sin_device_type(monkeypatch):
  def _roto():
    raise OSError("devicetree ilegible")
  mqtt_comandos.tipo_dispositivo.cache_clear()
  monkeypatch.setattr(HARDWARE, "get_device_type", _roto)
  try:
    c = _comandos(verde=True)
    assert c.maybe_publish_caps(forzar=True)
    d = json.loads(next(p for t, p, *_ in c.mqttc.publicados if t == TOPIC_CAPS.format(DONGLE)))
    assert "device_type" not in d and d["v"] == 2
  finally:
    mqtt_comandos.tipo_dispositivo.cache_clear()


def test_las_capacidades_no_se_republican_si_no_cambian():
  c = _comandos(verde=True)
  assert c.maybe_publish_caps(forzar=True)
  assert not c.maybe_publish_caps()
  c.plane.gates.platform = "FORD_KUGA_MK3"
  assert c.maybe_publish_caps()


def test_sin_dongle_valido_no_se_publican_capacidades():
  c = _comandos(verde=True)
  c.dongle_valido = False
  assert not c.maybe_publish_caps(forzar=True)


# --------------------------------------------------------------------- healthcheck

def test_healthcheck_v1_pasa_por_el_router_y_exige_modo_copiloto():
  c = _comandos(modo=Mode.OBSERVER, verde=True)
  assert _entregar(c, f"{V1}/healthcheck", 'request') == 0
  assert "OrbitHealthcheckRequest" not in c.params.valores
  assert (Phase.REJECTED, "MODE") in _acks(c)

  c = _comandos(modo=Mode.COPILOT, verde=True)
  assert _entregar(c, f"{V1}/healthcheck", 'request') == 1
  assert c.params.valores["OrbitHealthcheckRequest"]


# ---------------------------------------------------- topics v1 que NO son verbos

def test_los_topics_de_configuracion_no_pasan_por_el_router_y_no_mueven_nada():
  """jetson_config, camera_config, enroll_ack y speed_increment son configuracion e
  identidad. Ninguno escribe un actuador; se comprueba aqui para que la lista del
  docstring de on_message no se quede en una afirmacion."""
  c = _comandos(verde=True)
  _entregar(c, f"{V1}/speed_increment", '3')
  assert c.params.valores["orbit_speed_increment"] == 3.0
  _entregar(c, f"{V1}/enroll_ack", '{"claimed": true, "user_id": 7}')
  assert c.params.valores["OrbitClaimed"] is True
  # Ningun actuador tocado por el camino.
  for clave in ("ForceLaneChangeLeft", "ForceLaneChangeRight", "brutebreak_active",
                "SteerTorqueMode", "orbit_speed_increase", "orbit_speed_decrease"):
    assert clave not in c.params.valores, clave
  assert _acks(c) == []


# ------------------------------------------------------------- enrolamiento

class _ParamsContados(_ParamsFalsos):
  """Cuenta las escrituras de OrbitOwnerRole para fijar 'escribir solo si cambia'."""

  def __init__(self, *a, **k):
    super().__init__(*a, **k)
    self.escrituras_rol = 0

  def put(self, key, value, *args, **kwargs):
    self.escrituras_rol += key == "OrbitOwnerRole"
    super().put(key, value, *args, **kwargs)

  def remove(self, key):
    self.escrituras_rol += key == "OrbitOwnerRole" and key in self.valores
    super().remove(key)


def _ack(c, **campos):
  _entregar(c, f"{V1}/enroll_ack", json.dumps(campos), retain=True)


@pytest.mark.parametrize("rol", ["user", "developer", "superadmin"])
def test_el_enroll_ack_guarda_el_rol_del_dueno(rol):
  c = _comandos(params=_ParamsContados())
  _ack(c, claimed=True, user_id=7, user_name="Ana", user_role=rol)
  v = c.params.valores
  assert v["OrbitClaimed"] is True and v["OrbitOwner"] == "Ana" and v["OrbitOwnerRole"] == rol
  # El backend republica el ack retenido en cada reconexion: mismo rol, cero escrituras.
  _ack(c, claimed=True, user_id=7, user_name="Ana", user_role=rol)
  assert c.params.escrituras_rol == 1


@pytest.mark.parametrize("extra", [{}, {"user_role": "admin"}, {"user_role": None}, {"user_role": ["developer"]}])
def test_un_rol_ausente_o_desconocido_borra_el_anterior(extra):
  c = _comandos(params=_ParamsFalsos({"OrbitOwnerRole": "superadmin"}))
  _ack(c, claimed=True, user_id=7, user_email="a@b.c", **extra)
  v = c.params.valores
  assert "OrbitOwnerRole" not in v
  assert v["OrbitClaimed"] is True and v["OrbitOwner"] == "a@b.c"   # lo de siempre sigue igual


def test_claimed_false_borra_el_rol_junto_al_dueno():
  c = _comandos(params=_ParamsFalsos({"OrbitOwnerRole": "developer", "OrbitOwner": "Ana", "OrbitClaimed": True}))
  _ack(c, claimed=False)
  v = c.params.valores
  assert v["OrbitClaimed"] is False and v["OrbitEnrollRegen"] is True
  assert "OrbitOwner" not in v and "OrbitOwnerRole" not in v


def test_un_enroll_ack_sin_claimed_no_toca_el_rol():
  c = _comandos(params=_ParamsFalsos({"OrbitOwnerRole": "developer"}))
  _ack(c, user_role="user")
  assert c.params.valores["OrbitOwnerRole"] == "developer"


def test_el_anuncio_de_enrolamiento_lleva_el_modelo_real():
  from openpilot.orbit.mqtt_envio_general import MQTTEnvioGeneral
  e = object.__new__(MQTTEnvioGeneral)
  e.params = _ParamsFalsos({"Version": "0.0.0"})
  e.dongle_valido = True
  e.conectado = True
  e.DongleID = DONGLE
  e.mqttc = _ClienteFalso()
  e._pairing_code = None
  e._enroll_issued_at = 0.0
  e._last_enroll = 0.0
  e.ENROLL_TTL_S = 600
  e.ENROLL_ANNOUNCE_SECS = 30.0
  e._maybe_announce_enroll()
  (topic, payload, _, retain), = e.mqttc.publicados
  d = json.loads(payload)
  assert topic == f"telemetry_mqtt/{DONGLE}/enroll" and retain is False
  assert d["hw"] == HARDWARE.get_device_type() and d["hw"] != "comma3x"
  assert d["pairing_code"] == e.params.valores["OrbitPairingCode"]


# --------------------------------------------------- forma del sobre y del ACK

def test_el_puente_v1_fabrica_un_sobre_v2_de_verdad():
  """No es "llamar al handler saltandose el sobre": se construye el sobre de la seccion
  3.2 y se mete por handle_payload, que es el mismo metodo que atiende la red."""
  c = _comandos(verde=True)
  visto = {}
  original = c.router.handle_payload

  def espia(topic, payload, retain=False, fuente="net"):
    visto["topic"] = topic
    visto["sobre"] = json.loads(payload)
    visto["retain"] = retain
    visto["fuente"] = fuente
    return original(topic, payload, retain, fuente)

  c.router.handle_payload = espia
  _entregar(c, f"{V1}/left", '{"ForceLaneChangeLeft": true}')

  assert visto["topic"] == V2
  assert visto["retain"] is False
  assert visto["fuente"] == "v1"
  sobre = visto["sobre"]
  assert sobre["v"] == 2
  assert sobre["verb"] == "lane_change"
  assert sobre["args"] == {"direction": "left"}
  assert sobre["id"].startswith("v1-")
  assert isinstance(sobre["ts_ms"], int)
  assert isinstance(sobre["seq"], int)
  # El emisor v1 no sabe en que modo esta el coche: declararlo seria mentir.
  assert sobre["mode"] is None
  assert sobre["actor"]["via"] == "v1"


def test_los_ack_van_a_su_topic_con_qos_1_y_sin_retain():
  c = _comandos(verde=True)
  _entregar(c, f"{V1}/left", '{"ForceLaneChangeLeft": true}')
  acks = [(t, q, r) for t, _, q, r in c.mqttc.publicados if "/ack/" in t]
  assert acks, c.mqttc.publicados
  for topic, qos, retain in acks:
    assert topic == f"orbit/v2/ack/{DONGLE}"
    assert qos == 1
    assert retain is False


def test_el_seq_del_puente_v1_no_pisa_al_del_backend():
  """`seq` es monotono POR DONGLE y lo lleva el backend. Si el puente compartiera su
  contador, uno de los dos acabaria rechazandose a si mismo con SUPERSEDED para siempre."""
  c = _comandos(modo=Mode.MANEUVER, verde=True)
  sobre = json.dumps({"v": 2, "id": "backend-1", "seq": 5000, "verb": "lane_change",
                      "args": {"direction": "left"}, "ts_ms": ahora_epoch_ms(),
                      "mode": "maniobra"})
  assert _entregar(c, V2, sobre) == 1
  # El v1 usa seq 1 y NO se descarta pese a ser mucho menor que 5000.
  assert _entregar(c, f"{V1}/right", '{"ForceLaneChangeRight": true}') == 1
  assert ("rejected", "SUPERSEDED") not in _acks(c)


def test_un_cruise_delta_por_debajo_del_paso_minimo_no_mueve_nada():
  """El consumidor acota el incremento a [1,5] km/h: pedir 0.4 y aplicar 1.0 seria
  ejecutar MAS de lo que se pidio."""
  c = _comandos(modo=Mode.COPILOT, verde=True)
  sobre = json.dumps({"v": 2, "id": "x1", "seq": 1, "verb": "cruise_delta",
                      "args": {"delta_kph": 0.4}, "ts_ms": ahora_epoch_ms()})
  _entregar(c, V2, sobre)
  assert "orbit_speed_increase" not in c.params.valores
  assert (Phase.FAILED, "INTERNAL") in _acks(c)


def test_un_verbo_inocuo_no_alarga_la_ventana_del_actuador():
  """El plano tiene UN solo deadline. Un healthcheck (TTL 30 s) llegado justo despues de
  un lane_change (TTL 3 s) no puede regalarle 27 segundos de autoridad."""
  c = _comandos(modo=Mode.MANEUVER, verde=True)
  assert _entregar(c, f"{V1}/left", '{"ForceLaneChangeLeft": true}') == 1
  tras_maniobra = c.plane.store.snapshot()["deadline_mono"]
  assert tras_maniobra > 0.0

  assert _entregar(c, f"{V1}/healthcheck", 'request') == 1
  tras_healthcheck = c.plane.store.snapshot()["deadline_mono"]
  assert tras_healthcheck == tras_maniobra


def test_un_verbo_que_si_arma_actuador_pone_su_propia_ventana():
  c = _comandos(modo=Mode.MANEUVER, verde=True)
  assert c.plane.store.snapshot()["deadline_mono"] == 0.0
  assert _entregar(c, f"{V1}/left", '{"ForceLaneChangeLeft": true}') == 1
  assert c.plane.store.snapshot()["deadline_mono"] > 0.0
  # Y disarm_all lo devuelve a cero AHORA: neutro inmediato.
  assert _entregar(c, f"{V1}/left", 'false') == 1
  assert c.plane.store.snapshot()["deadline_mono"] == 0.0
