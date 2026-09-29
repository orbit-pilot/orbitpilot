from __future__ import annotations

import pyray as rl
import qrcode
import numpy as np
import time

from openpilot.common.swaglog import cloudlog
from openpilot.common.params import Params
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.lib.application import FontWeight, gui_app
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.lib.wrap_text import wrap_text
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets.button import Button, ButtonStyle, IconButton
from openpilot.selfdrive.ui import orbit_theme as t
from openpilot.selfdrive.ui.widgets.orbit_ajustes import texto_caducidad, url_enrolamiento
from openpilot.selfdrive.ui.widgets.orbit_mando import PARAM_OWNER_ROLE, rol_etiqueta


def generar_textura_qr(url: str, previa: rl.Texture | None = None) -> rl.Texture | None:
  """Textura del QR de vinculacion; la comparten el comma 3X y el comma 4.

  Modulos NEGROS sobre BLANCO con zona de silencio de 4 modulos (border=4): el escaner
  de la app (mobile_scanner) no esta verificado con QR invertidos. Libera `previa`.
  None si falla la generacion.
  """
  try:
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)

    pil_img = qr.make_image(fill_color="black", back_color="white").convert('RGBA')
    img_array = np.array(pil_img, dtype=np.uint8)

    if previa and previa.id != 0:
      rl.unload_texture(previa)

    rl_image = rl.Image()
    rl_image.data = rl.ffi.cast("void *", img_array.ctypes.data)
    rl_image.width = pil_img.width
    rl_image.height = pil_img.height
    rl_image.mipmaps = 1
    rl_image.format = rl.PixelFormat.PIXELFORMAT_UNCOMPRESSED_R8G8B8A8

    return rl.load_texture_from_image(rl_image)
  except Exception:
    cloudlog.exception("QR code generation failed")
    return None


class OrbitEnrollDialog(Widget):
  """Dialog for linking this device to ORBIT with a QR code."""

  QR_REFRESH_INTERVAL = 300  # 5 minutes in seconds
  SUCCESS_HOLD = 2.5  # seconds the "linked" confirmation stays up before closing

  def __init__(self):
    super().__init__()
    self.params = Params()
    self.qr_texture: rl.Texture | None = None
    self.last_qr_generation = float('-inf')
    self._last_code: str | None = None
    self._claimed_at: float | None = None
    self._close_btn = IconButton(gui_app.texture("icons/close2.png", 80, 80))
    self._close_btn.set_click_callback(gui_app.pop_widget)
    self._regen_btn = Button(tr("REGENERAR CODIGO"), self._request_regen, font_size=44,
                             button_style=ButtonStyle.NORMAL, border_radius=16)

  def _get_pairing_code(self) -> str:
    try:
      return self.params.get("OrbitPairingCode") or ""
    except Exception:
      cloudlog.exception("Failed to read OrbitPairingCode")
      return ""

  def _get_pairing_url(self) -> str:
    try:
      dongle_id = self.params.get("DongleId") or ""
    except Exception:
      cloudlog.exception("Failed to read DongleId")
      dongle_id = ""
    return url_enrolamiento(dongle_id, self._get_pairing_code())

  def _generate_qr_code(self) -> None:
    self.qr_texture = generar_textura_qr(self._get_pairing_url(), self.qr_texture)

  def _check_qr_refresh(self) -> None:
    current_time = time.monotonic()
    code = self._get_pairing_code()

    # Regenerate when the pairing code changes, in addition to the periodic refresh.
    if code != self._last_code or current_time - self.last_qr_generation >= self.QR_REFRESH_INTERVAL:
      self._generate_qr_code()
      self._last_code = code
      self.last_qr_generation = current_time

  def _get_owner(self) -> str:
    try:
      return self.params.get("OrbitOwner") or ""
    except Exception:
      return ""

  def _request_regen(self) -> None:
    try:
      self.params.put_bool("OrbitEnrollRegen", True)
    except Exception:
      cloudlog.exception("Failed to set OrbitEnrollRegen")

  def _get_countdown_text(self) -> str:
    try:
      expiry = self.params.get("OrbitEnrollExpiry")
    except Exception:
      expiry = 0
    return texto_caducidad(expiry, time.time_ns() // 1_000_000)

  def _update_state(self):
    # On claim, hold a success confirmation for a moment before closing.
    try:
      claimed = bool(self.params.get_bool("OrbitClaimed"))
    except Exception:
      cloudlog.exception("Failed to read OrbitClaimed")
      return

    if not claimed:
      self._claimed_at = None
    elif self._claimed_at is None:
      self._claimed_at = time.monotonic()
    elif time.monotonic() - self._claimed_at >= self.SUCCESS_HOLD:
      gui_app.pop_widget()

  def _render(self, rect: rl.Rectangle) -> int:
    rl.clear_background(t.FONDO)

    if self._claimed_at is not None:
      self._render_success(rect)
      return -1

    self._check_qr_refresh()

    margin = 70
    content_rect = rl.Rectangle(rect.x + margin, rect.y + margin, rect.width - 2 * margin, rect.height - 2 * margin)
    y = content_rect.y

    # Close button
    close_size = 80
    pad = 20
    close_rect = rl.Rectangle(content_rect.x - pad, y - pad, close_size + pad * 2, close_size + pad * 2)
    self._close_btn.render(close_rect)

    y += close_size + 40

    # Title
    title = tr("Vincula este dispositivo a ORBIT")
    title_font = gui_app.font(FontWeight.NORMAL)
    left_width = int(content_rect.width * 0.5 - 15)

    title_wrapped = wrap_text(title_font, title, 75, left_width)
    rl.draw_text_ex(title_font, "\n".join(title_wrapped), rl.Vector2(content_rect.x, y), 75, 0.0, t.TEXTO1)  # titulo
    # Acento de seccion (vehiculo): filete corto bajo el titulo, como SectionHeaderSP.
    # Nunca relleno: solo esta marca.
    rl.draw_rectangle_rounded(rl.Rectangle(content_rect.x, y + len(title_wrapped) * 75 + 10, 24, 4),
                              1.0, 8, t.SECCION['vehiculo'])
    y += len(title_wrapped) * 75 + 60

    # Two columns: instructions and QR code
    remaining_height = content_rect.height - (y - content_rect.y)
    right_width = content_rect.width // 2 - 20

    # Instructions
    self._render_instructions(rl.Rectangle(content_rect.x, y, left_width, remaining_height))

    # Expiry countdown + manual regeneration, bottom of the left column
    btn_rect = rl.Rectangle(content_rect.x, content_rect.y + content_rect.height - 96, 520, 96)
    countdown = self._get_countdown_text()
    if countdown:
      cd_font = gui_app.font(FontWeight.MEDIUM)
      cd_size = measure_text_cached(cd_font, countdown, 40)
      rl.draw_text_ex(cd_font, countdown, rl.Vector2(int(content_rect.x), int(btn_rect.y - 24 - cd_size.y)),
                      40, 0.0, t.TEXTO2)  # cuenta atras
    self._regen_btn.render(btn_rect)

    # QR code (leave room below it for the pairing code + device ID)
    qr_size = min(right_width, content_rect.height - 110) - 40
    qr_x = content_rect.x + left_width + 40 + (right_width - qr_size) // 2
    qr_y = content_rect.y
    self._render_qr_code(rl.Rectangle(qr_x, qr_y, qr_size, qr_size))

    return -1

  def _render_instructions(self, rect: rl.Rectangle) -> None:
    instructions = [
      tr("Abre la app ORBIT"),
      tr("Ve a Inicio - Vincular dispositivo"),
      tr("Escanea el QR (o escribe el código de abajo)"),
    ]

    font = gui_app.font(FontWeight.BOLD)
    y = rect.y

    for i, text in enumerate(instructions):
      circle_radius = 25
      circle_x = rect.x + circle_radius + 15
      text_x = rect.x + circle_radius * 2 + 40
      text_width = rect.width - (circle_radius * 2 + 40)

      wrapped = wrap_text(font, text, 47, int(text_width))
      text_height = len(wrapped) * 47
      circle_y = y + text_height // 2

      # Circulo: relleno de boton (ACCION), no color de seccion; numero en SOBRE_ACCION.
      rl.draw_circle(int(circle_x), int(circle_y), circle_radius, t.ACCION)
      number = str(i + 1)
      number_size = measure_text_cached(font, number, 30)
      rl.draw_text_ex(font, number, (int(circle_x - number_size.x // 2), int(circle_y - number_size.y // 2)), 30, 0, t.SOBRE_ACCION)

      # Text
      rl.draw_text_ex(font, "\n".join(wrapped), rl.Vector2(text_x, y), 47, 0.0, t.TEXTO1)
      y += text_height + 50

  def _render_qr_code(self, rect: rl.Rectangle) -> None:
    if not self.qr_texture:
      rl.draw_rectangle_rounded(rect, 0.1, 20, t.SUP1)  # placeholder, texto de error en tinta PELIGRO
      error_font = gui_app.font(FontWeight.BOLD)
      rl.draw_text_ex(
        error_font, tr("Error generando el QR"), rl.Vector2(rect.x + 20, rect.y + rect.height // 2 - 15), 30, 0.0, t.PELIGRO
      )
      return

    # ORBIT: white quiet-zone tile behind QR so it stays scannable (QR texture kept untinted)
    tile_pad = 12
    tile_rect = rl.Rectangle(rect.x - tile_pad, rect.y - tile_pad, rect.width + tile_pad * 2, rect.height + tile_pad * 2)
    rl.draw_rectangle_rounded(tile_rect, 0.05, 20, rl.WHITE)

    source = rl.Rectangle(0, 0, self.qr_texture.width, self.qr_texture.height)
    rl.draw_texture_pro(self.qr_texture, source, rect, rl.Vector2(0, 0), 0, rl.WHITE)

    # Human-readable raw pairing code (manual-entry fallback) beneath the QR
    code = self._get_pairing_code()
    code_font = gui_app.font(FontWeight.BOLD)
    code_text = code if code else tr("esperando código...")
    code_size = 40
    code_measure = measure_text_cached(code_font, code_text, code_size)
    code_x = rect.x + (rect.width - code_measure.x) // 2
    code_y = rect.y + rect.height + 20
    rl.draw_text_ex(code_font, code_text, rl.Vector2(code_x, code_y), code_size, 0.0, t.TEXTO1)  # codigo

    # Full DongleId beneath the code (the app's manual-entry mode asks for it)
    try:
      dongle_id = self.params.get("DongleId") or ""
    except Exception:
      cloudlog.exception("Failed to read DongleId")
      dongle_id = ""
    if dongle_id:
      id_font = gui_app.font(FontWeight.NORMAL)
      id_text = f"ID: {dongle_id}"
      id_measure = measure_text_cached(id_font, id_text, 28)
      id_x = rect.x + (rect.width - id_measure.x) // 2
      rl.draw_text_ex(id_font, id_text, rl.Vector2(id_x, code_y + code_size + 14), 28, 0.0, t.TEXTO2)  # id del dispositivo

  def _render_success(self, rect: rl.Rectangle) -> None:
    # Big green check + owner, held briefly by _update_state before the pop.
    owner = self._get_owner()
    text = f"Vinculado - {owner}" if owner else "Vinculado a ORBIT"
    try:
      rol = rol_etiqueta(self.params.get(PARAM_OWNER_ROLE))
    except Exception:
      rol = ""
    font = gui_app.font(FontWeight.BOLD)
    rol_font = gui_app.font(FontWeight.NORMAL)

    check_size = 220
    rol_size = 48
    check_measure = measure_text_cached(font, "✓", check_size)
    text_measure = measure_text_cached(font, text, 64)
    # El rol del dueno (contrato C2) en su PROPIA linea: pegado al nombre, un OrbitOwner
    # largo (es el email si no hay nombre) se salia de la pantalla antes que sin el.
    rol_measure = measure_text_cached(rol_font, rol, rol_size) if rol else None
    gap = 40
    block_h = check_measure.y + gap + text_measure.y + (12 + rol_measure.y if rol_measure else 0)
    cx = rect.x + rect.width / 2
    y = rect.y + (rect.height - block_h) / 2

    rl.draw_text_ex(font, "✓", rl.Vector2(int(cx - check_measure.x / 2), int(y)), check_size, 0.0, t.OK)
    text_y = y + check_measure.y + gap
    rl.draw_text_ex(font, text, rl.Vector2(int(cx - text_measure.x / 2), int(text_y)), 64, 0.0, t.TEXTO1)
    if rol_measure:
      rl.draw_text_ex(rol_font, rol, rl.Vector2(int(cx - rol_measure.x / 2), int(text_y + text_measure.y + 12)), rol_size,
                      0.0, t.TEXTO2)

  def __del__(self):
    if self.qr_texture and self.qr_texture.id != 0:
      rl.unload_texture(self.qr_texture)


if __name__ == "__main__":
  gui_app.init_window("orbit enroll")
  enroll = OrbitEnrollDialog()
  gui_app.push_widget(enroll)
  try:
    for _ in gui_app.render():
      pass
  finally:
    del enroll
