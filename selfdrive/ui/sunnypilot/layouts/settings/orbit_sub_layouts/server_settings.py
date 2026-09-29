"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Filas de CONEXION con el servidor ORBIT (broker MQTT).

`ServerRows` construye las filas compartidas (editar broker + probar conexion)
que usan dos superficies: la seccion CONEXION del panel ORBIT de ajustes
(orbit_panel.py) y el modal a pantalla completa que abre la tarjeta SERVIDOR
de la home (`ServerSettingsLayout`). Escribe la key "broker" a traves de
orbit/config_broker.py: en el comma va a /data/orbit_config_mqtt.json, FUERA
del arbol git, porque el updater hace `git reset --hard` y devolvia
orbit/config_mqtt.json a "broker": "" en cada OTA (la IP "se perdia" al
arrancar). mqtt_envio_general vigila ese fichero y recarga el broker en
caliente al detectar el cambio.
"""
import threading
from collections.abc import Callable

import pyray as rl

from openpilot.orbit import config_broker
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.sunnypilot.widgets.input_dialog import InputDialogSP
from openpilot.selfdrive.ui.widgets.orbit_ajustes import validar_host
from openpilot.selfdrive.ui.widgets.orbit_server import probar_servidor
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.widgets.confirm_dialog import alert_dialog
from openpilot.system.ui.sunnypilot.widgets.list_view import button_item_sp
from openpilot.system.ui.widgets import Widget, DialogResult
from openpilot.system.ui.widgets.button import ButtonStyle
from openpilot.system.ui.widgets.network import NavButton
from openpilot.system.ui.widgets.scroller_tici import Scroller


class ServerRows:
  """Filas broker-EDITAR y PROBAR, con estado del test compartido.

  El contenedor debe llamar a `poll()` desde su _render (hilo UI): el probe
  corre en un worker y el dialogo de resultado solo puede abrirse aqui.
  """

  def __init__(self):
    self._test_status = ""
    self._pending_test: str | None = None

    self._broker_row = button_item_sp(
      title=lambda: tr("Servidor ORBIT (broker MQTT)"),
      button_text=lambda: tr("EDITAR"),
      description=lambda: tr("Direccion actual:") + f" {self._broker_label()}",
      callback=self._edit_broker,
    )
    self._test_row = button_item_sp(
      title=lambda: tr("Probar conexion con ORBIT"),
      button_text=lambda: tr("PROBAR"),
      description=lambda: self._test_status or tr("Comprueba el broker MQTT y la API del backend."),
      callback=self._test_connection,
      button_style=ButtonStyle.ACTION,
    )

  @property
  def items(self) -> list:
    return [self._broker_row, self._test_row]

  def poll(self):
    if self._pending_test is not None:
      msg, self._pending_test, self._test_status = self._pending_test, None, ""
      gui_app.push_widget(alert_dialog(msg))

  # ---------------------------------------------------------------- broker
  def _read_broker_ip(self) -> str:
    broker = config_broker.leer_config().get("broker")
    return broker if isinstance(broker, str) else ""

  def _read_broker_port(self) -> int:
    try:
      return int(config_broker.leer_config().get("broker_port", 1883) or 1883)
    except (TypeError, ValueError):
      return 1883

  def _broker_label(self) -> str:
    ip = self._read_broker_ip()
    return f"{ip}:{self._read_broker_port()}" if ip else "-"

  def _edit_broker(self):
    current = self._read_broker_ip()

    def on_input(result: DialogResult, text: str):
      if result != DialogResult.CONFIRM:
        return
      text = text.strip()
      if not text:
        return
      # Mismo validador que el comma 4: solo se rechaza lo claramente invalido (IPv4 fuera
      # de rango, esquema, puerto pegado, espacios...). "1.2.3.4:1883" se guardaba entero
      # como host y el broker no conectaba nunca.
      error = validar_host(text)
      if error:
        gui_app.push_widget(alert_dialog(tr("Direccion del servidor no valida:") + f"\n{error}"))
        return
      # Solo la key "broker": el resto (puerto, backend, credenciales) se preserva.
      # Un fallo de escritura se dice: antes se tragaba y la pantalla seguia
      # mostrando la IP vieja sin explicar por que.
      if not config_broker.escribir_config({"broker": text}):
        gui_app.push_widget(alert_dialog(tr("No se pudo guardar la IP del servidor")))

    InputDialogSP(tr("IP del servidor ORBIT"), current_text=current, min_text_size=1, callback=on_input).show()

  # ---------------------------------------------------------------- probe
  def _test_connection(self):
    ip = self._read_broker_ip()
    port = self._read_broker_port()
    if not ip:
      gui_app.push_widget(alert_dialog(tr("No hay IP configurada")))
      return
    self._test_status = tr("Probando...")

    def _run():
      broker_ok, backend_ok = probar_servidor(ip, port, timeout=3.0)
      broker_msg = tr("Broker OK") if broker_ok else tr("Broker sin respuesta")
      backend_msg = tr("API OK") if backend_ok else tr("API sin respuesta")
      self._pending_test = f"{ip}:{port}\n{broker_msg} - {backend_msg}"

    threading.Thread(target=_run, name="orbit_probe", daemon=True).start()


class ServerSettingsLayout(Widget):
  """Pantalla completa (Back + filas): la envuelve el modal SERVIDOR de la home."""

  def __init__(self, back_btn_callback: Callable):
    super().__init__()
    self._back_button = NavButton(tr("Back"))
    self._back_button.set_click_callback(back_btn_callback)

    self._rows = ServerRows()
    self._scroller = Scroller(self._rows.items, line_separator=False, spacing=0)

  def _render(self, rect):
    self._rows.poll()
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
    self._scroller.show_event()
