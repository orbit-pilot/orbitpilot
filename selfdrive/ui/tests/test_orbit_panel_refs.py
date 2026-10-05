"""Las pantallas ORBIT solo pueden referirse a nombres que orbit_mando defina de verdad.

REGRESION REAL (commit 606f44760): orbit_panel._refresh_status comparaba contra
`mando.ARM_REASON_STEER` despues de que esa constante desapareciera de orbit_mando. La
rama solo se ejecutaba con el banco ARMADO, asi que ningun arranque ni ningun test lo
detectaba hasta que alguien pulsaba ARMAR en el coche: AttributeError dentro del
_render -> muere el proceso `ui` (el bucle de render no captura nada) -> manager lo
resucita -> splash de ORBIT ("el comma se reinicia") -> el armado sigue en Params ->
vuelve a morir al abrir el panel. Un bucle de reinicios por un nombre que ya no existia.

Es un test de AST a proposito: no importa pyray ni Params, asi que no necesita ni el
build ni una ventana, y corre en cualquier PC con `python3 -m pytest` o a pelo con
`python3 selfdrive/ui/tests/test_orbit_panel_refs.py`.
"""
import ast
from pathlib import Path

UI_DIR = Path(__file__).resolve().parents[1]
MANDO = UI_DIR / "widgets" / "orbit_mando.py"


def _leer(path: Path) -> ast.Module:
  return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _nombres_modulo(tree: ast.Module) -> set[str]:
  """Nombres definidos en el nivel superior del modulo: lo que `mando.X` puede resolver."""
  nombres: set[str] = set()
  for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
      nombres.add(node.name)
    elif isinstance(node, ast.Assign):
      nombres.update(t.id for t in node.targets if isinstance(t, ast.Name))
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
      nombres.add(node.target.id)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
      nombres.update((a.asname or a.name).split(".")[0] for a in node.names)
  return nombres


def _miembros_clase(tree: ast.Module, clase: str) -> set[str]:
  """Metodos, propiedades, atributos de clase y `self.X = ...` de la clase pedida."""
  for node in tree.body:
    if isinstance(node, ast.ClassDef) and node.name == clase:
      miembros: set[str] = set()
      for sub in node.body:
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
          miembros.add(sub.name)
        elif isinstance(sub, ast.Assign):
          miembros.update(t.id for t in sub.targets if isinstance(t, ast.Name))
        elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
          miembros.add(sub.target.id)
      for sub in ast.walk(node):
        if (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
            and sub.value.id == "self" and isinstance(sub.ctx, ast.Store)):
          miembros.add(sub.attr)
      return miembros
  raise AssertionError(f"no existe la clase {clase} en {MANDO}")


def _alias_de_mando(tree: ast.Module) -> set[str]:
  """Como se llama orbit_mando dentro del fichero (`import orbit_mando as mando`)."""
  alias: set[str] = set()
  for node in ast.walk(tree):
    if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("selfdrive.ui.widgets"):
      alias.update(a.asname or a.name for a in node.names if a.name == "orbit_mando")
  return alias


def _ficheros_ui() -> list[Path]:
  return sorted(p for p in UI_DIR.rglob("*.py")
                if p != MANDO and "orbit_mando" in p.read_text(encoding="utf-8"))


def _referencias(tree: ast.Module, objeto: set[str]) -> list[tuple[int, str]]:
  """(linea, atributo) de cada `X.attr` con X en `objeto`."""
  return [(node.lineno, node.attr) for node in ast.walk(tree)
          if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in objeto]


def test_referencias_a_orbit_mando_existen():
  definidos = _nombres_modulo(_leer(MANDO))
  rotas = []
  ficheros_con_alias = 0
  for path in _ficheros_ui():
    tree = _leer(path)
    alias = _alias_de_mando(tree)
    if not alias:
      continue
    ficheros_con_alias += 1
    rotas += [f"{path.relative_to(UI_DIR)}:{linea} mando.{attr}"
              for linea, attr in _referencias(tree, alias) if attr not in definidos]
  # El panel y el selector de volante importan el modulo con alias: si esto baja a cero
  # el test se habria quedado sin nada que vigilar y nadie se enteraria.
  assert ficheros_con_alias >= 2, "ningun fichero de la UI importa orbit_mando con alias"
  assert not rotas, "referencias a nombres que orbit_mando NO define:\n" + "\n".join(rotas)


def test_uso_del_bench_guard_existe():
  """Lo que el panel llama sobre `guard` (ui_state.orbit_bench_guard) existe en BenchGuard."""
  miembros = _miembros_clase(_leer(MANDO), "BenchGuard")
  rotas = []
  for path in _ficheros_ui():
    rotas += [f"{path.relative_to(UI_DIR)}:{linea} guard.{attr}"
              for linea, attr in _referencias(_leer(path), {"guard"}) if attr not in miembros]
  assert not rotas, "la UI usa miembros que BenchGuard NO tiene:\n" + "\n".join(rotas)


def test_las_pantallas_de_telemetria_estan_vigiladas():
  """Las dos pantallas del submenu Telemetria deshabilitan la posicion con `mando.privacy_muted()`.

  Si una dejara de importar orbit_mando con alias (o cambiara el alias), el test de arriba
  dejaria de comprobar sus referencias sin avisar, y un nombre roto saldria en el coche:
  AttributeError en el render -> muere el proceso `ui`."""
  esperadas = {UI_DIR / "sunnypilot" / "layouts" / "settings" / "orbit_sub_layouts" / "telemetry_settings.py",
               UI_DIR / "sunnypilot" / "mici" / "layouts" / "orbit_telemetria.py"}
  vigiladas = {p for p in _ficheros_ui() if _alias_de_mando(_leer(p))}
  assert esperadas <= vigiladas, f"sin vigilar: {sorted(str(p.relative_to(UI_DIR)) for p in esperadas - vigiladas)}"
  for path in esperadas:
    assert any(attr == "privacy_muted" for _, attr in _referencias(_leer(path), _alias_de_mando(_leer(path))))


if __name__ == "__main__":
  test_referencias_a_orbit_mando_existen()
  test_uso_del_bench_guard_existe()
  test_las_pantallas_de_telemetria_estan_vigiladas()
  print("OK")
