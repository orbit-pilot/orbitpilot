from __future__ import annotations

import datetime
import os
import socket
import subprocess
import threading
import time
import pyray as rl
from enum import IntEnum
from cereal import log
from openpilot.common.params import Params
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui.widgets.offroad_alerts import UpdateAlert, OffroadAlert
from openpilot.selfdrive.ui.widgets.orbit_mando import PARAM_OWNER_ROLE, rol_etiqueta
from openpilot.selfdrive.ui.widgets.orbit_server import ServerMonitor, read_broker
from openpilot.system.hardware import HARDWARE
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.lib.application import gui_app, FontWeight, MousePos, FONT_SCALE
from openpilot.system.ui.lib.multilang import tr, trn
from openpilot.system.ui.widgets import Widget
from openpilot.selfdrive.ui.widgets import orbit_fx as fx
from openpilot.selfdrive.ui.widgets import orbit_duplex as duplex
from openpilot.selfdrive.ui import orbit_theme as t

HEADER_HEIGHT = 130
HEAD_BUTTON_FONT_SIZE = 36
CONTENT_MARGIN = 40
SPACING = 25
REFRESH_INTERVAL = 10.0       # slow refresh: params for system/updater/network info
FAST_REFRESH_INTERVAL = 2.0   # fast refresh: orbit link params (claimed/connected/...)

# ORBIT palette: alias hacia los tokens unicos (ver orbit_theme.py).
VOID = t.FONDO
NAVY = t.SUP1
PANEL = t.SUP2
HAIRLINE = t.BORDE
COMMANDS = t.OK             # downlink confirmado
PULSE = t.PULSO             # accento en vivo
INK = t.TEXTO1
MUTED = t.TEXTO2
MUTED_DIM = t.TEXTO3
AMBER = t.AVISO             # warning / no-connection
BLUE = t.ACCION              # relleno de boton de accion
BLUE_HI = t.ACCION

# Brand block (top-left): anillo Dúplex + wordmark + acronym
LOGO_SIZE = 120
DUPLEX_RADIO = LOGO_SIZE * 76 / 240   # mismo ratio r76/caja240 del SVG del logo
DUPLEX_ENTRADA_S = 0.7                 # s: dibujo 0->1 del anillo al mostrarse el home
WORDMARK_SIZE = 64
WORDMARK_SPACING = 10
TAGLINE = "Open Remote Bidirectional IoV Telemetry"
TAGLINE_SIZE = 26

# Link pill (top-right)
PILL_HEIGHT = 60
PILL_FONT_SIZE = 30

# "powered by drago" credit badge (bottom-right corner)
DRAGO_LOGO_H = 72
DRAGO_ASPECT = 469 / 640   # source logo-drago.png is portrait
POWERED_SIZE = 28

# Modelo comercial por HARDWARE.get_device_type(); cualquier otro valor se ensena tal cual.
MODELOS = {"tici": "comma 3", "tizi": "comma 3X", "mici": "comma 4", "pc": "PC"}

# Card grid: row 1 = live status (SERVIDOR / ENLACE / DISPOSITIVO),
# row 2 = info strip (SISTEMA / ACTUALIZACION / RED)
CARD_GAP = 26
ROW1_MAX_H = 390
ROW2_MAX_H = 280
CARD_PAD = 30
CARD_BOTTOM_RESERVE = 96   # space below the cards for the drago badge

# Telemetry pulse (ECG-style polyline on the SERVIDOR card), one beat per period
# as (fraction of period, offset in amplitudes; negative = up).
WAVE_BEAT = ((0.00, 0.0), (0.30, 0.0), (0.36, -0.30), (0.42, 0.0), (0.48, 0.20),
             (0.54, -1.00), (0.60, 0.35), (0.66, 0.0), (1.00, 0.0))
WAVE_PERIOD = 125.0   # px
WAVE_AMP = 22.0       # px
WAVE_SPEED = 70.0     # px/s

# Entrance + ambient animation
CASCADE_START = 0.15      # s after show before the first card animates in
HEADER_FADE_S = 0.30      # header fade-in
SHIMMER_PERIOD = 6.0      # s between shimmer sweeps on the wordmark underline
SHIMMER_S = 0.8           # sweep duration
SHIMMER_W = 46            # sweep width px

NetworkType = log.DeviceState.NetworkType
NET_TYPE_NAMES = {
  NetworkType.none: "--",
  NetworkType.wifi: "WiFi",
  NetworkType.ethernet: "ETH",
  NetworkType.cell2G: "2G",
  NetworkType.cell3G: "3G",
  NetworkType.cell4G: "LTE",
  NetworkType.cell5G: "5G",
}


class HomeLayoutState(IntEnum):
  HOME = 0
  UPDATE = 1
  ALERTS = 2


class _ServerSettingsModal(Widget):
  """Full-screen modal wrapping the ORBIT server settings so they can be
  opened straight from the home (tap the SERVIDOR card)."""

  def __init__(self):
    super().__init__()
    from openpilot.selfdrive.ui.sunnypilot.layouts.settings.orbit_sub_layouts.server_settings import (
      ServerSettingsLayout,
    )
    self._panel = ServerSettingsLayout(back_btn_callback=gui_app.pop_widget)

  def show_event(self):
    super().show_event()
    self._panel.show_event()

  def _render(self, rect: rl.Rectangle):
    rl.draw_rectangle(int(rect.x), int(rect.y), int(rect.width), int(rect.height), VOID)
    m = 60
    self._panel.render(rl.Rectangle(rect.x + m, rect.y + m, rect.width - 2 * m, rect.height - 2 * m))


class HomeLayout(Widget):
  def __init__(self):
    super().__init__()
    self.params = Params()

    self.update_alert = UpdateAlert()
    self.offroad_alert = OffroadAlert()

    self._layout_widgets = {HomeLayoutState.UPDATE: self.update_alert, HomeLayoutState.ALERTS: self.offroad_alert}

    self.current_state = HomeLayoutState.HOME
    self.last_refresh = 0.0
    self._last_fast_refresh = 0.0

    self.update_available = False
    self.alert_count = 0
    self._prev_update_available = False
    self._prev_alerts_present = False

    # Cached param reads — slow refresh (REFRESH_INTERVAL)
    self._version_text = ""
    self._sys_branch = ""
    self._sys_commit = ""
    self._upd_state = "idle"
    self._upd_desc = ""
    self._upd_checked: str | None = None
    self._net_ip = ""
    self._net_type = "--"
    self._net_ssid = ""
    self._ssid_inflight = False
    self._broker_addr = ""
    self._drago_rect = rl.Rectangle(0, 0, 0, 0)

    # Cached param reads — fast refresh (FAST_REFRESH_INTERVAL)
    self._claimed = False
    self._owner = ""
    self._owner_role = ""
    self._connected = False
    self._dongle = ""
    self._orbit_last_publish = 0.0

    self.header_rect = rl.Rectangle(0, 0, 0, 0)
    self.content_rect = rl.Rectangle(0, 0, 0, 0)

    self.update_notif_rect = rl.Rectangle(0, 0, 200, 60)
    self.alert_notif_rect = rl.Rectangle(0, 0, 220, 60)

    try:
      self._drago = gui_app.texture("img_drago_logo.png", int(DRAGO_LOGO_H * DRAGO_ASPECT) + 6,
                                    DRAGO_LOGO_H, keep_aspect_ratio=True)
    except Exception:
      self._drago = None

    # Live reachability of the Orbit server (background TCP+HTTP probe) + tappable hitboxes.
    self._server = ServerMonitor()
    tipo = HARDWARE.get_device_type()
    self._modelo = MODELOS.get(tipo, tipo)
    self._card_rects: dict[str, rl.Rectangle] = {}
    self._pill_rect = rl.Rectangle(0, 0, 0, 0)

    # Ambient background + entrance cascade
    self._campo = fx.CampoOrbital(seed=7)
    self._cascade = fx.Cascade(stagger=0.07, duration=0.35, rise=24.0)
    self._shown_at = time.monotonic()
    self._last_render_t = time.monotonic()

    self._setup_callbacks()

  def show_event(self):
    super().show_event()
    self.last_refresh = time.monotonic()
    self._last_fast_refresh = self.last_refresh
    self._fast_refresh()
    self._refresh()
    self._shown_at = time.monotonic()

  def _setup_callbacks(self):
    self.update_alert.set_dismiss_callback(lambda: self._set_state(HomeLayoutState.HOME))
    self.offroad_alert.set_dismiss_callback(lambda: self._set_state(HomeLayoutState.HOME))

  def _set_state(self, state: HomeLayoutState):
    # propagate show/hide events
    if state != self.current_state:
      if state in self._layout_widgets:
        self._layout_widgets[state].show_event()
      if self.current_state in self._layout_widgets:
        self._layout_widgets[self.current_state].hide_event()

    self.current_state = state

  def _render(self, rect: rl.Rectangle):
    current_time = time.monotonic()
    # If we were occluded (splash/onboarding/settings on top: only the top
    # widget renders), restart the entrance animation on re-appearance.
    if current_time - self._last_render_t > 0.5:
      self._shown_at = current_time
    self._last_render_t = current_time
    if current_time - self.last_refresh >= REFRESH_INTERVAL:
      self._refresh()
      self.last_refresh = current_time
    if current_time - self._last_fast_refresh >= FAST_REFRESH_INTERVAL:
      self._fast_refresh()
      self._last_fast_refresh = current_time

    rl.draw_rectangle(int(rect.x), int(rect.y), int(rect.width), int(rect.height), VOID)
    # Campo orbital detrás de todo, centrado en el anillo Dúplex del bloque de marca.
    centro_anillo = rl.Vector2(self.header_rect.x + LOGO_SIZE / 2, self.header_rect.y + self.header_rect.height / 2)
    self._campo.render(rect, centro_anillo, t.SECCION['vehiculo'], current_time, self._connected)

    self._render_header()

    # Render content based on current state
    if self.current_state == HomeLayoutState.HOME:
      self._render_home_content()
    elif self.current_state == HomeLayoutState.UPDATE:
      self._render_update_view()
    elif self.current_state == HomeLayoutState.ALERTS:
      self._render_alerts_view()

  def _update_state(self):
    self.header_rect = rl.Rectangle(
      self._rect.x + CONTENT_MARGIN, self._rect.y + CONTENT_MARGIN, self._rect.width - 2 * CONTENT_MARGIN, HEADER_HEIGHT
    )

    content_y = self._rect.y + CONTENT_MARGIN + HEADER_HEIGHT + SPACING
    content_height = self._rect.height - CONTENT_MARGIN - HEADER_HEIGHT - SPACING - CONTENT_MARGIN

    self.content_rect = rl.Rectangle(
      self._rect.x + CONTENT_MARGIN, content_y, self._rect.width - 2 * CONTENT_MARGIN, content_height
    )

  def _handle_mouse_release(self, mouse_pos: MousePos):
    super()._handle_mouse_release(mouse_pos)

    if self.update_available and rl.check_collision_point_rec(mouse_pos, self.update_notif_rect):
      self._set_state(HomeLayoutState.UPDATE)
      return
    if self.alert_count > 0 and rl.check_collision_point_rec(mouse_pos, self.alert_notif_rect):
      self._set_state(HomeLayoutState.ALERTS)
      return

    # Link pill lives in the header (all states): tap to enroll while unclaimed.
    if not self._claimed and rl.check_collision_point_rec(mouse_pos, self._pill_rect):
      self._open_enroll()
      return

    # Tappable status cards (home view only)
    if self.current_state == HomeLayoutState.HOME:
      empty = rl.Rectangle(0, 0, 0, 0)
      if rl.check_collision_point_rec(mouse_pos, self._card_rects.get("server", empty)):
        self._open_server_settings()
      elif not self._claimed and rl.check_collision_point_rec(mouse_pos, self._card_rects.get("link", empty)):
        self._open_enroll()
      elif self.update_available and rl.check_collision_point_rec(mouse_pos, self._card_rects.get("update", empty)):
        self._set_state(HomeLayoutState.UPDATE)
      elif rl.check_collision_point_rec(mouse_pos, self._drago_rect):
        from openpilot.selfdrive.ui.widgets.about_drago import AboutDragoDialog
        gui_app.push_widget(AboutDragoDialog())

  # ---------------------------------------------------------------------------
  # Header: brand top-left, link pill + notification buttons top-right
  # ---------------------------------------------------------------------------
  def _render_header(self):
    hdr = self.header_rect
    normal = gui_app.font(FontWeight.NORMAL)
    medium = gui_app.font(FontWeight.MEDIUM)

    ha = fx.ease_out_cubic((time.monotonic() - self._shown_at) / HEADER_FADE_S)

    # Brand block: anillo Dúplex + ORBIT wordmark with cyan accent + acronym below
    entrada = fx.ease_out_cubic((time.monotonic() - self._shown_at) / DUPLEX_ENTRADA_S)
    duplex.draw_duplex_ring(hdr.x + LOGO_SIZE / 2, hdr.y + hdr.height / 2, DUPLEX_RADIO,
                            self._connected, dibujo=entrada)

    tx = hdr.x + LOGO_SIZE + 30
    xbold = gui_app.font(FontWeight.EXTRA_BOLD)   # wordmark: Inter 800 con degradado, como el mockup
    wm = measure_text_cached(xbold, "ORBIT", WORDMARK_SIZE, WORDMARK_SPACING)
    tg = measure_text_cached(normal, TAGLINE, TAGLINE_SIZE)
    block_h = wm.y + 16 + tg.y
    ty = hdr.y + (hdr.height - block_h) / 2
    fx.draw_text_gradient_v(xbold, "ORBIT", rl.Vector2(int(tx), int(ty)), WORDMARK_SIZE,
                            WORDMARK_SPACING, width=wm.x, height=wm.y, alpha=ha)
    rl.draw_rectangle(int(tx), int(ty + wm.y + 5), int(wm.x), 3, fx.col(PULSE, ha))
    rl.draw_text_ex(normal, TAGLINE, rl.Vector2(int(tx), int(ty + wm.y + 16)), TAGLINE_SIZE, 0, fx.col(MUTED, ha))

    # Periodic shimmer sweeping across the cyan underline
    phase = time.monotonic() % SHIMMER_PERIOD
    if phase < SHIMMER_S and ha >= 1.0:
      sx = int(tx + (wm.x - SHIMMER_W) * (phase / SHIMMER_S))
      uy = int(ty + wm.y + 5)
      half = SHIMMER_W // 2
      rl.draw_rectangle_gradient_h(sx, uy, half, 3, fx.col(INK, 0.0), fx.col(INK, 0.9))
      rl.draw_rectangle_gradient_h(sx + half, uy, half, 3, fx.col(INK, 0.9), fx.col(INK, 0.0))

    # Right side, laid right-to-left: link pill, then alert/update notif buttons.
    right = hdr.x + hdr.width
    pill_w = self._render_link_pill(right, hdr.y + (hdr.height - PILL_HEIGHT) / 2)
    right -= pill_w + SPACING

    if self.alert_count > 0:
      self.alert_notif_rect.x = right - self.alert_notif_rect.width
      self.alert_notif_rect.y = hdr.y + (hdr.height - self.alert_notif_rect.height) / 2

      # Relleno rojo: unico rojo permitido es FRENO (destructivo/alerta), con texto SOBRE_FRENO.
      rl.draw_rectangle_rounded(self.alert_notif_rect, 0.3, 10, t.FRENO)

      alert_text = trn("{} ALERT", "{} ALERTS", self.alert_count).format(self.alert_count)
      text_size = measure_text_cached(medium, alert_text, HEAD_BUTTON_FONT_SIZE)
      text_x = self.alert_notif_rect.x + (self.alert_notif_rect.width - text_size.x) // 2
      text_y = self.alert_notif_rect.y + (self.alert_notif_rect.height - text_size.y) // 2
      rl.draw_text_ex(medium, alert_text, rl.Vector2(int(text_x), int(text_y)), HEAD_BUTTON_FONT_SIZE, 0, t.SOBRE_FRENO)
      right -= self.alert_notif_rect.width + SPACING

    if self.update_available:
      self.update_notif_rect.x = right - self.update_notif_rect.width
      self.update_notif_rect.y = hdr.y + (hdr.height - self.update_notif_rect.height) / 2

      # BLUE_HI y BLUE son el mismo token ACCION en Grafito: el resalte de
      # seleccionado ya no puede ser un relleno mas claro, se marca con un aro.
      rl.draw_rectangle_rounded(self.update_notif_rect, 0.3, 10, BLUE)
      if self.current_state == HomeLayoutState.UPDATE:
        rl.draw_rectangle_rounded_lines_ex(self.update_notif_rect, 0.3, 10, 2, t.TEXTO1)

      text = tr("UPDATE")
      text_size = measure_text_cached(medium, text, HEAD_BUTTON_FONT_SIZE)
      text_x = self.update_notif_rect.x + (self.update_notif_rect.width - text_size.x) // 2
      text_y = self.update_notif_rect.y + (self.update_notif_rect.height - text_size.y) // 2
      rl.draw_text_ex(medium, text, rl.Vector2(int(text_x), int(text_y)), HEAD_BUTTON_FONT_SIZE, 0, t.SOBRE_ACCION)

  def _render_update_view(self):
    self.update_alert.render(self.content_rect)

  def _render_alerts_view(self):
    self.offroad_alert.render(self.content_rect)

  # ---------------------------------------------------------------------------
  # ORBIT home: two rows of data cards + powered-by badge
  # ---------------------------------------------------------------------------
  def _render_home_content(self):
    content = self.content_rect
    avail = content.height - CARD_BOTTOM_RESERVE
    row1_h = min(ROW1_MAX_H, (avail - CARD_GAP) * 0.56)
    row2_h = min(ROW2_MAX_H, avail - CARD_GAP - row1_h)
    block_h = row1_h + CARD_GAP + row2_h
    y0 = content.y + max((avail - block_h) / 2, 0)

    self._card_rects = {}
    self._render_status_cards(rl.Rectangle(content.x, y0, content.width, row1_h))
    self._render_info_cards(rl.Rectangle(content.x, y0 + row1_h + CARD_GAP, content.width, row2_h))
    self._render_powered_by()

  def _render_status_cards(self, rect: rl.Rectangle):
    # Live, actionable status. SERVIDOR -> tap opens server-IP settings (with a
    # test button); ENLACE -> tap shows the QR to claim the device; DISPOSITIVO -> info.
    claimed = self._claimed
    broker_ok = self._server.broker_ok
    backend_ok = self._server.backend_ok
    dev_val = self._dongle if self._dongle and self._dongle != "UnregisteredDevice" else "sin registrar"

    # Solo ASCII: el "check" unicode no existe en las fuentes bitmap.
    if broker_ok and backend_ok:
      server_val, server_color = "BROKER OK - API OK", COMMANDS
    elif broker_ok:
      server_val, server_color = "BROKER OK - sin API", AMBER
    elif backend_ok:
      server_val, server_color = "sin BROKER - API OK", AMBER
    else:
      server_val, server_color = "Sin conexion", AMBER
    server_sub = self._last_publish_text() if (broker_ok or backend_ok) else "toca para configurar la IP"

    cards = [
      ("server", "SERVIDOR", server_val, server_color,
       self._broker_addr or "sin configurar", server_sub, True),
      ("link", "ENLACE ORBIT",
       # Cyan (PULSO) solo significa «en vivo»: sin enlazar es una invitación
       # a tocar, no un enlace activo, así que usa tinta/borde neutros.
       "Enlazado" if claimed else "Sin enlazar", COMMANDS if claimed else INK,
       (f"propietario: {self._owner}" if self._owner else "cuenta ORBIT activa") if claimed else "escanea el QR con la app",
       (f"rol: {self._owner_role}" if self._owner_role else "dispositivo activo") if claimed else "toca para ver el QR", not claimed),
      ("device", "DISPOSITIVO", dev_val, INK,
       self._modelo if dev_val != "sin registrar" else "conecta el dispositivo",
       "ID de dispositivo" if dev_val != "sin registrar" else "aún sin dongle", False),
    ]

    now = time.monotonic()
    ts = now - self._shown_at - CASCADE_START
    n = len(cards)
    cw = (rect.width - CARD_GAP * (n - 1)) / n
    for i, (key, title, value, color, detail, sub, tappable) in enumerate(cards):
      a, dy, _scale = self._cascade.values(ts, i)
      card = rl.Rectangle(rect.x + i * (cw + CARD_GAP), rect.y + dy, cw, rect.height)
      self._card_rects[key] = card
      chevron_color, border = color, None
      if key == "link" and not claimed:
        # Ni el borde ni el chevron son PULSO aquí: sin enlazar no hay nada
        # «en vivo» que marcar. Borde reforzado (tappable, >=3:1) + chevron neutro.
        chevron_color, border = MUTED, t.BORDE_FUERTE
      self._draw_card(card, title, value, color, detail, sub, tappable, value_size=52,
                      value_y=rect.height * 0.30, chevron_color=chevron_color, border=border, alpha=a)
      if key == "server":
        self._render_telemetry_wave(card, alpha=a)
        if self._connected:
          # Live pulse dot right after the SERVIDOR title
          tw = measure_text_cached(gui_app.font(FontWeight.MEDIUM), "SERVIDOR", 26, 3)
          dot_x, dot_y = card.x + CARD_PAD + tw.x + 22, card.y + 28 + tw.y / 2
          blink = 0.35 + 0.65 * fx.pulse01(now, 1.6)
          fx.draw_glow_circle(dot_x, dot_y, 9, PULSE, 0.5 * blink * a)
          rl.draw_circle(int(dot_x), int(dot_y), 6, fx.col(PULSE, blink * a))

  def _render_info_cards(self, rect: rl.Rectangle):
    # Second strip: system / updater / network info (all cached, refreshed slowly).
    sys_detail = f"commit {self._sys_commit}" if self._sys_commit else "commit --"

    if self.update_available:
      upd_val, upd_color = "DISPONIBLE", COMMANDS
      upd_detail = self._upd_desc or "nueva versión lista"
      upd_sub = "toca para instalar"
    elif self._upd_state not in ("", "idle"):
      upd_val, upd_color = self._upd_state, PULSE
      upd_detail = "actualizador trabajando"
      upd_sub = f"comprobado {self._upd_checked}" if self._upd_checked else "aún sin comprobar"
    else:
      upd_val, upd_color = "Al día", MUTED
      upd_detail = "sin novedades"
      upd_sub = f"comprobado {self._upd_checked}" if self._upd_checked else "aún sin comprobar"

    if self._net_ssid:
      net_detail = f"WiFi: {self._net_ssid}"
    elif self._net_type != "--":
      net_detail = f"vía {self._net_type}"
    else:
      net_detail = "tipo desconocido"

    cards = [
      ("system", "SISTEMA", self._sys_branch or "--", INK, sys_detail, self._version_text, False),
      ("update", "ACTUALIZACION", upd_val, upd_color, upd_detail, upd_sub, self.update_available),
      ("net", "RED", self._net_ip or "--", INK, net_detail, "IP local", False),
    ]

    now = time.monotonic()
    ts = now - self._shown_at - CASCADE_START
    n = len(cards)
    cw = (rect.width - CARD_GAP * (n - 1)) / n
    for i, (key, title, value, color, detail, sub, tappable) in enumerate(cards):
      a, dy, _scale = self._cascade.values(ts, 3 + i)   # continues after the status row
      card = rl.Rectangle(rect.x + i * (cw + CARD_GAP), rect.y + dy, cw, rect.height)
      self._card_rects[key] = card
      self._draw_card(card, title, value, color, detail, sub, tappable, value_size=42,
                      value_y=rect.height * 0.28, chevron_color=color, alpha=a)

  def _draw_card(self, card: rl.Rectangle, title: str, value: str, color: rl.Color,
                 detail: str, sub: str, tappable: bool, value_size: int, value_y: float,
                 chevron_color: rl.Color, alpha: float = 1.0, border: rl.Color | None = None):
    hdr_font = gui_app.font(FontWeight.MEDIUM)
    val_font = gui_app.font(FontWeight.BOLD)
    sub_font = gui_app.font(FontWeight.NORMAL)

    border = border if border is not None else (chevron_color if tappable else HAIRLINE)
    fx.draw_card(card, border=border, alpha=alpha)

    max_w = card.width - 2 * CARD_PAD
    rl.draw_text_ex(hdr_font, title, rl.Vector2(int(card.x + CARD_PAD), int(card.y + 28)), 26, 3,
                    fx.col(MUTED, alpha))
    if tappable:
      self._draw_chevron(card.x + card.width - CARD_PAD - 16, card.y + card.height / 2, 20,
                         fx.col(chevron_color, alpha))

    # El valor nunca se corta: primero encoge la fuente (hasta 32px) y solo si
    # aun asi no cabe, elipsa. Los valores son strings estables (no crece la cache).
    max_w_value = max_w - (36 if tappable else 0)
    while value_size > 32 and measure_text_cached(val_font, value, value_size).x > max_w_value:
      value_size -= 2
    value = self._ellipsize(val_font, value, value_size, max_w_value)
    vy = card.y + value_y
    rl.draw_text_ex(val_font, value, rl.Vector2(int(card.x + CARD_PAD), int(vy)), value_size, 0,
                    fx.col(color, alpha))

    if detail:
      detail = self._ellipsize(sub_font, detail, 26, max_w - (36 if tappable else 0))
      rl.draw_text_ex(sub_font, detail, rl.Vector2(int(card.x + CARD_PAD), int(vy + value_size + 18)),
                      26, 0, fx.col(MUTED, alpha))

    if sub:
      sub = self._ellipsize(sub_font, sub, 24, max_w)
      rl.draw_text_ex(sub_font, sub, rl.Vector2(int(card.x + CARD_PAD), int(card.y + card.height - 52)),
                      24, 0, fx.col(MUTED_DIM, alpha))

  def _render_telemetry_wave(self, card: rl.Rectangle, alpha: float = 1.0):
    # Live-uplink pulse along the top-right of the SERVIDOR card: an animated
    # ECG-style polyline while OrbitConnected, a flat muted line otherwise.
    x1 = card.x + card.width - CARD_PAD
    x0 = x1 - 300
    base_y = card.y + 48
    if x1 - x0 < 80:
      return

    if not self._connected:
      rl.draw_line_ex(rl.Vector2(x0, base_y), rl.Vector2(x1, base_y), 3, fx.col(MUTED_DIM, alpha))
      return

    phase = (time.monotonic() * WAVE_SPEED) % WAVE_PERIOD
    step = 5.0
    prev = None
    x = x0
    while x <= x1:
      u = ((x - x0 + phase) % WAVE_PERIOD) / WAVE_PERIOD
      pt = rl.Vector2(x, base_y + WAVE_AMP * self._wave_offset(u))
      if prev is not None:
        rl.draw_line_ex(prev, pt, 3, fx.col(PULSE, alpha))
      prev = pt
      x += step

  def _wave_offset(self, u: float) -> float:
    for (u0, v0), (u1, v1) in zip(WAVE_BEAT, WAVE_BEAT[1:], strict=False):
      if u <= u1:
        t = (u - u0) / (u1 - u0) if u1 > u0 else 0.0
        return v0 + (v1 - v0) * t
    return WAVE_BEAT[-1][1]

  def _last_publish_text(self) -> str:
    if self._orbit_last_publish <= 0:
      return "sin datos"
    age = max(0, int(time.time() - self._orbit_last_publish))  # noqa: TID251 (OrbitLastPublish is epoch seconds)
    return f"último dato hace {age}s"

  def _draw_chevron(self, x: float, y: float, size: float, color: rl.Color):
    # A ">" affordance meaning "tap to open".
    half = size / 2
    rl.draw_line_ex(rl.Vector2(x, y - half), rl.Vector2(x + half, y), 4, color)
    rl.draw_line_ex(rl.Vector2(x + half, y), rl.Vector2(x, y + half), 4, color)

  def _ellipsize(self, font, text: str, size: int, max_w: float) -> str:
    # Fast path (no measurement): avoids growing the measure cache with strings
    # that change every second (e.g. "último dato hace Ns") and clearly fit.
    if len(text) * size * FONT_SCALE * 0.75 <= max_w:
      return text
    if measure_text_cached(font, text, size).x <= max_w:
      return text
    while text and measure_text_cached(font, text + "...", size).x > max_w:
      text = text[:-1]
    return text + "..."

  def _open_enroll(self):
    from openpilot.selfdrive.ui.widgets.orbit_enroll_dialog import OrbitEnrollDialog
    gui_app.push_widget(OrbitEnrollDialog())

  def _open_server_settings(self):
    gui_app.push_widget(_ServerSettingsModal())

  def _render_link_pill(self, right_x: float, y: float) -> float:
    """Draw the link pill right-aligned at right_x; returns its width."""
    font = gui_app.font(FontWeight.MEDIUM)
    claimed = self._claimed

    if claimed:
      text = f"VINCULADO - {self._owner}" if self._owner else "VINCULADO A ORBIT"
      if self._owner_role:
        text += f" ({self._owner_role})"
      accent = COMMANDS
    else:
      text = "SIN VINCULAR"
      accent = MUTED

    text_size = measure_text_cached(font, text, PILL_FONT_SIZE)
    dot_r = 8
    inner_pad = 32
    dot_gap = 16
    pill_w = inner_pad * 2 + dot_r * 2 + dot_gap + text_size.x

    # Slide in from the right + fade during the home entrance
    ts = time.monotonic() - self._shown_at
    e = fx.ease_out_cubic((ts - 0.1) / 0.4)
    pill_rect = rl.Rectangle(right_x - pill_w + (1.0 - e) * 30.0, y, pill_w, PILL_HEIGHT)
    self._pill_rect = pill_rect

    rl.draw_rectangle_rounded(pill_rect, 1.0, 20, fx.col(PANEL, e))
    rl.draw_rectangle_rounded_lines_ex(pill_rect, 1.0, 20, 2,
                                       fx.col(accent if claimed else HAIRLINE, e))

    # Status dot (breathing glow ring while linked)
    dot_x = pill_rect.x + inner_pad + dot_r
    dot_y = pill_rect.y + PILL_HEIGHT / 2
    if claimed:
      fx.draw_glow_circle(dot_x, dot_y, dot_r + 3, COMMANDS,
                          (0.35 + 0.35 * fx.pulse01(ts, 2.6)) * e)
    rl.draw_circle(int(dot_x), int(dot_y), dot_r, fx.col(accent, e))

    text_x = dot_x + dot_r + dot_gap
    text_y = pill_rect.y + (PILL_HEIGHT - text_size.y) / 2
    rl.draw_text_ex(font, text, rl.Vector2(int(text_x), int(text_y)), PILL_FONT_SIZE, 0,
                    fx.col(accent, e))
    return pill_w

  def _render_powered_by(self):
    # Bottom-right corner credit: "powered by drago" + the green dragon mark.
    if self._drago is None:
      return

    a = fx.ease_out_cubic((time.monotonic() - self._shown_at - 0.6) / 0.4)

    tex = self._drago
    # Pin to the true bottom-right corner of the screen (self._rect), not the
    # content inset, so it sits lower and further right.
    margin = 16
    x_right = self._rect.x + self._rect.width - margin
    y_bottom = self._rect.y + self._rect.height - margin

    dragon_x = x_right - tex.width
    dragon_y = y_bottom - tex.height
    rl.draw_texture_ex(tex, rl.Vector2(int(dragon_x), int(dragon_y)), 0.0, 1.0,
                       rl.Color(255, 255, 255, int(255 * fx.clamp01(a))))

    # "powered by " (muted) + "DRAGO" (green, matching the dragon), right-aligned
    # to the left of the mark and vertically centered on it.
    font = gui_app.font(FontWeight.MEDIUM)
    part1, part2 = "powered by ", "DRAGO"
    s1 = measure_text_cached(font, part1, POWERED_SIZE)
    s2 = measure_text_cached(font, part2, POWERED_SIZE)
    text_x = dragon_x - 16 - (s1.x + s2.x)
    text_y = dragon_y + (tex.height - max(s1.y, s2.y)) / 2
    rl.draw_text_ex(font, part1, rl.Vector2(int(text_x), int(text_y)), POWERED_SIZE, 0, fx.col(MUTED, a))
    rl.draw_text_ex(font, part2, rl.Vector2(int(text_x + s1.x), int(text_y)), POWERED_SIZE, 0, fx.col(COMMANDS, a))

    # Zona pulsable del badge (tap -> pantalla sobre drago), con margen extra.
    pad = 18
    self._drago_rect = rl.Rectangle(text_x - pad, dragon_y - pad,
                                    x_right - text_x + 2 * pad, tex.height + 2 * pad)

  # ---------------------------------------------------------------------------
  # Cached data refresh
  # ---------------------------------------------------------------------------
  def _fast_refresh(self):
    # Orbit link state changes on user action -> refresh every couple of seconds.
    try:
      self._claimed = bool(self.params.get_bool("OrbitClaimed"))
      self._owner = self.params.get("OrbitOwner") or ""
      self._connected = bool(self.params.get_bool("OrbitConnected"))
      self._dongle = self.params.get("DongleId") or ""
      self._orbit_last_publish = float(self.params.get("OrbitLastPublish") or 0)
      self._owner_role = rol_etiqueta(self.params.get(PARAM_OWNER_ROLE)) if self._claimed else ""
    except Exception:
      pass

  def _refresh(self):
    self._version_text = self._get_version_text()
    update_available = self.update_alert.refresh()
    alert_count = self.offroad_alert.refresh()
    alerts_present = alert_count > 0

    # Show panels on transition from no alert/update to any alerts/update
    if not update_available and not alerts_present:
      self._set_state(HomeLayoutState.HOME)
    elif update_available and ((not self._prev_update_available) or (not alerts_present and self.current_state == HomeLayoutState.ALERTS)):
      self._set_state(HomeLayoutState.UPDATE)
    elif alerts_present and ((not self._prev_alerts_present) or (not update_available and self.current_state == HomeLayoutState.UPDATE)):
      self._set_state(HomeLayoutState.ALERTS)

    self.update_available = update_available
    self.alert_count = alert_count
    self._prev_update_available = update_available
    self._prev_alerts_present = alerts_present

    # System / updater info
    try:
      self._sys_branch = self.params.get("GitBranch") or ""
      self._sys_commit = (self.params.get("GitCommit") or "")[:7]
      self._upd_state = self.params.get("UpdaterState") or "idle"
      self._upd_desc = self.params.get("UpdaterNewDescription") or ""
      self._upd_checked = self._time_ago(self.params.get("LastUpdateTime"))
    except Exception:
      pass

    # Network info: type from deviceState (already subscribed by ui_state), IP locally.
    try:
      self._net_type = NET_TYPE_NAMES.get(ui_state.sm["deviceState"].networkType.raw, "--")
    except Exception:
      self._net_type = "--"
    self._net_ip = self._local_ip()
    # SSID is not in deviceState; read it from NetworkManager in the background.
    self._refresh_ssid()

    # Orbit broker address (read from config_mqtt.json, cheap at this cadence)
    try:
      ip, port = read_broker()
      self._broker_addr = f"{ip}:{port}" if ip else ""
    except Exception:
      self._broker_addr = ""

  def _refresh_ssid(self) -> None:
    # nmcli blocks; run it off the render thread and cache the result. One at a time.
    if self._ssid_inflight:
      return
    self._ssid_inflight = True

    def worker():
      ssid = ""
      try:
        # Force C locale so the ACTIVE column is "yes"/"no" regardless of system language.
        out = subprocess.run(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
                             capture_output=True, text=True, timeout=3,
                             env={**os.environ, "LC_ALL": "C", "LANG": "C"}).stdout
        for line in out.splitlines():
          active, _, name = line.partition(":")
          if active == "yes":
            ssid = name.strip()
            break
      except Exception:
        ssid = ""
      finally:
        self._net_ssid = ssid
        self._ssid_inflight = False

    threading.Thread(target=worker, daemon=True).start()

  def _local_ip(self) -> str:
    # UDP connect() does not send packets; it just picks the outbound interface.
    try:
      s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
      try:
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
      finally:
        s.close()
    except Exception:
      return ""

  def _time_ago(self, date) -> str | None:
    if not isinstance(date, datetime.datetime):
      return None
    if date.tzinfo is None:
      date = date.replace(tzinfo=datetime.UTC)
    diff = int((datetime.datetime.now(datetime.UTC) - date).total_seconds())
    if diff < 60:
      return "ahora"
    if diff < 3600:
      return f"hace {diff // 60} min"
    if diff < 86400:
      return f"hace {diff // 3600} h"
    return f"hace {diff // 86400} d"

  def _get_version_text(self) -> str:
    try:
      version = (self.params.get("Version") or "").split("-")[0]
    except Exception:
      version = ""
    return f"ORBIT {version}".strip()
