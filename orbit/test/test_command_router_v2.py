"""Plan de pruebas de fallo del mando remoto v2 (seccion 12 del diseno).

Cubre las filas de esa tabla que se pueden comprobar SIN coche:

    Reinyectar un comando capturado ......... DUPLICATE
    Dos comandos fuera de orden ............. una sola maniobra, SUPERSEDED en el otro
    Reloj desfasado 5 min ................... CLOCK, no ejecucion
    Publicar con retain ..................... descartado
    Payload vacio ........................... descartado
    Comando de otro dongle_id ............... descartado
    TTL vencido ............................. EXPIRED
    Saturar la cola ......................... BUSY, sin bloquear el hilo de red

Las filas que exigen coche o banco (cortar el enlace a mitad de maniobra, Jetson colgada,
matar el proceso con un viaje abierto) NO estan aqui: no se pueden falsear de forma
honesta con dobles y fingirlas seria peor que no tenerlas.

Se anaden ademas las invariantes que, si se rompen, no dan un fallo ruidoso sino un
comportamiento equivocado en silencio: la correspondencia de la mascara de gates y de las
fases del ACK con cereal/custom.capnp, y la excepcion explicita de disarm_all.
"""
import json
import queue

import pytest

from cereal import custom
from openpilot.orbit.command_gates import GateMonitor
from openpilot.orbit.command_router import CLOCK_SKEW_MAX_MS, CommandRouter
from openpilot.orbit.command_spec import (COMMANDS, GATE_CEREAL_NAMES, MODE_CEREAL_NAMES, PHASES, Gate, Mode,
                                          Phase, ahora_epoch_ms, ahora_mono, get_spec, validar_args)
from openpilot.orbit.command_state import (PARAM_BENCH_ARMED, PARAM_BENCH_EXPIRY, PARAM_DISARM_ALL, PARAM_MODE,
                                           CommandStateService, CommandStateStore)

DONGLE = "0123456789abcdef"
TOPIC = f"orbit/v2/cmd/{DONGLE}"


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


class _GatesFalsos:
  """Doble del GateMonitor con la superficie que usa el router."""

  def __init__(self, clock=True, verde=True, brand="ford"):
    self.clock_synced = clock
    self.verde = verde
    self.brand = brand
    self.mask = 0xFFFF if verde else 0
    self.links = 0

  def note_link(self, cuando_mono=None):
    self.links += 1

  def update(self, timeout_ms=0):
    return self.mask

  def evaluate(self, spec, args=None):
    if self.verde:
      return True, []
    return False, [g for g in Gate if int(spec.gates) & g]


class _PubMasterFalso:
  def __init__(self):
    self.enviados = []

  def send(self, servicio, msg):
    self.enviados.append((servicio, msg))


class _Mensaje:
  """Doble del mensaje de paho."""

  def __init__(self, topic, payload, retain=False):
    self.topic = topic
    self.payload = payload.encode() if isinstance(payload, str) else payload
    self.retain = retain


# --------------------------------------------------------------------------- utiles

def _store(mode=Mode.MANEUVER, bench=False, bench_ttl_ms=300_000):
  valores = {PARAM_MODE: int(mode), PARAM_BENCH_ARMED: bench, PARAM_DISARM_ALL: False}
  if bench:
    valores[PARAM_BENCH_EXPIRY] = str(ahora_epoch_ms() + bench_ttl_ms)
  st = CommandStateStore(params=_ParamsFalsos(valores))
  st.refresh_params(forzar=True)
  return st


def _router(mode=Mode.MANEUVER, clock=True, verde=True, bench=False, verbos=("lane_change", "disarm_all"),
            queue_max=64, lru_max=512):
  publicados = []
  ejecutados = []
  r = CommandRouter(DONGLE, gates=_GatesFalsos(clock=clock, verde=verde), store=_store(mode, bench=bench),
                    publish=lambda t, p, q, ret: publicados.append((t, json.loads(p), q, ret)),
                    queue_max=queue_max, lru_max=lru_max)
  for v in verbos:
    r.register_handler(v, lambda cmd: ejecutados.append(cmd))
  return r, publicados, ejecutados


_CONTADOR = [0]


def _sobre(verb="lane_change", **kw):
  _CONTADOR[0] += 1
  base = {
    "v": 2,
    "id": kw.pop("id", f"id-{_CONTADOR[0]}"),
    "seq": kw.pop("seq", _CONTADOR[0]),
    "verb": verb,
    "args": kw.pop("args", {"direction": "left"} if verb == "lane_change" else {}),
    "ts_ms": kw.pop("ts_ms", ahora_epoch_ms()),
    "mono_ms": 1,
    "ttl_ms": kw.pop("ttl_ms", 3000),
    "mode": kw.pop("mode", "maniobra"),
    "actor": {"user_id": 7, "via": "app"},
    "sig": None,
  }
  base.update(kw)
  return json.dumps(base)


def _fases(publicados, cmd_id):
  return [p["phase"] for _, p, _, _ in publicados if p["id"] == cmd_id]


def _motivos(publicados, cmd_id):
  return [p["reason"] for _, p, _, _ in publicados if p["id"] == cmd_id and p["phase"] != Phase.RECEIVED]


# ------------------------------------------------------- tabla de la seccion 12

def test_retain_se_descarta():
  """Un mando retenido lo reentrega el broker en CADA reconexion: una frenada retenida
  volveria a frenar cada vez que reaparece la cobertura."""
  r, pub, ej = _router()
  res = r.handle_payload(TOPIC, _sobre(), retain=True)
  assert res.descartado and not res.aceptado
  assert pub == []          # un retenido ni siquiera merece ACK
  assert ej == []


def test_payload_vacio_se_descarta():
  """Payload de longitud cero es el gesto MQTT para BORRAR un retenido, no una orden."""
  r, pub, ej = _router()
  for vacio in ("", "   ", b""):
    res = r.handle_payload(TOPIC, vacio)
    assert res.descartado
  assert pub == []
  assert ej == []


def test_dongle_ajeno_se_descarta():
  r, pub, ej = _router()
  res = r.handle_payload("orbit/v2/cmd/otrodongle", _sobre())
  assert res.descartado
  assert pub == []
  assert ej == []


def test_topic_sin_dongle_se_descarta():
  """No existe ningun topic de mando sin <dongle> en la ruta. Nunca mas (seccion 3.1)."""
  r, pub, _ = _router()
  assert r.handle_payload("orbit/v2/cmd/", _sobre()).descartado
  assert r.handle_payload("telemetry_config/global/left", _sobre()).descartado
  assert pub == []


def test_duplicado_da_DUPLICATE():
  """Reinyectar un comando capturado. QoS 1 es at-least-once: el broker reentrega."""
  r, pub, ej = _router()
  sobre = _sobre()
  primero = r.handle_payload(TOPIC, sobre)
  assert primero.aceptado
  segundo = r.handle_payload(TOPIC, sobre)
  assert not segundo.aceptado
  assert segundo.reason == "DUPLICATE"
  r.drenar()
  assert len(ej) == 1                      # una sola maniobra
  assert "DUPLICATE" in _motivos(pub, segundo.cmd_id)


def test_fuera_de_orden_da_SUPERSEDED():
  """Dos comandos fuera de orden: una sola maniobra, SUPERSEDED en el otro."""
  r, pub, ej = _router()
  nuevo = r.handle_payload(TOPIC, _sobre(seq=50, id="nuevo"))
  viejo = r.handle_payload(TOPIC, _sobre(seq=49, id="viejo"))
  assert nuevo.aceptado
  assert not viejo.aceptado and viejo.reason == "SUPERSEDED"
  r.drenar()
  assert len(ej) == 1
  assert ej[0].seq == 50
  assert Phase.SUPERSEDED in _fases(pub, "viejo")


def test_mismo_seq_repetido_tambien_es_SUPERSEDED():
  r, _, _ = _router()
  assert r.handle_payload(TOPIC, _sobre(seq=7, id="a")).aceptado
  assert r.handle_payload(TOPIC, _sobre(seq=7, id="b")).reason == "SUPERSEDED"


def test_reloj_desfasado_da_CLOCK():
  """Reloj desfasado 5 min. Tiene que decir CLOCK y no EXPIRED: el motivo tiene que decir
  la verdad al usuario, si no la app le hace reintentar para siempre."""
  r, pub, ej = _router()
  res = r.handle_payload(TOPIC, _sobre(ts_ms=ahora_epoch_ms() - 5 * 60 * 1000))
  assert res.reason == "CLOCK"
  r.drenar()
  assert ej == []
  assert "CLOCK" in _motivos(pub, res.cmd_id)


def test_reloj_del_dispositivo_sin_sincronizar_da_CLOCK():
  """Un comma sin fix GPS ni NTP arranca con la hora equivocada: preferible un coche que
  no obedece a uno que obedece una orden de hace diez minutos."""
  r, _, ej = _router(clock=False)
  assert r.handle_payload(TOPIC, _sobre()).reason == "CLOCK"
  r.drenar()
  assert ej == []


def test_desfase_por_debajo_del_umbral_no_es_un_problema_de_reloj():
  """Por debajo del umbral el retraso es jitter de red y lo juzga el TTL, no el reloj.
  healthcheck tiene 30 s de TTL, asi que 20 s de retraso todavia cabe."""
  r, _, _ = _router(mode=Mode.COPILOT, verbos=("healthcheck",))
  desfase = CLOCK_SKEW_MAX_MS - 10_000
  res = r.handle_payload(TOPIC, _sobre("healthcheck", args={}, mode="copiloto",
                                       ts_ms=ahora_epoch_ms() - desfase, ttl_ms=30_000))
  assert res.aceptado, res


def test_ttl_vencido_da_EXPIRED():
  r, pub, ej = _router()
  res = r.handle_payload(TOPIC, _sobre(ts_ms=ahora_epoch_ms() - 4000, ttl_ms=3000))
  assert res.reason == "EXPIRED"
  assert res.phase == Phase.EXPIRED
  r.drenar()
  assert ej == []
  assert Phase.EXPIRED in _fases(pub, res.cmd_id)


def test_ttl_del_sobre_no_puede_ampliar_el_del_catalogo():
  """El limite lo pone el coche. Un emisor no puede regalarse una hora de validez."""
  r, _, ej = _router()
  assert r.handle_payload(TOPIC, _sobre(ttl_ms=3_600_000)).aceptado
  r.drenar()
  assert ej[0].ttl_ms == COMMANDS["lane_change"].ttl_ms


def test_cola_llena_da_BUSY_sin_bloquear():
  """Saturar el broker: sin bloqueo del hilo de red, sin crecimiento de RAM."""
  r, _, _ = _router(queue_max=2)
  assert r.handle_payload(TOPIC, _sobre(seq=1)).aceptado
  assert r.handle_payload(TOPIC, _sobre(seq=2)).aceptado
  res = r.handle_payload(TOPIC, _sobre(seq=3))
  assert res.reason == "BUSY"
  assert r._cola.qsize() == 2


# --------------------------------------------------- disarm_all: la excepcion explicita

def test_disarm_all_atraviesa_todos_los_filtros():
  """Bajar autoridad nunca se descarta (secciones 2 y 3.4): fuera de orden, con el TTL
  vencido, con el reloj torcido, en modo observador y con todos los gates en rojo."""
  r, pub, ej = _router(mode=Mode.OBSERVER, clock=False, verde=False,
                       verbos=("lane_change", "disarm_all"))
  # un disarm anterior con seq alto: el siguiente llega con seq 1, es decir FUERA DE ORDEN
  r.handle_payload(TOPIC, _sobre("disarm_all", seq=9999, id="previo", args={}))
  r.drenar()

  res = r.handle_payload(TOPIC, _sobre("disarm_all", seq=1, id="tarde", args={},
                                       ts_ms=ahora_epoch_ms() - 10 * 60 * 1000, ttl_ms=1, mode="banco"))
  assert res.aceptado
  r.drenar()
  assert [c.verb for c in ej] == ["disarm_all", "disarm_all"]
  assert Phase.APPLIED in _fases(pub, "tarde")


def test_disarm_all_duplicado_se_ejecuta_igual():
  """Desarmar dos veces es desarmar. Tragarse el segundo por DUPLICATE perderia el
  desarme si el primero se quedo en un buffer."""
  r, _, ej = _router(verbos=("disarm_all",))
  sobre = _sobre("disarm_all", args={}, id="mismo")
  assert r.handle_payload(TOPIC, sobre).aceptado
  assert r.handle_payload(TOPIC, sobre).aceptado
  r.drenar()
  assert len(ej) == 2


def test_disarm_all_retenido_sigue_descartandose():
  """Limite de la excepcion, escrito a proposito: retain se rechaza incondicionalmente en
  el namespace de mando (seccion 3.4). Un disarm retenido se reentregaria en cada
  reconexion y desarmaria al usuario cada vez que vuelve la cobertura."""
  r, _, ej = _router(verbos=("disarm_all",))
  assert r.handle_payload(TOPIC, _sobre("disarm_all", args={}), retain=True).descartado
  r.drenar()
  assert ej == []


def test_disarm_all_deja_el_plano_de_estado_en_neutro():
  r, _, _ = _router(verbos=("lane_change", "disarm_all"))
  r.handle_payload(TOPIC, _sobre(seq=1))
  r.drenar()
  assert r.store.snapshot()["deadline_mono"] > 0.0
  r.handle_payload(TOPIC, _sobre("disarm_all", args={}, seq=2))
  r.drenar()
  snap = r.store.snapshot()
  assert snap["deadline_mono"] == 0.0
  assert snap["active_verb"] == ""


# ------------------------------------------------------------- sobre, tipos y rangos

def test_version_distinta_da_TYPE():
  r, _, _ = _router()
  assert r.handle_payload(TOPIC, _sobre(v=1)).reason == "TYPE"
  assert r.handle_payload(TOPIC, _sobre(v="2")).reason == "TYPE"


def test_ts_ms_tiene_que_ser_entero():
  """Hoy el campo temporal viaja como int, como string y como ISO-8601 en el MISMO topic:
  eso es la enfermedad, no un detalle (seccion 2)."""
  r, _, _ = _router()
  for malo in ("2026-08-23T10:00:00Z", str(ahora_epoch_ms()), float(ahora_epoch_ms()), True, None):
    assert r.handle_payload(TOPIC, _sobre(ts_ms=malo)).reason == "TYPE"


def test_verbo_desconocido_da_UNSUPPORTED_VERB():
  r, _, _ = _router()
  assert r.handle_payload(TOPIC, _sobre("brutebreak", args={})).reason == "UNSUPPORTED_VERB"


def test_verbo_de_la_tabla_sin_handler_da_UNSUPPORTED_VERB():
  """Un verbo declarado y no implementado NO se acepta en silencio: es el boton que
  miente de la seccion 3.5."""
  r, _, _ = _router(verbos=("disarm_all",))
  assert r.handle_payload(TOPIC, _sobre()).reason == "UNSUPPORTED_VERB"


def test_un_sobre_que_no_es_una_orden_no_genera_ACK_received():
  """El broker es abierto (D1): contestar a cada byte que llega convierte nuestro propio
  enlace de subida en el amplificador de la inundacion. Hasta saber que el verbo existe y
  esta implementado, solo se contesta el rechazo."""
  r, pub, _ = _router()
  r.handle_payload(TOPIC, _sobre(v=1))
  r.handle_payload(TOPIC, _sobre("brutebreak", args={}))
  assert Phase.RECEIVED not in [p["phase"] for _, p, _, _ in pub]
  assert len(pub) == 2


def test_sobre_sin_id_se_descarta():
  r, _, _ = _router()
  assert r.handle_payload(TOPIC, _sobre(id="")).descartado


def test_json_invalido_se_descarta():
  r, _, _ = _router()
  assert r.handle_payload(TOPIC, "no soy json").descartado
  assert r.handle_payload(TOPIC, "[1,2,3]").descartado


def test_cadena_false_no_dispara_un_cambio_de_carril():
  """El bug vivo: bool('false') es True. La validacion es estricta y sin coercion."""
  spec = get_spec("mads")
  assert validar_args(spec, {"enabled": "false"})[0] == "TYPE"
  assert validar_args(spec, {"enabled": 1})[0] == "TYPE"
  assert validar_args(spec, {"enabled": False})[0] is None


def test_argumentos_fuera_de_rango_dan_RANGE():
  r, _, _ = _router(verbos=("lane_change", "assisted_decel", "disarm_all"))
  assert r.handle_payload(TOPIC, _sobre(args={"direction": "arriba"})).reason == "RANGE"
  # brutebreak aceptaba [-10,-1]; assisted_decel acota a [-2.5,-1.0] (seccion 0).
  res = r.handle_payload(TOPIC, _sobre("assisted_decel", args={"accel": -8.0}))
  assert res.reason == "RANGE"


def test_argumento_desconocido_da_TYPE():
  """Si el emisor cree que manda 'dir' y el coche espera 'direction', ignorarlo ejecutaria
  la maniobra con el valor por defecto en vez de decir que el sobre esta mal."""
  r, _, _ = _router()
  assert r.handle_payload(TOPIC, _sobre(args={"dir": "left"})).reason == "TYPE"


def test_presupuesto_de_ritmo_de_cruise_delta():
  """El tope por orden no sirve de nada si se pueden encadenar veinte ordenes de +5."""
  r, _, _ = _router(mode=Mode.COPILOT, verbos=("cruise_delta",))
  aceptados = 0
  for i in range(10):
    res = r.handle_payload(TOPIC, _sobre("cruise_delta", args={"delta_kph": 5.0}, seq=100 + i, mode="copiloto"))
    aceptados += int(res.aceptado)
    if not res.aceptado:
      assert res.reason == "RANGE"
      # La app distingue este RANGE por la palabra (command_bus.dart): "demasiados cambios
      # seguidos; espera un momento" en vez de "argumentos fuera de rango".
      assert "presupuesto" in res.detail
  assert aceptados == 4   # 4 x 5 km/h = el presupuesto de 20 km/h por minuto


# ------------------------------------------------------------------- modos y gates

def test_modo_insuficiente_da_MODE():
  r, _, ej = _router(mode=Mode.COPILOT)
  assert r.handle_payload(TOPIC, _sobre()).reason == "MODE"
  r.drenar()
  assert ej == []


def test_modo_declarado_por_encima_del_real_da_MODE():
  """El emisor operaba con una foto vieja del coche."""
  r, _, _ = _router(mode=Mode.COPILOT, verbos=("cruise_delta",))
  res = r.handle_payload(TOPIC, _sobre("cruise_delta", args={"delta_kph": 1.0}, mode="maniobra"))
  assert res.reason == "MODE"


def test_gate_en_rojo_da_GATE_con_nombre():
  r, pub, ej = _router(verde=False)
  res = r.handle_payload(TOPIC, _sobre())
  assert res.reason.startswith("GATE_")
  assert res.reason == "GATE_ENGAGED"
  assert "LAT_ACTIVE" in res.detail          # el detalle lleva TODOS los que faltan
  r.drenar()
  assert ej == []
  assert res.reason in _motivos(pub, res.cmd_id)


def test_verbo_de_banco_sin_armado_fisico_da_MODE():
  """Ningun verbo fisico es alcanzable solo por MQTT (seccion 1)."""
  r, _, ej = _router(mode=Mode.BENCH, bench=False, verbos=("torque_mode",))
  res = r.handle_payload(TOPIC, _sobre("torque_mode", args={"mode": 2}, mode="banco"))
  assert res.reason == "MODE"
  assert "armado fisico" in res.detail
  r.drenar()
  assert ej == []


def test_verbo_de_banco_con_armado_fisico_pasa():
  r, _, ej = _router(mode=Mode.BENCH, bench=True, verbos=("torque_mode",))
  assert r.handle_payload(TOPIC, _sobre("torque_mode", args={"mode": 2}, mode="banco")).aceptado
  r.drenar()
  assert len(ej) == 1


def test_armado_de_banco_caducado_no_arma():
  """TTL 300 s (seccion 4.1): pasado el plazo, el armado no vale aunque el bool siga a 1."""
  st = CommandStateStore(params=_ParamsFalsos({
    PARAM_BENCH_ARMED: True,
    PARAM_BENCH_EXPIRY: str(ahora_epoch_ms() - 1000),
  }))
  st.refresh_params(forzar=True)
  assert not st.bench_armado_vigente


def test_armado_de_banco_sin_caducidad_no_arma():
  """Fail-closed: quien arme el banco tiene que escribir TAMBIEN la caducidad."""
  st = CommandStateStore(params=_ParamsFalsos({PARAM_BENCH_ARMED: True}))
  st.refresh_params(forzar=True)
  assert not st.bench_armado_vigente


# --------------------------------------------------------------- revalidacion tardia

def test_el_gate_se_reevalua_antes_de_ejecutar():
  """El GateMonitor filtra pronto; el consumidor decide tarde. Entre aceptar y ejecutar
  cabe un desenganche entero."""
  r, pub, ej = _router()
  res = r.handle_payload(TOPIC, _sobre())
  assert res.aceptado
  r.gates.verde = False                      # el conductor pisa el freno mientras espera
  r.drenar()
  assert ej == []
  assert Phase.REJECTED in _fases(pub, res.cmd_id)


def test_ttl_que_vence_en_la_cola_da_EXPIRED():
  r, pub, ej = _router()
  res = r.handle_payload(TOPIC, _sobre(ttl_ms=3000))
  assert res.aceptado
  # se fuerza el vencimiento del plazo monotono ya calculado
  cmd = r._cola.queue[0]
  cmd.deadline_mono -= 10.0
  r.drenar()
  assert ej == []
  assert Phase.EXPIRED in _fases(pub, res.cmd_id)


# ------------------------------------------------------------------------ ACK y fases

def test_secuencia_de_fases_de_un_comando_bueno():
  r, pub, _ = _router()
  res = r.handle_payload(TOPIC, _sobre())
  r.drenar()
  # lane_change declara cierra_consumidor=True: el router NO puede anunciar APPLIED, porque
  # lo unico que ha pasado es que hay un flag en disco. Quien acepta o rechaza es
  # desire_helper, y su veredicto llega por OrbitCmdResult. Se queda en EXECUTING.
  assert _fases(pub, res.cmd_id) == [Phase.RECEIVED, Phase.ACCEPTED, Phase.EXECUTING]
  ack = [p for _, p, _, _ in pub][0]
  assert ack["v"] == 2
  assert isinstance(ack["ts_ms"], int)       # epoch ms ENTERO, siempre
  assert all(qos == 1 and not retain for _, _, qos, retain in pub)
  assert all(topic == f"orbit/v2/ack/{DONGLE}" for topic, _, _, _ in pub)


def test_handler_que_lanza_da_FAILED_y_no_mata_el_worker():
  r, pub, _ = _router(verbos=("disarm_all",))
  r.register_handler("lane_change", lambda cmd: (_ for _ in ()).throw(RuntimeError("boom")))
  res = r.handle_payload(TOPIC, _sobre())
  r.drenar()
  assert Phase.FAILED in _fases(pub, res.cmd_id)
  assert "RuntimeError" in [p["detail"] for _, p, _, _ in pub if p["phase"] == Phase.FAILED][0]


def test_on_message_nunca_propaga_una_excepcion():
  """paho corre las callbacks con suppress_exceptions=False: una excepcion aqui mata el
  hilo de red EN SILENCIO y el coche se queda mudo marcando 'conectado'."""
  r, _, _ = _router()
  class _Roto:
    topic = TOPIC
    retain = False
    @property
    def payload(self):
      raise ValueError("payload ilegible")
  res = r.on_message(None, None, _Roto())
  assert res.descartado


def test_on_message_acepta_un_mensaje_de_paho():
  r, _, ej = _router()
  assert r.on_message(None, None, _Mensaje(TOPIC, _sobre())).aceptado
  r.drenar()
  assert len(ej) == 1


# ------------------------------------------------------------------ idempotencia LRU

def test_la_lru_esta_acotada():
  r, _, _ = _router(lru_max=8, verbos=("disarm_all",))
  for i in range(50):
    r.handle_payload(TOPIC, _sobre("disarm_all", args={}, id=f"x{i}"))
    r.drenar()
  assert len(r._vistos) <= 8


def test_la_lru_por_defecto_es_de_512():
  from openpilot.orbit.command_router import LRU_MAX
  assert LRU_MAX == 512


# ------------------------------------------------- correspondencia con cereal/custom.capnp

def test_la_mascara_de_gates_coincide_con_cereal():
  """Si esto se desincroniza, el firmware publica una mascara que el consumidor
  interpreta al reves: ejecutar una maniobra creyendo que el gate estaba verde."""
  enumerantes = dict(custom.OrbitCommandState.Gate.schema.enumerants)
  assert len(enumerantes) == len(list(Gate))
  for gate in Gate:
    nombre = GATE_CEREAL_NAMES[gate]
    assert nombre in enumerantes, f"{gate.name} no existe en cereal"
    assert int(gate) == 1 << enumerantes[nombre]


def test_las_fases_del_ack_coinciden_con_cereal():
  enumerantes = dict(custom.OrbitCommandState.AckPhase.schema.enumerants)
  assert set(PHASES) == set(enumerantes)


def test_los_modos_coinciden_con_cereal():
  enumerantes = dict(custom.OrbitCommandState.Mode.schema.enumerants)
  assert {MODE_CEREAL_NAMES[m]: int(m) for m in Mode} == enumerantes


# --------------------------------------------------------------- plano de estado cereal

def test_el_mensaje_orbitcommandstate_se_construye():
  store = _store(Mode.MANEUVER)
  store.begin_command("lane_change", "abc", 42, 123.5)
  store.note_ack(Phase.APPLIED, "OK")
  store.set_clock_synced(True)
  svc = CommandStateService(gates=_GatesFalsos(), store=store, pm=_PubMasterFalso())
  msg = svc.build_message()
  st = msg.orbitCommandState
  assert str(st.mode) == "maneuver"
  assert st.activeVerb == "lane_change"
  assert st.cmdId == "abc"
  assert st.seq == 42
  assert st.deadlineMono == pytest.approx(123.5)
  assert str(st.lastAckPhase) == "applied"
  assert st.clockSynced
  assert msg.valid


def test_el_servicio_publica_a_traves_del_pubmaster():
  pm = _PubMasterFalso()
  svc = CommandStateService(gates=_GatesFalsos(), store=_store(), pm=pm)
  svc.step()
  assert [s for s, _ in pm.enviados] == ["orbitCommandState"]


def test_el_deadman_devuelve_el_plano_a_neutro():
  """Watchdog de actuador (seccion 5): si el plazo vence, neutro. Es lo que impide que un
  override lateral se quede pegado."""
  store = _store()
  store.begin_command("lane_change", "abc", 1, deadline_mono=0.0001)
  assert store.expire_if_due()
  snap = store.snapshot()
  assert snap["deadline_mono"] == 0.0 and snap["active_verb"] == ""


def test_el_boton_fisico_de_desarme_dispara_sin_red():
  """OrbitDisarmAll es redundante con el verbo A PROPOSITO: bajar autoridad no puede
  depender de que el enlace MQTT siga vivo."""
  params = _ParamsFalsos({PARAM_MODE: 2, PARAM_DISARM_ALL: True})
  store = CommandStateStore(params=params)
  disparos = []
  svc = CommandStateService(gates=_GatesFalsos(), store=store, pm=_PubMasterFalso(),
                            on_disarm=lambda: disparos.append(1))
  svc.step()
  assert disparos == [1]
  assert params.valores[PARAM_DISARM_ALL] is False
  svc.step()
  assert disparos == [1]        # one-shot: no se repite


# -------------------------------------------------------------------- GateMonitor real

class _SubMasterFalso:
  def __init__(self, datos, vivos=None):
    self.datos = datos
    self.alive = dict.fromkeys(datos, True)
    self.valid = dict.fromkeys(datos, True)
    self.seen = dict.fromkeys(datos, True)
    if vivos is not None:
      self.alive.update(vivos)

  def __getitem__(self, k):
    return self.datos[k]

  def update(self, timeout=0):
    pass


def _sm_completo(**cambios):
  from types import SimpleNamespace
  from cereal import log
  datos = {
    "carState": SimpleNamespace(vEgo=25.0, standstill=False, gasPressed=False, brakePressed=False,
                                steeringPressed=False, seatbeltUnlatched=False, doorOpen=False),
    "selfdriveState": SimpleNamespace(enabled=True),
    "carControl": SimpleNamespace(latActive=True, longActive=True),
    "carParams": SimpleNamespace(dashcamOnly=False, passive=False, brand="ford"),
    "deviceState": SimpleNamespace(started=True),
    "liveCalibration": SimpleNamespace(calStatus=log.LiveCalibrationData.Status.calibrated),
    "driverMonitoringState": SimpleNamespace(visionPolicyState=SimpleNamespace(faceDetected=True)),
  }
  for servicio, campos in cambios.items():
    for k, v in campos.items():
      setattr(datos[servicio], k, v)
  return datos


def test_gatemonitor_todo_en_verde():
  gm = GateMonitor(sm=_SubMasterFalso(_sm_completo()))
  gm.note_link()
  mask = gm.update(0)
  for gate in (Gate.ENGAGED, Gate.LAT_ACTIVE, Gate.LONG_ACTIVE, Gate.SPEED_RANGE, Gate.DRIVER_IDLE,
               Gate.DRIVER_PRESENT, Gate.CALIBRATED, Gate.NOT_DEGRADED, Gate.LINK_FRESH):
    assert mask & gate, f"{gate.name} deberia estar en verde"
  ok, fallados = gm.evaluate(COMMANDS["lane_change"])
  assert ok and fallados == []


def test_driver_present_es_la_precondicion_que_hoy_no_se_usa():
  """Ordenar un cambio de carril con el asiento vacio y hacerlo con el conductor atento
  son dos productos distintos (seccion 4.2)."""
  for servicio, campo in (("carState", "seatbeltUnlatched"), ("carState", "doorOpen")):
    gm = GateMonitor(sm=_SubMasterFalso(_sm_completo(**{servicio: {campo: True}})))
    gm.note_link()
    gm.update(0)
    assert not (gm.mask & Gate.DRIVER_PRESENT)
    ok, fallados = gm.evaluate(COMMANDS["lane_change"])
    assert not ok and Gate.DRIVER_PRESENT in fallados

  gm = GateMonitor(sm=_SubMasterFalso(_sm_completo(driverMonitoringState={
    "visionPolicyState": type("V", (), {"faceDetected": False})()})))
  gm.note_link()
  gm.update(0)
  assert not (gm.mask & Gate.DRIVER_PRESENT)


def test_driver_idle_lo_cancela_cualquier_intervencion():
  for campo in ("gasPressed", "brakePressed", "steeringPressed"):
    gm = GateMonitor(sm=_SubMasterFalso(_sm_completo(carState={campo: True})))
    gm.update(0)
    assert not (gm.mask & Gate.DRIVER_IDLE), campo


def test_speed_range_usa_el_rango_del_verbo():
  gm = GateMonitor(sm=_SubMasterFalso(_sm_completo(carState={"vEgo": 10.0})))  # 36 km/h
  gm.note_link()
  gm.update(0)
  assert not (gm.mask & Gate.SPEED_RANGE)                      # fuera de 40-130
  ok, fallados = gm.evaluate(COMMANDS["lane_change"])
  assert not ok and Gate.SPEED_RANGE in fallados
  # el mismo estado esta DENTRO del rango de un verbo de banco... salvo que 36 km/h
  # tampoco cabe en su tope de 20
  ok, fallados = gm.evaluate(COMMANDS["steering_pulse"])
  assert Gate.SPEED_RANGE in fallados


def test_servicio_muerto_deja_los_gates_en_rojo():
  """Fail-closed: un gate que se cae en verde ante un error autoriza una maniobra justo
  cuando el coche esta peor informado."""
  gm = GateMonitor(sm=_SubMasterFalso(_sm_completo(), vivos={"carState": False, "selfdriveState": False}))
  gm.note_link()
  gm.update(0)
  assert not (gm.mask & Gate.ENGAGED)
  assert not (gm.mask & Gate.DRIVER_IDLE)
  # y SPEED_RANGE no puede darse por bueno con vEgo desconocido (valdria 0.0)
  ok, fallados = gm.evaluate(COMMANDS["physical_control"])
  assert not ok and Gate.SPEED_RANGE in fallados


def test_sin_submaster_todos_los_gates_estan_en_rojo():
  gm = GateMonitor(sm=None, servicios=())
  gm._sm = None
  ok, fallados = gm.evaluate(COMMANDS["lane_change"])
  assert not ok
  assert len(fallados) == len([g for g in Gate if int(COMMANDS["lane_change"].gates) & g])


def test_link_fresh_caduca():
  gm = GateMonitor(sm=_SubMasterFalso(_sm_completo()), link_fresh_max_s=0.0)
  gm.note_link()
  gm.update(0)
  assert not (gm.mask & Gate.LINK_FRESH)


# ------------------------------------------------------------------------ capacidades

def test_el_descriptor_solo_declara_verbos_con_handler():
  r, _, _ = _router(verbos=("lane_change", "disarm_all"))
  caps = r.capabilities_payload(platform="FORD_FOCUS_MK4", fw="0.0.0")
  assert set(caps["verbs"]) == {"lane_change", "disarm_all"}
  assert caps["unsupported"]["cruise_delta"] == "no_handler"
  assert caps["unsupported"]["brutebreak"] == "replaced_by_assisted_decel"
  assert caps["brand"] == "ford"
  json.dumps(caps)      # tiene que ser serializable tal cual


def test_registrar_un_verbo_que_no_esta_en_la_tabla_falla():
  """COMMANDS es la UNICA fuente de verdad: no se cuela un verbo por la puerta de atras."""
  r, _, _ = _router()
  with pytest.raises(ValueError):
    r.register_handler("frenazo_secreto", lambda cmd: None)


def test_el_worker_ejecuta_en_su_propio_hilo():
  """El callback de paho es el hilo de RED: un handler lento ahi tira el PINGRESP."""
  import threading
  hilos = queue.Queue()
  r, _, _ = _router(verbos=("disarm_all",))
  r.register_handler("lane_change", lambda cmd: hilos.put(threading.current_thread().name))
  r.start()
  try:
    assert r.handle_payload(TOPIC, _sobre()).aceptado
    nombre = hilos.get(timeout=3.0)
  finally:
    r.stop()
  assert nombre == "OrbitCommandWorker"
  assert nombre != threading.current_thread().name


# ------------------------------------------- el enlace fresco del primer mando (M4)

def test_el_primer_mando_de_la_sesion_no_muere_por_LINK_FRESH():
  """handle_payload marca el enlace y evalua los gates EN EL MISMO INSTANTE.

  El bit LINK_FRESH lo calcula GateMonitor.update() a 10 Hz, asi que en el primer mando
  -- cuando aun no habia habido trafico -- la mascara todavia dice 0 y el ACK salia
  GATE_LINK_FRESH. Fail-closed, pero es un boton que falla siempre la primera vez.
  """
  gm = GateMonitor(sm=_SubMasterFalso(_sm_completo()))
  gm.update(0)                                    # tick del plano SIN trafico previo
  assert not (gm.mask & Gate.LINK_FRESH), "el escenario ya no reproduce el fallo"

  publicados = []
  r = CommandRouter(DONGLE, gates=gm, store=_store(Mode.MANEUVER),
                    publish=lambda t, p, q, ret: publicados.append((t, json.loads(p), q, ret)))
  r.register_handler("lane_change", lambda cmd: None)
  res = r.handle_payload(TOPIC, _sobre())
  assert res.aceptado, f"{res.reason}: {res.detail}"


def test_si_el_enlace_no_esta_fresco_de_verdad_el_gate_sigue_rojo():
  """No se relaja la precondicion: se lee la misma fuente sin esperar al siguiente tick.
  Con una ventana de frescura de 0 s, ningun trafico cuenta como reciente."""
  gm = GateMonitor(sm=_SubMasterFalso(_sm_completo()), link_fresh_max_s=0.0)
  gm.update(0)
  r = CommandRouter(DONGLE, gates=gm, store=_store(Mode.MANEUVER), publish=lambda *a: None)
  r.register_handler("lane_change", lambda cmd: None)
  res = r.handle_payload(TOPIC, _sobre())
  assert not res.aceptado and res.reason == "GATE_LINK_FRESH", res.reason


# ------------------------------------ la ventana del actuador es UNA sola (ALTA 1)

def _deadline(r):
  return r.store.snapshot()["deadline_mono"]


def test_un_verbo_no_hereda_la_ventana_larga_de_otro():
  """El plano tiene UN solo deadline_mono. Tras un torque_mode (15 s) la ventana no
  puede quedarse abierta para el verbo siguiente, que dura 3 s."""
  r, _, _ = _router(mode=Mode.BENCH, bench=True, verbos=("torque_mode", "lane_change"))
  assert r.handle_payload(TOPIC, _sobre("torque_mode", args={"mode": 2}, mode="banco",
                                        ttl_ms=15_000)).aceptado
  r.drenar()
  ventana_banco = _deadline(r)
  assert ventana_banco > ahora_mono() + 10.0     # ~15 s

  assert r.handle_payload(TOPIC, _sobre("lane_change")).aceptado
  r.drenar()
  # La ventana pasa a ser la del cambio de carril (3 s), no la heredada de banco.
  assert _deadline(r) <= ahora_mono() + 3.1
  assert _deadline(r) < ventana_banco


def test_un_verbo_no_puede_alargar_la_ventana_ya_abierta_por_otro():
  """Y al reves: un verbo de TTL largo llegado con una ventana corta viva no la
  alarga. Recortar BAJA autoridad, y bajar autoridad siempre se acepta (seccion 2)."""
  r, _, _ = _router(mode=Mode.BENCH, bench=True, verbos=("torque_mode", "lane_change"))
  assert r.handle_payload(TOPIC, _sobre("lane_change")).aceptado
  r.drenar()
  ventana_corta = _deadline(r)

  assert r.handle_payload(TOPIC, _sobre("torque_mode", args={"mode": 2}, mode="banco",
                                        ttl_ms=15_000)).aceptado
  r.drenar()
  assert _deadline(r) == ventana_corta, "el verbo de banco se regalo su TTL entero"


def test_renovar_el_MISMO_verbo_si_refresca_su_ventana():
  """Si no, un mando de banco renovado cada pocos segundos se apagaria solo al llegar al
  deadline del primero y no habria forma de mantenerlo vivo."""
  r, _, _ = _router(mode=Mode.BENCH, bench=True, verbos=("torque_mode",))
  assert r.handle_payload(TOPIC, _sobre("torque_mode", args={"mode": 2}, mode="banco",
                                        ttl_ms=15_000)).aceptado
  r.drenar()
  primera = _deadline(r)
  assert r.handle_payload(TOPIC, _sobre("torque_mode", args={"mode": 2}, mode="banco",
                                        ttl_ms=15_000)).aceptado
  r.drenar()
  assert _deadline(r) >= primera


# -------------------------------------- eco de la publicacion dual v1+v2 (M2)

def test_la_copia_v1_de_un_mando_v2_no_se_ejecuta_dos_veces():
  """Las rutas legacy del backend publican el topic v1 Y el verbo v2 de la MISMA
  pulsacion, con id y contador de secuencia distintos: sin esto son dos maniobras."""
  r, publicados, ejecutados = _router()
  assert r.handle_payload(TOPIC, _sobre("lane_change", args={"direction": "left"})).aceptado
  res = r.submit_local("lane_change", {"direction": "left"})
  assert res.descartado and not res.aceptado
  r.drenar()
  assert len(ejecutados) == 1, "el cambio de carril se ejecuto dos veces"
  # Y la copia descartada NO publica un segundo veredicto. lane_change espera al
  # consumidor, asi que el veredicto que se cuenta es el EXECUTING del gemelo v2: uno solo.
  assert [p["phase"] for _, p, _, _ in publicados].count(Phase.EXECUTING) == 1
  assert [p["phase"] for _, p, _, _ in publicados].count(Phase.APPLIED) == 0


def test_si_llega_antes_la_copia_v1_la_v2_dice_DUPLICATE_y_no_se_repite():
  """El orden entre las dos publicaciones no esta garantizado. Se ejecuta una sola vez y
  se dice por que, en vez de dejar que la app crea que su orden se perdio."""
  r, publicados, ejecutados = _router()
  assert r.submit_local("lane_change", {"direction": "right"}).aceptado
  res = r.handle_payload(TOPIC, _sobre("lane_change", args={"direction": "right"}))
  assert not res.aceptado and res.reason == "DUPLICATE"
  r.drenar()
  assert len(ejecutados) == 1


def test_dos_ordenes_iguales_por_el_MISMO_camino_son_dos_ordenes():
  """La supresion cruza caminos distintos y solo eso: dos pulsaciones reales del usuario
  viajan siempre por el mismo, y tragarse la segunda seria un boton que no responde."""
  r, _, ejecutados = _router()
  assert r.handle_payload(TOPIC, _sobre("lane_change", args={"direction": "left"})).aceptado
  assert r.handle_payload(TOPIC, _sobre("lane_change", args={"direction": "left"})).aceptado
  r.drenar()
  assert len(ejecutados) == 2


def test_el_presupuesto_de_ritmo_no_se_gasta_dos_veces_por_la_publicacion_dual():
  """Medido antes del arreglo: un cruise_delta de la ruta legacy gastaba el DOBLE del
  presupuesto de +-20 km/h por minuto."""
  r, _, _ = _router(mode=Mode.COPILOT, verbos=("cruise_delta",))
  for _ in range(4):
    assert r.handle_payload(TOPIC, _sobre("cruise_delta", args={"delta_kph": 5.0},
                                          mode="copiloto")).aceptado
    assert r.submit_local("cruise_delta", {"delta_kph": 5.0}).descartado
  # 4 x 5 = 20 km/h: justo el presupuesto. Con la copia v1 contando serian 40.
  res = r.handle_payload(TOPIC, _sobre("cruise_delta", args={"delta_kph": 5.0}, mode="copiloto"))
  assert not res.aceptado and res.reason == "RANGE"


def test_con_id_de_origen_las_dos_copias_son_LA_MISMA_orden():
  """Cuando el emisor v1 sabe con que id salio el sobre v2 gemelo, el cruce lo hace la
  LRU de idempotencia y no una heuristica por parecido."""
  r, publicados, ejecutados = _router()
  sobre = json.loads(_sobre("lane_change", args={"direction": "left"}))
  assert r.handle_payload(TOPIC, json.dumps(sobre)).aceptado
  res = r.submit_local("lane_change", {"direction": "left"}, origin_id=sobre["id"])
  assert res.descartado
  r.drenar()
  assert len(ejecutados) == 1
  assert [p["id"] for _, p, _, _ in publicados].count(sobre["id"]) == len(publicados)


def test_un_verbo_distinto_no_se_confunde_con_un_eco():
  r, _, ejecutados = _router(verbos=("lane_change", "disarm_all"))
  assert r.handle_payload(TOPIC, _sobre("lane_change", args={"direction": "left"})).aceptado
  assert r.submit_local("lane_change", {"direction": "right"}).aceptado
  r.drenar()
  assert len(ejecutados) == 2


# ------------------------------------------------------------- verbo set_mode (§4.1)

def test_set_mode_es_alcanzable_desde_observador_y_no_arma_actuador():
  """Si exigiera el modo de destino no se podria subir nunca. Y cambiar de modo no mueve
  nada, asi que no puede abrir una ventana de actuador."""
  spec = get_spec("set_mode")
  assert spec is not None
  assert spec.mode_min == Mode.OBSERVER
  assert spec.arma_actuador is False
  assert spec.ttl_ms == 5_000


def test_set_mode_no_ofrece_el_modo_banco():
  """El modo banco solo se alcanza con armado FISICO en la pantalla del comma. Si
  estuviera aqui, un mensaje MQTT abriria torque_mode, steering_pulse y physical_control."""
  opciones = get_spec("set_mode").args_schema["target_mode"].opciones
  assert set(opciones) == {"observador", "copiloto", "maniobra"}
  reason, detail, _ = validar_args(get_spec("set_mode"), {"target_mode": "banco"})
  assert reason == "RANGE", detail


def test_assisted_decel_ya_no_declara_un_hold_que_nadie_lee():
  """El hold real es la constante fija de controlsd (ORBIT_DECEL_HOLD_MAX_S). Declarar
  hold_ms era prometer un control que el ejecutor ignora."""
  assert set(get_spec("assisted_decel").args_schema) == {"accel"}
  reason, _, _ = validar_args(get_spec("assisted_decel"), {"accel": -2.0, "hold_ms": 500})
  assert reason == "TYPE"
