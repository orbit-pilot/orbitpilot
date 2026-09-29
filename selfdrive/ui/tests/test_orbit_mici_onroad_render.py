"""Render de los overlays ORBIT onroad del comma 4 (mici) con alertas de serie encima.

Con la AugmentedRoadView real y las alertas de serie mas altas (text1 corto a dos lineas +
text2):
  1. el texto de la alerta no baja de ALERT_TEXT_FLOOR a la derecha del volante (es la
     suposicion en la que se apoya la fila de orbit_overlays),
  2. con ORBIT encima, ningun pixel que cambie respecto a la alerta sola cae en las filas de
     su texto ni sobre el volante; las pildoras salen por prioridad y sin huecos (la
     primera siempre), y
  3. con FRENADO REMOTO y alerta solo se pinta la banda, tambien bajo ese suelo.

Proceso aparte, como test_orbit_mici_render: `BIG` se lee al importar application.py y
gui_app es un singleton con una ventana.
"""
import os
import subprocess
import sys

from openpilot.common.basedir import BASEDIR

GUION = r'''
import time
import numpy as np
import pyray as rl
rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app

gui_app.init_window("test orbit mici onroad")
assert (gui_app.width, gui_app.height) == (536, 240)

from openpilot.selfdrive.ui.mici.onroad import alert_renderer as ar
from openpilot.selfdrive.ui.mici.onroad.augmented_road_view import AugmentedRoadView
from openpilot.selfdrive.ui.sunnypilot.mici.onroad import orbit_overlays as ov

A, S, St = ar.Alert, ar.AlertSize, ar.AlertStatus
ALERTAS = [  # events.py / alert_renderer.py
  ar.ALERT_CRITICAL_TIMEOUT,
  A("TAKE CONTROL", "Resume Driving Manually", S.mid, St.userPrompt),
  A("TAKE CONTROL", "Steering Temporarily Unavailable", S.full, St.critical),
  A("Dashcam Mode", "Car Unrecognized", S.mid, St.normal),
  A("Dashcam Mode", "Security Key Not Available", S.mid, St.normal),
]
alerta = [None]
ar.AlertRenderer.get_alert = lambda self, sm: alerta[0]

W, H = gui_app.width, gui_app.height
view = AugmentedRoadView(lambda: None)
view.set_rect(rl.Rectangle(0, 0, W, H))
ui_state.started = True
overlay, w = view.orbit_overlay, view.orbit_overlay.watch
DODGE, AUTH, TOAST = "ESQUIVANDO - NEUTRO", "MANIOBRA", "PULSO DIRECCION"

# Traza por frame: (texto, borde izquierdo, y)
traza = []
_pill = overlay._pill
def _pill_spy(b, size, x, y, align=1.0, pad_y=ov.PAD_Y):
  traza.append((b[0], x - overlay._size(b[0], size, b[4], pad_y)[0] * align, y))
  return _pill(b, size, x, y, align, pad_y)
overlay._pill = _pill_spy

rt = rl.load_render_texture(W, H)
def frames(n):
  for _ in range(n):
    traza.clear()
    rl.begin_drawing()
    rl.begin_texture_mode(rt)
    rl.clear_background(rl.BLACK)
    view.render(rl.Rectangle(0, 0, W, H))
    rl.end_texture_mode()
    rl.end_drawing()
  img = rl.load_image_from_texture(rt.texture)
  rl.image_flip_vertical(img)
  px = np.frombuffer(rl.ffi.buffer(img.data, W * H * 4), np.uint8).reshape(H, W, 4)[:, :, :3].astype(int)
  rl.unload_image(img)
  return px

def orbit(dodge="", auth="", toast="", banco=False, freno=False):
  w.dodge, w.dodge_bsm = dodge, False
  w.authority, w.authority_urgent = auth, True
  w.label, w.toast_until = toast, (time.monotonic() + 999 if toast else 0.0)
  w.brake_active = freno
  ui_state.force_onroad = banco

def cambiados(base, px):
  ys, xs = np.nonzero(np.abs(px - base).max(axis=2) > 24)
  return ys, xs

for a in ALERTAS:
  alerta[0] = a
  # 1) Solo la alerta. Texto = pixel claro (blanco al 65-90 %); fondo, degradado y arco no llegan
  orbit()
  base = frames(60)
  claro = base[:, ov.WHEEL_CLEAR:int(view._content_rect.width)].min(axis=2) > 140
  filas = np.nonzero(claro.any(axis=1))[0]
  assert len(filas) and filas.min() < 100, (a.text1, "no se detecta el texto de la alerta")
  assert filas.max() < ov.ALERT_TEXT_FLOOR, (a.text1, a.text2, filas.max())
  assert (frames(3) == base).all(), (a.text1, "la escena de serie no esta quieta: el diff no vale")

  # 2) ORBIT encima: lo que cambie, solo bajo el texto y a la derecha del volante
  for estado in (
    dict(dodge=DODGE, auth=AUTH, toast=TOAST, banco=True),
    dict(dodge="ESQUIVANDO <-", auth=AUTH, toast=TOAST),  # la orden no cabe: la autoridad, aunque quepa, tampoco
    dict(auth=AUTH, toast=TOAST, banco=True),
    dict(auth=AUTH, banco=True),
    dict(banco=True),
  ):
    orbit(**estado)
    px = frames(3)
    # Por prioridad y sin huecos: la primera siempre; si una no cabe, fuera tambien las de detras
    prioridad = [p for p in (estado.get("dodge"), estado.get("toast"), estado.get("auth"), estado.get("banco") and ov.BENCH_LABEL) if p]
    assert traza and [n for n, *_ in traza] == prioridad[:len(traza)], (a.text1, estado, traza)
    assert all(izq >= ov.WHEEL_CLEAR and y >= ov.ALERT_TEXT_FLOOR for _, izq, y in traza), traza
    ys, xs = cambiados(base, px)
    assert len(ys), (a.text1, estado, "ORBIT no pinta nada")
    assert ys.min() >= ov.ALERT_TEXT_FLOOR and xs.min() >= ov.WHEEL_CLEAR, (a.text1, estado, ys.min(), xs.min())

  # 3) Frenada remota con alerta: solo la banda, y tambien bajo el texto
  orbit(dodge=DODGE, auth=AUTH, toast=TOAST, banco=True, freno=True)
  px = frames(3)
  assert traza == [], (a.text1, traza)
  ys, xs = cambiados(base, px)
  assert len(ys) and ys.min() >= ov.ALERT_TEXT_FLOOR and xs.min() >= ov.WHEEL_CLEAR, (a.text1, ys.min(), xs.min())

# Sin alerta: columna arriba a la derecha + pildora, como siempre
alerta[0] = None
orbit(dodge=DODGE, auth=AUTH, toast=TOAST, banco=True)
frames(60)
assert {t for t, *_ in traza} == {DODGE, AUTH, ov.BENCH_LABEL, TOAST}, traza
print("OK")
'''


def test_overlays_mici_no_tapan_alertas_de_serie():
  env = dict(os.environ, OFFSCREEN="1", SCALE="1.0")
  env.pop("BIG", None)
  r = subprocess.run([sys.executable, "-c", GUION], cwd=BASEDIR, env=env, capture_output=True, text=True, timeout=300)
  assert r.returncode == 0 and "OK" in r.stdout, r.stdout[-3000:] + r.stderr[-3000:]
