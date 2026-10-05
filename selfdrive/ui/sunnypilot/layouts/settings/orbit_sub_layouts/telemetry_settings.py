"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Subpanel TELEMETRIA del panel ORBIT: que GRUPOS de datos manda el dispositivo a la app.

Un interruptor por grupo (modelo en orbit/telemetria_grupos.py: cada grupo apaga a la vez
su camino v1 y su camino v2) y, al final, el ahorro automatico en red movil. El emisor
relee los toggles cada 5 s como mucho, asi que no hay nada que reiniciar.

  * Sin configurar = ENCENDIDO. El estado se lee con orbit_ajustes.encendido_grupo, nunca
    con get_bool (lee "sin configurar" como apagado).
  * La POSICION se deshabilita mientras el interruptor de privacidad esta puesto: lo
    manda el, y la fila lo dice en su descripcion.
  * El estado se relee en show_event y a 2 Hz: la app o el interruptor de privacidad
    pueden cambiarlo con el panel abierto.
"""
import time
from collections.abc import Callable

import pyray as rl

from openpilot.orbit import telemetria_grupos as grupos
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui.widgets import orbit_ajustes as ajustes
from openpilot.selfdrive.ui.widgets import orbit_mando as mando
from openpilot.selfdrive.ui.widgets.orbit_section import SectionHeaderSP
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.sunnypilot.widgets.list_view import toggle_item_sp
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.confirm_dialog import alert_dialog
from openpilot.system.ui.widgets.network import NavButton
from openpilot.system.ui.widgets.scroller_tici import Scroller

REFRESH_SECONDS = 0.5


def _avisar_fallo(etiqueta: str) -> None:
  gui_app.push_widget(alert_dialog(tr("Fallo interno en:") + f" {etiqueta}\n" +
                                   tr("La accion no se ha completado. Revisa el log de la UI.")))


def _a_salvo(etiqueta: str, avisar: bool = False):
  """Una excepcion en un callback o en el refresco mata el proceso `ui` (ver orbit_panel._a_salvo)."""
  return ajustes.a_salvo(etiqueta, _avisar_fallo if avisar else None)


class TelemetrySettingsLayout(Widget):
  def __init__(self, back_btn_callback: Callable):
    super().__init__()
    self._back_button = NavButton(tr("Back"))
    self._back_button.set_click_callback(back_btn_callback)

    # Cacheado en _refrescar: los lambdas de titulo/descripcion/enabled se evaluan en cada
    # frame y mando.privacy_muted() abre un fichero.
    self._silenciado = mando.privacy_muted()
    self._ultimo_refresco = 0.0

    items = self._initialize_items()
    self._scroller = Scroller(items, line_separator=False, spacing=0)

  # ------------------------------------------------------------------------- items
  def _initialize_items(self):
    self._filas = {}
    for grupo in grupos.GRUPOS:
      self._filas[grupo.clave] = toggle_item_sp(
        title=lambda g=grupo: tr(g.titulo),
        description=lambda g=grupo: ajustes.descripcion_grupo(g, self._silenciado),
        initial_state=ajustes.encendido_grupo(ui_state.params, grupo, self._silenciado),
        callback=lambda encendido, g=grupo: self._on_grupo(g, encendido),
        enabled=lambda g=grupo: not ajustes.grupo_bloqueado(g, self._silenciado),
      )

    self._ahorro_fila = toggle_item_sp(
      title=lambda: tr("Ahorro automatico en red movil"),
      description=ajustes.texto_ahorro_movil,
      initial_state=grupos.ahorro_movil_activo(ui_state.params),
      callback=self._on_ahorro,
    )

    return [
      SectionHeaderSP(tr("DATOS QUE SE ENVIAN A LA APP"), seccion='ajustes'),
      *self._filas.values(),
      SectionHeaderSP(tr("RED MOVIL"), seccion='ajustes'),
      self._ahorro_fila,
    ]

  # ------------------------------------------------------------------------ acciones
  @_a_salvo("grupo de telemetria", avisar=True)
  def _on_grupo(self, grupo, encendido: bool):
    fallos = ajustes.cambiar_grupo(ui_state.params, grupo, encendido, self._silenciado)
    if fallos:
      gui_app.push_widget(alert_dialog(tr("No se pudo cambiar la telemetria:") + "\n" + "\n".join(fallos)))
    # Con un fallo (o la posicion bloqueada) el interruptor ya cambio de posicion pero lo
    # escrito no coincide: se vuelve a leer en el siguiente frame. Si fue bien NO se relee
    # ya: put_bool no bloquea y se leeria el valor viejo.
    self._ultimo_refresco = 0.0 if (fallos or ajustes.grupo_bloqueado(grupo, self._silenciado)) else time.monotonic()

  @_a_salvo("ahorro en red movil", avisar=True)
  def _on_ahorro(self, encendido: bool):
    fallos = ajustes.cambiar_ahorro_movil(ui_state.params, encendido)
    if fallos:
      gui_app.push_widget(alert_dialog(tr("No se pudo cambiar el ahorro en red movil:") + "\n" + "\n".join(fallos)))
      self._ultimo_refresco = 0.0

  # ------------------------------------------------------------------------ estado vivo
  @_a_salvo("refresco de telemetria")
  def _refrescar(self):
    self._ultimo_refresco = time.monotonic()
    self._silenciado = mando.privacy_muted()
    params = ui_state.params
    for grupo in grupos.GRUPOS:
      self._filas[grupo.clave].action_item.toggle.set_state(ajustes.encendido_grupo(params, grupo, self._silenciado))
    self._ahorro_fila.action_item.toggle.set_state(grupos.ahorro_movil_activo(params))

  def _update_state(self):
    super()._update_state()
    if time.monotonic() - self._ultimo_refresco >= REFRESH_SECONDS:
      self._refrescar()

  # ---------------------------------------------------------------------- lifecycle
  def _render(self, rect):
    self._back_button.set_position(self._rect.x, self._rect.y + 20)
    self._back_button.render()
    content_rect = rl.Rectangle(
      rect.x,
      rect.y + self._back_button.rect.height + 40,
      rect.width,
      rect.height - self._back_button.rect.height - 40,
    )
    self._scroller.render(content_rect)

  def show_event(self):
    self._refrescar()
    self._scroller.show_event()
