"""Submenu ORBIT > Telemetria: grupos de canales que el usuario enciende o apaga.

    Sin configurar = encendido ... un param que nunca se ha escrito NO es un OFF. `get_bool`
                                   los confunde y es lo que dejaba el GPS v1 apagado para
                                   siempre al quitar el interruptor de privacidad.
    Los dos caminos a la vez ...... apagar "Posicion" apaga v1 Y v2: si solo apagara uno la
                                   app seguiria recibiendo coordenadas por el otro.
    Cobertura ..................... todo canal v1 y v2 pertenece a UN grupo y todo param esta
                                   registrado en params_keys.h (un canal nuevo sin toggle o un
                                   param sin registrar fallan aqui, no en el coche).
    Emisor ........................ un canal v2 apagado no sale ni se encola, y un on-change
                                   no se calla hasta el keepalive al reactivarlo.
    Aislamiento ................... una excepcion en el camino v1 no deja mudo el v2.
    Ahorro en red movil ........... opt-in: GsmMetered vale "1" de fabrica.
"""
import json
import os
import types

import pytest

from openpilot.common.basedir import BASEDIR
from openpilot.orbit import events_mqtt
from openpilot.orbit import telemetria_grupos as g
from openpilot.orbit import telemetria_v1 as tel
from openpilot.orbit.test.test_config_y_privacidad_loop import MotorFalso, ParamsFalsos, _envio
from openpilot.orbit.test.test_telemetria_v2 import fuentes_minimas, lector

GRUPO = {x.clave: x for x in g.GRUPOS}


class ParamsQueLanzan:
  def get(self, *a, **k):
    raise RuntimeError("clave no registrada")

  get_bool = get
  put_bool = get


# ------------------------------------------------------------------ modelo

def test_sin_configurar_esta_encendido():
  p = ParamsFalsos()
  assert g.activo(p, "carState_toggle") is True
  assert all(g.grupo_activo(p, x) for x in g.GRUPOS)
  assert g.canales_v2_apagados(p) == frozenset()


def test_solo_un_false_explicito_apaga():
  p = ParamsFalsos({"tel2_pos_toggle": False, "carState_toggle": True})
  assert g.activo(p, "tel2_pos_toggle") is False
  assert g.activo(p, "carState_toggle") is True
  assert g.canales_v2_apagados(p) == frozenset({"pos"})


def test_un_error_de_lectura_no_apaga_nada():
  p = ParamsQueLanzan()
  assert g.activo(p, "tel2_pos_toggle") is True
  assert g.ahorro_movil_activo(p) is False


def test_un_grupo_a_medias_no_se_pinta_encendido():
  """La app (cfg/desired) puede apagar un solo canal v1 del grupo."""
  p = ParamsFalsos({"carControl_toggle": False})
  assert g.grupo_activo(p, GRUPO["vehiculo"]) is False


def test_apagar_un_grupo_apaga_v1_y_v2():
  p = ParamsFalsos()
  assert g.fijar_grupo(p, GRUPO["posicion"], False) == []
  assert p.valores == {"gpsLocation_toggle": False, "gpsLocationExternal_toggle": False,
                       "tel2_pos_toggle": False}
  assert g.grupo_activo(p, GRUPO["posicion"]) is False
  g.fijar_grupo(p, GRUPO["posicion"], True)
  assert g.grupo_activo(p, GRUPO["posicion"]) is True


def test_fijar_grupo_devuelve_lo_que_fallo():
  assert set(g.fijar_grupo(ParamsQueLanzan(), GRUPO["eventos"], False)) == {"tel2_event_toggle", "tel2_trip_toggle"}


def test_el_ahorro_en_red_movil_esta_apagado_por_defecto():
  assert g.ahorro_movil_activo(ParamsFalsos()) is False
  assert g.ahorro_movil_activo(ParamsFalsos({g.PARAM_AHORRO_MOVIL: True})) is True


# ------------------------------------------------------------------ cobertura

def test_todo_canal_v2_pertenece_a_un_grupo():
  en_grupos = [c for x in g.GRUPOS for c in x.v2]
  assert sorted(en_grupos) == sorted(tel.CANALES), "un canal v2 sin grupo no se puede apagar desde el menu"
  assert len(en_grupos) == len(set(en_grupos))


def test_todo_canal_v1_pertenece_a_un_grupo():
  with open(os.path.join(BASEDIR, "orbit", "canales.json")) as f:
    v1 = [c["canal"] for c in json.load(f)["canales"]]
  en_grupos = [c for x in g.GRUPOS for c in x.v1]
  assert sorted(en_grupos) == sorted(v1)
  assert len(en_grupos) == len(set(en_grupos))


def test_todos_los_params_estan_registrados():
  with open(os.path.join(BASEDIR, "common", "params_keys.h")) as f:
    cabecera = f.read()
  for x in g.GRUPOS:
    for k in g.params_de(x):
      assert f'"{k}"' in cabecera, f"{k} no esta en common/params_keys.h"
  assert f'"{g.PARAM_AHORRO_MOVIL}"' in cabecera


def test_solo_el_grupo_de_posicion_lo_gobierna_la_privacidad():
  assert [x.clave for x in g.GRUPOS if x.posicion] == ["posicion"]


# ------------------------------------------------------------------ emisor

def _mensajes():
  return [("vehicle", {"d": {"v": 1}}), ("perception", {"d": {"lead": 0}}), ("openpilot", {"d": {"a": 1}})]


def test_un_canal_v2_apagado_no_sale_y_se_revierte_su_sello(tmp_path):
  motor = MotorFalso(_mensajes())
  e = _envio(tmp_path, params=ParamsFalsos({"tel2_perception_toggle": False}), motor=motor)
  e._ciclo_v2(1000.0)
  topics = [t for t, *_ in e.mqttc.publicados]
  assert [t.rsplit("/", 1)[1] for t in topics] == ["vehicle", "openpilot"]
  # El estado on-change ya se sello dentro del tick: sin revertir, al reactivar el canal se
  # callaria hasta el keepalive (road: 600 s en normal).
  assert motor.revertidos == ["perception"]


def test_con_todo_encendido_sale_todo(tmp_path):
  e = _envio(tmp_path, motor=MotorFalso(_mensajes()))
  e._ciclo_v2(1000.0)
  assert len(e.mqttc.publicados) == 3


def test_reactivar_un_canal_se_nota_en_caliente(tmp_path):
  p = ParamsFalsos({"tel2_vehicle_toggle": False})
  e = _envio(tmp_path, params=p, motor=MotorFalso(_mensajes()))
  e._ciclo_v2(1000.0)
  assert len(e.mqttc.publicados) == 2
  p.valores["tel2_vehicle_toggle"] = True
  e._tel2_ts = float("-inf")           # vence la cache de 1 s
  e._ciclo_v2(1001.0)
  assert len(e.mqttc.publicados) == 2 + 3


def test_un_canal_apagado_tampoco_se_encola_en_el_spool(tmp_path):
  from openpilot.orbit import spool as spool_mod
  s = spool_mod.Spool(ruta=str(tmp_path / "spool"))
  e = _envio(tmp_path, params=ParamsFalsos({"tel2_perception_toggle": False}),
             motor=MotorFalso(_mensajes()), spool=s)
  e.conectado = False                   # sin enlace todo va al spool
  e._ciclo_v2(1000.0)
  s.flush()
  assert s.estado()["filas"] == 2
  s.cerrar()


def test_el_ahorro_en_red_movil_llega_al_motor(tmp_path):
  p = ParamsFalsos()
  e = _envio(tmp_path, params=p)
  e._last_perfil_check = float("-inf")
  e._maybe_reload_perfil(1000.0)
  assert e.motor_v2.degradar_por_red is False
  p.valores[g.PARAM_AHORRO_MOVIL] = True
  e._last_perfil_check = float("-inf")
  e._maybe_reload_perfil(1010.0)
  assert e.motor_v2.degradar_por_red is True


def test_el_motor_no_degrada_si_el_ahorro_esta_apagado():
  motor = tel.MotorTelemetria(tel.PERFIL_NORMAL)
  motor.degradar_por_red = False
  metered = lector("deviceState", networkMetered=True, started=False)
  motor.tick(10.0, fuentes_minimas(deviceState=metered), ts_ms=1)
  assert motor.perfil == tel.PERFIL_NORMAL


def test_una_excepcion_en_el_camino_v1_no_deja_mudo_el_v2(tmp_path):
  e = _envio(tmp_path)
  llamadas = []
  e.sm = types.SimpleNamespace(update=lambda t: None)
  e._last_v1 = float("-inf")
  e.velocidadActualizacion = 1.0
  e.TICK_SECS = 0.0

  def v1_roto():
    raise RuntimeError("un campo que ya no existe tras un rebase")

  e._ciclo_v1 = v1_roto
  e._ciclo_v2 = lambda ahora: llamadas.append(ahora)
  e._loop_once()
  assert len(llamadas) == 1, "el v2 no corrio porque el v1 lanzo"


# ------------------------------------------------------------------ eventos v1

def test_eventos_v1_apagados_se_dan_por_atendidos_sin_tocar_el_broker(monkeypatch):
  monkeypatch.setattr(events_mqtt, "Params", lambda: ParamsFalsos({"tel2_event_toggle": False}))
  monkeypatch.setattr(events_mqtt, "_ensure_mqtt_client",
                      lambda: pytest.fail("no debe abrir el cliente con el grupo Eventos apagado"))
  # True, no False: mirror_alerts reintentaria un False en cada ciclo de selfdrived.
  assert events_mqtt.send_event_full("t", "m", 3, dongle_id="d", event_name="x", event_type="warning") is True
