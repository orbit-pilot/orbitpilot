"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

OrbitLayout - panel ORBIT de los ajustes.

Reparto de interfaz de la seccion 9 del diseno: aqui SOLO queda lo que necesita ojos en
el coche o lo que tiene que funcionar con la red caida.

  * estado del MANDO REMOTO (modo, salud del enlace, ultimo comando y su resultado),
  * ARMADO DE BANCO con confirmacion, TTL de 300 s y cuenta atras,
  * DESARMAR TODO, anclado abajo y fuera del scroll: es el unico control que nunca
    se bloquea y nunca puede quedar fuera de alcance,
  * selector de modo de volante con confirmacion,
  * interruptor maestro LOCAL de privacidad,
  * QR de enrolamiento e IP del broker,
  * submenu Telemetria: que grupos de datos manda el dispositivo a la app,
  * "Restablecer valores seguros".

Se fue a la app: la configuracion de camara. Los canales de telemetria sueltos (8 toggles)
tambien se fueron; vuelven agrupados (posicion, vehiculo...) en el submenu Telemetria.
Se borro: la seccion PRUEBAS (modo_debug) junto con el overlay que consumia modo_debug.

EL ESTADO DEL MANDO SE LEE DE CEREAL, NO DE PARAMS. `ui_state.orbit_command` es la
vista del mensaje `orbitCommandState` (10 Hz) que refresca UIStateSP en cada frame. Los
unicos Params que se leen aqui son los de la cuenta atras del armado, y a 1 Hz.
"""
import time
from enum import IntEnum

import pyray as rl

from openpilot.selfdrive.ui.layouts.settings import settings as OP
from openpilot.selfdrive.ui import orbit_theme as t
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui.widgets import orbit_mando as mando
from openpilot.selfdrive.ui.widgets import orbit_ajustes as ajustes
from openpilot.selfdrive.ui.widgets.orbit_enroll_dialog import OrbitEnrollDialog
from openpilot.selfdrive.ui.widgets.orbit_section import SectionHeaderSP
from openpilot.selfdrive.ui.widgets.orbit_server import ServerMonitor
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.sunnypilot.widgets.list_view import toggle_item_sp, button_item_sp
from openpilot.system.ui.widgets import Widget, DialogResult
from openpilot.system.ui.widgets.button import Button, ButtonStyle
from openpilot.system.ui.widgets.confirm_dialog import ConfirmDialog, alert_dialog
from openpilot.system.ui.widgets.scroller_tici import Scroller
from openpilot.selfdrive.ui.sunnypilot.layouts.settings.orbit_sub_layouts.advanced_settings import AdvancedSettingsLayout
from openpilot.selfdrive.ui.sunnypilot.layouts.settings.orbit_sub_layouts.server_settings import ServerRows
from openpilot.selfdrive.ui.sunnypilot.layouts.settings.orbit_sub_layouts.steer_mode import SteerModeRows
from openpilot.selfdrive.ui.sunnypilot.layouts.settings.orbit_sub_layouts.telemetry_settings import TelemetrySettingsLayout

_REFRESH_SECONDS = 1.0

# Barra anclada de DESARMAR TODO.
_DISARM_BAR_HEIGHT = 132
_DISARM_BAR_GAP = 20
_DISARM_FEEDBACK_S = 2.5

# Tinta de error/aviso: solo texto (nunca relleno), tokens de orbit_theme.
_RED = t.PELIGRO
_AMBER = t.AVISO


def _a_salvo(etiqueta: str, avisar: bool = False):
  """Decorador: la funcion NUNCA deja salir una excepcion al bucle de la UI.

  POR QUE. El bucle de render (system/ui/lib/application.py, `render`) no envuelve
  `widget.render` en ningun try, y los callbacks de los botones se ejecutan dentro de
  ese mismo render. Cualquier excepcion en un `_render`, en un refresco o en un callback
  mata el proceso `ui` ENTERO; manager lo resucita (restart_if_crash) y el usuario ve el
  splash de ORBIT como si el comma se hubiera reiniciado. Y como el armado de banco vive
  en Params y sobrevive al reinicio de la UI, un fallo que dependa del estado "armado"
  vuelve a matarla en cuanto se abre este panel: bucle de reinicios. Es exactamente lo
  que paso al pulsar ARMAR con una referencia a una constante que ya no existia
  (mando.ARM_REASON_STEER) dentro de `_refresh_status`.

  Con `avisar=True` (acciones del usuario: armar, desarmar, restablecer) ademas se lo
  cuenta con un dialogo: una accion que falla muda es peor que una que no existe. Los
  refrescos de pintado solo lo dejan en el log (van a 1 Hz, no inundan).
  """
  return ajustes.a_salvo(etiqueta, _avisar_fallo if avisar else None)


def _avisar_fallo(etiqueta: str) -> None:
  gui_app.push_widget(alert_dialog(tr("Fallo interno en:") + f" {etiqueta}\n" +
                                   tr("La accion no se ha completado. Revisa el log de la UI.")))


# Tinta de cada tono de ajustes.lineas_mando.
_TONOS = {"ok": OP.ORBIT_GREEN, "aviso": _AMBER, "peligro": _RED, "normal": OP.ORBIT_INK}


def ayuda_html() -> str:
  """Definiciones de la terminologia que menos se explica sola (HTML <h2>/<p>).

  Funcion de modulo: la usa este panel y la pagina de ayuda del comma 4
  (sunnypilot/mici/layouts/orbit.py), asi que las dos pantallas dicen lo mismo.
  """
  return (
    "<h2>" + tr("ARMAR (modo banco)") + "</h2><p>" +
    tr("Habilita durante 5 minutos los verbos de control fisico (torque del volante, " +
       "pulso de direccion, control directo) para que se ejecuten DE FORMA REMOTA " +
       "desde la app. Se arma solo desde esta pantalla, con el coche parado y alguien " +
       "delante. Se desarma solo al agotarse el tiempo, al superar los 5 km/h o al " +
       "pasar a offroad.") + "</p>" +
    "<h2>" + tr("DESARMAR TODO") + "</h2><p>" +
    tr("Cancela cualquier orden en curso y devuelve el control. Es la unica accion " +
       "que funciona siempre, desde cualquier pantalla y aunque la conexion vaya mal.") + "</p>" +
    "<h2>" + tr("MODO (observador / copiloto / maniobra)") + "</h2><p>" +
    tr("El nivel de autoridad que la app tiene sobre el coche. El coche arranca " +
       "siempre en observador; subir el modo desde la app solo dura un rato " +
       "(copiloto 15 min, maniobra 2 min).") + "</p>" +
    "<h2>" + tr("ENLACE") + "</h2><p>" +
    tr("Salud de la conexion de mando con el servidor. Si esta caido, las ordenes " +
       "remotas no llegan.") + "</p>" +
    "<h2>" + tr("BANCO ARMADO") + "</h2><p>" +
    tr("El modo banco esta activo y quedan los segundos que se muestran. Mientras " +
       "este armado, la app puede ejecutar verbos de control fisico.") + "</p>"
  )


class PanelType(IntEnum):
  MAIN = 0
  ADVANCED = 1
  TELEMETRY = 2


class _MandoCard(Widget):
  """La tarjeta de cabecera del panel: identidad ORBIT + estado del MANDO REMOTO.

  Absorbe el hero anterior (una tarjeta de 312 px que solo llevaba logo, wordmark y
  chips) porque el panel tenia ~2.950 px de scroll y la seccion 9 pide adelgazarlo. La
  informacion no se pierde: los chips SERVIDOR / ENLACE / CUENTA siguen aqui, en la
  franja superior, y debajo van las tres lineas que exige el punto 1 del encargo:

    MODO            modo vigente del mando (y el chip de BANCO ARMADO con su cuenta atras)
    ENLACE          salud del enlace de mando
    ULTIMO COMANDO  verbo + fase del ACK + motivo

  Los tres salen del mensaje cereal `orbitCommandState` a traves de
  `ui_state.orbit_command`, que UIStateSP refresca en cada frame. Si el plano no publica
  NO se pinta verde ni se conserva el ultimo valor conocido: se pinta "SUBSISTEMA DE
  MANDO CAIDO". Un panel de mando que miente sobre el estado del mando es peor que no
  tenerlo.

  Lo que se lee de Params (chips y cuenta atras) esta limitado a _REFRESH_SECONDS; por
  frame no se lee nada.
  """

  HEIGHT = 300

  def __init__(self, monitor: ServerMonitor):
    super().__init__()
    self._rect = rl.Rectangle(0, 0, 0, self.HEIGHT)
    self._monitor = monitor
    self._font_bold = gui_app.font(FontWeight.BOLD)
    self._font = gui_app.font(FontWeight.NORMAL)
    try:
      self._logo = gui_app.texture("img_orbit_logo.png", 56, 56)
    except Exception:
      self._logo = None

    self._last_refresh = 0.0
    self._chips: list[tuple[str, bool]] = []
    self._bench_restante = 0.0

  def set_parent_rect(self, parent_rect: rl.Rectangle) -> None:
    super().set_parent_rect(parent_rect)
    self._rect.width = parent_rect.width

  @_a_salvo("refresco de la tarjeta de mando")
  def _refresh(self):
    now = time.monotonic()
    if now - self._last_refresh < _REFRESH_SECONDS:
      return
    self._last_refresh = now

    # Cuenta atras del armado: se refresca a 1 Hz, que es justo lo que necesita un
    # contador en segundos, y no en cada frame (serian 60 lecturas de Params).
    _, self._bench_restante = mando.bench_snapshot()

    params = ui_state.params
    connected = ajustes.enlace_orbit(params)
    # Dueno + rol ("ADRIAN • SUPERADMIN"): el rol lo escribe mqtt_comandos desde el
    # enroll_ack del backend (contrato C2).
    cuenta = ajustes.etiqueta_cuenta(params.get(mando.PARAM_OWNER), params.get(mando.PARAM_OWNER_ROLE)).upper()

    server_ok = self._monitor.broker_ok and self._monitor.backend_ok

    self._chips = [
      (tr("SERVIDOR"), server_ok),
      (tr("ENLACE"), connected),
      (cuenta or tr("SIN VINCULAR"), bool(cuenta)),
    ]

  # --------------------------------------------------------------------- pintado
  def _draw_identity(self, card: rl.Rectangle, pad: float) -> float:
    """Franja superior: logo + wordmark a la izquierda, chips a la derecha."""
    y = card.y + 20
    strip_h = 56
    x = card.x + pad
    if self._logo is not None:
      rl.draw_texture_pro(
        self._logo,
        rl.Rectangle(0, 0, self._logo.width, self._logo.height),
        rl.Rectangle(x, y, 56, 56),
        rl.Vector2(0, 0), 0, rl.WHITE,
      )
      x += 56 + 18

    wm = measure_text_cached(self._font_bold, "ORBIT", 36, 3)
    titulo_y = y + (strip_h - wm.y) / 2
    rl.draw_text_ex(self._font_bold, "ORBIT", rl.Vector2(x, titulo_y), 36, 3, OP.ORBIT_INK)
    # Filete corto en el color de seccion bajo el titulo (como SectionHeaderSP):
    # el borde de la tarjeta ya no lleva el color de seccion, solo esto.
    rl.draw_rectangle_rounded(rl.Rectangle(x, titulo_y + wm.y + 6, 24, 4), 1.0, 8, t.SECCION['mando'])
    x += wm.x

    chip_h = 46
    chip_y = y + (strip_h - chip_h) / 2
    right = card.x + card.width - pad
    for label, ok in reversed(self._chips):
      size = measure_text_cached(self._font_bold, label, 24, 1)
      chip_w = size.x + chip_h + 30
      if right - chip_w < x + 30:
        break
      chip = rl.Rectangle(right - chip_w, chip_y, chip_w, chip_h)
      rl.draw_rectangle_rounded(chip, 1.0, 12, OP.ORBIT_VOID)
      rl.draw_rectangle_rounded_lines_ex(chip, 1.0, 12, 2, OP.ORBIT_HAIRLINE)
      dot = OP.ORBIT_GREEN if ok else rl.Color(OP.ORBIT_MUTED.r, OP.ORBIT_MUTED.g, OP.ORBIT_MUTED.b, 120)
      rl.draw_circle(int(chip.x + 24), int(chip_y + chip_h / 2), 7, dot)
      rl.draw_text_ex(self._font_bold, label, rl.Vector2(chip.x + 42, chip_y + (chip_h - size.y) / 2),
                      24, 1, OP.ORBIT_INK if ok else OP.ORBIT_MUTED)
      right -= chip_w + 14

    sep_y = y + strip_h + 16
    rl.draw_line_ex(rl.Vector2(card.x + pad, sep_y), rl.Vector2(card.x + card.width - pad, sep_y),
                    2, OP.ORBIT_HAIRLINE)
    return sep_y + 18

  # Columna de valores: "ULTIMO COMANDO" a 26 px mide ~300, asi que el valor arranca
  # despues o se solapa con su propia etiqueta.
  VALUE_X = 340

  def _line(self, y: float, x: float, etiqueta: str, valor: str, color: rl.Color, size: int = 34) -> None:
    rl.draw_text_ex(self._font, etiqueta, rl.Vector2(x, y + 6), 26, 1, OP.ORBIT_MUTED)
    rl.draw_text_ex(self._font_bold, valor, rl.Vector2(x + self.VALUE_X, y), size, 0, color)

  def _render(self, _):
    self._refresh()
    card = rl.Rectangle(self._rect.x, self._rect.y + 8, self._rect.width, self._rect.height - 16)
    rl.draw_rectangle_rounded(card, 0.12, 12, OP.ORBIT_NAVY)
    # El borde nunca lleva color de seccion (regla de la spec): BORDE liso.
    # El acento de "mando" queda solo en el filete corto bajo el titulo (_draw_identity).
    rl.draw_rectangle_rounded_lines_ex(card, 0.12, 12, 2, t.BORDE)

    pad = 28
    x = card.x + pad
    y = self._draw_identity(card, pad)

    st = getattr(ui_state, "orbit_command", None)
    if st is None or not st.available:
      rl.draw_text_ex(self._font_bold, tr("SUBSISTEMA DE MANDO CAIDO"), rl.Vector2(x, y), 34, 0, _RED)
      rl.draw_text_ex(self._font, tr("No se publica orbitCommandState: sin mando remoto y sin gates."),
                      rl.Vector2(x, y + 48), 26, 0, OP.ORBIT_MUTED)
      return

    # Modo / enlace / ultimo comando: el texto y el tono salen de ajustes.lineas_mando
    # (el comma 4 pinta lo mismo); aqui solo se pone la tinta y la geometria.
    (modo_et, modo_val, modo_tono), (enl_et, enl_val, enl_tono), (ult_et, texto, ult_tono) = ajustes.lineas_mando(st)

    # 1) modo vigente + chip de armado con cuenta atras
    self._line(y, x, modo_et, modo_val, _TONOS[modo_tono])
    if st.bench_armed:
      etiqueta = tr("BANCO ARMADO") + f"  {int(self._bench_restante)}s"
      size = measure_text_cached(self._font_bold, etiqueta, 24, 1)
      chip = rl.Rectangle(card.x + card.width - pad - size.x - 36, y - 2, size.x + 36, 42)
      rl.draw_rectangle_rounded(chip, 1.0, 10, _AMBER)
      rl.draw_text_ex(self._font_bold, etiqueta, rl.Vector2(chip.x + 18, chip.y + (42 - size.y) / 2),
                      24, 1, t.FONDO)

    # 2) salud del enlace de mando
    y += 62
    self._line(y, x, enl_et, enl_val, _TONOS[enl_tono])

    # 3) ultimo comando y como acabo
    y += 62
    # El motivo puede ser largo: se recorta al ancho de la tarjeta en vez de desbordarla.
    max_w = card.width - (x - card.x) - self.VALUE_X - pad
    while texto and measure_text_cached(self._font_bold, texto, 30, 0).x > max_w:
      texto = texto[:-1]
    self._line(y, x, ult_et, texto, _TONOS[ult_tono], size=30)


class OrbitLayout(Widget):
  def __init__(self):
    super().__init__()

    self._current_panel = PanelType.MAIN
    self._advanced_layout = AdvancedSettingsLayout(lambda: self._set_current_panel(PanelType.MAIN))
    self._telemetry_layout = TelemetrySettingsLayout(lambda: self._set_current_panel(PanelType.MAIN))

    self._monitor = ServerMonitor()
    self._server_rows = ServerRows()
    self._steer_rows = SteerModeRows()

    self._last_refresh = 0.0
    self._bench_status = ""
    # Cacheados en _refresh_status (1 Hz): los lambdas de las filas se evaluan en cada
    # frame y leer Params ahi es abrir ficheros 60 veces por segundo.
    self._bench_armed = False
    self._disarm_feedback_until = 0.0

    # DESARMAR TODO vive FUERA del Scroller: anclado abajo, siempre a la vista. Un boton
    # de panico al que hay que llegar haciendo scroll no es un boton de panico.
    self._disarm_button = Button(
      lambda: tr("DESARMADO") if time.monotonic() < self._disarm_feedback_until else tr("DESARMAR TODO"),
      click_callback=self._do_disarm_all,
      font_size=52,
      font_weight=FontWeight.BOLD,
      button_style=ButtonStyle.DANGER,
      border_radius=16,
    )

    items = self._initialize_items()
    self._scroller = Scroller(items, line_separator=False, spacing=0)

  # ------------------------------------------------------------------------- items
  def _initialize_items(self):
    self._mando_card = _MandoCard(self._monitor)

    self._bench_button = button_item_sp(
      title=lambda: tr("Modo banco (armado local)"),
      button_text=lambda: tr("DESARMAR") if self._bench_armed else tr("ARMAR"),
      description=lambda: self._bench_status,
      callback=self._toggle_bench,
    )

    self._help_button = button_item_sp(
      title=lambda: tr("Qué significa esto"),
      button_text=lambda: tr("VER"),
      description=lambda: tr("Definiciones de ARMAR, DESARMAR, los modos y el enlace."),
      callback=self._show_help,
    )

    self._privacy_toggle = toggle_item_sp(
      title=lambda: tr("NO EMITIR POSICION NI CAMARA"),
      description=lambda: tr("Interruptor maestro local. Corta el envio de posicion y de imagen " +
                             "desde el propio coche: funciona sin red y con el movil apagado."),
      initial_state=mando.privacy_muted(),
      callback=self._on_privacy,
    )

    self._enroll_button = button_item_sp(
      title=lambda: tr("Vincular con la app (QR)"),
      button_text=lambda: tr("MOSTRAR"),
      description=lambda: tr("Muestra el codigo QR de enrolamiento en esta pantalla."),
      callback=lambda: gui_app.push_widget(OrbitEnrollDialog()),
    )

    self._telemetry_button = button_item_sp(
      title=lambda: tr("Telemetria"),
      button_text=lambda: tr("ABRIR"),
      description=lambda: tr("Que datos manda el dispositivo a la app: posicion, vehiculo, percepcion... " +
                             "y el ahorro automatico en red movil."),
      callback=lambda: self._set_current_panel(PanelType.TELEMETRY),
    )

    self._advanced_button = button_item_sp(
      title=lambda: tr("Ajustes avanzados"),
      button_text=lambda: tr("ABRIR"),
      description=lambda: tr("Avisos de HUD (angulo muerto, cambio de carril) y enlace con la " +
                             "Jetson (IPs, puertos, calidad de imagen)."),
      callback=lambda: self._set_current_panel(PanelType.ADVANCED),
    )

    self._safe_reset_button = button_item_sp(
      title=lambda: tr("Restablecer valores seguros"),
      # "RESTABLECER" (11 caracteres) no cabe en los 300 px del ButtonAction y se partia
      # en dos lineas ("RESTABLECE" / "R"). El titulo de la fila ya dice que restablece.
      button_text=lambda: tr("APLICAR"),
      description=lambda: tr("Devuelve los params ORBIT que afectan a la conduccion a su estado " +
                             "seguro (volante COMMA, sin comandos remotos pendientes, alertas visibles)."),
      callback=self._confirm_safe_reset,
    )

    # Tres cabeceras, no seis: cada SectionHeaderSP son 96 px de scroll.
    return [
      self._mando_card,
      self._bench_button,
      self._help_button,
      SectionHeaderSP(tr("VOLANTE"), seccion='mando'),
      *self._steer_rows.items,
      SectionHeaderSP(tr("CONEXION"), seccion='ajustes'),
      self._enroll_button,
      # Solo la fila del broker: "Probar conexion" sigue en el modal SERVIDOR de la home.
      self._server_rows.items[0],
      SectionHeaderSP(tr("DISPOSITIVO"), seccion='mando'),
      self._privacy_toggle,
      self._telemetry_button,
      self._advanced_button,
      self._safe_reset_button,
    ]

  # --------------------------------------------------------------- armado de banco
  @_a_salvo("modo banco", avisar=True)
  def _toggle_bench(self):
    if mando.bench_armed():   # sin cache: es una accion, no un pintado
      self._apply_bench_disarm()
      return

    msg = ajustes.texto_armar_banco()

    def on_result(result: DialogResult):
      if result != DialogResult.CONFIRM:
        return
      self._apply_bench_arm()

    gui_app.push_widget(ConfirmDialog(msg, tr("SI, armar 5 minutos"), tr("Cancelar"), callback=on_result))

  @_a_salvo("armar el banco", avisar=True)
  def _apply_bench_arm(self):
    """Corre como callback del ConfirmDialog, o sea dentro del render: a salvo."""
    fallos = mando.arm_bench()
    guard = getattr(ui_state, "orbit_bench_guard", None)
    if guard is not None:
      guard.note_local_arm(mando.ARM_REASON_BENCH)
    self._last_refresh = 0.0
    if fallos:
      gui_app.push_widget(alert_dialog(tr("No se pudo armar el banco:") + "\n" + "\n".join(fallos)))

  @_a_salvo("desarmar el banco", avisar=True)
  def _apply_bench_disarm(self):
    fallos = mando.disarm_bench()
    guard = getattr(ui_state, "orbit_bench_guard", None)
    if guard is not None:
      guard.note_disarm()
    self._last_refresh = 0.0
    if fallos:
      gui_app.push_widget(alert_dialog(tr("No se pudo desarmar el banco:") + "\n" + "\n".join(fallos)))

  # ------------------------------------------------------------------ definiciones
  @_a_salvo("definiciones del panel", avisar=True)
  def _show_help(self):
    """Dialogo con las definiciones de la terminologia que menos se explica sola.

    El usuario pidio un boton en el menu que diga que son ARMAR y DESARMAR y el resto
    de conceptos que no quedan claros. Es un dialogo informativo (sin confirmacion):
    no toca ningun param, asi que no puede romper nada.

    Se usa rich=True (HTML + Scroller) a proposito: el texto es largo y el modo
    plano (rich=False) recorta con scissor lo que no cabe en el area fija del modal.
    Con rich el contenido se desplaza y nada se pierde.
    """
    msg = ayuda_html()
    gui_app.push_widget(ConfirmDialog(msg, tr("OK"), rich=True))

  # ------------------------------------------------------------------ desarmar todo
  @_a_salvo("DESARMAR TODO", avisar=True)
  def _do_disarm_all(self):
    fallos = mando.disarm_all()
    self._steer_rows.sync()
    guard = getattr(ui_state, "orbit_bench_guard", None)
    if guard is not None:
      guard.note_disarm()
    self._last_refresh = 0.0
    if fallos:
      gui_app.push_widget(alert_dialog(tr("DESARME INCOMPLETO. Han fallado:") + "\n" + "\n".join(fallos)))
    else:
      self._disarm_feedback_until = time.monotonic() + _DISARM_FEEDBACK_S

  # ---------------------------------------------------------------------- privacidad
  @_a_salvo("interruptor de privacidad", avisar=True)
  def _on_privacy(self, enabled: bool):
    fallos = mando.set_privacy_mute(bool(enabled))
    if fallos:
      gui_app.push_widget(alert_dialog(tr("El corte de emision fallo en:") + "\n" + "\n".join(fallos)))

  # -------------------------------------------------- restablecer valores seguros
  def _confirm_safe_reset(self):
    msg = ajustes.texto_valores_seguros()

    def on_result(result: DialogResult):
      if result != DialogResult.CONFIRM:
        return
      self._apply_safe_reset()

    gui_app.push_widget(ConfirmDialog(msg, tr("SI, restablecer"), tr("Cancelar"), callback=on_result))

  @_a_salvo("restablecer valores seguros", avisar=True)
  def _apply_safe_reset(self):
    """Siete grupos de escrituras independientes: ver ajustes.aplicar_valores_seguros."""
    fallos = ajustes.aplicar_valores_seguros(ui_state.params)

    guard = getattr(ui_state, "orbit_bench_guard", None)
    if guard is not None:
      guard.note_disarm()
    self._steer_rows.sync()
    self._last_refresh = 0.0  # repintar el estado vivo de inmediato

    if fallos:
      gui_app.push_widget(alert_dialog(tr("RESTABLECIMIENTO INCOMPLETO. Han fallado:") +
                                       "\n" + "\n".join(fallos)))

  # ------------------------------------------------------------------ estado vivo
  @_a_salvo("refresco del estado del armado")
  def _refresh_status(self):
    now = time.monotonic()
    if now - self._last_refresh < _REFRESH_SECONDS:
      return
    self._last_refresh = now

    armado, restante_s = mando.bench_snapshot()
    self._bench_armed = armado
    # Solo existe UN motivo de armado (ARM_REASON_BENCH, el boton de esta pantalla): el
    # selector de volante no arma el banco, va por OrbitSteerModeLocal (ver steer_mode.py).
    # Aqui se comparaba con `mando.ARM_REASON_STEER`, una constante que se retiro con ese
    # cambio, y como solo se ejecutaba con el banco ARMADO nadie lo vio hasta que alguien
    # pulso ARMAR en el coche: AttributeError en cada frame, muerte del proceso ui y bucle
    # de reinicios (ver _a_salvo).
    self._bench_status = ajustes.texto_estado_banco(armado, restante_s)

  # -------------------------------------------------------------------- lifecycle
  def _set_current_panel(self, panel: PanelType):
    self._current_panel = panel
    if panel == PanelType.TELEMETRY:
      # Relee los toggles al abrir: la app o el interruptor de privacidad pudieron cambiarlos.
      self._telemetry_layout.show_event()

  def _render(self, rect):
    if self._current_panel == PanelType.ADVANCED:
      self._advanced_layout.render(rect)
      return
    if self._current_panel == PanelType.TELEMETRY:
      self._telemetry_layout.render(rect)
      return

    self._refresh_status()
    self._server_rows.poll()
    self._steer_rows.poll()

    # El scroller cede la franja inferior a la barra de desarme para que no se solapen:
    # si compartieran rect, un toque en el boton contaria tambien como arrastre.
    bar_h = _DISARM_BAR_HEIGHT + _DISARM_BAR_GAP
    self._scroller.render(rl.Rectangle(rect.x, rect.y, rect.width, max(0.0, rect.height - bar_h)))
    self._disarm_button.render(rl.Rectangle(rect.x, rect.y + rect.height - _DISARM_BAR_HEIGHT,
                                            rect.width, _DISARM_BAR_HEIGHT))

  def show_event(self):
    self._set_current_panel(PanelType.MAIN)
    self._last_refresh = 0.0
    self._steer_rows.sync()
    self._privacy_toggle.action_item.toggle.set_state(mando.privacy_muted())
    self._scroller.show_event()
