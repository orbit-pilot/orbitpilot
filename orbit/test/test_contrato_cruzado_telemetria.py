"""Contrato CRUZADO de TELEMETRIA: el descriptor del coche contra el del backend y la app.

POR QUE EXISTE ESTE FICHERO. Es el gemelo de `test_contrato_cruzado.py` (que cubre el
plano de MANDO) para el plano de TELEMETRIA, y existe por la misma razon y con el mismo
antecedente: firmware, backend y app tenian cada uno su tabla, cada una coherente consigo
misma y con sus propios tests EN VERDE, y los nombres habian divergido. En el mando costo
el 100 % de los envios de tres verbos. Aqui la divergencia es mucho mayor:

    senal              coche                backend/app
    velocidad          speed_kph (km/h)     v_ego (m/s)
    intermitente izq   blink_left           left_blinker
    angulo de volante  steer_deg            steering_angle_deg
    distancia al lider lead_dist_m          lead_d_rel
    posicion           lat / lon            latitude / longitude
    limite de via      speed_limit_kph      speed_limit (m/s)
    personalidad       enum (cadena)        int

El backend DESCARTA EN SILENCIO todo campo que no declare su descriptor
(`telemetry_descriptor.coerce`) y `guardar_muestra` devuelve True igualmente: el canal
parece vivo y la fila se escribe VACIA. Ningun test de un solo repo puede ver eso, porque
cada lado es coherente consigo mismo. Este si.

Se lee el descriptor del backend COMO DATOS (su `schema/telemetry_v2.json`), sin importar
`server/`, que arrastra flask, paho y la base de datos. La app se lee como TEXTO: solo se
comprueba que los nombres que publica el coche aparezcan en el decodificador.

Si el repo companero no esta en esta maquina la prueba se SALTA -- no se aprueba: un
contrato que no se ha podido comparar no es un contrato verificado.
"""
import json
import os
import re
from pathlib import Path

import pytest

from openpilot.orbit.telemetria_v1 import descriptor_backend, firma_telemetria

_CANDIDATOS = (
  os.environ.get("ORBIT_IOV", ""),
  "/home/drago/Escritorio/PROYECTS/APPS/orbit-iov",
  str(Path(__file__).resolve().parents[3] / "orbit-iov"),
)

# Firma del descriptor del coche. Si cambia el contrato, este valor cambia Y hay que
# reconciliar los otros dos repos en el mismo commit. Es el trinquete.
FIRMA_ESPERADA = "0410ffd4105d9cf0"

# Un `enum` del coche viaja como cadena JSON, asi que el backend puede declararlo `str`.
# Cualquier otra pareja es una divergencia real: un `int` no puede recibir un `enum`.
# Tipo del coche -> tipo del cable que el backend sabe guardar. `enum` y `list[str]` son
# cadenas en el cable (la segunda, con el JSON dentro).
#
# `list[obj]` es la carga del canal de eventos y NO es una columna de muestras: su sitio
# es la tabla device_events, y el backend la ingiere por su propio camino. Se declara
# igual en los dos lados a proposito, para que el contrato documente su forma; lo que no
# puede pasar es que alguien la convierta en columna.
COMPATIBLES = {
  ("float", "float"), ("int", "int"), ("bool", "bool"),
  ("str", "str"), ("enum", "str"), ("list[str]", "str"),
  ("list[obj]", "list[obj]"),
}


def _raiz_iov():
  for base in _CANDIDATOS:
    if base and (Path(base) / "backend").is_dir():
      return Path(base)
  return None


def _descriptor_backend_json():
  raiz = _raiz_iov()
  if raiz is None:
    pytest.skip("repo orbit-iov no encontrado; define ORBIT_IOV")
  ruta = Path(os.environ.get("ORBIT_TELEMETRY_DESCRIPTOR", "")) if os.environ.get(
    "ORBIT_TELEMETRY_DESCRIPTOR") else raiz / "backend" / "schema" / "telemetry_v2.json"
  if not ruta.is_file():
    pytest.skip(f"descriptor del backend no encontrado en {ruta}")
  return json.loads(ruta.read_text(encoding="utf-8"))


def _campos_backend(desc):
  """{canal: {campo: tipo}} aceptando las dos grafias que admite el cargador del backend."""
  canales = desc.get("canales") or desc.get("channels") or {}
  fuera = {}
  for canal, cuerpo in canales.items():
    campos = cuerpo.get("campos") or cuerpo.get("fields") or {}
    fuera[canal] = {k: (v.get("tipo") or v.get("type")) for k, v in campos.items()}
  return fuera


def _campos_firmware():
  return {c: {x["nombre"]: x["tipo"] for x in d["campos"]}
          for c, d in descriptor_backend()["canales"].items()}


# --------------------------------------------------------------------------- firma

def test_firma_del_descriptor_congelada():
  """El contrato no cambia de tapadillo: si cambia, se reconcilian los tres repos."""
  assert firma_telemetria() == FIRMA_ESPERADA, (
    "El descriptor del coche ha cambiado. Actualiza FIRMA_ESPERADA en el MISMO commit "
    + "en el que reconcilies backend/schema/telemetry_v2.json y app/lib/models/cabin_state.dart."
  )


# --------------------------------------------------------------------------- canales

def test_los_dos_repos_declaran_los_mismos_canales():
  fw = set(_campos_firmware())
  be = set(_campos_backend(_descriptor_backend_json()))
  assert fw == be, (
    f"canales solo en el coche: {sorted(fw - be)}\n"
    + f"canales solo en el backend: {sorted(be - fw)}"
  )


# --------------------------------------------------------------------------- campos

def test_cada_campo_que_publica_el_coche_lo_ingiere_el_backend():
  """El backend descarta EN SILENCIO lo que no declara: cada hueco es telemetria perdida."""
  fw, be = _campos_firmware(), _campos_backend(_descriptor_backend_json())
  lineas, perdidos, total = [], 0, 0
  for canal in sorted(fw):
    faltan = sorted(set(fw[canal]) - set(be.get(canal, {})))
    total += len(fw[canal])
    perdidos += len(faltan)
    if faltan:
      lineas.append(f"  {canal}: el backend NO declara {faltan}")
  assert not lineas, (
    f"{perdidos} de {total} campos que publica el coche se descartan en silencio "
    + "en el backend (coerce los ignora y guardar_muestra devuelve True igual):\n"
    + "\n".join(lineas)
  )


def test_los_tipos_coinciden_campo_a_campo():
  fw, be = _campos_firmware(), _campos_backend(_descriptor_backend_json())
  malos = []
  for canal in sorted(fw):
    for campo, tipo in sorted(fw[canal].items()):
      tb = be.get(canal, {}).get(campo)
      if tb is not None and (tipo, tb) not in COMPATIBLES:
        malos.append(f"  {canal}.{campo}: coche={tipo} backend={tb}")
  assert not malos, "tipos incompatibles (el backend descarta la muestra del campo):\n" + "\n".join(malos)


def test_el_backend_no_espera_campos_que_nadie_publica():
  """Columna declarada que el coche no emite = columna siempre NULL y contrato fantasma."""
  fw, be = _campos_firmware(), _campos_backend(_descriptor_backend_json())
  lineas = []
  for canal in sorted(be):
    sobran = sorted(set(be[canal]) - set(fw.get(canal, {})))
    if sobran:
      lineas.append(f"  {canal}: el coche NO publica {sobran}")
  assert not lineas, "campos que el backend declara y nadie emite:\n" + "\n".join(lineas)


# --------------------------------------------------------------------------- app

def _fuente_app():
  raiz = _raiz_iov()
  if raiz is None:
    pytest.skip("repo orbit-iov no encontrado; define ORBIT_IOV")
  ruta = raiz / "app" / "lib" / "models" / "cabin_state.dart"
  if not ruta.is_file():
    pytest.skip(f"decodificador de la app no encontrado en {ruta}")
  return ruta.read_text(encoding="utf-8")


# Senales que la Cabina PINTA. Si el nombre que publica el coche no aparece en el
# decodificador, la celda queda en '--' para siempre por mucho que el dato viaje.
SENALES_DE_CABINA = {
  "vehicle": ["speed_kph", "gear", "blink_left", "blink_right", "steer_deg",
              "gas_pressed", "brake_pressed", "standstill", "door_open", "seatbelt_off"],
  "openpilot": ["enabled", "active", "mads_enabled", "cal_status", "cal_pct"],
  "perception": ["lead", "lead_dist_m", "bsm_left", "bsm_right"],
  "pos": ["lat", "lon"],
  "road": ["speed_limit_kph"],
}


def test_la_app_lee_los_nombres_que_publica_el_coche():
  fuente = _fuente_app()
  literales = set(re.findall(r"'([A-Za-z_][A-Za-z0-9_]*)'", fuente))
  faltan = []
  for canal, campos in sorted(SENALES_DE_CABINA.items()):
    for campo in campos:
      assert campo in {x["nombre"] for x in descriptor_backend()["canales"][canal]["campos"]}, (
        f"{canal}.{campo} no existe en el descriptor del coche; corrige la lista de la prueba")
      if campo not in literales:
        faltan.append(f"  {canal}.{campo}")
  assert not faltan, (
    "la app no reconoce estos nombres del coche (la celda queda en '--' aunque el dato llegue):\n"
    + "\n".join(faltan)
  )


def test_las_unidades_de_velocidad_no_se_convierten_dos_veces():
  """El coche publica km/h ya convertidos; si la app vuelve a multiplicar, miente x3,6."""
  fuente = _fuente_app()
  campos = {x["nombre"]: x for x in descriptor_backend()["canales"]["road"]["campos"]}
  assert campos["speed_limit_kph"]["unidad"] == "km/h"
  sospechosas = [ln.strip() for ln in fuente.splitlines()
                 if "_aKmh(" in ln and re.search(r"speed_limit|v_ego|'speed'", ln)]
  assert not sospechosas, (
    "la app convierte a km/h una senal que el coche YA publica en km/h "
    + "(speed_kph / speed_limit_kph):\n  " + "\n  ".join(sospechosas)
  )
