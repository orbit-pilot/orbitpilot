"""
Vigilante de ordenes remotas ORBIT para la UI mici (comma 4). NO dibuja.

Por que va aparte del overlay: en mici la vista onroad NO se renderiza mientras el
conductor esta en home, en alertas o en ajustes (el Scroller se salta las paginas fuera
de pantalla), asi que latchear flancos dentro de render() -- como hace el overlay de la UI
grande -- pierde ordenes. `update()` se llama desde un tick del nav stack
(MiciMainLayout._handle_transitions), que corre en CADA frame haya lo que haya encima.

Disparadores, de mas fiable a menos:
  1. `orbitCommandState.seq` (cereal, ya en RAM): solo avanza con ordenes que pasaron modo
     y gates y van a ejecutarse. Una ventana de actuador NUEVA (`deadline_mono`) cuenta
     igual, por si un seq se repitiera entre fuentes. Da la pildora generica ORDEN REMOTA.
  2. Flancos de los Params que escribe orbit/mqtt_comandos.py: dan la etiqueta concreta.
     ForceLaneChange* vive como mucho 0.2 s (desire_helper ORBIT_LC_FLAG_TTL_S), por eso
     se sondean a 20 Hz y solo onroad; offroad no hay orden actuadora que pase los gates.

FRENADO REMOTO no se latchea: es `brutebreak_active` Y la ventana del actuador abierta.
Si controlsd rechaza la frenada (gates) borra el flag en <= 0.35 s y la banda se va con
el; la UI grande la dejaba 3 s en pantalla sin frenar nada.
"""
import time

from openpilot.selfdrive.ui.widgets.orbit_mando import CommandStateView

TOAST_S = 3.0          # lo que dura la pildora tras la orden
PARAMS_POLL_S = 0.05   # 20 Hz: por debajo del TTL de 0.2 s de ForceLaneChange*
MODE_POLL_S = 0.5      # SteerTorqueMode; el estado del esquive va a 20 Hz (controlsd lo sostiene solo 0.3 s)
MISMA_ORDEN_S = 1.0    # un flanco de Params y un avance de seq tan juntos son la misma orden

# Sin el prefijo "ORBIT - " de la UI grande: no cabe en 476 px y el punto cian ya lo dice.
BOOL_LABELS = {
  "ForceLaneChangeLeft": "CARRIL IZQ",
  "ForceLaneChangeRight": "CARRIL DER",
  "orbit_speed_increase": "VELOCIDAD +",
  "orbit_speed_decrease": "VELOCIDAD -",
}
PULSE_PARAM, PULSE_LABEL = "orbit_steering_pulse", "PULSO DIRECCION"
BRAKE_PARAM = "brutebreak_active"
GENERIC_LABEL = "ORDEN REMOTA"
# Si el plano llega a mostrar el verbo (lane_change lo mantiene hasta el veredicto; el resto
# solo microsegundos), la generica se concreta un poco aunque se escapara el flanco de Params.
VERB_LABELS = {"lane_change": "CAMBIO CARRIL", "cruise_delta": "VELOCIDAD", "steering_pulse": PULSE_LABEL}

# Esquive del Jetson (SteerTorqueMode == 3). Flechas ASCII: ninguna fuente Inter tiene U+2190.
DODGE_LABELS = {
  "DODGING_LEFT": ("ESQUIVANDO <-", False),
  "DODGING_RIGHT": ("ESQUIVANDO ->", False),
  "DODGING_HOLD": ("ESQUIVANDO - NEUTRO", False),
  "BSM_BLOCKED_LEFT": ("BSM BLOQUEA <-", True),
  "BSM_BLOCKED_RIGHT": ("BSM BLOQUEA ->", True),
}


class OrbitRemoteWatch:
  def __init__(self, params):
    self._params = params
    self.label = ""
    self.toast_until = 0.0
    self._toast_start = -1e9
    self._specific = False
    self._key: tuple[int, float] | None = None   # (seq, deadline) ya visto; None = sin cebar
    self._prev_bools = dict.fromkeys(BOOL_LABELS, False)
    self._prev_pulse: str | None = None
    self._next_poll = 0.0
    self._next_mode = 0.0
    self._steer_mode = 0
    self._brake_flag = False
    self.brake_active = False
    self.authority = ""          # "" | COPILOTO | MANIOBRA | BANCO
    self.authority_urgent = False
    self.dodge = ""              # etiqueta del esquive; "" = no hay
    self.dodge_bsm = False

  def toast_visible(self, now: float | None = None) -> bool:
    return (time.monotonic() if now is None else now) < self.toast_until

  def _toast(self, label: str, now: float, specific: bool) -> bool:
    misma = now - self._toast_start < MISMA_ORDEN_S
    if misma and self._specific and not specific:
      return False  # el avance de seq de la orden cuyo flanco ya se vio: se queda la etiqueta concreta
    self.label, self._specific = label, specific
    self._toast_start, self.toast_until = now, now + TOAST_S
    return True

  def update(self, view: CommandStateView, started: bool, now: float | None = None) -> bool:
    """Devuelve True si en este tick llego una orden remota nueva."""
    now = time.monotonic() if now is None else now
    nueva = False

    # 1) Plano de estado. El primer valor visto es la linea base: el ultimo seq aplicado
    #    puede ser de hace una hora y no es una orden nueva.
    if view.available:
      key = (view.seq, view.deadline_mono)
      if self._key is not None and key != self._key and (view.seq != self._key[0] or view.deadline_mono > now):
        nueva |= self._toast(VERB_LABELS.get(view.active_verb, GENERIC_LABEL), now, specific=False)
      self._key = key
      mode = view.mode
      banco = view.bench_armed or mode == "bench"
      self.authority = "BANCO" if banco else ("" if mode == "observer" else CommandStateView.MODE_LABELS.get(mode, mode.upper()))
      self.authority_urgent = banco or mode == "maneuver"
    else:
      self._key = None
      self.authority, self.authority_urgent = "", False

    # 2) Params, solo onroad.
    if not started:
      self._prev_bools = dict.fromkeys(BOOL_LABELS, False)
      self._prev_pulse = None
      self._brake_flag = False
      self.dodge, self.dodge_bsm = "", False
    else:
      if now >= self._next_mode:
        self._next_mode = now + MODE_POLL_S
        self._poll_mode()
      if now >= self._next_poll:
        self._next_poll = now + PARAMS_POLL_S
        nueva |= self._poll_params(now)
        self._poll_dodge()

    # Mismo criterio que view.actuator_live, contra el `now` de este tick.
    self.brake_active = started and self._brake_flag and view.available and view.deadline_mono > now
    return nueva

  def _poll_params(self, now: float) -> bool:
    p = self._params
    nueva = False
    try:
      for name, label in BOOL_LABELS.items():
        cur = bool(p.get_bool(name))
        if cur and not self._prev_bools[name]:
          nueva |= self._toast(label, now, specific=True)
        self._prev_bools[name] = cur
      # "direccion:start_ms": un start_ms nuevo es un pulso nuevo. El primero ceba.
      pulse = p.get(PULSE_PARAM) or ""
      if self._prev_pulse is not None and pulse and pulse != self._prev_pulse:
        nueva |= self._toast(PULSE_LABEL, now, specific=True)
      self._prev_pulse = pulse
      self._brake_flag = bool(p.get_bool(BRAKE_PARAM))
    except Exception:
      # Un Param ilegible no puede tirar el bucle de la UI; la banda de freno cae a apagada
      # pero la ventana del actuador (cereal) sigue dando la pildora generica.
      self._brake_flag = False
    return nueva

  def _poll_mode(self) -> None:
    try:
      self._steer_mode = int(self._params.get("SteerTorqueMode") or 0)
    except (ValueError, TypeError):
      self._steer_mode = 0

  def _poll_dodge(self) -> None:
    # Solo en modo 3 (Jetson) se lee el estado; fuera de el no hay esquive que mostrar.
    estado = ""
    if self._steer_mode == 3:
      try:
        estado = self._params.get("JetsonObstacleStatus") or ""
      except (ValueError, TypeError):
        estado = ""
    self.dodge, self.dodge_bsm = DODGE_LABELS.get(estado, ("", False))
