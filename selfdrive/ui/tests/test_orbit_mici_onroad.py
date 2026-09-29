# Seguridad onroad del comma 4 (mici): que dispara la pildora de orden remota, la banda de
# FRENADO REMOTO y la pildora de autoridad. Logica pura, sin ventana ni raylib.
import ast
import pathlib
import time
from types import SimpleNamespace

from cereal import messaging
from openpilot.selfdrive.ui.sunnypilot.mici.onroad.orbit_remoto import GENERIC_LABEL, OrbitRemoteWatch
from openpilot.selfdrive.ui.tests.test_orbit_onroad_quieto import PROHIBIDOS, _modulos
from openpilot.selfdrive.ui.widgets.orbit_mando import CommandStateView

FPS = 60


class _Sm:
  def __init__(self, st, alive=True):
    self._st = st
    self.alive = {CommandStateView.SERVICE: alive}
    self.valid = {CommandStateView.SERVICE: alive}

  def __getitem__(self, k):
    return self._st


class _Params:
  """Params falsos cuyo contenido depende del reloj simulado."""
  def __init__(self):
    self.now = 0.0
    self.bools: dict[str, tuple[float, float]] = {}   # nombre -> [desde, hasta)
    self.strs: dict[str, str] = {}
    self.lecturas = 0

  def get_bool(self, k):
    self.lecturas += 1
    desde, hasta = self.bools.get(k, (0.0, 0.0))
    return desde <= self.now < hasta

  def get(self, k):
    self.lecturas += 1
    v = self.strs.get(k)
    return v(self.now) if callable(v) else v


def _vista(seq=0, deadline=0.0, mode="observer", bench=False, available=True, verb=""):
  return SimpleNamespace(available=available, seq=seq, deadline_mono=deadline, mode=mode, bench_armed=bench, active_verb=verb)


def _correr(watch, params, vista_en, t0, t1, started=True):
  """Ticks del nav stack a 60 fps entre t0 y t1. Devuelve los instantes con orden nueva."""
  nuevas, n = [], 0
  while t0 + n / FPS < t1:
    now = t0 + n / FPS
    params.now = now
    if watch.update(vista_en(now), started, now):
      nuevas.append(now)
    n += 1
  return nuevas


def test_command_state_view_seq_deadline_y_failsafe():
  msg = messaging.new_message('orbitCommandState')
  st = msg.orbitCommandState
  st.mode = 'maneuver'
  st.seq = 42
  st.deadlineMono = time.monotonic() + 5.0
  v = CommandStateView()
  v.update(_Sm(st))
  assert v.available and v.seq == 42 and v.mode == 'maneuver'
  assert v.actuator_live

  st.deadlineMono = time.monotonic() - 0.1   # ventana vencida
  v.update(_Sm(st))
  assert v.available and not v.actuator_live

  st.deadlineMono = time.monotonic() + 5.0
  v.update(_Sm(st, alive=False))              # plano muerto: todo a cero
  assert (v.available, v.seq, v.deadline_mono, v.mode, v.actuator_live) == (False, 0, 0.0, 'observer', False)

  v.update(SimpleNamespace(alive={}, valid={}))  # servicio ausente del SubMaster
  assert not v.available and v.seq == 0 and not v.actuator_live


def test_seq_ceba_y_dispara_generico():
  p = _Params()
  w = OrbitRemoteWatch(p)
  # Primer valor visto: linea base, no es una orden nueva
  assert not _correr(w, p, lambda now: _vista(seq=41), 0.0, 1.0)
  # Avance de seq: pildora generica durante 3 s
  nuevas = _correr(w, p, lambda now: _vista(seq=41 if now < 2.0 else 42), 1.0, 3.0)
  assert len(nuevas) == 1 and w.label == GENERIC_LABEL
  assert w.toast_visible(4.9) and not w.toast_visible(5.1)
  # El plano cae y vuelve con otro seq (reinicio del proceso de mando): se vuelve a cebar
  _correr(w, p, lambda now: _vista(available=False), 6.0, 6.5)
  assert not _correr(w, p, lambda now: _vista(seq=3), 6.5, 7.0)
  # Flanco perdido pero el plano aun muestra el verbo: etiqueta del verbo
  assert _correr(w, p, lambda now: _vista(seq=4, verb='lane_change'), 7.0, 7.5) and w.label == 'CAMBIO CARRIL'


def test_ventana_nueva_sin_seq_nuevo_tambien_cuenta():
  p = _Params()
  w = OrbitRemoteWatch(p)
  _correr(w, p, lambda now: _vista(seq=5), 0.0, 0.5)
  # Misma seq pero se abre una ventana de actuador nueva: orden nueva
  assert len(_correr(w, p, lambda now: _vista(seq=5, deadline=9.0 if now >= 0.8 else 0.0), 0.5, 1.5)) == 1
  # La ventana caduca (vuelve a 0.0): eso NO es una orden
  assert not _correr(w, p, lambda now: _vista(seq=5, deadline=0.0), 10.0, 11.0)


def test_pulso_de_carril_de_02s_no_se_pierde_y_manda_la_etiqueta():
  p = _Params()
  w = OrbitRemoteWatch(p)
  _correr(w, p, lambda now: _vista(seq=7), 0.0, 1.0)
  # desire_helper consume el flag en <= 0.2 s; aqui vive 0.12 s. El seq llega 80 ms despues.
  p.bools['ForceLaneChangeLeft'] = (2.00, 2.12)
  nuevas = _correr(w, p, lambda now: _vista(seq=7 if now < 2.08 else 8), 1.0, 3.0)
  assert nuevas and w.label == 'CARRIL IZQ'   # el seq de la misma orden no la pisa
  # Orden inversa: primero el seq (generica), luego el flanco -> se concreta la etiqueta
  p.bools['orbit_speed_increase'] = (6.05, 6.25)
  _correr(w, p, lambda now: _vista(seq=8 if now < 6.0 else 9), 5.0, 7.0)
  assert w.label == 'VELOCIDAD +'
  # Pulso de direccion: cadena nueva = pulso nuevo. La que ya estaba al pasar a onroad ceba.
  p.strs['orbit_steering_pulse'] = 'left:1000'
  _correr(w, p, lambda now: _vista(seq=9), 9.0, 9.5, started=False)
  assert not _correr(w, p, lambda now: _vista(seq=9), 10.0, 10.5)
  p.strs['orbit_steering_pulse'] = 'right:2000'
  assert _correr(w, p, lambda now: _vista(seq=9), 20.0, 20.5) and w.label == 'PULSO DIRECCION'


def test_frenada_rechazada_no_deja_la_banda_3s():
  p = _Params()
  w = OrbitRemoteWatch(p)
  _correr(w, p, lambda now: _vista(seq=1, mode='maneuver'), 0.0, 1.0)
  # assisted_decel: ventana de 1.5 s; controlsd la rechaza y borra el flag a los 0.3 s
  p.bools['brutebreak_active'] = (1.0, 1.3)
  en_banda = []
  vista = lambda now: _vista(seq=2 if now >= 1.0 else 1, deadline=2.5, mode='maneuver')  # noqa: E731
  n = 0
  while 1.0 + n / FPS < 4.0:
    now = 1.0 + n / FPS
    p.now = now
    w.update(vista(now), True, now)
    if w.brake_active:
      en_banda.append(now)
    n += 1
  assert en_banda and max(en_banda) < 1.4   # se apaga con el flag, no 3 s despues
  # Flag rancio sin ventana de actuador abierta: tampoco hay banda
  p.bools['brutebreak_active'] = (5.0, 9.0)
  _correr(w, p, lambda now: _vista(seq=2, deadline=2.5, mode='maneuver'), 5.0, 6.0)
  assert not w.brake_active


def test_offroad_no_lee_params_ni_frena():
  p = _Params()
  w = OrbitRemoteWatch(p)
  p.bools['brutebreak_active'] = (0.0, 99.0)
  _correr(w, p, lambda now: _vista(seq=1, deadline=50.0), 0.0, 2.0, started=False)
  assert p.lecturas == 0 and not w.brake_active


def test_autoridad_y_esquive():
  p = _Params()
  w = OrbitRemoteWatch(p)
  casos = [
    (_vista(mode='observer'), ('', False)),
    (_vista(mode='copilot'), ('COPILOTO', False)),
    (_vista(mode='maneuver'), ('MANIOBRA', True)),
    (_vista(mode='observer', bench=True), ('BANCO', True)),
    (_vista(mode='maneuver', available=False), ('', False)),
  ]
  for vista, esperado in casos:
    w.update(vista, True, 0.0)
    assert (w.authority, w.authority_urgent) == esperado, vista
  p.strs.update(SteerTorqueMode=3, JetsonObstacleStatus='BSM_BLOCKED_LEFT')
  w.update(_vista(), True, 1.0)
  assert (w.dodge, w.dodge_bsm) == ('BSM BLOQUEA <-', True)
  p.strs['SteerTorqueMode'] = 0
  w.update(_vista(), True, 2.0)
  assert w.dodge == ''


def test_esquive_breve_no_se_pierde():
  # controlsd sostiene un esquive terminado solo 0.30 s (OBSTACLE_STATUS_HOLD_S): a 2 Hz se escapaba
  p = _Params()
  w = OrbitRemoteWatch(p)
  p.strs['SteerTorqueMode'] = '3'
  p.strs['JetsonObstacleStatus'] = lambda now: 'DODGING_RIGHT' if 1.37 <= now < 1.67 else ''
  visto = []
  n = 0
  while n / FPS < 3.0:
    now = n / FPS
    p.now = now
    w.update(_vista(), True, now)
    if w.dodge:
      visto.append(now)
    n += 1
  assert visto and visto[0] - 1.37 <= 0.06 and w.dodge == ''


def test_overlays_mici_quietos_y_con_tokens():
  # Mismo criterio que test_orbit_onroad_quieto para la UI grande: onroad no se mueve nada.
  onroad = pathlib.Path(__file__).resolve().parents[1] / 'sunnypilot' / 'mici' / 'onroad'
  for nombre in ('orbit_overlays.py', 'orbit_remoto.py'):
    texto = (onroad / nombre).read_text()
    mods = list(_modulos(ast.parse(texto)))
    assert not [m for m in mods if any(x in m for x in PROHIBIDOS)], nombre
    assert 'rl.Color(' not in texto and 'import math' not in texto, nombre
  assert 'orbit_theme' in (onroad / 'orbit_overlays.py').read_text()
