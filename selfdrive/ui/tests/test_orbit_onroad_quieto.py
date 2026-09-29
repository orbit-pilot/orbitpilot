# En marcha nada decorativo se mueve: los overlays onroad ORBIT no pueden
# importar módulos de animación y toman el color de orbit_theme.
import ast
import pathlib

ONROAD = pathlib.Path(__file__).resolve().parents[1] / 'sunnypilot' / 'onroad'
MICI = ONROAD.parent / 'mici' / 'onroad'
FICHEROS = [ONROAD / 'orbit_command_overlay.py', ONROAD / 'orbit_follow_coach.py', ONROAD / 'orbit_hardbrake_overlay.py',
            MICI / 'orbit_overlays.py']
# El vigilante de mici no dibuja (no hay tokens que exigirle), pero tampoco puede animar
VIGILANTES = [MICI / 'orbit_remoto.py']
PROHIBIDOS = {'orbit_fx', 'orbit_duplex', 'orbit_icons'}


def _modulos(arbol):
  for n in ast.walk(arbol):
    if isinstance(n, ast.Import):
      yield from (a.name for a in n.names)
    elif isinstance(n, ast.ImportFrom):
      yield n.module or ''
      yield from (f'{n.module}.{a.name}' for a in n.names)


def test_onroad_sin_animacion_y_con_tokens():
  for fichero in FICHEROS + VIGILANTES:
    nombre, texto = fichero.name, fichero.read_text()
    mods = list(_modulos(ast.parse(texto)))
    assert not [m for m in mods if any(p in m for p in PROHIBIDOS)], nombre
    assert fichero in VIGILANTES or any('orbit_theme' in m for m in mods), f'{nombre} no usa orbit_theme'
    # Sin colores sueltos (solo tokens de orbit_theme) y sin `math` (trigonometria
    # de animacion): estos overlays son latches, no dibujan movimiento nuevo.
    assert 'rl.Color(' not in texto, f'{nombre} usa un literal rl.Color(...) en vez de un token'
    assert 'import math' not in texto, f'{nombre} importa math'
