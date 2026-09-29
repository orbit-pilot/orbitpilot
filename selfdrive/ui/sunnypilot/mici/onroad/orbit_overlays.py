"""
Overlays ORBIT de la vista onroad del comma 4 (mici, contenido de 476x240).

Requisito de seguridad: el conductor tiene que saber SIEMPRE que la app esta mandando
ordenes al coche. Aqui solo se dibuja; que ha pasado lo decide OrbitRemoteWatch
(orbit_remoto.py) desde un tick del nav stack, porque esta vista no se renderiza cuando
el conductor esta en otra pagina.

  * Indicadores, por prioridad: esquive del Jetson, autoridad concedida (COPILOTO /
    MANIOBRA / BANCO) y "BANCO • SIN COCHE" con ForceOnroad.
  * Pildora de orden: CARRIL IZQ/DER, VELOCIDAD +/-, PULSO DIRECCION u ORDEN REMOTA, con
    el punto cian de ORBIT. 3 s.
  * Banda FRENADO REMOTO (abajo, a la derecha del volante) mientras la frenada remota es
    REAL. Se dibuja la ultima.

Sin alerta de serie los indicadores van en columna arriba a la derecha (arriba a la
izquierda estan la velocidad fijada y el DMoji) y la pildora abajo al centro, sobre el arco
de par. CON alerta su texto manda: ocupa desde arriba a la izquierda todo el ancho y hasta
~197 px de alto, asi que ORBIT se queda en UNA fila compacta bajo ALERT_TEXT_FLOOR, desde
la derecha hasta el volante, por prioridad (esquive, orden, autoridad, banco). Lo que no
cabe no se pinta, y con ello todo lo de menos prioridad. Con FRENADO REMOTO y alerta, solo
la banda: ya dice lo que esta pasando.

No se porta: el chip de frenada probable (el FCW de serie sale del mismo
hardBrakePredicted y ya se pinta en mici), el coach de distancia (informativo) ni el
texto de angulo muerto (BlindSpotIndicators ya lo cubre). Nada de esto se anima: onroad
no se mueve nada nuevo.
"""
import pyray as rl

from openpilot.selfdrive.ui import orbit_theme as t
from openpilot.selfdrive.ui.sunnypilot.mici.onroad.orbit_remoto import OrbitRemoteWatch
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.text_measure import measure_text_cached

TOAST_FONT = 30
TOP_FONT = 24
ALERT_FONT, ALERT_PAD_Y = 22, 3   # 26 + 6 = 32 px: con su borde, de ALERT_TEXT_FLOOR (200) a 236
BRAKE_FONT = 36
BRAKE_H = 48
MARGIN = 8
GAP = 6
PAD_X, PAD_Y, DOT_R = 14, 6, 6
BORDER = 2   # raylib pinta el borde por FUERA de la caja
# El arco de par llega hasta 82 px del borde inferior a par maximo (torque_bar.py).
TOAST_BOTTOM = 86
# Hasta aqui baja el texto de las alertas de serie mas altas ("dashcam mode" + "security key
# not available", "take control" + text2 largo). Lo comprueba test_orbit_mici_onroad_render.
ALERT_TEXT_FLOOR = 200
# Volante de serie abajo a la izquierda con su exclamacion de steerRequired (hud_renderer: x 21-90).
WHEEL_CLEAR = 96

BENCH_LABEL = "BANCO • SIN COCHE"   # U+2022: las fuentes Inter no tienen U+00B7
BRAKE_LABEL = "FRENADO REMOTO"

_PILL_FILL = t.con_alfa(t.SUP1, 230 / 255)
_BRAKE_FILL = t.con_alfa(t.FRENO, 0xF1 / 255)


class OrbitOnroadOverlay:
  def __init__(self):
    self._font = gui_app.font(FontWeight.BOLD)
    self.watch = OrbitRemoteWatch(ui_state.params)

  def tick(self) -> bool:
    """Desde el nav stack, en cada frame. True = orden remota nueva."""
    return self.watch.update(ui_state.orbit_command, ui_state.started)

  def _size(self, text: str, size: int, dot: bool, pad_y: int) -> tuple[float, float]:
    ts = measure_text_cached(self._font, text, size)
    return PAD_X * 2 + (DOT_R * 2 + 10 if dot else 0) + ts.x, ts.y + pad_y * 2

  def _pill(self, b: tuple, size: int, x: float, y: float, align: float = 1.0, pad_y: int = PAD_Y) -> float:
    """Pildora anclada en x: align 1 = x es su borde derecho, 0.5 = su centro. Devuelve su alto."""
    text, fill, ink, border, dot = b
    w, h = self._size(text, size, dot, pad_y)
    x -= w * align
    box = rl.Rectangle(x, y, w, h)
    rl.draw_rectangle_rounded(box, 0.5, 10, fill)
    if border is not None:
      rl.draw_rectangle_rounded_lines_ex(box, 0.5, 10, BORDER, border)
    if dot:
      rl.draw_circle(int(x + PAD_X + DOT_R), int(y + h / 2), DOT_R, t.PULSO)
    rl.draw_text_ex(self._font, text, rl.Vector2(x + PAD_X + (DOT_R * 2 + 10 if dot else 0), y + pad_y), size, 0, ink)
    return h

  def _badges(self) -> list[tuple]:
    """Indicadores por prioridad: (texto, relleno, tinta, borde, punto)."""
    w, out = self.watch, []
    if w.dodge:
      out.append((w.dodge, t.FRENO, t.SOBRE_FRENO, None, False) if w.dodge_bsm else (w.dodge, t.AVISO, t.FONDO, None, False))
    # Con ForceOnroad, "BANCO • SIN COCHE" ya dice BANCO: la pildora de autoridad sobraria
    if w.authority and not (ui_state.force_onroad and w.authority == "BANCO"):
      if w.authority_urgent:
        out.append((w.authority, t.AVISO, t.FONDO, None, False))
      else:
        out.append((w.authority, _PILL_FILL, t.TEXTO1, t.BORDE_FUERTE, False))
    if ui_state.force_onroad:
      out.append((BENCH_LABEL, _PILL_FILL, t.AVISO, t.AVISO, False))
    return out

  def _toast(self) -> tuple:
    return (self.watch.label, _PILL_FILL, t.TEXTO1, t.BORDE_FUERTE, True)

  def render(self, rect: rl.Rectangle, alert_showing: bool) -> None:
    w = self.watch

    if alert_showing:
      # Una sola fila bajo el texto de la alerta, de derecha a izquierda. La orden recien
      # llegada va justo detras del esquive: es el aviso puntual, lo demas ya se veia antes.
      if not w.brake_active:
        items = self._badges()
        if w.toast_visible():
          items.insert(1 if w.dodge else 0, self._toast())
        x = rect.x + rect.width - MARGIN
        for b in items:
          bw = self._size(b[0], ALERT_FONT, b[4], ALERT_PAD_Y)[0]
          if x - bw - BORDER < rect.x + WHEEL_CLEAR:
            break  # no cabe: fuera ella y las de menos prioridad
          self._pill(b, ALERT_FONT, x, rect.y + ALERT_TEXT_FLOOR + BORDER, pad_y=ALERT_PAD_Y)
          x -= bw + GAP
    else:
      # Columna superior derecha; el esquive (un ordenador externo moviendo el volante), mas grande
      x, y = rect.x + rect.width - MARGIN, rect.y + MARGIN
      for b in self._badges():
        y += self._pill(b, TOAST_FONT if b[0] == w.dodge else TOP_FONT, x, y) + GAP
      # Pildora de la orden, abajo al centro; si la columna baja tanto (banco + esquive), debajo
      if w.toast_visible():
        h = self._size(w.label, TOAST_FONT, True, PAD_Y)[1]
        self._pill(self._toast(), TOAST_FONT, rect.x + rect.width / 2, max(rect.y + rect.height - TOAST_BOTTOM - h, y), align=0.5)

    # La ultima: nada puede taparla. Empieza a la derecha del volante (su aviso de
    # steerRequired tiene que seguir viendose) y, con alerta, bajo su texto ("car unrecognized"
    # baja hasta y=197).
    if w.brake_active:
      h = min(BRAKE_H, rect.height - ALERT_TEXT_FLOOR) if alert_showing else BRAKE_H
      left, top = rect.x + WHEEL_CLEAR, rect.y + rect.height - h
      width = rect.x + rect.width - left
      rl.draw_rectangle(int(left), int(top), int(width), int(h), _BRAKE_FILL)
      ts = measure_text_cached(self._font, BRAKE_LABEL, BRAKE_FONT)
      rl.draw_text_ex(self._font, BRAKE_LABEL, rl.Vector2(left + (width - ts.x) / 2, top + (h - ts.y) / 2),
                      BRAKE_FONT, 0, t.SOBRE_FRENO)
