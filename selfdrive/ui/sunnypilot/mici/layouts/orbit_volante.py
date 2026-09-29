"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Selector de FUENTE DE TORQUE DEL VOLANTE (SteerTorqueMode) en el comma 4.

Mismo flujo que el de la UI grande (sunnypilot/layouts/settings/orbit_sub_layouts/steer_mode.py):
cada modo pide su confirmacion (TEST MAX en rojo), COMMA+JETSON pregunta despues como
esquivar (CURVATURA recomendado, u otra opcion -> TORQUE con su propia confirmacion) y el
commit es orbit_ajustes.aplicar_modo_volante: modo, autorizacion PRESENCIAL
(OrbitSteerModeLocal, nunca el armado de banco) y buzon MQTT. Pulsar el modo vigente no
hace nada salvo en COMMA+JETSON, que vuelve a preguntar el esquive.
"""
import time

from openpilot.selfdrive.ui.sunnypilot.mici.widgets.orbit_widgets import Baldosa, PaginaConfirmacion, TarjetaTexto, a_salvo
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui.widgets import orbit_ajustes as ajustes
from openpilot.selfdrive.ui.widgets import orbit_mando as mando
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.widgets.scroller import NavScroller

POLL_S = 0.5  # el modo tambien lo cambia la app: se resincroniza a 2 Hz


def estado_volante() -> tuple[int, str]:
  """(modo, texto de estado) leyendo Params. Lo usa tambien la baldosa del panel ORBIT."""
  modo = mando.read_steer_mode()
  objetivo = ui_state.params.get(ajustes.PARAM_APPLY_TARGET) if modo == 3 else None
  necesita = modo in ajustes.MODES_REQUIRING_BENCH
  armado, restante = mando.bench_snapshot() if necesita else (False, 0.0)
  local = necesita and mando.steer_local_activo()
  return modo, ajustes.texto_estado_volante(modo, objetivo, armado, restante, local)


def nombre_modo(modo: int) -> str:
  """'COMMA+JETSON' -> 'comma + jetson': con espacios para que parta por palabras."""
  return ajustes.MODE_BUTTONS[ajustes.MODE_TO_INDEX.get(modo, 0)].lower().replace("+", " + ")


# Titulo del deslizador de cada modo: el BigSlider pinta a 48 px, dos lineas de ~12 letras.
DESLIZAR = {0: "desliza para\nusar comma", 1: "desliza para\nusar jetson", 2: "desliza para\ntest max",
            3: "desliza para\nactivar"}
DESLIZAR_TORQUE = "desliza para\nusar torque"


class VolanteLayoutMici(NavScroller):
  def __init__(self):
    super().__init__()
    self._ultimo_poll = float("-inf")
    self._estado = TarjetaTexto("", "volante")

    self._botones: dict[int, Baldosa] = {}
    for indice in range(len(ajustes.MODE_BUTTONS)):
      modo = ajustes.INDEX_TO_MODE[indice]
      boton = Baldosa(nombre_modo(modo), "")
      boton.set_click_callback(lambda m=modo: self._pulsar(m))
      self._botones[modo] = boton

    self._scroller.add_widgets([self._estado, *self._botones.values()])

  # ---------------------------------------------------------------- estado
  @a_salvo("sincronizar el selector de volante")
  def sync(self) -> None:
    modo, estado = estado_volante()
    self._estado.set_value(estado.lower())
    for m, boton in self._botones.items():
      boton.set_value("en uso" if m == modo else "")

  def _update_state(self):
    super()._update_state()
    ahora = time.monotonic()
    if ahora - self._ultimo_poll >= POLL_S:
      self._ultimo_poll = ahora
      self.sync()

  def show_event(self):
    super().show_event()
    self._ultimo_poll = float("-inf")

  # ---------------------------------------------------------------- accion
  @a_salvo("selector de volante", avisar=True)
  def _pulsar(self, modo: int) -> None:
    actual = mando.read_steer_mode()
    if modo == actual and modo != 3:
      self._botones[modo].trigger_shake()
      return
    if modo == 3 and actual == 3:
      self._preguntar_esquive()
      return

    texto, _ = ajustes.confirmacion_modo(modo)
    siguiente = self._preguntar_esquive if modo == 3 else (lambda: self._aplicar(modo))
    gui_app.push_widget(PaginaConfirmacion(ajustes.MODE_NAMES[modo].lower(), texto, DESLIZAR[modo], siguiente,
                                           rojo=(modo == 2)))

  @a_salvo("elegir el esquive de la jetson", avisar=True)
  def _preguntar_esquive(self) -> None:
    texto, curvatura, otra = ajustes.texto_esquive_curvatura()
    gui_app.push_widget(PaginaConfirmacion("comma+jetson", texto, None, None, opciones=[
      (curvatura.lower(), "", lambda: self._fijar_esquive("curvature")),
      (otra.lower(), "beta", self._preguntar_torque),
    ]))

  @a_salvo("elegir el esquive en torque", avisar=True)
  def _preguntar_torque(self) -> None:
    texto, _ = ajustes.texto_esquive_torque("Desliza hacia abajo para no cambiar nada.")
    gui_app.push_widget(PaginaConfirmacion("torque (beta)", texto, DESLIZAR_TORQUE, lambda: self._fijar_esquive("torque")))

  @a_salvo("fijar el esquive de la jetson", avisar=True)
  def _fijar_esquive(self, objetivo: str) -> None:
    ajustes.fijar_objetivo_esquive(ui_state.params, objetivo)
    self._aplicar(3)

  @a_salvo("cambiar el modo de volante", avisar=True)
  def _aplicar(self, modo: int) -> None:
    ajustes.aplicar_modo_volante(ui_state.params, modo)
    self._ultimo_poll = float("-inf")
    self.sync()
