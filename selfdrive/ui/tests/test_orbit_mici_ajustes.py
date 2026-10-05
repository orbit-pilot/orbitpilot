"""Logica compartida de los ajustes ORBIT (comma 3X y comma 4): selfdrive/ui/widgets/orbit_ajustes.py.

Logica pura con Params falsos: sin ventana, sin raylib y sin tocar Params reales ni
orbit/config_*.json (que en el PC son ficheros del arbol git).
"""
import ast
import json
import pathlib
from types import SimpleNamespace

import pytest

from openpilot.orbit import telemetria_grupos as tg
from openpilot.selfdrive.ui.widgets import orbit_ajustes as aj
from openpilot.selfdrive.ui.widgets import orbit_mando as mando

UI_DIR = pathlib.Path(__file__).resolve().parents[1]


class _Params:
  """Params en memoria con la firma que usa la UI (put(k, v, block), put_bool, remove)."""

  def __init__(self, datos=None, rotas=()):
    self.d = dict(datos or {})
    self.rotas = set(rotas)
    self.escrituras: list[str] = []

  def _check(self, k):
    if k in self.rotas:
      raise TypeError(f"rota {k}")

  def get(self, k):
    return self.d.get(k)

  def get_bool(self, k):
    return bool(self.d.get(k, False))

  def put(self, k, v, block=False):
    self._check(k)
    self.d[k] = v
    self.escrituras.append(k)

  def put_bool(self, k, v, block=False):
    self.put(k, bool(v), block)

  def remove(self, k):
    self._check(k)
    self.d.pop(k, None)
    self.escrituras.append(k)


@pytest.fixture
def params(monkeypatch):
  p = _Params({"DongleId": "abc123"})
  # orbit_mando usa su propia instancia de Params (set_steer_local, disarm_bench...)
  monkeypatch.setattr(mando, "params", lambda: p)
  return p


# --------------------------------------------------------------------------- servidor
@pytest.mark.parametrize("host", ["192.168.1.10", "83.61.25.38", "0.0.0.0", "255.255.255.255", " 10.0.0.1 ",
                                  "localhost", "orbit.example.com", "mi-servidor.lan", "a" * 63 + ".com"])
def test_validar_host_acepta(host):
  assert aj.validar_host(host) == ""


@pytest.mark.parametrize("host", ["", "   ", "192.168.1.300", "192.168.1", "1.2.3.4.5", "999", "1.2.3.4:1883",
                                  "http://1.2.3.4", "orbit.com/api", "1.2.3 .4", "mi servidor", "a..b", "-a.com",
                                  "a-.com", "srv_1.lan", "a" * 64 + ".com", ("a." * 127) + "com", "1.2.3.4."])
def test_validar_host_rechaza(host):
  assert aj.validar_host(host) != ""


def test_url_y_caducidad_del_qr():
  assert aj.url_enrolamiento("abc123", "K7QX4MPA") == "orbit://enroll?d=abc123&c=K7QX4MPA"
  assert aj.url_enrolamiento(None, None) == "orbit://enroll?d=&c="
  assert aj.texto_caducidad(str(1_000_000 + 487_000), 1_000_000) == "caduca en 8:07"
  assert aj.texto_caducidad("1000", 5_000) == "caduca en 0:00"
  assert aj.texto_caducidad(None, 0) == "" and aj.texto_caducidad("basura", 0) == ""


def test_etiqueta_cuenta_con_rol():
  assert aj.etiqueta_cuenta("Adrian Garcia", "superadmin") == "Adrian • superadmin"
  assert aj.etiqueta_cuenta("Ana", "developer") == "Ana • desarrollador"
  assert aj.etiqueta_cuenta("usuario@correo.com", "raro") == "usuario@corr"
  assert aj.etiqueta_cuenta("", "user") == "" and aj.etiqueta_cuenta(None, None) == ""


def test_enlace_orbit(monkeypatch):
  monkeypatch.setattr(aj.time, "time_ns", lambda: 1_000 * 10**9)
  assert aj.enlace_orbit(_Params({"OrbitConnected": True, "OrbitLastPublish": "990"}))
  assert not aj.enlace_orbit(_Params({"OrbitConnected": True, "OrbitLastPublish": "960"}))   # rancio (> 30 s)
  assert not aj.enlace_orbit(_Params({"OrbitConnected": False, "OrbitLastPublish": "999"}))
  assert not aj.enlace_orbit(_Params({"OrbitConnected": True}))


def test_desvincular_borra_dueno_y_rol_y_pide_codigo(params):
  params.d.update({"OrbitClaimed": True, "OrbitOwner": "Ana", "OrbitOwnerRole": "developer"})
  assert aj.desvincular(params) == []
  assert params.d["OrbitClaimed"] is False and params.d["OrbitEnrollRegen"] is True
  assert "OrbitOwner" not in params.d and "OrbitOwnerRole" not in params.d
  assert aj.desvincular(_Params(rotas={"OrbitOwnerRole"})) == ["OrbitOwnerRole"]  # independientes


# --------------------------------------------------------------------------- mando
def _st(**kw):
  base = dict(available=True, mode="observer", mode_label="OBSERVADOR", link_ok=True, clock_synced=True,
              last_ack_phase="none", active_verb="", phase_label="-", last_reason="", phase_is_bad=False)
  base.update(kw)
  return SimpleNamespace(**base)


def test_lineas_mando_mismos_tonos_que_la_ui_grande():
  (_, modo, t_modo), (_, enlace, t_enl), (_, ultimo, t_ult) = aj.lineas_mando(_st())
  assert (modo, t_modo, enlace, t_enl, ultimo, t_ult) == ("OBSERVADOR", "ok", "OK", "ok", "ninguno", "normal")
  lineas = aj.lineas_mando(_st(mode="copilot", mode_label="COPILOTO", clock_synced=False, last_ack_phase="rejected",
                               active_verb="lane_change", phase_label="RECHAZADO", last_reason="gate", phase_is_bad=True))
  assert [x[2] for x in lineas] == ["aviso", "aviso", "peligro"]
  assert lineas[2][1] == "lane_change  -  RECHAZADO  (gate)"
  assert aj.lineas_mando(_st(link_ok=False))[1][1:] == ("CAIDO", "peligro")


# --------------------------------------------------------------------------- volante
def test_estado_del_volante():
  assert aj.texto_estado_volante(0, None, False, 0, False) == "torque del modelo interno"
  assert aj.texto_estado_volante(3, "torque", False, 0, False) == "esquive en TORQUE"
  assert aj.texto_estado_volante(3, None, False, 0, False) == "esquive en CURVATURA"
  assert aj.texto_estado_volante(1, None, True, 42.9, False) == "ACTIVO, banco armado (42s)"
  assert aj.texto_estado_volante(2, None, False, 0, False).startswith("SIN EFECTO")
  # Elegido en ESTA pantalla (OrbitSteerModeLocal): controlsd lo aplica sin banco armado.
  for modo in (1, 2):
    assert aj.texto_estado_volante(modo, None, False, 0, True) == "ACTIVO, elegido en pantalla"
  for modo in (0, 1, 2, 3):
    texto, boton = aj.confirmacion_modo(modo)
    assert texto and boton
    # El selector no arma el banco: el texto no puede prometerlo.
    assert "ARMA EL BANCO" not in texto
  assert "TEST MAX" in aj.confirmacion_modo(2)[1]


def test_esquive_torque_dice_como_cancelar_en_cada_pantalla():
  assert aj.texto_esquive_torque()[0].endswith("Pulsa Cancelar para no cambiar nada.")
  texto = aj.texto_esquive_torque("Desliza hacia abajo para no cambiar nada.")[0]
  assert "Cancelar" not in texto and texto.endswith("Desliza hacia abajo para no cambiar nada.")


def test_aplicar_modo_volante_escribe_modo_autorizacion_y_payload(params):
  aj.aplicar_modo_volante(params, 1)
  assert params.d["SteerTorqueMode"] == 1 and params.d[mando.PARAM_STEER_LOCAL] is True
  assert params.escrituras.index("SteerTorqueMode") < params.escrituras.index(mando.PARAM_STEER_LOCAL)
  payload = json.loads(params.d["SteerTorqueModeMqttPayload"])
  assert list(payload) == ["dongle_id", "steer_torque_mode", "source", "timestamp"]
  assert (payload["dongle_id"], payload["steer_torque_mode"], payload["source"]) == ("abc123", 1, "comma_ui")
  # El selector NUNCA arma el banco (eso abriria los verbos fisicos por MQTT).
  assert mando.PARAM_BENCH_ARMED not in params.d

  aj.aplicar_modo_volante(params, 3)
  assert params.d["SteerTorqueMode"] == 3 and params.d[mando.PARAM_STEER_LOCAL] is False


def test_objetivo_de_esquive(params):
  aj.fijar_objetivo_esquive(params, "torque")
  assert params.d["JetsonObstacleApplyTarget"] == "torque"
  assert json.loads(params.d["JetsonObstacleApplyTargetMqttPayload"])["apply_target"] == "torque"


# --------------------------------------------------------------------------- valores seguros
def test_valores_seguros_siete_grupos(params):
  params.d.update({"SteerTorqueMode": 2, "brutebreak_active": True, "ForceLaneChangeLeft": True,
                   "orbit_steering_pulse": "x", mando.PARAM_BENCH_ARMED: True, mando.PARAM_COMMAND_MODE: 2,
                   "sic_adelantar": True, "modo_debug": True})
  assert aj.aplicar_valores_seguros(params) == []
  d = params.d
  assert d["SteerTorqueMode"] == 0 and d[mando.PARAM_COMMAND_MODE] == 0
  assert not any(d[k] for k in ("brutebreak_active", "ForceLaneChangeLeft", "ForceLaneChangeRight", mando.PARAM_BENCH_ARMED,
                                "sic_adelantar", "modo_debug"))
  assert "orbit_steering_pulse" not in d and mando.PARAM_BENCH_EXPIRY not in d


def test_valores_seguros_un_fallo_no_para_el_resto(monkeypatch):
  p = _Params(rotas={"SteerTorqueMode", "modo_debug"})
  monkeypatch.setattr(mando, "params", lambda: p)
  assert aj.aplicar_valores_seguros(p) == ["SteerTorqueMode", "modo_debug"]
  assert p.d["sic_adelantar"] is False and p.d["brutebreak_active"] is False


# --------------------------------------------------------------------------- telemetria
GRUPO = {g.clave: g for g in tg.GRUPOS}


def test_telemetria_sin_configurar_esta_todo_encendido_y_nada_bloqueado():
  # El _Params falso, como el real, lee un param sin configurar como False con get_bool: el
  # helper no puede usarlo o pintaria OFF mientras el canal emite.
  p = _Params()
  assert all(aj.encendido_grupo(p, g, False) for g in tg.GRUPOS)
  assert not any(aj.grupo_bloqueado(g, False) for g in tg.GRUPOS)
  assert all(aj.descripcion_grupo(g, False) == g.descripcion for g in tg.GRUPOS)


def test_telemetria_un_solo_canal_apagado_apaga_el_grupo():
  # La app puede apagar un canal v1 suelto por cfg/desired: el grupo no se pinta "todo encendido".
  p = _Params({"carControl_toggle": False})
  assert not aj.encendido_grupo(p, GRUPO["vehiculo"], False)
  assert aj.encendido_grupo(p, GRUPO["percepcion"], False)
  assert not aj.encendido_grupo(_Params({"tel2_trip_toggle": False}), GRUPO["eventos"], False)


def test_telemetria_posicion_con_la_privacidad_puesta():
  p = _Params()
  pos = GRUPO["posicion"]
  assert aj.grupo_bloqueado(pos, True)
  assert not aj.encendido_grupo(p, pos, True)   # no se pinta ON lo que la privacidad esta cortando
  texto = aj.descripcion_grupo(pos, True)
  assert "privacidad" in texto and texto != pos.descripcion
  # Solo la posicion: el resto sigue editable y con su texto.
  resto = [g for g in tg.GRUPOS if not g.posicion]
  assert resto and not any(aj.grupo_bloqueado(g, True) for g in resto)
  assert all(aj.descripcion_grupo(g, True) == g.descripcion for g in resto)


def test_telemetria_cambiar_grupo_escribe_todos_sus_params():
  p = _Params()
  for g in tg.GRUPOS:
    assert aj.cambiar_grupo(p, g, False, False) == []
    assert not aj.encendido_grupo(p, g, False)
    assert all(p.d[k] is False for k in tg.params_de(g))
    assert aj.cambiar_grupo(p, g, True, False) == []
    assert aj.encendido_grupo(p, g, False)
    assert all(p.d[k] is True for k in tg.params_de(g))


def test_telemetria_cambiar_posicion_con_privacidad_no_escribe_nada():
  # Cinturon: la fila ya sale deshabilitada, pero encender aqui anularia el interruptor.
  p = _Params()
  assert aj.cambiar_grupo(p, GRUPO["posicion"], True, True) == [] and p.escrituras == []


def test_telemetria_cambiar_grupo_devuelve_los_que_fallan_y_escribe_el_resto():
  p = _Params(rotas={"tel2_vehicle_toggle"})
  assert aj.cambiar_grupo(p, GRUPO["vehiculo"], False, False) == ["tel2_vehicle_toggle"]
  assert p.d["carState_toggle"] is False and p.d["carControl_toggle"] is False


def test_ahorro_movil_apagado_por_defecto_y_se_escribe():
  p = _Params()
  assert not tg.ahorro_movil_activo(p)
  assert aj.cambiar_ahorro_movil(p, True) == [] and tg.ahorro_movil_activo(p)
  assert aj.cambiar_ahorro_movil(p, False) == [] and not tg.ahorro_movil_activo(p)
  assert aj.cambiar_ahorro_movil(_Params(rotas={tg.PARAM_AHORRO_MOVIL}), True) == [tg.PARAM_AHORRO_MOVIL]
  texto = aj.texto_ahorro_movil()
  assert "AHORRO" in texto and "pedales" in texto


# --------------------------------------------------------------------------- privacidad
@pytest.fixture
def privacidad(params, monkeypatch, tmp_path):
  """set_privacy_mute con todo su estado en tmp_path: nada toca /data ni orbit/config_jetson.json."""
  monkeypatch.setattr(mando, "PRIVACY_STATE_FILE", str(tmp_path / "orbit_privacy.json"))
  monkeypatch.setattr(mando, "CAMERA_CONFIG_FILE", str(tmp_path / "orbit_camera_config.json"))
  monkeypatch.setattr(mando, "_jetson_config_path", lambda: str(tmp_path / "config_jetson.json"))
  return params


def test_quitar_la_privacidad_deja_encendido_el_gps_que_nunca_se_configuro(privacidad):
  p = privacidad
  assert "gpsLocation_toggle" not in p.d and "gpsLocationExternal_toggle" not in p.d
  assert mando.set_privacy_mute(True) == [] and mando.privacy_muted()
  assert p.d["gpsLocation_toggle"] is False and p.d["gpsLocationExternal_toggle"] is False
  # Silenciar dos veces no puede guardar el False del primer silencio como "lo de antes".
  assert mando.set_privacy_mute(True) == []
  assert mando.set_privacy_mute(False) == [] and not mando.privacy_muted()
  assert p.d["gpsLocation_toggle"] is True and p.d["gpsLocationExternal_toggle"] is True


def test_quitar_la_privacidad_respeta_el_gps_apagado_a_proposito(privacidad):
  p = privacidad
  p.d["gpsLocation_toggle"] = False
  assert mando.set_privacy_mute(True) == [] and mando.set_privacy_mute(False) == []
  assert p.d["gpsLocation_toggle"] is False            # lo apago el usuario: se queda asi
  assert p.d["gpsLocationExternal_toggle"] is True     # este nunca se configuro: encendido

# --------------------------------------------------------------------------- jetson
def test_config_jetson_guardar_y_cargar(tmp_path, params):
  ruta = str(tmp_path / "sub" / "config_jetson.json")
  config = aj.cargar_config_jetson(ruta)
  assert config == aj.CONFIG_DEFAULTS
  config["jetson_ip"] = "10.0.0.2"
  assert aj.guardar_config_jetson(ruta, config, params)
  assert aj.cargar_config_jetson(ruta)["jetson_ip"] == "10.0.0.2"
  assert params.d["JetsonConfigChanged"] is True
  payload = json.loads(params.d["JetsonConfigMqttPayload"])
  assert payload["jetson_ip"] == "10.0.0.2" and payload["_version"] == config["_version"]
  assert not aj.guardar_config_jetson("/proc/no/se/puede/config.json", dict(config), _Params())


def test_parsear_campo_como_la_ui_grande():
  assert aj.parsear_campo(" 5557 ", True) == 5557
  assert aj.parsear_campo("abc", True) is None and aj.parsear_campo("  ", False) is None
  assert aj.parsear_campo("84", True, (10, 100), 10) == 80
  assert aj.parsear_campo("7", True, (10, 100), 10) == 10
  assert aj.parsear_campo("400", True, (10, 100), 10) == 100
  assert aj.parsear_campo(" 192.168.1.50 ", False) == "192.168.1.50"


def test_estado_vivo_de_la_jetson():
  assert aj.texto_torque("0.1234", "99.0", 100.0) == "+0.12"
  assert aj.texto_torque("0.5", "90.0", 100.0) == "-"      # rancio
  assert aj.texto_torque("", "99", 100.0) == "-" and aj.texto_torque("x", "99", 100.0) == "-"
  assert aj.texto_obstaculo('{"obstacle": true}') == "SI"
  assert aj.texto_obstaculo('{"obstacle": false}') == "NO" and aj.texto_obstaculo("{roto") == "NO"


# --------------------------------------------------------------------------- tarjetas
@pytest.mark.parametrize("texto", [aj.texto_armar_banco(), aj.texto_valores_seguros(), aj.confirmacion_modo(1)[0],
                                   aj.confirmacion_modo(2)[0], aj.texto_esquive_curvatura()[0]])
def test_trocear_no_pierde_texto_ni_se_pasa(texto):
  trozos = aj.trocear(texto, 90)
  assert all(len(t) <= 90 for t in trozos)
  assert " ".join(" ".join(trozos).split()) == " ".join(texto.split())
  assert all(len(t) >= 8 for t in trozos)  # ninguna palabra huerfana en su propia tarjeta


# --------------------------------------------------------------------------- una sola fuente
def test_la_ui_grande_usa_la_logica_compartida():
  """Las dos pantallas llaman a las mismas funciones: nadie reintroduce una copia."""
  usos = {
    "sunnypilot/layouts/settings/orbit_sub_layouts/steer_mode.py": ["aplicar_modo_volante", "confirmacion_modo",
                                                                     "texto_estado_volante", "fijar_objetivo_esquive"],
    "sunnypilot/layouts/settings/orbit_sub_layouts/advanced_settings.py": ["guardar_config_jetson", "parsear_campo"],
    "sunnypilot/layouts/settings/orbit_sub_layouts/server_settings.py": ["validar_host"],
    "sunnypilot/layouts/settings/orbit_panel.py": ["aplicar_valores_seguros", "lineas_mando", "etiqueta_cuenta"],
    "widgets/orbit_enroll_dialog.py": ["url_enrolamiento", "texto_caducidad"],
    "sunnypilot/mici/layouts/orbit.py": ["aplicar_valores_seguros", "lineas_mando", "etiqueta_cuenta", "validar_host"],
    "sunnypilot/mici/layouts/orbit_volante.py": ["aplicar_modo_volante", "confirmacion_modo", "fijar_objetivo_esquive"],
    "sunnypilot/mici/layouts/orbit_avanzado.py": ["guardar_config_jetson", "parsear_campo"],
    # El submenu Telemetria: las dos pantallas leen, bloquean y escriben por los mismos helpers.
    "sunnypilot/layouts/settings/orbit_sub_layouts/telemetry_settings.py": [
      "encendido_grupo", "grupo_bloqueado", "descripcion_grupo", "cambiar_grupo", "cambiar_ahorro_movil", "texto_ahorro_movil"],
    "sunnypilot/mici/layouts/orbit_telemetria.py": [
      "encendido_grupo", "grupo_bloqueado", "descripcion_grupo", "cambiar_grupo", "cambiar_ahorro_movil", "texto_ahorro_movil"],
  }
  for fichero, funciones in usos.items():
    nombres = {n.attr for n in ast.walk(ast.parse((UI_DIR / fichero).read_text())) if isinstance(n, ast.Attribute)}
    nombres |= {a.name for n in ast.walk(ast.parse((UI_DIR / fichero).read_text())) if isinstance(n, ast.ImportFrom) for a in n.names}
    faltan = [f for f in funciones if f not in nombres]
    assert not faltan, f"{fichero} no usa {faltan}"
