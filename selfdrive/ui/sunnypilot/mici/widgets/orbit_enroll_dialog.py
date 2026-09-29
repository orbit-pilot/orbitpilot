"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

QR de vinculacion con la app ORBIT en la pantalla del comma 4 (536x240).

El mismo contrato que el dialogo de la UI grande (selfdrive/ui/widgets/orbit_enroll_dialog.py,
de donde sale la textura del QR): carga orbit://enroll?d={DongleId}&c={OrbitPairingCode},
modulos negros sobre blanco con zona de silencio, codigo y dongle en texto para la entrada
manual, cuenta atras de OrbitEnrollExpiry, "nuevo codigo" (OrbitEnrollRegen) y pantalla de
exito al pasar OrbitClaimed a True.

No hereda de PairingDialog: su _update_state se cierra cuando el comma queda emparejado
con comma connect (prime), que no tiene nada que ver con ORBIT.
"""
import time

import pyray as rl

from openpilot.selfdrive.ui.mici.widgets.button import LABEL_COLOR, COMPLICATION_GREY
from openpilot.selfdrive.ui.sunnypilot.mici.widgets.orbit_widgets import a_salvo, recortar
from openpilot.selfdrive.ui.ui_state import device, ui_state
from openpilot.selfdrive.ui.widgets import orbit_ajustes as ajustes
from openpilot.selfdrive.ui.widgets import orbit_mando as mando
from openpilot.selfdrive.ui.widgets.orbit_enroll_dialog import generar_textura_qr
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.label import UnifiedLabel
from openpilot.system.ui.widgets.nav_widget import NavWidget


class _BotonTexto(Widget):
  """Pastilla pequena con texto: cabe en la columna derecha, al lado del QR."""

  def __init__(self, texto: str, al_pulsar):
    super().__init__()
    self.texto = texto
    self._fuente = gui_app.font(FontWeight.MEDIUM)
    self.set_click_callback(al_pulsar)

  def _render(self, _):
    alfa = 0.3 if self.is_pressed else 0.15
    rl.draw_rectangle_rounded(self._rect, 1.0, 12, rl.Color(255, 255, 255, int(255 * alfa)))
    tam = measure_text_cached(self._fuente, self.texto, 24)
    rl.draw_text_ex(self._fuente, self.texto, rl.Vector2(self._rect.x + (self._rect.width - tam.x) / 2,
                                                         self._rect.y + (self._rect.height - tam.y) / 2), 24, 0, LABEL_COLOR)


class OrbitEnrollDialogMici(NavWidget):
  QR_REFRESH_INTERVAL = 300  # s, como el de la UI grande
  SUCCESS_HOLD = 2.5         # s que se ve "vinculado" antes de cerrar
  POLL_S = 0.5               # lectura de Params a 2 Hz, no por frame
  REGEN_AVISO_S = 3.0

  def __init__(self):
    super().__init__()
    self._qr: rl.Texture | None = None
    self._ultima_gen = float("-inf")
    self._codigo_qr: str | None = None
    self._ultimo_poll = float("-inf")
    self._regen_hasta = 0.0

    self._codigo = ""
    self._dongle = ""
    self._caducidad = ""
    self._vinculado = False
    self._vinculado_en: float | None = None
    self._cuenta = ""

    self._f_bold = gui_app.font(FontWeight.BOLD)
    self._f_display = gui_app.font(FontWeight.DISPLAY)
    self._f_roman = gui_app.font(FontWeight.ROMAN)
    self._titulo = UnifiedLabel("escanea con\nla app orbit", 30, font_weight=FontWeight.BOLD, text_color=LABEL_COLOR, line_height=0.9)
    self._icono_ok = gui_app.texture("icons_mici/setup/driver_monitoring/dm_check.png", 64, 64)
    self._boton = _BotonTexto("nuevo código", self._regenerar)
    self._boton.set_enabled(lambda: self.enabled and not self.is_dismissing)

  # ---------------------------------------------------------------- params
  @a_salvo("lectura del QR de vinculacion")
  def _leer(self) -> None:
    p = ui_state.params
    self._codigo = p.get("OrbitPairingCode") or ""
    self._dongle = p.get("DongleId") or ""
    self._caducidad = ajustes.texto_caducidad(p.get("OrbitEnrollExpiry"), time.time_ns() // 1_000_000)
    self._vinculado = bool(p.get_bool("OrbitClaimed"))
    self._cuenta = ajustes.etiqueta_cuenta(p.get(mando.PARAM_OWNER), p.get(mando.PARAM_OWNER_ROLE))

  @a_salvo("regenerar el codigo de vinculacion", avisar=True)
  def _regenerar(self) -> None:
    ui_state.params.put_bool("OrbitEnrollRegen", True)
    self._regen_hasta = time.monotonic() + self.REGEN_AVISO_S

  def _update_state(self):
    super()._update_state()
    ahora = time.monotonic()
    if ahora - self._ultimo_poll >= self.POLL_S:
      self._ultimo_poll = ahora
      self._leer()

    if not self._vinculado:
      self._vinculado_en = None
    elif self._vinculado_en is None:
      self._vinculado_en = ahora
    elif ahora - self._vinculado_en >= self.SUCCESS_HOLD and not self.is_dismissing:
      self.dismiss()

  def _refrescar_qr(self) -> None:
    ahora = time.monotonic()
    # Se regenera al cambiar el codigo, ademas de cada 5 minutos.
    if self._codigo != self._codigo_qr or ahora - self._ultima_gen >= self.QR_REFRESH_INTERVAL:
      self._qr = generar_textura_qr(ajustes.url_enrolamiento(self._dongle, self._codigo), self._qr)
      self._codigo_qr = self._codigo
      self._ultima_gen = ahora

  # ---------------------------------------------------------------- pintado
  def _render(self, rect: rl.Rectangle):
    if self._vinculado_en is not None:
      self._render_exito(rect)
      return

    lado = rect.height
    qr_rect = rl.Rectangle(rect.x + 8, rect.y, lado, lado)
    if not self._codigo:
      # Sin codigo (p. ej. justo tras desvincular: el claim borra OrbitPairingCode pero no
      # OrbitEnrollExpiry) un QR con `c=` vacio y una cuenta atras parecen un codigo valido.
      rl.draw_rectangle_rounded(qr_rect, 0.08, 12, rl.Color(255, 255, 255, 20))
      tam = measure_text_cached(self._f_bold, "sin código", 28)
      rl.draw_text_ex(self._f_bold, "sin código", rl.Vector2(qr_rect.x + (lado - tam.x) / 2, qr_rect.y + (lado - tam.y) / 2),
                      28, 0, COMPLICATION_GREY)
    else:
      self._refrescar_qr()
      if self._qr is not None:
        rl.draw_texture_pro(self._qr, rl.Rectangle(0, 0, self._qr.width, self._qr.height), qr_rect, rl.Vector2(0, 0), 0, rl.WHITE)
      else:
        rl.draw_text_ex(self._f_bold, "error del QR", rl.Vector2(qr_rect.x + 20, qr_rect.y + lado / 2 - 15), 30, 0, rl.RED)

    x = qr_rect.x + lado + 18
    ancho = rect.x + rect.width - 14 - x
    self._titulo.set_max_width(int(ancho))
    self._titulo.set_position(x, rect.y + 12)
    self._titulo.render()

    codigo = self._codigo or "esperando código..."
    tam_codigo = 40 if self._codigo else 26
    rl.draw_text_ex(self._f_display, recortar(self._f_display, codigo, tam_codigo, ancho), rl.Vector2(x, rect.y + 82),
                    tam_codigo, 0, rl.WHITE)
    if self._codigo and self._caducidad:
      rl.draw_text_ex(self._f_roman, self._caducidad, rl.Vector2(x, rect.y + 134), 24, 0, COMPLICATION_GREY)
    if self._dongle:
      rl.draw_text_ex(self._f_roman, recortar(self._f_roman, f"id {self._dongle}", 18, ancho), rl.Vector2(x, rect.y + 162),
                      18, 0, COMPLICATION_GREY)

    self._boton.texto = "pedido, espera..." if time.monotonic() < self._regen_hasta else "nuevo código"
    self._boton.render(rl.Rectangle(x, rect.y + rect.height - 50, ancho, 44))

  def _render_exito(self, rect: rl.Rectangle) -> None:
    cx = rect.x + rect.width / 2
    rl.draw_texture_ex(self._icono_ok, rl.Vector2(cx - self._icono_ok.width / 2, rect.y + 28), 0, 1.0, rl.WHITE)
    titulo = "vinculado"
    tam = measure_text_cached(self._f_display, titulo, 48)
    rl.draw_text_ex(self._f_display, titulo, rl.Vector2(cx - tam.x / 2, rect.y + 104), 48, 0, LABEL_COLOR)
    if self._cuenta:
      cuenta = recortar(self._f_roman, self._cuenta, 32, rect.width - 40)
      tam = measure_text_cached(self._f_roman, cuenta, 32)
      rl.draw_text_ex(self._f_roman, cuenta, rl.Vector2(cx - tam.x / 2, rect.y + 170), 32, 0, COMPLICATION_GREY)

  # ---------------------------------------------------------------- ciclo de vida
  def show_event(self):
    super().show_event()
    self._ultimo_poll = float("-inf")
    self._vinculado_en = None
    self._leer()
    # Sin esto el dialogo se cierra a los 30 s sin tocar la pantalla (offroad), y
    # escanear + escribir en el movil tarda mas que eso. Como ReviewTrainingGuide.
    device.set_override_interactive_timeout(300)

  def hide_event(self):
    super().hide_event()
    device.set_override_interactive_timeout(None)

  def __del__(self):
    if self._qr is not None and self._qr.id != 0:
      rl.unload_texture(self._qr)
