"""Humo de render del panel ORBIT del comma 4 (mici, 536x240).

Abre los ajustes, la baldosa "orbit", cada subpagina y dialogo (volante y sus
confirmaciones, ayuda, avanzados, telemetria, QR, IP del servidor, valores seguros, desvincular) y
pinta ~30 frames de cada uno sin excepciones. Recorre ademas los caminos que no se ven:
IP invalida (reabre la entrada con el aviso encima), IP valida (escribe SOLO la clave
broker), DESARMAR TODO, el QR que se cierra al vincular y el override del timeout.

Corre en un proceso aparte: `BIG` se lee al importar application.py y gui_app es un
singleton con una ventana, asi que no puede compartir proceso con otros tests. Params
van al OPENPILOT_PREFIX aislado que pone conftest; config_broker se sustituye para no
tocar orbit/config_mqtt.json (en el PC es un fichero del arbol git) y ServerMonitor
para no sondear la red.
"""
import os
import subprocess
import sys

from openpilot.common.basedir import BASEDIR

GUION = r'''
import os
from types import SimpleNamespace
import pyray as rl
rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)

from openpilot.common.params import Params
from openpilot.orbit import config_broker
from openpilot.orbit import telemetria_grupos as tg
from openpilot.system.ui.lib.application import gui_app

assert not gui_app.big_ui()
escritos = []
config_broker.escribir_config = lambda cambios: escritos.append(dict(cambios)) or True

p = Params()
p.put("DongleId", "0123456789abcdef", True)
p.put("OrbitPairingCode", "K7QX4MPA", True)
p.put_bool("OrbitClaimed", False, True)

from openpilot.selfdrive.ui.ui_state import device, ui_state
gui_app.init_window("test orbit mici")
assert (gui_app.width, gui_app.height) == (536, 240)

import openpilot.selfdrive.ui.sunnypilot.mici.layouts.orbit as orbit
import openpilot.selfdrive.ui.sunnypilot.mici.layouts.orbit_telemetria as tel_mod
from openpilot.selfdrive.ui.widgets import orbit_mando
from openpilot.selfdrive.ui.mici.widgets.dialog import BigDialog, BigInputDialog
from openpilot.selfdrive.ui.sunnypilot.mici.widgets import orbit_enroll_dialog as qr
from openpilot.selfdrive.ui.sunnypilot.mici.widgets.orbit_widgets import PaginaConfirmacion


class _Monitor:
  broker_ok = backend_ok = True
  def stop(self):
    pass


orbit.ServerMonitor = _Monitor


class _ParamsMem:
  """Params en memoria SOLO para la pantalla de telemetria.

  Las claves tel2_* y OrbitAhorroRedMovil se registran en params_keys.h, pero el params_pyx.so
  del PC solo conoce las de cuando se compilo y lanza UnknownKeyName hasta recompilar. Aqui se
  prueba la pantalla, no el registro (eso lo vigila orbit/test/test_telemetria_grupos.py).
  """
  def __init__(self):
    self.d = {}
  def get(self, k):
    return self.d.get(k)
  def get_bool(self, k):
    return bool(self.d.get(k, False))
  def put_bool(self, k, v, block=False):
    self.d[k] = bool(v)


mem = _ParamsMem()
tel_mod.ui_state = SimpleNamespace(params=mem)
orbit.REFRESH_S = 0.0  # sin throttle: cada frame relee Params
tel_mod.POLL_S = 0.0
qr.OrbitEnrollDialogMici.POLL_S = 0.0
qr.OrbitEnrollDialogMici.SUCCESS_HOLD = 0.0

from openpilot.selfdrive.ui.sunnypilot.mici.layouts.settings import SettingsLayoutSP
ajustes = SettingsLayoutSP()
ui_state.update_params()
rt = rl.load_render_texture(gui_app.width, gui_app.height)
pila = gui_app._nav_stack


def frames(n=30):
  for _ in range(n):
    ui_state.update()
    rl.begin_drawing()
    rl.begin_texture_mode(rt)
    rl.clear_background(rl.BLACK)
    for w in pila[-gui_app._nav_stack_widgets_to_render:]:
      w.render(rl.Rectangle(0, 0, gui_app.width, gui_app.height))
    rl.end_texture_mode()
    rl.end_drawing()


def abre(boton, tipo=None):
  n = len(pila)
  boton._click_callback()
  frames()
  assert len(pila) == n + 1, (getattr(boton, "text", boton), [type(w).__name__ for w in pila])
  if tipo is not None:
    assert isinstance(pila[-1], tipo), type(pila[-1]).__name__
  return pila[-1]


def cierra():
  gui_app.pop_widget()
  frames(2)


gui_app.push_widget(ajustes)
frames()
visibles = [i for i in ajustes._scroller._items if i.is_visible]
assert getattr(visibles[0], "text", "") == "orbit", [getattr(i, "text", "") for i in visibles]

panel = abre(visibles[0], orbit.OrbitLayoutMici)
for boton, tipo in ((panel._btn_banco, PaginaConfirmacion), (panel._btn_ayuda, orbit.AyudaOrbitMici),
                    (panel._btn_seguros, PaginaConfirmacion), (panel._btn_avanzado, orbit.AvanzadoLayoutMici),
                    (panel._btn_telemetria, orbit.TelemetriaLayoutMici),
                    (panel._btn_servidor, BigInputDialog)):
  abre(boton, tipo)
  cierra()

volante = abre(panel._btn_volante)
for boton in volante._botones.values():
  if boton.value != "en uso":
    abre(boton, PaginaConfirmacion)
    cierra()
volante._preguntar_esquive()
frames()
assert isinstance(pila[-1], PaginaConfirmacion)
cierra()
# JETSON elegido en pantalla: autorizacion presencial, sin banco, y el estado dice ACTIVO.
volante._aplicar(1)
frames(5)
assert p.get_bool("OrbitSteerModeLocal") and not p.get_bool("OrbitBenchArmed")
assert volante._estado.value == "activo, elegido en pantalla", volante._estado.value
volante._aplicar(0)
cierra()

avanzado = abre(panel._btn_avanzado)
for boton in avanzado._campos.values():
  abre(boton, BigInputDialog)
  cierra()
cierra()

# TELEMETRIA: un interruptor por grupo; sin configurar esta todo encendido y el ahorro apagado.
tele = abre(panel._btn_telemetria, orbit.TelemetriaLayoutMici)
assert set(tele._grupos) == {g.clave for g in tg.GRUPOS}
assert all(b._checked and b.enabled for b in tele._grupos.values()) and not tele._ahorro._checked
for g in tg.GRUPOS:
  tele._on_grupo(g, False)
  frames(3)
  assert not tg.grupo_activo(mem, g) and not tele._grupos[g.clave]._checked, g.clave
  assert all(mem.get(k) is False for k in tg.params_de(g)), g.clave
  tele._on_grupo(g, True)
  frames(3)
  assert tg.grupo_activo(mem, g) and tele._grupos[g.clave]._checked, g.clave
tele._on_ahorro(True)
frames(3)
assert tg.ahorro_movil_activo(mem) and tele._ahorro._checked
tele._on_ahorro(False)
frames(3)
assert not tg.ahorro_movil_activo(mem) and not tele._ahorro._checked
# Con la privacidad puesta la posicion sale apagada, deshabilitada y con el motivo; un toque
# (aunque llegue) no escribe nada. Al quitarla vuelve sola, sin reabrir la pantalla.
tele._on_grupo(tg.GRUPOS[0], False)
orbit_mando.privacy_muted = lambda: True
frames(3)
pos = tele._grupos["posicion"]
motivo = tele._descripciones["posicion"].value
assert not pos.enabled and not pos._checked and motivo != tg.GRUPOS[0].descripcion, (pos.enabled, pos._checked, motivo)
assert all(b.enabled for c, b in tele._grupos.items() if c != "posicion")
tele._on_grupo(tg.GRUPOS[0], True)
frames(3)
assert not pos._checked and mem.get("tel2_pos_toggle") is False
orbit_mando.privacy_muted = lambda: False
frames(3)
assert pos.enabled and tele._descripciones["posicion"].value == tg.GRUPOS[0].descripcion
cierra()

# IP invalida: se reabre la entrada con lo escrito y el motivo encima; nada se guarda.
dlg = abre(panel._btn_servidor, BigInputDialog)
assert dlg._keyboard._current_keys is dlg._keyboard._special_keys  # capa de numeros
dlg._keyboard.set_text("192.168.1.300")
dlg._confirm_callback()
frames(150)
assert isinstance(pila[-1], BigDialog) and isinstance(pila[-2], BigInputDialog), [type(w).__name__ for w in pila]
assert pila[-2]._keyboard.text() == "192.168.1.300" and escritos == []
cierra()
# IP valida: solo la clave broker.
pila[-1]._keyboard.set_text("10.0.0.5")
pila[-1]._confirm_callback()
frames(150)
assert pila[-1] is panel and escritos == [{"broker": "10.0.0.5"}], escritos

# QR: timeout interactivo ampliado mientras esta abierto; se cierra solo al vincular.
dialogo = abre(panel._btn_vincular, qr.OrbitEnrollDialogMici)
assert dialogo._qr is not None and dialogo._codigo == "K7QX4MPA"
assert device._override_interactive_timeout == 300
p.put_bool("OrbitClaimed", True, True)
p.put("OrbitOwner", "Adrian Garcia", True)
p.put("OrbitOwnerRole", "superadmin", True)
frames(150)
assert pila[-1] is panel, [type(w).__name__ for w in pila]
assert device._override_interactive_timeout is None
assert panel._cuenta == "Adrian • superadmin" and not panel._btn_vincular.is_visible
assert panel._btn_desvincular.is_visible
abre(panel._btn_desvincular, PaginaConfirmacion)
cierra()

# Desvincular y reabrir: el MISMO dialogo (gc.disable() en el proceso ui: uno por toque
# dejaba su textura en la GPU) y, sin codigo, ni QR ni cuenta atras.
panel._desvincular()
p.remove("OrbitPairingCode")
frames(5)
assert abre(panel._btn_vincular) is dialogo and dialogo._codigo == ""
cierra()

# DESARMAR TODO: un toque, sin confirmacion.
p.put("SteerTorqueMode", 2, True)
panel._btn_desarmar._click_callback()
frames(5)
assert p.get("SteerTorqueMode") == 0 and p.get_bool("OrbitDisarmAll")
assert panel._btn_desarmar.text == "desarmado"

print("RENDER_OK", flush=True)
rl.close_window()
os._exit(0)
'''


def test_panel_orbit_mici_renderiza():
  env = dict(os.environ, OFFSCREEN="1", SCALE="1.0")
  env.pop("BIG", None)
  r = subprocess.run([sys.executable, "-c", GUION], cwd=BASEDIR, env=env, capture_output=True, text=True, timeout=300)
  assert r.returncode == 0 and "RENDER_OK" in r.stdout, (r.stdout[-3000:] + r.stderr[-6000:])
