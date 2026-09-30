"""El ACK de un verbo lo cierra el CONSUMIDOR, no el router (ALTA 2 de la auditoria).

Antes de esto, command_router publicaba APPLIED en cuanto el handler retornaba. Para
lane_change eso significaba anunciar "Hecho" en la app cuando lo unico cierto es que
habia un flag en disco: quien decide es desire_helper, que puede rechazarlo por sus
propios gates (velocidad, latActive, angulo muerto, pedales, cinturon...).

El canal de resultado estaba DOBLEMENTE roto: OrbitCmdResult no estaba registrado en
common/params_keys.h, y aunque lo estuviera nadie lo leia. Estos tests fijan las dos
mitades del arreglo.

cruise_delta tenia el mismo defecto: `applied` en cuanto habia flag en disco, y 17 de 78
medidos en el coche no movieron la consigna. Su bloque, al final, recorre la cadena real
handler -> card -> consumidor -> OrbitCmdResult -> router.
"""
import json
import threading
from collections import deque

import pytest

from openpilot.orbit.command_router import CommandRouter
from openpilot.orbit.command_spec import Gate, Mode, Phase, ahora_epoch_ms, ahora_mono, get_spec

DONGLE = "0123456789abcdef"
TOPIC = f"orbit/v2/cmd/{DONGLE}"



def _router(verbo="lane_change"):
  # Se reutiliza el doble de plano de estado de la bateria de la seccion 12 en vez de
  # escribir otro: dos dobles distintos del mismo objeto acaban divergiendo.
  from openpilot.orbit.test.test_command_router_v2 import _GatesFalsos, _store
  publicados = []
  ejecutados = []
  r = CommandRouter(DONGLE, gates=_GatesFalsos(), store=_store(Mode.MANEUVER),
                    publish=lambda t, p, q, ret: publicados.append((t, json.loads(p), q, ret)))
  r.register_handler(verbo, lambda cmd: ejecutados.append(cmd))
  return r, publicados, ejecutados


def _sobre(verb="lane_change", args=None, ttl_ms=3000):
  return json.dumps({
    "v": 2, "id": f"test-{verb}", "seq": 1, "verb": verb,
    "args": args if args is not None else {"direction": "left"},
    "ts_ms": ahora_epoch_ms(), "mono_ms": int(ahora_mono() * 1000),
    "ttl_ms": ttl_ms, "mode": "maniobra", "actor": {"user_id": 1, "via": "api"},
  })


def _fases(pub):
  return [p["phase"] for _, p, _, _ in pub]


def test_la_tabla_declara_que_lane_change_lo_cierra_el_consumidor():
  assert get_spec("lane_change").cierra_consumidor is True
  # Un verbo que se agota en escribir su Param NO espera a nadie.
  assert get_spec("disarm_all").cierra_consumidor is False


def test_sin_veredicto_el_comando_se_queda_en_executing():
  r, pub, ejec = _router()
  r.handle_payload(TOPIC, _sobre())
  r.drenar()
  assert len(ejec) == 1, "el handler tiene que haberse ejecutado"
  assert _fases(pub)[-1] == Phase.EXECUTING
  assert Phase.APPLIED not in _fases(pub), "APPLIED sin que el consumidor haya confirmado"


def test_el_veredicto_del_consumidor_cierra_con_applied():
  r, pub, _ = _router()
  r.handle_payload(TOPIC, _sobre())
  r.drenar()
  r._cerrar_con_resultado({"v": 2, "verb": "lane_change", "id": "test-lane_change",
                           "phase": "applied", "reason": "OK", "detail": ""})
  assert _fases(pub)[-1] == Phase.APPLIED


def test_el_rechazo_del_consumidor_cierra_con_failed_y_su_motivo():
  r, pub, _ = _router()
  r.handle_payload(TOPIC, _sobre())
  r.drenar()
  r._cerrar_con_resultado({"v": 2, "verb": "lane_change", "id": "test-lane_change",
                           "phase": "rejected", "reason": "GATE_BLIND_SPOT",
                           "detail": "vehiculo en el angulo muerto"})
  ultimo = [p for _, p, _, _ in pub][-1]
  assert ultimo["phase"] == Phase.FAILED
  assert ultimo["reason"] == "GATE_BLIND_SPOT"
  assert "angulo muerto" in ultimo["detail"]


def test_un_veredicto_de_otro_comando_no_inventa_un_ack():
  r, pub, _ = _router()
  r.handle_payload(TOPIC, _sobre())
  r.drenar()
  antes = len(pub)
  r._cerrar_con_resultado({"id": "de-otra-sesion", "phase": "applied", "reason": "OK"})
  assert len(pub) == antes, "se publico un ACK por un veredicto que no era de este comando"


def test_si_el_consumidor_no_contesta_se_declara_no_result_y_no_applied():
  r, pub, _ = _router()
  r.handle_payload(TOPIC, _sobre())
  r.drenar()
  # Vencer el plazo a mano: el limite es TTL + MARGEN_RESULTADO_S.
  with r._lock:
    for cid, (verbo, _lim) in list(r._pendientes.items()):
      r._pendientes[cid] = (verbo, ahora_mono() - 1.0)
  r._caducar_pendientes()
  ultimo = [p for _, p, _, _ in pub][-1]
  assert ultimo["phase"] == Phase.FAILED
  assert ultimo["reason"] == "NO_RESULT"
  assert Phase.APPLIED not in _fases(pub)


def test_el_veredicto_se_consume_una_sola_vez():
  """Si no se borrase el param, el mismo veredicto cerraria tambien el comando siguiente."""
  r, pub, _ = _router()
  r.handle_payload(TOPIC, _sobre())
  r.drenar()
  veredicto = {"id": "test-lane_change", "phase": "applied", "reason": "OK"}
  r._cerrar_con_resultado(veredicto)
  n = len(pub)
  r._cerrar_con_resultado(veredicto)
  assert len(pub) == n, "el mismo veredicto cerro dos veces"


def test_disarm_all_no_espera_a_nadie():
  """Bajar autoridad se confirma en el acto: no depende de que un consumidor conteste."""
  r, pub, _ = _router(verbo="disarm_all")
  r.handle_payload(TOPIC, _sobre("disarm_all", args={}))
  r.drenar()
  assert _fases(pub)[-1] == Phase.APPLIED
  assert not r._pendientes


# ---------------------------------------------------------------------------------------
# Lo que se vio en el coche (command_log del backend, 2026-09-21): tres lane_change con todo
# en verde cerrados como failed/NO_RESULT y uno cerrado como `failed` con reason `OK` y
# detail "maniobra iniciada". Estos tests fijan el arreglo de las dos mitades.
# ---------------------------------------------------------------------------------------

def test_una_fase_intermedia_del_consumidor_no_cierra_ni_falla():
  """`executing` ("maniobra iniciada") se reenvia como ACK y la orden sigue pendiente."""
  r, pub, _ = _router()
  r.handle_payload(TOPIC, _sobre())
  r.drenar()
  r._cerrar_con_resultado({"v": 2, "verb": "lane_change", "id": "test-lane_change",
                           "phase": "executing", "reason": "OK", "detail": "maniobra iniciada"})
  ultimo = [p for _, p, _, _ in pub][-1]
  assert ultimo["phase"] == Phase.EXECUTING
  assert Phase.FAILED not in _fases(pub), "una maniobra que EMPIEZA se cerraba como fallo"
  assert "test-lane_change" in r._pendientes, "la orden tiene que seguir esperando su final"
  # El plazo pasa a ser el de la EJECUCION (una maniobra dura hasta 10 s), no el del sobre.
  _, limite = r._pendientes["test-lane_change"]
  assert limite - ahora_mono() > r.MARGEN_RESULTADO_S + 3.0
  r._cerrar_con_resultado({"v": 2, "verb": "lane_change", "id": "test-lane_change",
                           "phase": "applied", "reason": "OK", "detail": "cambio de carril completado"})
  assert _fases(pub)[-1] == Phase.APPLIED
  assert not r._pendientes


def test_un_veredicto_sin_id_se_correlaciona_por_verbo():
  """desire_helper puede leer el flag antes de ver el cmdId en el plano (20 Hz contra 10 Hz):
  un veredicto sin id pero con verbo cierra el UNICO pendiente de ese verbo."""
  r, pub, _ = _router()
  r.handle_payload(TOPIC, _sobre())
  r.drenar()
  r._cerrar_con_resultado({"v": 2, "verb": "lane_change", "id": "",
                           "phase": "rejected", "reason": "GATE_SPEED_RANGE", "detail": ""})
  ultimo = [p for _, p, _, _ in pub][-1]
  assert ultimo["id"] == "test-lane_change"
  assert ultimo["phase"] == Phase.FAILED
  assert ultimo["reason"] == "GATE_SPEED_RANGE"
  assert not r._pendientes


def test_un_id_ajeno_no_se_correlaciona_por_verbo():
  """Con id, el id manda: uno que no es nuestro no cierra nuestra orden aunque el verbo coincida."""
  r, pub, _ = _router()
  r.handle_payload(TOPIC, _sobre())
  r.drenar()
  antes = len(pub)
  r._cerrar_con_resultado({"v": 2, "verb": "lane_change", "id": "otra-sesion",
                           "phase": "applied", "reason": "OK"})
  assert len(pub) == antes
  assert "test-lane_change" in r._pendientes


def test_el_plano_conserva_el_cmd_id_hasta_el_veredicto():
  """El consumidor firma con el cmdId que ve en el plano de estado: borrarlo al retornar el
  handler dejaba el veredicto sin id (y la orden en NO_RESULT)."""
  r, pub, _ = _router()
  r.handle_payload(TOPIC, _sobre())
  r.drenar()
  assert r.store.snapshot()["cmd_id"] == "test-lane_change"
  assert r.store.snapshot()["active_verb"] == "lane_change"
  r._cerrar_con_resultado({"v": 2, "verb": "lane_change", "id": "test-lane_change",
                           "phase": "executing", "reason": "OK", "detail": "maniobra iniciada"})
  # Visto por el consumidor: el plano deja de anunciarlo como activo (no da BUSY a otros).
  assert r.store.snapshot()["cmd_id"] == ""
  assert r.store.snapshot()["active_verb"] == ""


def test_el_no_result_tambien_libera_el_plano():
  r, pub, _ = _router()
  r.handle_payload(TOPIC, _sobre())
  r.drenar()
  with r._lock:
    for cid, (verbo, _lim) in list(r._pendientes.items()):
      r._pendientes[cid] = (verbo, ahora_mono() - 1.0)
  r._caducar_pendientes()
  assert r.store.snapshot()["cmd_id"] == ""


def test_un_verbo_sin_consumidor_sigue_cerrando_el_plano_al_instante():
  r, pub, _ = _router(verbo="disarm_all")
  r.handle_payload(TOPIC, _sobre("disarm_all", args={}))
  r.drenar()
  assert r.store.snapshot()["cmd_id"] == ""


def test_lane_change_busy_no_roba_el_id_de_la_maniobra_en_curso():
  """La segunda orden se rechaza con SU id; el final conserva el id de la primera."""
  from openpilot.selfdrive.controls.lib.desire_helper import DesireHelper, LaneChangeState

  class ParamsFalsos:
    def __init__(self):
      self.resultados = []

    def get_bool(self, key):
      return key == "ForceLaneChangeLeft"

    def remove(self, _key):
      pass

    def put(self, _key, value):
      self.resultados.append(json.loads(value))

  class AuthFalsa:
    active_verb = "lane_change"
    cmd_id = "segunda"

    def allows(self, *_a):
      return True, "OK"

  h = DesireHelper.__new__(DesireHelper)
  h.params = ParamsFalsos()
  h._orbit_now_mono = ahora_mono()
  h._orbit_flags = (False, False)
  h._orbit_flags_hasta = 0.0
  h._orbit_auth = AuthFalsa()
  h._orbit_lc_cmd_id = "primera"
  h._orbit_lc_candidate_cmd_id = ""
  h._orbit_result_roto = False
  h.lane_change_state = LaneChangeState.laneChangeStarting

  direccion, motivo = h._orbit_consumir_flag()
  assert motivo == ""
  assert h._orbit_lc_cmd_id == "primera"
  assert h._orbit_lc_candidate_cmd_id == "segunda"
  assert h._orbit_evaluar(None, True, direccion) == "BUSY"

  h._orbit_reportar("rejected", "BUSY", cmd_id=h._orbit_lc_candidate_cmd_id)
  assert h.params.resultados[-1]["id"] == "segunda"
  assert h._orbit_lc_cmd_id == "primera"
  h._orbit_reportar("applied", "OK")
  assert h.params.resultados[-1]["id"] == "primera"


# ---------------------------------------------------------------------------------------
# cruise_delta: el ACK lo cierra el consumidor de card (orbit_speed_ultra_simple), no el
# router al escribir el flag.
# ---------------------------------------------------------------------------------------

_TODOS_LOS_GATES = sum(int(g) for g in Gate)


class _CC:
  """carControl del ciclo: lo unico que mira el consumidor."""

  def __init__(self, enabled=True, longActive=True):
    self.enabled = enabled
    self.longActive = longActive


class _CS:
  def __init__(self, gasPressed=False, brakePressed=False):
    self.gasPressed = gasPressed
    self.brakePressed = brakePressed


class _VCruise:
  def __init__(self, kph=85.0):
    self.v_cruise_kph = kph
    self.v_cruise_cluster_kph = kph
    self.v_cruise_initialized = True


@pytest.fixture
def consumidor(monkeypatch):
  """Consumidor limpio (sin presupuesto gastado ni espera a medias) y flags del modulo a cero."""
  import openpilot.orbit.orbit_speed_ultra_simple as sp
  from openpilot.orbit.test.test_mando_v1_por_router import _ParamsFalsos
  nuevo = sp.OrbitSpeedUltraSimple()
  nuevo.params = _ParamsFalsos({"orbit_speed_increment": 5.0})
  monkeypatch.setattr(sp, "orbit_speed_ultra_simple", nuevo)
  monkeypatch.setattr(sp, "orbit_speed_increase", False)
  monkeypatch.setattr(sp, "orbit_speed_decrease", False)
  return nuevo


def _foto(store=None, **kw):
  """Lo que card lee de orbitCommandState. Con `store`, la foto de ese store (el plano ya tico)."""
  from openpilot.orbit.orbit_control_ultra_simple import OrbitAuthority
  base = dict(fresh=True, mode=Mode.COPILOT, gates=_TODOS_LOS_GATES, clock_synced=True,
              deadline_mono=ahora_mono() + 2.0)
  if store is not None:
    s = store.snapshot()
    base.update(mode=s["mode"], active_verb=s["active_verb"], cmd_id=s["cmd_id"], deadline_mono=s["deadline_mono"])
  base.update(kw)
  return OrbitAuthority(**base)


def _card(params, foto):
  """Car REAL sin __init__ (ni CAN ni coche): lo que usa cruise_delta y nada mas."""
  from openpilot.selfdrive.car.card import Car

  class _Plano:
    def poll(self):
      return foto()

  car = Car.__new__(Car)
  car.params = params
  car.sm = {"carControl": _CC()}
  car.v_cruise_helper = _VCruise(85.0)
  car._orbit_link = _Plano()
  car._orbit_auth = None
  car._orbit_now_mono = 0.0
  car._orbit_speed_mod = None
  car._orbit_speed_roto = False
  car._orbit_ultimo_error_mono = 0.0
  car._orbit_veredictos = deque(maxlen=8)
  car._orbit_despertar = threading.Event()
  car._orbit_aviso_veredicto = False
  return car


def _cadena(consumidor):
  """MQTTComandos + router + handler REALES y card REAL sobre el mismo Params falso."""
  from openpilot.orbit.test.test_mando_v1_por_router import _comandos
  c = _comandos(modo=Mode.COPILOT, verde=True)
  c.router._params_res = c.params     # el router lee OrbitCmdResult del mismo "disco"
  consumidor.params = c.params        # y el consumidor, el paso que escribe el handler
  return c, _card(c.params, lambda: _foto(c.plane.store))


def _pulsar(c, cmd_id="cd-1", delta=5.0, seq=1, verb="cruise_delta", ttl_ms=2000):
  from openpilot.orbit.test.test_mando_v1_por_router import V2, _entregar
  args = {"delta_kph": delta} if verb == "cruise_delta" else {"accel": -1.5}
  sobre = json.dumps({"v": 2, "id": cmd_id, "seq": seq, "verb": verb, "args": args,
                      "ts_ms": ahora_epoch_ms(), "ttl_ms": ttl_ms, "mode": "copiloto",
                      "actor": {"user_id": 1, "via": "api"}})
  assert _entregar(c, V2, sobre) == 1


def _router_lee(r):
  """Una vuelta de _bucle_resultados, sin hilo."""
  dato = r._consumir_resultado()
  if dato is not None:
    r._cerrar_con_resultado(dato)
  r._caducar_pendientes()


def _acks_de(c):
  return [json.loads(p) for t, p, _, _ in c.mqttc.publicados if "/ack/" in t]


def test_la_tabla_declara_que_cruise_delta_lo_cierra_el_consumidor():
  assert get_spec("cruise_delta").cierra_consumidor is True
  # Pero sin conservar el plano: firma sin id y el router lo correlaciona por verbo.
  assert get_spec("cruise_delta").conserva_plano is False
  assert get_spec("lane_change").conserva_plano is True


def test_cruise_delta_solo_es_applied_cuando_el_consumidor_mueve_la_consigna(consumidor):
  c, car = _cadena(consumidor)
  _pulsar(c)
  # El handler ya corrio: hay un flag en disco y nada mas. Antes aqui salia `applied`.
  assert c.params.valores["orbit_speed_increase"] is True
  assert [a["phase"] for a in _acks_de(c)] == [Phase.RECEIVED, Phase.ACCEPTED, Phase.EXECUTING]
  # El plano ya no la anuncia (no da BUSY a nadie); su ventana, si.
  assert c.plane.store.snapshot()["active_verb"] == ""

  car._orbit_leer_flags_velocidad()      # params_thread: flag -> RAM
  car._orbit_cruise_delta(_CS())         # state_update: decide y ENCOLA (no toca disco)
  assert car.v_cruise_helper.v_cruise_kph == 90.0
  assert "OrbitCmdResult" not in c.params.valores
  assert car._orbit_despertar.is_set(), "params_thread se despierta para escribirlo ya"
  _router_lee(c.router)
  assert _acks_de(c)[-1]["phase"] == Phase.EXECUTING

  escrituras = []
  put = c.params.put
  c.params.put = lambda k, v, *a, **kw: (escrituras.append((k, a, kw)), put(k, v, *a, **kw))
  car._orbit_publicar_veredicto()        # params_thread: veredicto -> OrbitCmdResult
  assert escrituras == [("OrbitCmdResult", (), {})], "put() sin block: params_thread no espera al fsync"
  crudo = c.params.valores["OrbitCmdResult"]
  from openpilot.common.params import Params
  Params()._put_cast("OrbitCmdResult", crudo)   # tabla real de tipos: la clave es STRING
  veredicto = json.loads(crudo)
  assert isinstance(veredicto["ts_ms"], int) and isinstance(veredicto["mono_ms"], int)

  _router_lee(c.router)
  ultimo = _acks_de(c)[-1]
  assert (ultimo["id"], ultimo["phase"], ultimo["reason"]) == ("cd-1", Phase.APPLIED, "OK")
  assert "85 -> 90" in ultimo["detail"]
  assert "OrbitCmdResult" not in c.params.valores, "el veredicto se consume una sola vez"
  assert not c.router._pendientes
  assert c.plane.store.snapshot()["cmd_id"] == ""


def test_cruise_delta_con_el_presupuesto_del_consumidor_agotado_cierra_con_su_motivo(consumidor):
  """El caso del borde de la ventana: el router acepta (su cuenta ya solto la orden vieja) y
  el consumidor, que la anoto unos ms despues, todavia la cuenta. Antes: `applied` y la
  consigna quieta."""
  c, car = _cadena(consumidor)
  consumidor._gasto = [(ahora_mono() - 1.0, 20.0)]
  _pulsar(c)
  car._orbit_leer_flags_velocidad()
  car._orbit_cruise_delta(_CS())
  car._orbit_publicar_veredicto()
  _router_lee(c.router)
  assert car.v_cruise_helper.v_cruise_kph == 85.0
  ultimo = _acks_de(c)[-1]
  # `failed` y no `rejected`: ya se habia anunciado `executing` (igual que lane_change).
  assert (ultimo["id"], ultimo["phase"], ultimo["reason"]) == ("cd-1", Phase.FAILED, "RANGE")
  assert "presupuesto" in ultimo["detail"]
  assert Phase.APPLIED not in [a["phase"] for a in _acks_de(c)]


def test_cruise_delta_sin_veredicto_cierra_con_no_result_y_nunca_applied(consumidor):
  c, _car = _cadena(consumidor)
  _pulsar(c)
  with c.router._lock:
    for cid, (verbo, _lim) in list(c.router._pendientes.items()):
      c.router._pendientes[cid] = (verbo, ahora_mono() - 1.0)
  _router_lee(c.router)
  ultimo = _acks_de(c)[-1]
  assert (ultimo["id"], ultimo["phase"], ultimo["reason"]) == ("cd-1", Phase.FAILED, "NO_RESULT")
  assert Phase.APPLIED not in [a["phase"] for a in _acks_de(c)]
  assert c.plane.store.snapshot()["cmd_id"] == ""


@pytest.mark.parametrize("cc, cs, kph, motivo", [
  (_CC(enabled=False), _CS(), 85.0, "GATE_ENGAGED"),
  (_CC(longActive=False), _CS(), 85.0, "GATE_LONG_ACTIVE"),
  (_CC(), _CS(brakePressed=True), 85.0, "GATE_DRIVER_IDLE"),
  (_CC(), _CS(), 145.0, "RANGE"),         # ya en V_CRUISE_MAX
])
def test_un_rechazo_del_consumidor_viaja_con_su_motivo(consumidor, cc, cs, kph, motivo):
  """Los `detail` que se fijan aqui los compara la app (command_bus.dart) para decirle al
  conductor que pasa; cambiarlos la devuelve en silencio al texto generico."""
  import openpilot.orbit.orbit_speed_ultra_simple as sp
  sp.orbit_speed_increase = True
  vch = _VCruise(kph)
  assert consumidor.process_speed_commands(cc, cs, vch, autoridad=_foto(), now_mono=ahora_mono()) == motivo
  assert vch.v_cruise_kph == kph
  v = consumidor.veredicto(motivo, ahora_mono())
  assert (v["id"], v["phase"], v["reason"]) == ("", Phase.REJECTED, motivo)
  if motivo == "RANGE":
    assert "limite" in v["detail"]
  else:
    assert v["detail"] == "precondicion en rojo al aplicar"


def test_el_consumidor_espera_a_ver_abierta_la_ventana_de_su_orden(consumidor):
  """La causa de los `applied` mudos. El flag (10 Hz) llega antes que la foto con la ventana
  de su orden (10 Hz): con la foto anterior -- la ventana de la orden previa ya cerrada --
  salia EXPIRED y la consigna no se movia."""
  import openpilot.orbit.orbit_speed_ultra_simple as sp
  sp.orbit_speed_increase = True
  vch = _VCruise(85.0)
  t0 = ahora_mono()
  anterior = _foto(deadline_mono=t0 - 0.5)
  assert consumidor.process_speed_commands(_CC(), _CS(), vch, autoridad=anterior, now_mono=t0) == ""
  assert sp.orbit_speed_increase is True, "el flag no se consume mientras se espera"
  nueva = _foto(deadline_mono=t0 + 1.9)          # sin activeVerb: basta la ventana
  assert consumidor.process_speed_commands(_CC(), _CS(), vch, autoridad=nueva, now_mono=t0 + 0.08) == "OK"
  assert vch.v_cruise_kph == 90.0
  assert consumidor.veredicto("OK", t0 + 0.08)["id"] == ""


def test_si_el_plano_no_la_publica_a_tiempo_se_decide_igual_y_sin_id(consumidor):
  """La espera no relaja nada: vencida, se decide con la foto que haya (aqui EXPIRED) y el
  router correlaciona el veredicto sin id por verbo."""
  import openpilot.orbit.orbit_speed_ultra_simple as sp
  sp.orbit_speed_increase = True
  vch = _VCruise(85.0)
  t0 = ahora_mono()
  anterior = _foto(deadline_mono=t0 - 0.5)
  assert consumidor.process_speed_commands(_CC(), _CS(), vch, autoridad=anterior, now_mono=t0) == ""
  t1 = t0 + sp.ESPERA_PLANO_S
  assert consumidor.process_speed_commands(_CC(), _CS(), vch, autoridad=anterior, now_mono=t1) == "EXPIRED"
  assert sp.orbit_speed_increase is False
  assert vch.v_cruise_kph == 85.0
  v = consumidor.veredicto("EXPIRED", t1)
  assert (v["id"], v["phase"]) == ("", Phase.REJECTED)

  r, pub, _ = _router(verbo="cruise_delta")
  r.handle_payload(TOPIC, _sobre("cruise_delta", args={"delta_kph": 5.0}))
  r.drenar()
  r._cerrar_con_resultado(v)
  ultimo = [p for _, p, _, _ in pub][-1]
  assert (ultimo["id"], ultimo["phase"], ultimo["reason"]) == ("test-cruise_delta", Phase.FAILED, "EXPIRED")


def test_dos_pulsaciones_pendientes_cierran_en_orden_y_sin_pisarse(consumidor):
  """Dos moviles a la vez. Los veredictos van sin id y el router los reparte por verbo, del
  pendiente mas antiguo al mas nuevo, que es el orden en que card decide sus flags. Y el
  segundo no se escribe encima del primero mientras el router no lo haya leido: pisado, la
  primera acababa en NO_RESULT con la consigna ya movida."""
  c, car = _cadena(consumidor)
  _pulsar(c, "cd-1")
  car._orbit_leer_flags_velocidad()
  car._orbit_cruise_delta(_CS())
  _pulsar(c, "cd-2", delta=-5.0, seq=2)
  car._orbit_leer_flags_velocidad()
  car._orbit_cruise_delta(_CS())
  assert car.v_cruise_helper.v_cruise_kph == 85.0
  car._orbit_publicar_veredicto()
  car._orbit_publicar_veredicto()                 # hueco ocupado: el segundo espera
  assert len(car._orbit_veredictos) == 1
  _router_lee(c.router)
  car._orbit_publicar_veredicto()
  _router_lee(c.router)
  hechos = [(a["id"], a["detail"]) for a in _acks_de(c) if a["phase"] == Phase.APPLIED]
  assert hechos == [("cd-1", "consigna 85 -> 90 km/h"), ("cd-2", "consigna 90 -> 85 km/h")]
  assert not c.router._pendientes


def test_un_put_que_falla_no_guarda_el_veredicto_para_reintentarlo(consumidor):
  """Reintentado, saldria tarde y el router lo casaria por verbo con una pulsacion posterior.
  Perdido, su orden cierra con NO_RESULT, que es lo honesto."""
  c, car = _cadena(consumidor)
  car._orbit_veredictos.append({"verb": "cruise_delta"})

  def put_roto(*_a, **_kw):
    raise OSError("disco")

  c.params.put = put_roto
  with pytest.raises(OSError):
    car._orbit_publicar_veredicto()
  assert not car._orbit_veredictos


def test_un_cruise_delta_no_corta_un_assisted_decel_en_marcha(consumidor):
  """controlsd reevalua allows('assisted_decel') en CADA ciclo del hold de 1.5 s. Si el plano
  anunciara el cruise_delta hasta su veredicto, veria BUSY y soltaria la frenada a neutro, con
  su ACK ya en `applied`, por pulsar +/- durante la deceleracion."""
  from openpilot.orbit.test.test_mando_v1_por_router import _comandos
  c = _comandos(modo=Mode.MANEUVER, verde=True)
  _pulsar(c, "dec-1", verb="assisted_decel", ttl_ms=1500)
  _pulsar(c, "cd-1", delta=-5.0)
  foto = _foto(c.plane.store)
  assert foto.active_verb == ""
  gates = int(Gate.ENGAGED | Gate.LONG_ACTIVE | Gate.DRIVER_IDLE)
  assert foto.allows("assisted_decel", Mode.MANEUVER, gates, ahora_mono()) == (True, "OK")
  fases = {(a["id"], a["phase"]) for a in _acks_de(c)}
  assert ("dec-1", Phase.APPLIED) in fases
  assert ("cd-1", Phase.EXECUTING) in fases and "cd-1" in c.router._pendientes
