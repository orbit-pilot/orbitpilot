import datetime
import time

from cereal import log
import pyray as rl
from collections.abc import Callable
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.layouts import HBoxLayout
from openpilot.system.ui.widgets.icon_widget import IconWidget
from openpilot.system.ui.widgets.label import UnifiedLabel, gui_label
from openpilot.system.ui.lib.application import gui_app, FontWeight, MousePos
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui import orbit_theme as t
from openpilot.selfdrive.ui.widgets import orbit_duplex as duplex
from openpilot.selfdrive.ui.widgets.orbit_mando import PARAM_OWNER, PARAM_OWNER_ROLE, rol_etiqueta
from openpilot.system.version import RELEASE_BRANCHES

HEAD_BUTTON_FONT_SIZE = 40
HOME_PADDING = 8
ALERTS_ZONE_WIDTH = 180
ORBIT_REFRESH_S = 2.0  # mismo ritmo que la home grande (OrbitClaimed/OrbitConnected/OrbitOwner)

NetworkType = log.DeviceState.NetworkType

NETWORK_TYPES = {
  NetworkType.none: "Offline",
  NetworkType.wifi: "WiFi",
  NetworkType.cell2G: "2G",
  NetworkType.cell3G: "3G",
  NetworkType.cell4G: "LTE",
  NetworkType.cell5G: "5G",
  NetworkType.ethernet: "Ethernet",
}


class AlertsPill(Widget):
  ICON_OFFSET = 12
  COUNT_OFFSET = 40

  def __init__(self):
    super().__init__()
    self.set_rect(rl.Rectangle(0, 0, 104, 52))

    self._pill_bg_txt = gui_app.texture("icons_mici/alerts_pill.png", 104, 52)
    self._icon_red = gui_app.texture("icons_mici/offroad_alerts/red_warning.png", 36, 36)
    self._icon_orange = gui_app.texture("icons_mici/offroad_alerts/orange_warning.png", 36, 36)
    self._icon_green = gui_app.texture("icons_mici/offroad_alerts/green_wheel.png", 36, 36)
    self._alert_count_callback: Callable[[], int] | None = None
    self._max_severity_callback: Callable[[], int | None] | None = None

  def set_alert_count_callback(self, callback: Callable[[], int] | None,
                               severity_callback: Callable[[], int | None] | None = None):
    self._alert_count_callback = callback
    self._max_severity_callback = severity_callback

  def _render(self, _):
    alert_count = self._alert_count_callback() if self._alert_count_callback else 0
    if alert_count > 0:
      pill_w, pill_h = self._pill_bg_txt.width, self._pill_bg_txt.height
      rl.draw_texture_ex(self._pill_bg_txt, rl.Vector2(self.rect.x, self.rect.y), 0.0, 1.0, rl.WHITE)

      severity = self._max_severity_callback() if self._max_severity_callback else None
      if severity == -1:
        warning_txt = self._icon_green
      elif severity is not None and severity > 0:
        warning_txt = self._icon_red
      else:
        warning_txt = self._icon_orange

      warn_x = self.rect.x + self.ICON_OFFSET
      warn_y = self.rect.y + (pill_h - warning_txt.height) / 2
      rl.draw_texture_ex(warning_txt, rl.Vector2(warn_x, warn_y), 0.0, 1.0, rl.WHITE)

      count_rect = rl.Rectangle(self.rect.x + self.COUNT_OFFSET, self.rect.y, pill_w - self.COUNT_OFFSET, pill_h)
      gui_label(count_rect, str(alert_count), font_size=36,
                alignment=rl.GuiTextAlignment.TEXT_ALIGN_CENTER,
                alignment_vertical=rl.GuiTextAlignmentVertical.TEXT_ALIGN_MIDDLE)


class NetworkIcon(Widget):
  def __init__(self):
    super().__init__()
    self.set_rect(rl.Rectangle(0, 0, 54, 44))  # max size of all icons
    self._net_type = NetworkType.none
    self._net_strength = 0

    self._wifi_slash_txt = gui_app.texture("icons_mici/settings/network/wifi_strength_slash.png", 50, 44)
    self._wifi_none_txt = gui_app.texture("icons_mici/settings/network/wifi_strength_none.png", 50, 37)
    self._wifi_low_txt = gui_app.texture("icons_mici/settings/network/wifi_strength_low.png", 50, 37)
    self._wifi_medium_txt = gui_app.texture("icons_mici/settings/network/wifi_strength_medium.png", 50, 37)
    self._wifi_full_txt = gui_app.texture("icons_mici/settings/network/wifi_strength_full.png", 50, 37)

    self._cell_none_txt = gui_app.texture("icons_mici/settings/network/cell_strength_none.png", 54, 36)
    self._cell_low_txt = gui_app.texture("icons_mici/settings/network/cell_strength_low.png", 54, 36)
    self._cell_medium_txt = gui_app.texture("icons_mici/settings/network/cell_strength_medium.png", 54, 36)
    self._cell_high_txt = gui_app.texture("icons_mici/settings/network/cell_strength_high.png", 54, 36)
    self._cell_full_txt = gui_app.texture("icons_mici/settings/network/cell_strength_full.png", 54, 36)

  def _update_state(self):
    device_state = ui_state.sm['deviceState']
    self._net_type = device_state.networkType
    strength = device_state.networkStrength
    self._net_strength = max(0, min(5, strength.raw + 1)) if strength.raw > 0 else 0

  def _render(self, _):
    if self._net_type == NetworkType.wifi:
      # There is no 1
      draw_net_txt = {0: self._wifi_none_txt,
                      2: self._wifi_low_txt,
                      3: self._wifi_medium_txt,
                      4: self._wifi_full_txt,
                      5: self._wifi_full_txt}.get(self._net_strength, self._wifi_low_txt)
    elif self._net_type in (NetworkType.cell2G, NetworkType.cell3G, NetworkType.cell4G, NetworkType.cell5G):
      draw_net_txt = {0: self._cell_none_txt,
                      2: self._cell_low_txt,
                      3: self._cell_medium_txt,
                      4: self._cell_high_txt,
                      5: self._cell_full_txt}.get(self._net_strength, self._cell_none_txt)
    else:
      draw_net_txt = self._wifi_slash_txt

    draw_x = self._rect.x + (self._rect.width - draw_net_txt.width) / 2
    draw_y = self._rect.y + (self._rect.height - draw_net_txt.height) / 2

    if draw_net_txt == self._wifi_slash_txt:
      # Offset by difference in height between slashless and slash icons to make center align match
      draw_y -= (self._wifi_slash_txt.height - self._wifi_none_txt.height) / 2

    rl.draw_texture_ex(draw_net_txt, rl.Vector2(draw_x, draw_y), 0.0, 1.0, rl.Color(255, 255, 255, int(255 * 0.9)))


class OrbitStatusIcon(Widget):
  """Estado ORBIT en el pie: anillo duplex (subida encendida = conectado) y punto de estado:
  PULSO conectado, AVISO vinculado pero sin enlace, TEXTO3 sin vincular. Quieto: offroad
  solo se mueven splash, satelite de la home grande e iconos de seccion."""

  def __init__(self):
    super().__init__()
    self.set_rect(rl.Rectangle(0, 0, 48, 48))
    self.set_enabled(False)  # el toque es de la home (abre ajustes)
    self.claimed = False
    self.connected = False

  def _render(self, _):
    cx, cy = self._rect.x + 24, self._rect.y + 24
    # Sin vincular manda: un equipo sin dueno tambien conecta al broker (para anunciarse)
    duplex.draw_duplex_ring(cx, cy, 19, subida_activa=self.claimed and self.connected)
    color = t.TEXTO3 if not self.claimed else (t.PULSO if self.connected else t.AVISO)
    rl.draw_circle(int(cx + 16), int(cy + 16), 8, rl.BLACK)
    rl.draw_circle(int(cx + 16), int(cy + 16), 6, color)


class MiciHomeLayout(Widget):
  def __init__(self):
    super().__init__()
    self._on_settings_click: Callable | None = None
    self._on_alerts_click: Callable | None = None
    self._alert_count_callback: Callable[[], int] | None = None

    self._mouse_down_t: None | float = None
    self._did_long_press = False
    self._is_pressed_prev = False

    self._version_text = self._get_version_text()

    self._experimental_icon = IconWidget("icons_mici/experimental_mode.png", (48, 48))
    self._egpu_icon = IconWidget("icons_mici/egpu.png", (50, 37))
    self._egpu_icon_gray = IconWidget("icons_mici/egpu_gray.png", (50, 37))
    self._mic_icon = IconWidget("icons_mici/microphone.png", (32, 46))
    self._body_icon = IconWidget("icons_mici/body.png", (54, 37))

    self._alerts_pill = AlertsPill()
    self._orbit_icon = OrbitStatusIcon()
    self._orbit_next_refresh = 0.0

    self._status_bar_layout = HBoxLayout([
      IconWidget("icons_mici/settings.png", (48, 48), opacity=0.9),
      NetworkIcon(),
      self._orbit_icon,
      self._experimental_icon,
      self._egpu_icon,
      self._egpu_icon_gray,
      self._body_icon,
      self._mic_icon,
    ], spacing=18)

    self._openpilot_label = UnifiedLabel("ORBIT", font_size=96, font_weight=FontWeight.DISPLAY, max_width=480, wrap_text=False)
    self._version_label = UnifiedLabel("", font_size=36, font_weight=FontWeight.ROMAN, max_width=480, wrap_text=False)
    self._large_version_label = UnifiedLabel("", font_size=64, text_color=rl.GRAY, font_weight=FontWeight.ROMAN, max_width=480, wrap_text=False)
    self._date_label = UnifiedLabel("", font_size=36, text_color=rl.GRAY, font_weight=FontWeight.ROMAN, max_width=480, wrap_text=False)
    self._branch_label = UnifiedLabel("", font_size=36, text_color=rl.GRAY, font_weight=FontWeight.ROMAN, scroll=True)
    self._version_commit_label = UnifiedLabel("", font_size=36, text_color=rl.GRAY, font_weight=FontWeight.ROMAN, max_width=480, wrap_text=False)
    # Dueno ORBIT y su rol, a la derecha del wordmark
    self._owner_label = UnifiedLabel("", font_size=32, font_weight=FontWeight.MEDIUM, max_width=220, wrap_text=False)
    self._role_label = UnifiedLabel("", font_size=28, text_color=rl.GRAY, font_weight=FontWeight.ROMAN, max_width=220, wrap_text=False)

  def _update_state(self):
    if self.is_pressed and not self._is_pressed_prev:
      self._mouse_down_t = time.monotonic()
    elif not self.is_pressed and self._is_pressed_prev:
      self._mouse_down_t = None
      self._did_long_press = False
    self._is_pressed_prev = self.is_pressed

    if self._mouse_down_t is not None:
      if time.monotonic() - self._mouse_down_t > 0.5:
        # long gating for experimental mode - only allow toggle if longitudinal control is available
        if ui_state.has_longitudinal_control:
          ui_state.experimental_mode = not ui_state.experimental_mode
          ui_state.params.put("ExperimentalMode", ui_state.experimental_mode, block=True)
        self._mouse_down_t = None
        self._did_long_press = True

    now = time.monotonic()
    if now >= self._orbit_next_refresh:
      self._orbit_next_refresh = now + ORBIT_REFRESH_S
      self._refresh_orbit()

  def _refresh_orbit(self):
    p = ui_state.params
    owner = role = ""
    try:
      self._orbit_icon.claimed = bool(p.get_bool("OrbitClaimed"))
      self._orbit_icon.connected = bool(p.get_bool("OrbitConnected"))
      if self._orbit_icon.claimed:
        owner = p.get(PARAM_OWNER) or ""
        role = rol_etiqueta(p.get(PARAM_OWNER_ROLE))
    except Exception:
      pass
    self._owner_label.set_text(owner or ("vinculado" if self._orbit_icon.claimed else "sin vincular"))
    self._role_label.set_text(role)

  def set_callbacks(self, on_settings: Callable | None = None, on_alerts: Callable | None = None,
                    alert_count_callback: Callable[[], int] | None = None,
                    max_severity_callback: Callable[[], int | None] | None = None):
    self._on_settings_click = on_settings
    self._on_alerts_click = on_alerts
    self._alert_count_callback = alert_count_callback
    self._alerts_pill.set_alert_count_callback(alert_count_callback, max_severity_callback)

  def _handle_mouse_release(self, mouse_pos: MousePos):
    if not self._did_long_press:
      relative_x = mouse_pos.x - self.rect.x
      has_alerts = self._alert_count_callback and self._alert_count_callback() > 0
      if has_alerts and relative_x > self.rect.width - ALERTS_ZONE_WIDTH:
        if self._on_alerts_click:
          self._on_alerts_click()
      elif self._on_settings_click:
        self._on_settings_click()
    self._did_long_press = False

  def _get_version_text(self) -> tuple[str, str, str, str] | None:
    version = ui_state.params.get("Version")
    branch = ui_state.params.get("GitBranch")
    commit = ui_state.params.get("GitCommit")

    if not all((version, branch, commit)):
      return None

    commit_date_raw = ui_state.params.get("GitCommitDate")
    try:
      # GitCommitDate format from get_commit_date(): '%ct %ci' e.g. "'1708012345 2024-02-15 ...'"
      unix_ts = int(commit_date_raw.strip("'").split()[0])
      date_str = datetime.datetime.fromtimestamp(unix_ts).strftime("%b %d")
    except (ValueError, IndexError, TypeError, AttributeError):
      date_str = ""

    return version, branch, commit[:7], date_str

  def _render(self, _):
    # TODO: why is there extra space here to get it to be flush?
    text_pos = rl.Vector2(self.rect.x - 2 + HOME_PADDING, self.rect.y - 16)
    self._openpilot_label.set_position(text_pos.x, text_pos.y)
    self._openpilot_label.render()

    # Dueno y rol ORBIT junto al wordmark (el estado de enlace va en el pie)
    owner_x = text_pos.x + self._openpilot_label.text_width + 24
    max_w = int(self.rect.x + self.rect.width - HOME_PADDING - owner_x)
    for label, y in ((self._owner_label, self.rect.y + 14), (self._role_label, self.rect.y + 54)):
      label.set_max_width(max_w)
      label.set_position(owner_x, y)
      label.render()

    if self._version_text is not None:
      # release branch
      release_branch = self._version_text[1] in RELEASE_BRANCHES
      version_pos = rl.Rectangle(text_pos.x, text_pos.y + self._openpilot_label.font_size + 16, 100, 44)
      self._version_label.set_text(self._version_text[0])
      self._version_label.set_position(version_pos.x, version_pos.y)
      self._version_label.render()

      self._date_label.set_text(" " + self._version_text[3])
      self._date_label.set_position(version_pos.x + self._version_label.text_width + 10, version_pos.y)
      self._date_label.render()

      self._branch_label.set_max_width(gui_app.width - self._version_label.text_width - self._date_label.text_width - 32)
      self._branch_label.set_text(" " + ("release" if release_branch else self._version_text[1]))
      self._branch_label.set_position(version_pos.x + self._version_label.text_width + self._date_label.text_width + 20, version_pos.y)
      self._branch_label.render()

      if not release_branch:
        # 2nd line
        self._version_commit_label.set_text(self._version_text[2])
        self._version_commit_label.set_position(version_pos.x, version_pos.y + self._date_label.font_size + 7)
        self._version_commit_label.render()

    # ***** Center-aligned bottom section icons *****
    self._experimental_icon.set_visible(ui_state.experimental_mode)
    self._egpu_icon.set_visible(ui_state.usbgpu and ui_state.usbgpu_compiled)
    self._egpu_icon_gray.set_visible(ui_state.usbgpu and not ui_state.usbgpu_compiled)
    self._mic_icon.set_visible(ui_state.recording_audio)
    self._body_icon.set_visible(ui_state.is_body)

    footer_rect = rl.Rectangle(self.rect.x + HOME_PADDING, self.rect.y + self.rect.height - 48, self.rect.width - HOME_PADDING, 48)
    self._status_bar_layout.render(footer_rect)

    # TODO: add alignment to hboxlayout and add to there
    self._alerts_pill.set_position(self.rect.x + self.rect.width - self._alerts_pill.rect.width - HOME_PADDING,
                                   self.rect.y + self.rect.height - self._alerts_pill.rect.height)
    self._alerts_pill.render()
