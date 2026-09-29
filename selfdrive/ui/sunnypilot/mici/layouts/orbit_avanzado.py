"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

AJUSTES AVANZADOS del panel ORBIT en el comma 4: lo mismo que el subpanel de la UI grande
(sunnypilot/layouts/settings/orbit_sub_layouts/advanced_settings.py).

  * avisos de HUD: show_blindspot y c_carril (Params, directos),
  * JETSON: envio activado, estado en vivo (torque y obstaculo) y red (IPs, puertos,
    calidad JPEG) en orbit/config_jetson.json, con la carga, el guardado atomico, el flag
    JetsonConfigChanged y el payload MQTT de orbit_ajustes.
"""
import os
import time

from openpilot.common.params import UnknownKeyName
from openpilot.selfdrive.ui.mici.widgets.button import BigParamControl, BigToggle
from openpilot.selfdrive.ui.sunnypilot.mici.widgets.orbit_widgets import Baldosa, TarjetaTexto, a_salvo, aviso, dialogo_numerico
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui.widgets import orbit_ajustes as ajustes
from openpilot.system.ui.widgets.scroller import NavScroller

POLL_S = 0.5  # estado en vivo y recarga del fichero a 2 Hz

ETIQUETAS = {
  "jetson_ip": "ip de la jetson",
  "comma_ip": "ip del comma",
  "jetson_img_port": "puerto de imágenes",
  "jetson_torque_port": "puerto de torque",
  "jpeg_quality": "calidad (10-100)",  # se redondea a decenas y se limita a este rango
}


class AvanzadoLayoutMici(NavScroller):
  def __init__(self):
    super().__init__()
    self._ruta = ajustes.ruta_config_jetson()
    self._config = ajustes.cargar_config_jetson(self._ruta)
    self._mtime = self._leer_mtime()
    self._ultimo_poll = float("-inf")

    self._angulo = BigParamControl("mostrar ángulo muerto", "show_blindspot")
    self._carril = BigParamControl("avisos en cambio de carril", "c_carril")
    self._jetson = BigToggle("envío a la jetson", initial_state=bool(self._config.get("jetson_enabled", False)),
                             toggle_callback=self._on_jetson)
    self._estado = TarjetaTexto("", "estado de la jetson")

    self._campos: dict[str, Baldosa] = {}
    for clave, es_entero, rango, paso in ajustes.CAMPOS_JETSON:
      boton = Baldosa(ETIQUETAS[clave], "")
      boton.set_click_callback(lambda c=clave, e=es_entero, r=rango, p=paso: self._editar(c, e, r, p))
      self._campos[clave] = boton
    self._sync_campos()

    self._scroller.add_widgets([self._angulo, self._carril, self._jetson, self._estado, *self._campos.values()])

  # ---------------------------------------------------------------- config
  def _leer_mtime(self) -> float:
    try:
      return os.path.getmtime(self._ruta)
    except OSError:
      return 0.0

  def _sync_campos(self) -> None:
    self._jetson.set_checked(bool(self._config.get("jetson_enabled", False)))
    for clave, boton in self._campos.items():
      boton.set_value(str(self._config.get(clave, ajustes.CONFIG_DEFAULTS.get(clave, ""))) or "sin configurar")

  def _recargar(self) -> None:
    self._config = ajustes.cargar_config_jetson(self._ruta)
    self._mtime = self._leer_mtime()
    self._sync_campos()

  def _guardar(self) -> None:
    if not ajustes.guardar_config_jetson(self._ruta, self._config, ui_state.params):
      aviso("no se pudo guardar", "config_jetson.json")
      self._recargar()
      return
    # La escritura propia no cuenta como cambio externo en la recarga en vivo.
    self._mtime = self._leer_mtime()
    self._sync_campos()

  @a_salvo("envio a la jetson", avisar=True)
  def _on_jetson(self, activo: bool) -> None:
    self._config["jetson_enabled"] = bool(activo)
    self._guardar()

  @a_salvo("editar la red de la jetson", avisar=True)
  def _editar(self, clave: str, es_entero: bool, rango, paso, texto: str | None = None) -> None:
    actual = texto if texto is not None else str(self._config.get(clave, ajustes.CONFIG_DEFAULTS.get(clave, "")))

    @a_salvo("guardar la red de la jetson", avisar=True)
    def al_confirmar(escrito: str) -> None:
      error = "" if es_entero else ajustes.validar_host(escrito)
      valor = None if error else ajustes.parsear_campo(escrito, es_entero, rango, paso)
      if valor is None:
        # Se reabre con lo escrito y el motivo encima: al cerrar el aviso se corrige.
        self._editar(clave, es_entero, rango, paso, escrito)
        aviso("valor no válido", error or "escribe un número")
        return
      self._config[clave] = valor
      self._guardar()

    dialogo_numerico(ETIQUETAS[clave] + "...", actual, al_confirmar)

  # ---------------------------------------------------------------- estado vivo
  def _param(self, clave: str) -> str:
    try:
      return ui_state.params.get(clave) or ""
    except UnknownKeyName:
      return ""

  @a_salvo("refresco del estado de la jetson")
  def _refrescar(self) -> None:
    torque = ajustes.texto_torque(self._param("JetsonTorque"), self._param("JetsonTorqueTimestamp"), time.time_ns() / 1e9)
    obstaculo = ajustes.texto_obstaculo(self._param("JetsonObstaclePulse")).lower()
    self._estado.set_value(f"torque actual: {torque}\nobstáculo detectado: {obstaculo}")
    # Recarga si alguien mas cambio el fichero (MQTT, el interruptor de privacidad...).
    mtime = self._leer_mtime()
    if mtime and mtime != self._mtime:
      self._recargar()

  def _update_state(self):
    super()._update_state()
    ahora = time.monotonic()
    if ahora - self._ultimo_poll >= POLL_S:
      self._ultimo_poll = ahora
      self._refrescar()

  def show_event(self):
    super().show_event()
    self._recargar()
    self._angulo.refresh()
    self._carril.refresh()
    self._ultimo_poll = float("-inf")
