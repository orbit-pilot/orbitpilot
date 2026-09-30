"""El descriptor del backend tiene que ser EXACTAMENTE el que genera el firmware.

Este test existe por un fallo concreto, no por higiene. La telemetria v2 se entrego con
TRES vocabularios: el coche publicaba `speed_kph`, el backend esperaba `v_ego` y la app
estaba alineada a un descriptor que el backend se habia inventado a partir de la prosa
del diseno. 86 de 102 campos se descartaban EN SILENCIO -- el backend devolvia True e
insertaba la fila igual -- asi que los tres repos daban verde con sus propios tests y no
llegaba un solo dato util. Es el mismo fallo que ya rompio el 100 % de tres verbos de
mando por los nombres de sus argumentos.

Un contrato copiado a mano diverge SIEMPRE. Aqui no se compara "que se parezcan": se
regenera y se exige igualdad byte a byte, de modo que editar el JSON del backend a mano o
tocar un campo del firmware sin regenerar sale en rojo.
"""
import json
import os

import pytest

from openpilot.orbit.gen_descriptor_backend import convertir
from openpilot.orbit.telemetria_v1 import firma_telemetria

# Misma variable que el test de contrato cruzado: con la ruta fija este test se SALTABA
# en cualquier maquina donde el repo companero no vive en esa carpeta, y nadie comprobaba
# que el fichero del backend se hubiera regenerado.
RUTA_IOV = os.path.join(os.environ.get("ORBIT_IOV", "/home/drago/Escritorio/PROYECTS/APPS/orbit-iov"),
                        "backend", "schema", "telemetry_v2.json")

pytestmark = pytest.mark.skipif(
  not os.path.exists(RUTA_IOV),
  reason="el repo companero orbit-iov no esta en esta maquina",
)


def _del_disco() -> dict:
  with open(RUTA_IOV, encoding="utf-8") as f:
    return json.load(f)


def test_el_descriptor_del_backend_es_el_generado_por_el_firmware():
  esperado = convertir()
  real = _del_disco()
  if real != esperado:
    faltan = set(esperado["canales"]) - set(real.get("canales", {}))
    sobran = set(real.get("canales", {})) - set(esperado["canales"])
    detalle = []
    if faltan:
      detalle.append(f"canales que faltan en el backend: {sorted(faltan)}")
    if sobran:
      detalle.append(f"canales que el backend declara de mas: {sorted(sobran)}")
    for canal, cuerpo in esperado["canales"].items():
      otros = (real.get("canales", {}).get(canal) or {}).get("campos", {})
      mios = cuerpo["campos"]
      if set(mios) != set(otros):
        solo_coche = sorted(set(mios) - set(otros))
        solo_backend = sorted(set(otros) - set(mios))
        detalle.append(f"{canal}: el coche publica {solo_coche} que el backend no declara; "
                       + f"el backend declara {solo_backend} que el coche no publica")
    pytest.fail("El descriptor del backend NO es el generado por el firmware.\n"
                + "Regenera con:\n  uv run python3 orbit/gen_descriptor_backend.py > "
                + f"{RUTA_IOV}\n" + "\n".join(detalle))


def test_la_firma_del_contrato_viaja_dentro_del_fichero():
  """Para que el backend pueda decir en un log CON QUE version del contrato habla."""
  assert _del_disco().get("firma") == firma_telemetria()


def test_los_tipos_del_cable_son_los_cuatro_que_el_backend_sabe_guardar():
  """El backend solo convierte float/int/bool/str a columna. Todo lo demas lo ignora con
  un WARN que nadie mira, y el canal parece vivo yendo vacio: eso ya paso con los 19
  campos declarados `enum`."""
  admitidos = {"float", "int", "bool", "str"}
  # UNICA excepcion admitida, y esta declarada: la carga del canal de eventos no es una
  # columna de muestras. Su sitio es device_events y el backend la ingiere por su propio
  # camino. Si aparece cualquier OTRO tipo no guardable, es un campo que se va a perder.
  EXCEPCIONES = {"event.ev"}
  sueltos = []
  for canal, cuerpo in convertir()["canales"].items():
    for nombre, spec in cuerpo["campos"].items():
      if spec["tipo"] not in admitidos and f"{canal}.{nombre}" not in EXCEPCIONES:
        sueltos.append(f"{canal}.{nombre}={spec['tipo']}")
  assert not sueltos, ("estos campos no llegaran nunca a la BD: " + ", ".join(sueltos))
