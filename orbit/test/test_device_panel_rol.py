"""Fila "Cuenta ORBIT" del panel Dispositivo de la UI grande: dueño y rol (contrato C2)."""
from openpilot.common.params import UnknownKeyName
from openpilot.selfdrive.ui.layouts.settings.device import DeviceLayout


class _Params:
  def __init__(self, datos):
    self.datos = datos

  def get(self, clave):
    if self.datos is None:
      raise UnknownKeyName(clave)
    return self.datos.get(clave)


def _texto(datos):
  panel = DeviceLayout.__new__(DeviceLayout)  # sin ventana: solo la logica de la fila
  panel._params = _Params(datos)
  return panel._orbit_account_text()


def test_dueno_y_rol():
  assert _texto({"OrbitOwner": "Ana", "OrbitOwnerRole": "developer"}) == "Ana • desarrollador"
  assert _texto({"OrbitOwner": "Ana", "OrbitOwnerRole": "superadmin"}) == "Ana • superadmin"


def test_rol_desconocido_o_ausente_solo_dueno():
  assert _texto({"OrbitOwner": "Ana", "OrbitOwnerRole": "hacker"}) == "Ana"
  assert _texto({"OrbitOwner": "Ana"}) == "Ana"


def test_sin_dueno():
  assert _texto({"OrbitOwnerRole": "user"}) == "usuario"
  assert _texto({}) == "N/A"
  assert _texto(None) == "N/A"  # claves sin registrar en un manifiesto viejo
