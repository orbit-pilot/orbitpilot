"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Subpanel AJUSTES AVANZADOS del panel ORBIT (antes `jetson_settings.py`).

Recoge lo que la seccion 9 del diseno manda a la app y que todavia no tiene equivalente
alli, para sacarlo del scroll del panel principal sin perder la capacidad:

  * PANTALLA: los dos avisos de HUD (angulo muerto y cambio de carril). Los overlays que
    consumen esos params se quedan onroad; lo que baja un nivel es su interruptor.
  * JETSON: enlace, estado en vivo y red (IPs, puertos, calidad JPEG) guardada en
    orbit/config_jetson.json (escritura atomica, sube _version y levanta
    JetsonConfigChanged, que camera_sender comprueba en cada vuelta de su bucle).

EL SELECTOR DE MODO DE VOLANTE YA NO ESTA AQUI. Se movio a
`orbit_sub_layouts/steer_mode.py` y se pinta en el panel ORBIT principal, porque la
seccion 9 lo deja en el comma como control de primera linea y enterrarlo tras dos
navegaciones era lo contrario.
"""
import os
import time
from collections.abc import Callable

import pyray as rl

from openpilot.common.params import UnknownKeyName
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui.widgets import orbit_ajustes as ajustes
from openpilot.selfdrive.ui.widgets.orbit_ajustes import CONFIG_DEFAULTS
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.sunnypilot.widgets.input_dialog import InputDialogSP
from openpilot.selfdrive.ui.widgets.orbit_section import SectionHeaderSP
from openpilot.selfdrive.ui.sunnypilot.layouts.settings.orbit_sub_layouts.steer_mode import (
  MODE_NAMES,
  TORQUE_STALE_SECONDS,
)
from openpilot.system.ui.sunnypilot.widgets.list_view import (
  button_item_sp,
  toggle_item_sp,
)
from openpilot.system.ui.widgets import Widget, DialogResult
from openpilot.system.ui.widgets.list_view import text_item
from openpilot.system.ui.widgets.network import NavButton
from openpilot.system.ui.widgets.scroller_tici import Scroller

# Reexportados para quien todavia los importe desde aqui; la definicion vive en
# orbit_ajustes.py (la comparte el panel del comma 4).
__all__ = ["AdvancedSettingsLayout", "MODE_NAMES", "TORQUE_STALE_SECONDS", "CONFIG_DEFAULTS"]

PARAM_READ_INTERVAL_FRAMES = 30  # ~0.5s at 60fps


class AdvancedSettingsLayout(Widget):
  def __init__(self, back_btn_callback: Callable):
    super().__init__()
    self._back_button = NavButton(tr("Back"))
    self._back_button.set_click_callback(back_btn_callback)

    self._config_path = ajustes.ruta_config_jetson()
    self._config: dict = {}
    self._load_config()
    self._config_mtime = self._current_mtime()

    # Throttle / live state
    self._frame = 0
    self._live_torque = ""
    self._live_torque_ts = ""
    self._live_obstacle = ""

    items = self._initialize_items()
    self._scroller = Scroller(items, line_separator=False, spacing=0)

  # ---------------------------------------------------------------- items
  def _initialize_items(self):
    self._jetson_enabled_toggle = toggle_item_sp(
      title=lambda: tr("Activar envio a la Jetson"),
      description=lambda: tr("Habilita el envio de imagenes a la Jetson y la recepcion de su torque."),
      initial_state=bool(self._config.get("jetson_enabled", False)),
      callback=self._on_jetson_enabled,
    )

    self._ip_button = button_item_sp(
      title=lambda: tr("IP de la Jetson"),
      button_text=lambda: tr("EDITAR"),
      callback=lambda: self._edit_config_field("jetson_ip", tr("IP de la Jetson"), is_int=False),
    )
    self._comma_ip_button = button_item_sp(
      title=lambda: tr("IP del Comma (este dispositivo)"),
      button_text=lambda: tr("EDITAR"),
      callback=lambda: self._edit_config_field("comma_ip", tr("IP del Comma (este dispositivo)"), is_int=False),
    )
    self._img_port_button = button_item_sp(
      title=lambda: tr("Puerto de imagenes"),
      button_text=lambda: tr("EDITAR"),
      callback=lambda: self._edit_config_field("jetson_img_port", tr("Puerto de imagenes"), is_int=True),
    )
    self._torque_port_button = button_item_sp(
      title=lambda: tr("Puerto de torque"),
      button_text=lambda: tr("EDITAR"),
      callback=lambda: self._edit_config_field("jetson_torque_port", tr("Puerto de torque"), is_int=True),
    )
    self._quality_button = button_item_sp(
      title=lambda: tr("Calidad de imagen (10-100)"),
      button_text=lambda: tr("EDITAR"),
      callback=lambda: self._edit_config_field("jpeg_quality", tr("Calidad de imagen (10-100)"), is_int=True,
                                               clamp=(10, 100), step=10),
    )

    # ESTADO: read-only live rows (param reads throttled in _update_state)
    self._status_torque = text_item(lambda: tr("Torque actual"), self._torque_text)
    self._status_obstacle = text_item(lambda: tr("Obstaculo detectado"), self._obstacle_yesno_text)

    self._show_blindspot_toggle = toggle_item_sp(
      param="show_blindspot",
      title=lambda: tr("MOSTRAR ANGULO MUERTO"),
      description=lambda: tr("Muestra el estado del angulo muerto en la pantalla de conduccion."),
    )
    self._lane_warn_toggle = toggle_item_sp(
      param="c_carril",
      title=lambda: tr("AVISOS EN CAMBIO DE CARRIL"),
      description=lambda: tr("Anade avisos en pantalla si hay un vehiculo en el angulo muerto " +
                             "durante un cambio de carril."),
    )

    items = [
      SectionHeaderSP(tr("PANTALLA"), seccion='desarrollo'),
      self._show_blindspot_toggle,
      self._lane_warn_toggle,
      SectionHeaderSP(tr("JETSON"), seccion='desarrollo'),
      self._jetson_enabled_toggle,
      SectionHeaderSP(tr("ESTADO"), seccion='desarrollo'),
      self._status_torque,
      self._status_obstacle,
      SectionHeaderSP(tr("RED"), seccion='desarrollo'),
      self._ip_button,
      self._comma_ip_button,
      self._img_port_button,
      self._torque_port_button,
      self._quality_button,
    ]
    return items

  # -------------------------------------------------------------- live state
  def _read_live_param(self, key: str) -> str:
    # New params written by orbit/zmq_client.py; tolerate older manifests.
    try:
      raw = ui_state.params.get(key)
    except UnknownKeyName:
      return ""
    return raw if raw else ""

  def _refresh_live_status(self):
    self._live_torque = self._read_live_param("JetsonTorque")
    self._live_torque_ts = self._read_live_param("JetsonTorqueTimestamp")
    self._live_obstacle = self._read_live_param("JetsonObstaclePulse")

  def _torque_text(self) -> str:
    return ajustes.texto_torque(self._live_torque, self._live_torque_ts, time.time_ns() / 1e9)

  def _obstacle_yesno_text(self) -> str:
    return ajustes.texto_obstaculo(self._live_obstacle)

  # --------------------------------------------------------- jetson enabled
  def _on_jetson_enabled(self, enabled: bool):
    self._config["jetson_enabled"] = bool(enabled)
    self._save_config()

  def _sync_jetson_enabled(self):
    toggle = getattr(self, "_jetson_enabled_toggle", None)
    if toggle is not None:
      toggle.action_item.toggle.set_state(bool(self._config.get("jetson_enabled", False)))

  # ------------------------------------------------------------- config IO
  def _current_mtime(self) -> float:
    try:
      return os.path.getmtime(self._config_path)
    except OSError:
      return 0.0

  def _load_config(self):
    self._config = ajustes.cargar_config_jetson(self._config_path)

  def _save_config(self):
    """Atomic write, bump _version, set JetsonConfigChanged, push MQTT payload (orbit_ajustes)."""
    if not ajustes.guardar_config_jetson(self._config_path, self._config, ui_state.params):
      return
    # Record our own write so the live-reload check does not treat it as external.
    self._config_mtime = self._current_mtime()

  def _edit_config_field(self, key: str, title: str, is_int: bool, clamp: tuple[int, int] | None = None,
                         step: int | None = None):
    current = str(self._config.get(key, CONFIG_DEFAULTS.get(key, "")))

    def on_input(result: DialogResult, text: str):
      if result != DialogResult.CONFIRM:
        return
      value = ajustes.parsear_campo(text, is_int, clamp, step)
      if value is None:
        return
      self._config[key] = value
      self._save_config()

    dialog = InputDialogSP(title, current_text=current, min_text_size=1, callback=on_input)
    dialog.show()

  # ------------------------------------------------------------- lifecycle
  def _update_state(self):
    super()._update_state()
    self._frame += 1
    if self._frame % PARAM_READ_INTERVAL_FRAMES == 0:
      self._refresh_live_status()
      # Live-reload config_jetson.json if an external writer (e.g. MQTT bridge, or el
      # interruptor de privacidad) changed it while the panel is open. Saving merges
      # keys, so this is safe even mid-edit; our own writes update _config_mtime to
      # avoid self-triggering.
      mtime = self._current_mtime()
      if mtime and mtime != self._config_mtime:
        self._config_mtime = mtime
        self._load_config()
        self._sync_jetson_enabled()

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
    self._load_config()
    self._config_mtime = self._current_mtime()
    self._sync_jetson_enabled()
    self._refresh_live_status()
    self._scroller.show_event()
