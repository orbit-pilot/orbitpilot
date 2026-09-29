"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Piezas de la UI mici (comma 4, 536x240) para el panel ORBIT.

La pantalla es pequena: los textos largos del panel grande (confirmaciones, avisos) se
parten en tarjetas que caben (`TarjetaTexto` + orbit_ajustes.trocear) y las
confirmaciones son de deslizar, como el resto de la UI mici. La logica vive en
selfdrive/ui/widgets/orbit_ajustes.py y es la misma que usa el comma 3X.
"""
from __future__ import annotations

from collections.abc import Callable

import pyray as rl

from openpilot.selfdrive.ui import orbit_theme as t
from openpilot.selfdrive.ui.mici.widgets.button import BigButton, GreyBigButton, LABEL_COLOR, COMPLICATION_GREY
from openpilot.selfdrive.ui.mici.widgets.dialog import BigDialog, BigInputDialog, BigConfirmationCircleButton
from openpilot.selfdrive.ui.widgets import orbit_ajustes as ajustes
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.lib.wrap_text import wrap_text
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.scroller import NavScroller

GRIS = COMPLICATION_GREY
# Tinta de cada tono de orbit_ajustes.lineas_mando (los mismos tokens que la UI grande).
TONOS = {"ok": t.OK, "aviso": t.AVISO, "peligro": t.PELIGRO, "normal": LABEL_COLOR}
_FONDO_TARJETA = rl.Color(255, 255, 255, int(255 * 0.15))  # el de GreyBigButton


def aviso(titulo: str, texto: str = "") -> None:
  gui_app.push_widget(BigDialog(titulo, texto))


def a_salvo(etiqueta: str, avisar: bool = False):
  """orbit_ajustes.a_salvo con el aviso de la pantalla pequena (ver orbit_panel._a_salvo)."""
  return ajustes.a_salvo(etiqueta, (lambda e: aviso("fallo interno", f"{e}\nrevisa el log de la ui")) if avisar else None)


def dialogo_numerico(hint: str, texto: str, al_confirmar: Callable[[str], None], minimo: int = 1) -> None:
  """BigInputDialog que abre YA en la capa de numeros (IPs y puertos: digitos y '.').

  No hay API publica para elegir capa. Se asigna la lista de teclas y NO se llama a
  `_set_keys` antes del primer render: Key.set_position fija su posicion original en la
  primera llamada y el hit-test quedaria mal. auto_return_to_letters="" para que el '.'
  no devuelva el teclado a las letras despues de cada octeto.
  """
  dlg = BigInputDialog(hint, texto, minimum_length=minimo, confirm_callback=al_confirmar, auto_return_to_letters="")
  dlg._keyboard._current_keys = dlg._keyboard._special_keys
  gui_app.push_widget(dlg)


def recortar(fuente: rl.Font, texto: str, tam: int, ancho: float) -> str:
  """Recorta `texto` al ancho dado (con '..' al final si hubo que cortar)."""
  if measure_text_cached(fuente, texto, tam).x <= ancho:
    return texto
  while texto and measure_text_cached(fuente, texto + "..", tam).x > ancho:
    texto = texto[:-1]
  return texto.rstrip() + ".."


class _ValorAjustable:
  """Mezcla para BigButton: el valor encoge (36 -> 24 px) hasta caber en los 180 px.

  El valor de BigButton va a 36 px fijos y lo que no cabe se elide: en 536x240 eso se
  comia justo la parte util ("torque del model..."). Se ajusta una vez por texto nuevo.
  """

  TAMANOS = (36, 32, 28, 26, 24)
  _ajustada = False

  def set_value(self, value: str):
    if value != self.value:
      super().set_value(value)
      self._ajustada = False

  def _ajustar(self) -> None:
    self._ajustada = True
    ancho = self._width_hint()
    titulo = self._label.get_content_height(ancho) + 8 if self._label.text else 0
    disponible = self._rect.height - 2 * self.LABEL_VERTICAL_PADDING - titulo
    for tam in self.TAMANOS:
      self._sub_label.set_font_size(tam)
      if self._sub_label.get_content_height(ancho) <= disponible:
        break

  def _render(self, rect):
    if not self._ajustada:
      self._ajustar()
    super()._render(rect)


class Baldosa(_ValorAjustable, BigButton):
  """BigButton con el valor ajustado al hueco."""


class TarjetaTexto(_ValorAjustable, GreyBigButton):
  """Tarjeta gris de texto (sin toque) con el texto ajustado al hueco."""

  def __init__(self, texto: str, titulo: str = "", icono: rl.Texture | None = None):
    super().__init__(titulo, texto, icono)


class TarjetaEstado(Widget):
  """Tarjeta de solo lectura (476x180): cabecera, chip opcional y filas etiqueta / valor.

  Quien la usa rellena `filas` [(etiqueta, valor, color)] y `chip` (texto, relleno); aqui
  solo se pinta. La columna de valores empieza tras la etiqueta mas ancha y cada valor se
  parte en varias lineas si no cabe (hasta llenar la tarjeta; lo que sobra se recorta).
  """

  ANCHO, ALTO = 476, 180
  TAM_ETIQUETA, TAM_VALOR, LINEA = 24, 26, 31

  def __init__(self, titulo: str):
    super().__init__()
    self.set_rect(rl.Rectangle(0, 0, self.ANCHO, self.ALTO))
    self._titulo = titulo
    self._f_titulo = gui_app.font(FontWeight.DISPLAY)
    self._f_etiqueta = gui_app.font(FontWeight.ROMAN)
    self._f_valor = gui_app.font(FontWeight.BOLD)
    self.filas: list[tuple[str, str, rl.Color]] = []
    self.chip: tuple[str, rl.Color] | None = None

  def _render(self, _):
    r = self._rect
    rl.draw_rectangle_rounded(r, 0.4, 10, _FONDO_TARJETA)
    x = r.x + 26
    rl.draw_text_ex(self._f_titulo, self._titulo, rl.Vector2(x, r.y + 12), 34, 0, LABEL_COLOR)

    if self.chip is not None:
      texto, color = self.chip
      tam = measure_text_cached(self._f_valor, texto, 22)
      chip = rl.Rectangle(r.x + r.width - 22 - tam.x - 28, r.y + 14, tam.x + 28, 38)
      rl.draw_rectangle_rounded(chip, 1.0, 10, color)
      rl.draw_text_ex(self._f_valor, texto, rl.Vector2(chip.x + 14, chip.y + (38 - tam.y) / 2), 22, 0, t.FONDO)

    etiquetas = [e for e, _, _ in self.filas if e]
    vx = x + (max(measure_text_cached(self._f_etiqueta, e, self.TAM_ETIQUETA).x for e in etiquetas) + 16 if etiquetas else 0)
    ancho = r.x + r.width - 22 - vx
    y = r.y + 60
    fondo = r.y + r.height - 10
    for etiqueta, valor, color in self.filas:
      if y + self.LINEA > fondo:
        break
      if etiqueta:
        rl.draw_text_ex(self._f_etiqueta, etiqueta, rl.Vector2(x, y + 3), self.TAM_ETIQUETA, 0, GRIS)
      lineas = wrap_text(self._f_valor, valor, self.TAM_VALOR, int(ancho)) or [""]
      for n, linea in enumerate(lineas):
        ultima = y + 2 * self.LINEA > fondo
        if ultima and n < len(lineas) - 1:
          linea = recortar(self._f_valor, linea + " " + " ".join(lineas[n + 1:]), self.TAM_VALOR, ancho)
        rl.draw_text_ex(self._f_valor, linea, rl.Vector2(vx, y), self.TAM_VALOR, 0, color)
        y += self.LINEA
        if ultima:
          break
      y += 4


class BotonRojo(Baldosa):
  """BigButton con relleno rojo de accion destructiva/de panico (token FRENO).

  Los fondos de BigButton son texturas PNG grises y no hay version roja rectangular,
  asi que se pinta el rectangulo redondeado a mano con la misma animacion de pulsado.
  """

  def __init__(self, text: str, value: str = "", icon: rl.Texture | None = None):
    super().__init__(text, value, icon)
    self._sub_label.set_text_color(rl.Color(255, 255, 255, int(255 * 0.85)))

  def _render(self, _):
    _, btn_x, btn_y, escala = self._handle_background()
    rect = rl.Rectangle(btn_x, btn_y, self._rect.width * escala, self._rect.height * escala)
    color = t.mezcla(t.FRENO, 0.75, rl.BLACK) if self.is_pressed else t.FRENO
    rl.draw_rectangle_rounded(rect, 0.35, 12, color)
    self._draw_content(btn_y)


class PaginaConfirmacion(NavScroller):
  """Texto largo en tarjetas + confirmacion (idioma mici, como AlphaLongConfirmPage).

  `deslizar` es el titulo del deslizador (None = sin deslizador) y `al_confirmar` corre
  DESPUES de cerrarse la pagina. `opciones` son botones de eleccion [(texto, valor,
  callback)] que tambien cierran la pagina antes de llamar a su callback. Deslizar hacia
  abajo cancela sin tocar nada.
  """

  def __init__(self, titulo: str, texto: str, deslizar: str | None, al_confirmar: Callable[[], None] | None,
               rojo: bool = False, opciones: list[tuple[str, str, Callable[[], None]]] | None = None):
    super().__init__()
    icono_aviso = gui_app.texture("icons_mici/setup/warning.png", 64, 64)
    items: list[Widget] = [TarjetaTexto("desliza para leer", titulo, icono_aviso)]
    items += [TarjetaTexto(trozo) for trozo in ajustes.trocear(texto)]

    if deslizar is not None and al_confirmar is not None:
      icono = gui_app.texture("icons_mici/setup/red_warning.png" if rojo else "icons_mici/setup/driver_monitoring/dm_check.png", 64, 64)
      items.append(BigConfirmationCircleButton(deslizar, icono, lambda: self.dismiss(al_confirmar), red=rojo))

    for texto_op, valor, callback in opciones or []:
      boton = Baldosa(texto_op, valor)
      boton.set_click_callback(lambda cb=callback: self.dismiss(cb))
      items.append(boton)

    self._scroller.add_widgets(items)
