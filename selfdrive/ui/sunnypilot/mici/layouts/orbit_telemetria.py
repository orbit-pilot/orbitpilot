"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

TELEMETRIA del panel ORBIT en el comma 4: lo mismo que el subpanel de la UI grande
(sunnypilot/layouts/settings/orbit_sub_layouts/telemetry_settings.py).

Un interruptor por grupo de datos que el dispositivo manda a la app (modelo en
orbit/telemetria_grupos.py) y, al final, el ahorro automatico en red movil. La descripcion
no cabe en la baldosa de un interruptor (402x180), asi que va en una tarjeta de texto justo
detras de cada uno. La posicion sale deshabilitada, apagada y con el motivo mientras el
interruptor de privacidad esta puesto. El estado se relee al abrir y a 2 Hz: la app o el
interruptor de privacidad pueden cambiarlo con la pantalla abierta.
"""
import time

from openpilot.orbit import telemetria_grupos as grupos
from openpilot.selfdrive.ui.mici.widgets.button import BigToggle
from openpilot.selfdrive.ui.sunnypilot.mici.widgets.orbit_widgets import TarjetaTexto, a_salvo, aviso
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui.widgets import orbit_ajustes as ajustes
from openpilot.selfdrive.ui.widgets import orbit_mando as mando
from openpilot.system.ui.widgets.scroller import NavScroller

POLL_S = 0.5


class TelemetriaLayoutMici(NavScroller):
  def __init__(self):
    super().__init__()
    # Cacheado en sync(): mando.privacy_muted() abre un fichero.
    self._silenciado = mando.privacy_muted()
    self._ultimo_poll = float("-inf")

    self._grupos: dict[str, BigToggle] = {}
    self._descripciones: dict[str, TarjetaTexto] = {}
    widgets = []
    for grupo in grupos.GRUPOS:
      boton = BigToggle(grupo.titulo.lower(), toggle_callback=lambda activo, g=grupo: self._on_grupo(g, activo))
      self._grupos[grupo.clave] = boton
      self._descripciones[grupo.clave] = TarjetaTexto(grupo.descripcion)
      widgets += [boton, self._descripciones[grupo.clave]]

    self._ahorro = BigToggle("ahorro en red móvil", toggle_callback=self._on_ahorro)
    widgets.append(self._ahorro)
    widgets += [TarjetaTexto(trozo) for trozo in ajustes.trocear(ajustes.texto_ahorro_movil())]

    self._scroller.add_widgets(widgets)
    self.sync()

  # ---------------------------------------------------------------- estado
  @a_salvo("sincronizar la telemetria")
  def sync(self) -> None:
    self._ultimo_poll = time.monotonic()
    self._silenciado = mando.privacy_muted()
    params = ui_state.params
    for grupo in grupos.GRUPOS:
      boton = self._grupos[grupo.clave]
      boton.set_checked(ajustes.encendido_grupo(params, grupo, self._silenciado))
      boton.set_enabled(not ajustes.grupo_bloqueado(grupo, self._silenciado))
      self._descripciones[grupo.clave].set_value(ajustes.descripcion_grupo(grupo, self._silenciado))
    self._ahorro.set_checked(grupos.ahorro_movil_activo(params))

  def _update_state(self):
    super()._update_state()
    if time.monotonic() - self._ultimo_poll >= POLL_S:
      self.sync()

  def show_event(self):
    super().show_event()
    self.sync()

  # ---------------------------------------------------------------- acciones
  @a_salvo("grupo de telemetria", avisar=True)
  def _on_grupo(self, grupo, activo: bool) -> None:
    fallos = ajustes.cambiar_grupo(ui_state.params, grupo, activo, self._silenciado)
    if fallos:
      aviso("no se pudo cambiar", "\n".join(fallos))
    # Con un fallo (o la posicion bloqueada) el interruptor ya cambio pero lo escrito no
    # coincide: se relee en el siguiente frame. Si fue bien se espera un ciclo entero:
    # put_bool no bloquea y releer ya leeria el valor viejo.
    bloqueado = ajustes.grupo_bloqueado(grupo, self._silenciado)
    self._ultimo_poll = float("-inf") if (fallos or bloqueado) else time.monotonic()

  @a_salvo("ahorro en red movil", avisar=True)
  def _on_ahorro(self, activo: bool) -> None:
    fallos = ajustes.cambiar_ahorro_movil(ui_state.params, activo)
    if fallos:
      aviso("no se pudo cambiar", "\n".join(fallos))
      self._ultimo_poll = float("-inf")
