"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Panel ORBIT de los ajustes del comma 4 (536x240): lo mismo que el panel ORBIT de la UI
grande (sunnypilot/layouts/settings/orbit_panel.py), en baldosas mici.

  * DESARMAR TODO, la primera baldosa y en rojo: se ve nada mas abrir el panel, sin
    arrastrar. Un boton de panico al que hay que llegar haciendo scroll no lo es.
  * estado del MANDO (de cereal via ui_state.orbit_command, nunca rancio) y de la
    CONEXION (servidor, enlace, cuenta con su rol),
  * modo banco, "que significa esto", selector de volante,
  * vincular con la app (QR) / cuenta + desvincular, IP del servidor y prueba,
  * interruptor maestro de privacidad, telemetria (que grupos de datos se envian a la app),
    ajustes avanzados y valores seguros.

La logica (textos, validaciones, escrituras) es la de orbit_ajustes / orbit_mando /
orbit_server: la misma que usa el comma 3X. Aqui solo hay baldosas. Params se leen a
2 Hz como mucho; todo callback va envuelto en a_salvo (una excepcion no puede matar el
proceso ui).
"""
import threading
import time

import pyray as rl

from openpilot.orbit import config_broker
from openpilot.selfdrive.ui import orbit_theme as t
from openpilot.selfdrive.ui.mici.widgets.button import BigToggle
from openpilot.selfdrive.ui.sunnypilot.mici.layouts.orbit_avanzado import AvanzadoLayoutMici
from openpilot.selfdrive.ui.sunnypilot.mici.layouts.orbit_telemetria import TelemetriaLayoutMici
from openpilot.selfdrive.ui.sunnypilot.mici.layouts.orbit_volante import VolanteLayoutMici, estado_volante, nombre_modo
from openpilot.selfdrive.ui.sunnypilot.mici.widgets.orbit_enroll_dialog import OrbitEnrollDialogMici
from openpilot.selfdrive.ui.sunnypilot.mici.widgets.orbit_widgets import (
  GRIS, TONOS, Baldosa, BotonRojo, PaginaConfirmacion, TarjetaEstado, TarjetaTexto, a_salvo, aviso, dialogo_numerico,
)
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui.widgets import orbit_ajustes as ajustes
from openpilot.selfdrive.ui.widgets import orbit_mando as mando
from openpilot.selfdrive.ui.widgets.orbit_server import ServerMonitor, probar_servidor, read_broker
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.widgets.html_render import ElementType, HtmlRenderer
from openpilot.system.ui.widgets.scroller import NavRawScrollPanel, NavScroller

REFRESH_S = 0.5            # Params y config a 2 Hz como mucho
DESARMADO_FEEDBACK_S = 2.5


class AyudaOrbitMici(NavRawScrollPanel):
  """"Que significa esto": las mismas definiciones que el dialogo de la UI grande."""

  def __init__(self):
    super().__init__()
    # Import perezoso: orbit_panel arrastra la UI grande entera; solo se paga al abrir.
    from openpilot.selfdrive.ui.sunnypilot.layouts.settings.orbit_panel import ayuda_html
    self._html = HtmlRenderer(text=ayuda_html(), text_size={ElementType.P: 26})

  def _render(self, rect):
    alto = self._html.get_total_height(int(rect.width - 40)) + 40
    desplazamiento = round(self._scroll_panel.update(rect, alto))
    self._html.render(rl.Rectangle(rect.x + 20, rect.y + 20 + desplazamiento, rect.width - 40, alto))


class OrbitLayoutMici(NavScroller):
  def __init__(self):
    super().__init__()
    self._monitor: ServerMonitor | None = None
    self._ultimo_refresco = float("-inf")
    self._desarmado_hasta = 0.0
    self._resultado_prueba: tuple[str, int, bool, bool] | None = None
    self._probando = False
    self._claimed = False
    self._cuenta = ""
    self._enlace = False
    self._banco_restante = 0.0

    self._btn_desarmar = BotonRojo("desarmar todo", "cancela órdenes")
    self._btn_desarmar.set_click_callback(self._desarmar_todo)

    self._tarjeta_mando = TarjetaEstado("mando")
    self._tarjeta_ultimo = TarjetaEstado("último comando")
    self._tarjeta_conexion = TarjetaEstado("conexión")

    self._btn_banco = Baldosa("modo banco", "")
    self._btn_banco.set_click_callback(self._pulsar_banco)

    self._volante = VolanteLayoutMici()
    self._btn_volante = Baldosa("volante", "")
    self._btn_volante.set_click_callback(lambda: gui_app.push_widget(self._volante))

    self._ayuda: AyudaOrbitMici | None = None
    self._btn_ayuda = Baldosa("qué significa esto", "definiciones")
    self._btn_ayuda.set_click_callback(self._abrir_ayuda)

    self._qr: OrbitEnrollDialogMici | None = None
    self._btn_vincular = Baldosa("vincular con la app", "muestra el qr")
    self._btn_vincular.set_click_callback(self._abrir_qr)
    self._btn_vincular.set_visible(lambda: not self._claimed)
    self._tarjeta_cuenta = TarjetaTexto("", "cuenta orbit")
    self._tarjeta_cuenta.set_visible(lambda: self._claimed)
    self._btn_desvincular = Baldosa("desvincular", "quitar la cuenta")
    self._btn_desvincular.set_click_callback(self._pedir_desvincular)
    self._btn_desvincular.set_visible(lambda: self._claimed)

    self._btn_servidor = Baldosa("servidor orbit", "")
    self._btn_servidor.set_click_callback(lambda: self._editar_broker())
    self._btn_probar = Baldosa("probar conexión", "broker y api")
    self._btn_probar.set_click_callback(self._probar)

    self._tgl_privacidad = BigToggle("no emitir", "posición ni cámara", initial_state=mando.privacy_muted(),
                                     toggle_callback=self._on_privacidad)

    self._telemetria = TelemetriaLayoutMici()
    self._btn_telemetria = Baldosa("telemetría", "qué se envía a la app")
    self._btn_telemetria.set_click_callback(lambda: gui_app.push_widget(self._telemetria))

    self._avanzado = AvanzadoLayoutMici()
    self._btn_avanzado = Baldosa("ajustes avanzados", "hud y jetson")
    self._btn_avanzado.set_click_callback(lambda: gui_app.push_widget(self._avanzado))

    self._btn_seguros = Baldosa("valores seguros", "restablecer")
    self._btn_seguros.set_click_callback(self._pedir_valores_seguros)

    self._scroller.add_widgets([
      self._btn_desarmar,
      self._tarjeta_mando,
      self._tarjeta_ultimo,
      self._tarjeta_conexion,
      self._btn_banco,
      self._btn_volante,
      self._btn_ayuda,
      self._btn_vincular,
      self._tarjeta_cuenta,
      self._btn_desvincular,
      self._btn_servidor,
      self._btn_probar,
      self._tgl_privacidad,
      self._btn_telemetria,
      self._btn_avanzado,
      self._btn_seguros,
    ])

  # ================================================================ estado vivo
  @a_salvo("refresco del panel ORBIT")
  def _refrescar(self) -> None:
    p = ui_state.params
    self._claimed = bool(p.get_bool("OrbitClaimed"))
    self._cuenta = ajustes.etiqueta_cuenta(p.get(mando.PARAM_OWNER), p.get(mando.PARAM_OWNER_ROLE))
    self._enlace = ajustes.enlace_orbit(p)

    armado, self._banco_restante = mando.bench_snapshot()
    # El valor dice que hace el toque: armado, un toque desarma SIN confirmacion.
    self._btn_banco.set_value(f"armado {int(self._banco_restante)} s\ntoca para desarmar" if armado
                              else "desarmado\ntoca para armar")

    modo, estado = estado_volante()
    self._btn_volante.set_value(f"{nombre_modo(modo)}\n{estado.lower()}")

    self._tarjeta_cuenta.set_value(self._cuenta)
    ip, _ = read_broker()
    self._btn_servidor.set_value(ip or "sin configurar")

  @a_salvo("tarjetas del panel ORBIT")
  def _pintar_tarjetas(self) -> None:
    """Cada frame, pero solo RAM: el mando es la vista cereal que UIStateSP refresca."""
    st = getattr(ui_state, "orbit_command", None)
    tm, tu = self._tarjeta_mando, self._tarjeta_ultimo
    if st is None or not st.available:
      # Un plano muerto no se pinta en verde ni con el ultimo valor conocido.
      tm.filas = [("", "subsistema de mando caído", t.PELIGRO),
                  ("", "no se publica orbitCommandState: sin mando remoto y sin gates", GRIS)]
      tm.chip = None
      tu.filas = [("", "sin datos del mando", GRIS)]
    else:
      (_, modo, tono_m), (_, enlace, tono_e), (_, ultimo, tono_u) = ajustes.lineas_mando(st)
      tm.filas = [("modo", modo.lower(), TONOS[tono_m]), ("enlace", enlace.lower(), TONOS[tono_e])]
      tm.chip = (f"banco {int(self._banco_restante)}s", t.AVISO) if st.bench_armed else None
      tu.filas = [("", ultimo.lower(), TONOS[tono_u])]

    mon = self._monitor
    if mon is None:
      servidor, color = "comprobando...", GRIS
    elif mon.broker_ok and mon.backend_ok:
      servidor, color = "ok", t.OK
    elif mon.broker_ok:
      servidor, color = "broker ok, sin api", GRIS
    else:
      servidor, color = "sin respuesta", GRIS
    self._tarjeta_conexion.filas = [
      ("servidor", servidor, color),
      ("enlace", "ok" if self._enlace else "caído", t.OK if self._enlace else GRIS),
      ("cuenta", self._cuenta or "sin vincular", TONOS["normal"] if self._cuenta else GRIS),
    ]

  def _update_state(self):
    super()._update_state()
    ahora = time.monotonic()
    if ahora - self._ultimo_refresco >= REFRESH_S:
      self._ultimo_refresco = ahora
      self._refrescar()
    self._pintar_tarjetas()

    texto = "desarmado" if ahora < self._desarmado_hasta else "desarmar todo"
    if self._btn_desarmar.text != texto:
      self._btn_desarmar.set_text(texto)

    if self._resultado_prueba is not None:
      ip, puerto, broker_ok, api_ok = self._resultado_prueba
      self._resultado_prueba = None
      self._probando = False
      self._btn_probar.set_value("broker y api")
      aviso(f"{ip}:{puerto}", f"broker {'ok' if broker_ok else 'sin respuesta'}\napi {'ok' if api_ok else 'sin respuesta'}")

  def _forzar_refresco(self) -> None:
    self._ultimo_refresco = float("-inf")

  # ================================================================ desarmar todo
  @a_salvo("DESARMAR TODO", avisar=True)
  def _desarmar_todo(self) -> None:
    # Funciona con el hilo ORBIT caido: ademas del disparo baja la autoridad en Params.
    fallos = mando.disarm_all()
    guard = getattr(ui_state, "orbit_bench_guard", None)
    if guard is not None:
      guard.note_disarm()
    self._volante.sync()
    self._forzar_refresco()
    if fallos:
      aviso("desarme incompleto", "han fallado:\n" + "\n".join(fallos))
    else:
      self._desarmado_hasta = time.monotonic() + DESARMADO_FEEDBACK_S

  # ================================================================ modo banco
  @a_salvo("modo banco", avisar=True)
  def _pulsar_banco(self) -> None:
    if mando.bench_armed():  # sin cache: es una accion, no un pintado
      self._desarmar_banco()
      return
    gui_app.push_widget(PaginaConfirmacion("modo banco", ajustes.texto_armar_banco(), "desliza para\narmar 5 min",
                                           self._armar_banco))

  @a_salvo("armar el banco", avisar=True)
  def _armar_banco(self) -> None:
    fallos = mando.arm_bench()
    guard = getattr(ui_state, "orbit_bench_guard", None)
    if guard is not None:
      guard.note_local_arm(mando.ARM_REASON_BENCH)
    self._forzar_refresco()
    if fallos:
      aviso("no se pudo armar", "\n".join(fallos))

  @a_salvo("desarmar el banco", avisar=True)
  def _desarmar_banco(self) -> None:
    fallos = mando.disarm_bench()
    guard = getattr(ui_state, "orbit_bench_guard", None)
    if guard is not None:
      guard.note_disarm()
    self._forzar_refresco()
    if fallos:
      aviso("no se pudo desarmar", "\n".join(fallos))

  # ================================================================ ayuda
  @a_salvo("definiciones del panel", avisar=True)
  def _abrir_ayuda(self) -> None:
    if self._ayuda is None:
      self._ayuda = AyudaOrbitMici()
    gui_app.push_widget(self._ayuda)

  # ================================================================ vinculacion
  @a_salvo("QR de vinculacion", avisar=True)
  def _abrir_qr(self) -> None:
    # UNA instancia para toda la vida del proceso: el proceso ui corre con gc.disable()
    # (config_realtime_process) y el dialogo tiene ciclos, asi que uno nuevo por toque
    # dejaba su textura del QR en la GPU para siempre. show_event reinicia su estado.
    if self._qr is None:
      self._qr = OrbitEnrollDialogMici()
    gui_app.push_widget(self._qr)

  @a_salvo("desvincular", avisar=True)
  def _pedir_desvincular(self) -> None:
    texto = ("Reinicia el enlace ORBIT en este dispositivo. Si la cuenta sigue vinculada en el servidor, " +
             "se volverá a vincular sola; para desvincularla del todo usa la app ORBIT.")
    gui_app.push_widget(PaginaConfirmacion("desvincular", texto, "desliza para\ndesvincular", self._desvincular, rojo=True))

  @a_salvo("desvincular", avisar=True)
  def _desvincular(self) -> None:
    fallos = ajustes.desvincular(ui_state.params)
    self._forzar_refresco()
    if fallos:
      aviso("desvínculo incompleto", "\n".join(fallos))

  # ================================================================ servidor
  @a_salvo("editar la IP del servidor", avisar=True)
  def _editar_broker(self, texto: str | None = None) -> None:
    actual = texto if texto is not None else read_broker()[0]

    @a_salvo("guardar la IP del servidor", avisar=True)
    def al_confirmar(escrito: str) -> None:
      escrito = escrito.strip()
      error = ajustes.validar_host(escrito)
      if error:
        # Se reabre con lo escrito y el motivo encima: al cerrar el aviso se corrige.
        self._editar_broker(escrito)
        aviso("ip no válida", error)
        return
      # Solo la clave "broker": puerto, backend y credenciales se conservan.
      if not config_broker.escribir_config({"broker": escrito}):
        aviso("", "no se pudo guardar la ip del servidor")
      self._forzar_refresco()

    dialogo_numerico("ip del servidor orbit...", actual, al_confirmar)

  @a_salvo("probar la conexion", avisar=True)
  def _probar(self) -> None:
    ip, puerto = read_broker()
    if not ip:
      aviso("", "no hay ip configurada")
      return
    if self._probando:
      return
    self._probando = True
    self._btn_probar.set_value("probando...")

    def _correr():
      try:
        broker_ok, api_ok = probar_servidor(ip, puerto, timeout=3.0)
      except Exception:
        broker_ok = api_ok = False
      # Se entrega al hilo de UI: el dialogo solo puede abrirse desde el render.
      self._resultado_prueba = (ip, puerto, broker_ok, api_ok)

    threading.Thread(target=_correr, name="orbit_probe", daemon=True).start()

  # ================================================================ privacidad
  @a_salvo("interruptor de privacidad", avisar=True)
  def _on_privacidad(self, activo: bool) -> None:
    fallos = mando.set_privacy_mute(bool(activo))
    self._tgl_privacidad.set_checked(mando.privacy_muted())
    if fallos:
      aviso("el corte de emisión falló en", "\n".join(fallos))

  # ================================================================ valores seguros
  @a_salvo("restablecer valores seguros", avisar=True)
  def _pedir_valores_seguros(self) -> None:
    gui_app.push_widget(PaginaConfirmacion("valores seguros", ajustes.texto_valores_seguros(), "desliza para\nrestablecer",
                                           self._valores_seguros))

  @a_salvo("restablecer valores seguros", avisar=True)
  def _valores_seguros(self) -> None:
    fallos = ajustes.aplicar_valores_seguros(ui_state.params)
    guard = getattr(ui_state, "orbit_bench_guard", None)
    if guard is not None:
      guard.note_disarm()
    self._volante.sync()
    self._forzar_refresco()
    if fallos:
      aviso("restablecimiento incompleto", "\n".join(fallos))

  # ================================================================ ciclo de vida
  def show_event(self):
    super().show_event()
    self._forzar_refresco()
    self._tgl_privacidad.set_checked(mando.privacy_muted())
    # El sondeo del servidor (TCP + HTTP cada 5 s) solo corre con el panel abierto.
    if self._monitor is None:
      self._monitor = ServerMonitor()

  def hide_event(self):
    super().hide_event()
    if self._monitor is not None:
      self._monitor.stop()
      self._monitor = None
