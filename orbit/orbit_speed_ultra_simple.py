#!/usr/bin/env python3
"""Consumidor del verbo `cruise_delta` (seccion 6: modo COPILOTO, gates ENGAGED y
LONG_ACTIVE, TTL 2 s, +-5 km/h por orden y +-20 km/h por minuto).

Unifica los cuatro caminos de velocidad que habia en v1 (speed_up, speed_down, control,
speed). El emisor solo pone en pie los flags; la decision de si el coche obedece se toma
AQUI, con el plano de estado v2 delante y reevaluando el gate en el mismo ciclo en que
se actua (seccion 4.2: el GateMonitor filtra pronto, el consumidor decide tarde).

QUE CAMBIO Y POR QUE
- Antes bastaba con que el crucero estuviera "enabled" O el longitudinal activo
  ("mas flexible para simuladores"). Esa disyuncion permitia mover la consigna con
  openpilot sin control longitudinal: la consigna cambiaba y el coche no la seguia, y el
  siguiente enganche arrancaba con la velocidad que alguien puso por MQTT hace un rato.
  Ahora se exigen las DOS cosas, que es lo que dice la tabla de verbos.
- Antes el incremento por defecto era 10 km/h y se admitia hasta 50 por orden. El
  contrato dice +-5. Se acota a 5.
- Antes no habia presupuesto por minuto: el tope por orden no sirve de nada si se pueden
  encadenar veinte ordenes de +5 en dos segundos. Ahora hay una ventana deslizante de
  60 s con 20 km/h de presupuesto, medida en reloj MONOTONO.
- Antes no habia ninguna nocion de autoridad: los flags eran suficientes. Ahora hace
  falta la ventana viva del plano de estado (deadman, seccion 5).
- Antes el ACK decia `applied` en cuanto el router escribia el flag, y el rechazo de este
  modulo solo llegaba al log de card: 17 de 78 `applied` medidos en el coche no movieron
  la consigna. Ahora el veredicto (veredicto()) viaja al router por OrbitCmdResult y es el
  que cierra el ACK, igual que el de desire_helper para lane_change.
"""
import time

from openpilot.common.params import Params
from openpilot.orbit.command_spec import CONTRACT_VERSION, Gate, Mode, Phase, ahora_epoch_ms

try:
  from openpilot.selfdrive.car.cruise import V_CRUISE_MAX, V_CRUISE_MIN, V_CRUISE_UNSET
except Exception:  # pragma: no cover - el emisor puede importar este modulo sin cereal
  V_CRUISE_MIN, V_CRUISE_MAX, V_CRUISE_UNSET = 8, 145, 255

# Variables globales para comandos de velocidad (las pone en pie el hilo del mando; el
# consumidor las lee y las limpia SIEMPRE, se ejecute o no la orden).
orbit_speed_increase = False
orbit_speed_decrease = False

# Contrato del verbo (command_spec.COMMANDS["cruise_delta"]).
VERB = "cruise_delta"
MODE_MIN = Mode.COPILOT
GATES_REQ = Gate.ENGAGED | Gate.LONG_ACTIVE
DELTA_MAX_KPH = 5.0            # por orden
PRESUPUESTO_KPH = 20.0         # por ventana
VENTANA_S = 60.0

# Cuanto se espera, como mucho, a que el plano de estado publique la ventana de la orden
# cuyo flag ya se leyo (ver process_speed_commands). Tres ticks del plano, que va a 10 Hz.
ESPERA_PLANO_S = 0.3


class OrbitSpeedUltraSimple:
  """Aplica cruise_delta sobre VCruiseHelper con limite por orden y por minuto."""

  def __init__(self):
    self.params = Params()
    self.speed_increment_default = DELTA_MAX_KPH
    self.last_speed_command = 0.0
    # Presupuesto: lista de (instante_monotono, kph_gastados) dentro de la ventana.
    self._gasto = []
    self._ultimo_motivo = ""
    self._flag_desde = None      # instante MONOTONO en que se vio el flag pendiente
    self._detalle = ""

  def get_speed_increment(self):
    """Incremento por orden, acotado al contrato (+-5 km/h).

    orbit_speed_increment es un param tipado FLOAT: get() devuelve float o None, nunca
    str/bytes. Se acota a [1, DELTA_MAX_KPH]: un valor viejo de 10 o 15 en disco no puede
    darle a una orden remota mas autoridad de la que declara la tabla de verbos.
    """
    try:
      increment = self.params.get("orbit_speed_increment", return_default=True)
      if increment is not None:
        return max(1.0, min(DELTA_MAX_KPH, float(increment)))
    except (ValueError, TypeError):
      pass  # error de lectura -> valor por defecto
    return self.speed_increment_default

  def _presupuesto_restante(self, now_mono: float) -> float:
    """km/h que aun se pueden mover en la ventana deslizante de 60 s."""
    self._gasto = [(t, v) for (t, v) in self._gasto if (now_mono - t) < VENTANA_S]
    return max(0.0, PRESUPUESTO_KPH - sum(v for _, v in self._gasto))

  def process_speed_commands(self, car_control, car_state, v_cruise_helper,
                             autoridad=None, now_mono: float | None = None) -> str:
    """Procesa un cruise_delta pendiente. Devuelve el motivo (codigo de la seccion 3.3), o
    "" si todavia no hay nada decidido (sin flag, o esperando al plano).

    `autoridad` es el OrbitAuthority del ciclo (orbit_control_ultra_simple). Sin el no se
    mueve nada: el plano de estado es quien dice que hay una orden viva y en que modo.
    """
    global orbit_speed_increase, orbit_speed_decrease

    now_mono = time.monotonic() if now_mono is None else now_mono
    pendiente = bool(orbit_speed_increase) or bool(orbit_speed_decrease)
    if not pendiente:
      self._flag_desde = None
      return ""

    # ESPERA AL PLANO. El flag (Params, leido a 10 Hz) y el plano de estado (cereal, 10 Hz)
    # llegan por caminos distintos, y el flag puede adelantarse a la foto que trae la
    # ventana de SU orden. Decidir con la foto anterior, con la ventana de la orden previa
    # ya cerrada, daba EXPIRED y la consigna no se movia. Medido en el coche: los 17
    # `applied` mudos de 78 llegaron sin otra orden en los 2 s previos (1 de cada 4 de
    # esas), y ninguna de las 13 que llegaron con esa ventana abierta. Se espera a ver la
    # ventana abierta, como mucho ESPERA_PLANO_S, y pasado ese plazo se decide igual con lo
    # que haya: la ventana solo dice CUANDO decidir, el permiso sigue siendo entero de
    # allows(). No se espera a activeVerb/cmdId: el router no los conserva para este verbo
    # (command_spec, conserva_plano).
    if self._flag_desde is None:
      self._flag_desde = now_mono
    abierta = autoridad is not None and autoridad.window_open(now_mono)
    if not abierta and (now_mono - self._flag_desde) < ESPERA_PLANO_S:
      return ""
    self._flag_desde = None

    # Se consume SIEMPRE, se ejecute o no: un flag que sobrevive a un rechazo es una
    # orden que se ejecuta sola en cuanto los gates se ponen verdes un rato despues,
    # que es justo lo que el TTL existe para impedir.
    subir = bool(orbit_speed_increase)
    orbit_speed_increase = False
    orbit_speed_decrease = False

    self._detalle = ""
    motivo = self._evaluar(car_control, car_state, v_cruise_helper, autoridad, now_mono, subir)
    self._ultimo_motivo = motivo
    return motivo

  def veredicto(self, motivo: str, now_mono: float) -> dict:
    """Resultado REAL de la ultima orden decidida, para OrbitCmdResult.

    Misma forma que el de desire_helper. `applied` solo si la consigna se movio de verdad;
    cualquier otro motivo es `rejected`, que el router publica como `failed` porque ya habia
    anunciado `executing`. Solo tipos nativos: card lo serializa y lo escribe con put().

    Va SIN id: el router lo correlaciona por verbo con el cruise_delta pendiente mas
    antiguo, que es el de este flag (la app no manda otra pulsacion hasta cerrar esta).
    La app compara `detail` con estos textos (presupuesto / limite / el de por defecto):
    cambiarlos es cambiar lo que lee el conductor.
    """
    return {
      "v": CONTRACT_VERSION,
      "verb": VERB,
      "id": "",
      "phase": Phase.APPLIED if motivo == "OK" else Phase.REJECTED,
      "reason": motivo,
      "detail": self._detalle or "precondicion en rojo al aplicar",
      "ts_ms": ahora_epoch_ms(),
      "mono_ms": int(now_mono * 1000),
    }

  def _evaluar(self, car_control, car_state, v_cruise_helper, autoridad, now_mono, subir) -> str:
    if autoridad is None:
      return "LINK"

    ok, motivo = autoridad.allows(VERB, MODE_MIN, int(GATES_REQ), now_mono)
    if not ok:
      return motivo

    # REEVALUACION LOCAL en el ciclo en que se actua (defensa en profundidad): la mascara
    # de gates viene del publicador de 10 Hz y puede tener hasta 100 ms; el desenganche
    # ocurre en un ciclo.
    engaged = bool(getattr(car_control, "enabled", False))
    long_active = bool(getattr(car_control, "longActive", False))
    if not engaged:
      return "GATE_ENGAGED"
    if not long_active:
      return "GATE_LONG_ACTIVE"

    # El conductor manda: si esta pisando algo, la orden remota no toca la consigna.
    if getattr(car_state, "gasPressed", False) or getattr(car_state, "brakePressed", False):
      return "GATE_DRIVER_IDLE"

    if not getattr(v_cruise_helper, "v_cruise_initialized", False) or \
       v_cruise_helper.v_cruise_kph == V_CRUISE_UNSET:
      # Sin consigna inicializada no hay nada que incrementar. Antes se inventaba una
      # (vEgo, o 40 km/h por defecto), es decir una orden de "+5" acababa FIJANDO una
      # velocidad de crucero que nadie habia puesto.
      return "GATE_ENGAGED"

    incremento = self.get_speed_increment()
    restante = self._presupuesto_restante(now_mono)
    if restante <= 0.0:
      self._detalle = f"presupuesto de {PRESUPUESTO_KPH:g} km/h por minuto agotado"
      return "RANGE"
    incremento = min(incremento, restante)

    actual = float(v_cruise_helper.v_cruise_kph)
    nueva = actual + incremento if subir else actual - incremento
    nueva = max(float(V_CRUISE_MIN), min(float(V_CRUISE_MAX), nueva))

    gastado = abs(nueva - actual)
    if gastado <= 0.0:
      self._detalle = f"la consigna ya esta en el limite ({actual:g} km/h)"
      return "RANGE"  # ya estaba en el tope: no hay cambio que aplicar

    v_cruise_helper.v_cruise_kph = nueva
    v_cruise_helper.v_cruise_cluster_kph = nueva
    self._gasto.append((now_mono, gastado))
    self.last_speed_command = now_mono
    self._detalle = f"consigna {actual:g} -> {nueva:g} km/h"
    return "OK"


# Instancia global del controlador de velocidad
orbit_speed_ultra_simple = OrbitSpeedUltraSimple()
