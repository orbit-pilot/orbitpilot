"""
Selector de FUENTE DE TORQUE DEL VOLANTE (param SteerTorqueMode), con confirmacion.

Vive en el panel ORBIT principal y no en un subpanel a proposito: la seccion 9 del
diseno lo deja en el comma porque necesita ojos en el coche, y enterrarlo tras dos
navegaciones es lo contrario de eso.

DOS AUTORIZACIONES DISTINTAS, Y NO SE PUEDEN MEZCLAR
---------------------------------------------------
El catalogo clasifica `torque_mode` 1 y 2 como modo BANCO, y controlsd exige autorizacion
antes de dejar que el override de torque toque el volante. Pero "armar el banco" y "elegir
el modo delante del coche" NO son lo mismo:

  OrbitBenchArmed     habilita los verbos FISICOS POR MQTT (torque_mode, steering_pulse,
                      physical_control) a cualquiera que publique en el broker.
  OrbitSteerModeLocal dice que alguien ha elegido el modo EN ESTA PANTALLA, estando
                      delante. No habilita nada remoto.

Una version anterior de este fichero armaba el BANCO para devolverle la funcion al
selector, y renovaba ese armado indefinidamente mientras el modo siguiera elegido. La
consecuencia, que no se declaro: con el conductor en modo JETSON, cualquiera que conociera
el dongle_id podia mandar `torque_mode {mode: 2}` y poner el volante al tope. El "motivo en
RAM" no protegia de nada, porque solo gobernaba el acto de armar y no el gate del router,
que solo mira el param.

Ahora la seleccion presencial escribe su propio flag. El camino MQTT sigue encontrandose el
banco desarmado y sigue mandando el modelo Comma.

El vigilante (`BenchGuard`, selfdrive/ui/widgets/orbit_mando.py) retira el flag al volver a
COMMA, al pasar a offroad, y -- en TEST MAX -- al superar los 5 km/h, devolviendo ademas el
selector a COMMA.
"""
import time

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui.widgets import orbit_mando as mando
from openpilot.selfdrive.ui.widgets import orbit_ajustes as ajustes
# Las constantes del selector viven en orbit_ajustes (las comparte la UI del comma 4); se
# reexportan aqui para quien las importe desde este modulo (advanced_settings).
from openpilot.selfdrive.ui.widgets.orbit_ajustes import (  # noqa: F401
  MODE_BUTTONS, INDEX_TO_MODE, MODE_TO_INDEX, MODE_NAMES, MODES_REQUIRING_BENCH, TORQUE_STALE_SECONDS,
)
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.sunnypilot.widgets.list_view import multiple_button_item_sp
from openpilot.system.ui.widgets import DialogResult
from openpilot.system.ui.widgets.confirm_dialog import ConfirmDialog

POLL_INTERVAL_S = 0.5


def read_mode() -> int:
  return mando.read_steer_mode()


class SteerModeRows:
  """Filas del selector de volante, para incrustar en un Scroller ajeno.

  El contenedor llama a `poll()` desde su `_render` (hilo de UI) para resincronizar el
  selector con el valor real del param: el modo tambien lo puede cambiar la app.
  """

  def __init__(self):
    self._last_poll = 0.0
    # Texto de estado cacheado. El lambda del titulo lo llama el render (y la medida de
    # texto) en CADA frame: recalcularlo ahi seria abrir Params 60 veces por segundo.
    self._status_cached = ""

    # El estado vivo va en el TITULO del selector y no en una fila aparte: son 188 px de
    # scroll menos y el dato queda pegado al control que lo produce.
    self._selector = multiple_button_item_sp(
      title=lambda: tr("Control del volante") + "  -  " + self._status_cached,
      description=lambda: tr("De donde sale el torque que se aplica al volante cuando el control " +
                             "lateral esta activo. Elegir JETSON o TEST MAX aqui es una autorizacion " +
                             "presencial: no arma el banco y dura hasta volver a COMMA o pasar a offroad."),
      buttons=MODE_BUTTONS,
      button_width=320,
      selected_index=MODE_TO_INDEX.get(read_mode(), 0),
      callback=self._on_mode_button,
    )

  @property
  def items(self) -> list:
    return [self._selector]

  def poll(self):
    now = time.monotonic()
    if now - self._last_poll < POLL_INTERVAL_S:
      return
    self._last_poll = now
    self.sync()

  def sync(self):
    """Resincroniza el boton marcado y el texto de estado con los Params reales."""
    self._selector.action_item.set_selected_button(MODE_TO_INDEX.get(read_mode(), 0))
    self._status_cached = self._status_text()

  # ------------------------------------------------------------------- estado
  def _status_text(self) -> str:
    """Estado REAL, no el boton pulsado (ver ajustes.texto_estado_volante)."""
    mode = read_mode()
    tgt = ui_state.params.get(ajustes.PARAM_APPLY_TARGET) if mode == 3 else None
    necesita = mode in MODES_REQUIRING_BENCH
    armado, restante = mando.bench_snapshot() if necesita else (False, 0.0)
    local = necesita and mando.steer_local_activo()
    return ajustes.texto_estado_volante(mode, tgt, armado, restante, local)

  # -------------------------------------------------------------------- accion
  def _on_mode_button(self, index: int):
    target_mode = INDEX_TO_MODE.get(index, 0)
    current = read_mode()

    # COMMA+JETSON siempre vuelve a preguntar el sub-objetivo (igual que el Qt viejo),
    # asi que no se hace early-return con el.
    if target_mode == current and target_mode != 3:
      return

    if target_mode == 3 and current == 3:
      self._ask_obstacle_apply_target()
      return
    msg, confirm_text = ajustes.confirmacion_modo(target_mode)

    def on_result(result: DialogResult):
      # Siempre resincronizar con el estado real (cubre el cancelar).
      self.sync()
      if result != DialogResult.CONFIRM:
        return
      if target_mode == 3:
        self._ask_obstacle_apply_target()
      else:
        self._commit_mode(target_mode)

    gui_app.push_widget(ConfirmDialog(msg, confirm_text, tr("Cancelar"), callback=on_result))

  def _ask_obstacle_apply_target(self):
    """Como debe esquivar la Jetson: 'curvature' (recomendado) o 'torque' (beta).

    ConfirmDialog solo ofrece dos botones, asi que es un flujo en dos pasos:
      paso 1: "esquive en CURVATURA?"  CONFIRM -> curvature ; CANCEL -> paso 2
      paso 2: "usar TORQUE (beta)?"    CONFIRM -> torque    ; CANCEL -> abortar
    """
    def commit_target(target: str):
      ajustes.fijar_objetivo_esquive(ui_state.params, target)
      self._commit_mode(3)

    def ask_torque():
      msg, confirm_text = ajustes.texto_esquive_torque()

      def on_torque(result: DialogResult):
        self.sync()
        if result == DialogResult.CONFIRM:
          commit_target("torque")

      gui_app.push_widget(ConfirmDialog(msg, confirm_text, tr("Cancelar"), callback=on_torque))

    msg, curvatura, otra = ajustes.texto_esquive_curvatura()

    def on_curvature(result: DialogResult):
      self.sync()
      if result == DialogResult.CONFIRM:
        commit_target("curvature")
      elif result == DialogResult.CANCEL:
        ask_torque()

    gui_app.push_widget(ConfirmDialog(msg, curvatura, otra, callback=on_curvature))

  def _commit_mode(self, mode: int):
    # Modo (INT, bloqueante) + autorizacion presencial + buzon MQTT: ver
    # ajustes.aplicar_modo_volante, que es lo que llama tambien el comma 4.
    ajustes.aplicar_modo_volante(ui_state.params, mode)
    self.sync()
