#!/usr/bin/env python3
import json
import os
import time
import threading
from collections import deque

import cereal.messaging as messaging

from cereal import car, log, custom

from openpilot.common.params import Params
from openpilot.common.realtime import config_realtime_process, Priority, Ratekeeper
from openpilot.common.swaglog import cloudlog, ForwardingHandler

from opendbc.car import DT_CTRL, structs
from opendbc.car.can_definitions import CanData, CanRecvCallable, CanSendCallable
from opendbc.car.carlog import carlog
from opendbc.car.fw_versions import ObdCallback
from opendbc.car.car_helpers import get_car, interfaces
from opendbc.car.interfaces import CarInterfaceBase, RadarInterfaceBase
from openpilot.selfdrive.pandad import can_capnp_to_list, can_list_to_can_capnp
from openpilot.selfdrive.car.cruise import VCruiseHelper
from openpilot.selfdrive.car.helpers import convert_carControlSP, convert_to_capnp

from openpilot.sunnypilot.mads.helpers import set_alternative_experience, set_car_specific_params
from openpilot.sunnypilot.selfdrive.car import interfaces as sunnypilot_interfaces

# [Orbit] Mando remoto v2: plano de estado (cereal orbitCommandState, seccion 5 del diseno).
# card es el consumidor del verbo `cruise_delta` porque VCruiseHelper vive aqui. Import
# defensivo: sin orbit/ el coche funciona igual, simplemente no hay mando remoto.
try:
  from openpilot.orbit.orbit_control_ultra_simple import OrbitCommandLink
  _ORBIT_MANDO = True
except Exception:
  OrbitCommandLink = None
  _ORBIT_MANDO = False

REPLAY = "REPLAY" in os.environ

EventName = log.OnroadEvent.EventName

# forward
carlog.addHandler(ForwardingHandler(cloudlog))


def obd_callback(params: Params) -> ObdCallback:
  def set_obd_multiplexing(obd_multiplexing: bool):
    if params.get_bool("ObdMultiplexingEnabled") != obd_multiplexing:
      cloudlog.warning(f"Setting OBD multiplexing to {obd_multiplexing}")
      params.remove("ObdMultiplexingChanged")
      params.put_bool("ObdMultiplexingEnabled", obd_multiplexing, block=True)
      params.get_bool("ObdMultiplexingChanged", block=True)
      cloudlog.warning("OBD multiplexing set successfully")
  return set_obd_multiplexing


def can_comm_callbacks(logcan: messaging.SubSocket, sendcan: messaging.PubSocket) -> tuple[CanRecvCallable, CanSendCallable]:
  def can_recv(wait_for_one: bool = False) -> list[list[CanData]]:
    """
    wait_for_one: wait the normal logcan socket timeout for a CAN packet, may return empty list if nothing comes

    Returns: CAN packets comprised of CanData objects for easy access
    """
    ret = []
    for can in messaging.drain_sock(logcan, wait_for_one=wait_for_one):
      ret.append([CanData(msg.address, msg.dat, msg.src) for msg in can.can])
    return ret

  def can_send(msgs: list[CanData]) -> None:
    sendcan.send(can_list_to_can_capnp(msgs, msgtype='sendcan'))

  return can_recv, can_send


class Car:
  CI: CarInterfaceBase
  RI: RadarInterfaceBase
  CP: car.CarParams
  CP_SP: structs.CarParamsSP
  CP_SP_capnp: custom.CarParamsSP

  def __init__(self, CI=None, RI=None) -> None:
    self.can_sock = messaging.sub_sock('can', timeout=20)
    self.sm = messaging.SubMaster(['pandaStates', 'carControl', 'onroadEvents'] + ['carControlSP', 'longitudinalPlanSP'])
    self.pm = messaging.PubMaster(['sendcan', 'carState', 'carParams', 'carOutput', 'liveTracks'] + ['carParamsSP', 'carStateSP'])

    self.can_rcv_cum_timeout_counter = 0

    self.CC_prev = car.CarControl.new_message()
    self.CS_prev = car.CarState.new_message()
    self.CS_SP_prev = custom.CarStateSP.new_message()
    self.initialized_prev = False

    self.last_actuators_output = structs.CarControl.Actuators()

    self.params = Params()

    self.can_callbacks = can_comm_callbacks(self.can_sock, self.pm.sock['sendcan'])

    is_release = False  # self.params.get_bool("IsReleaseBranch")
    is_release_sp = self.params.get_bool("IsReleaseSpBranch")

    if CI is None:
      # wait for one pandaState and one CAN packet
      print("Waiting for CAN messages...")
      while True:
        can = messaging.recv_one_retry(self.can_sock)
        if len(can.can) > 0:
          break

      alpha_long_allowed = self.params.get_bool("AlphaLongitudinalEnabled")

      cached_params = None
      cached_params_raw = self.params.get("CarParamsCache")
      if cached_params_raw is not None:
        with car.CarParams.from_bytes(cached_params_raw) as _cached_params:
          cached_params = _cached_params

      fixed_fingerprint = (self.params.get("CarPlatformBundle") or {}).get("platform", None)
      init_params_list_sp = sunnypilot_interfaces.initialize_params(self.params)

      self.CI = get_car(*self.can_callbacks, obd_callback(self.params), alpha_long_allowed, is_release, cached_params,
                        fixed_fingerprint, init_params_list_sp, is_release_sp)
      sunnypilot_interfaces.setup_interfaces(self.CI, self.params)
      self.RI = interfaces[self.CI.CP.carFingerprint].RadarInterface(self.CI.CP, self.CI.CP_SP)
      self.CP = self.CI.CP
      self.CP_SP = self.CI.CP_SP

      # continue onto next fingerprinting step in pandad
      self.params.put_bool("FirmwareQueryDone", True, block=True)
    else:
      self.CI, self.CP, self.CP_SP = CI, CI.CP, CI.CP_SP
      self.RI = RI

    self.CP.alternativeExperience = 0
    # mads
    set_alternative_experience(self.CP, self.CP_SP, self.params)
    set_car_specific_params(self.CP, self.CP_SP, self.params)

    # Dynamic Experimental Control
    self.dynamic_experimental_control = self.params.get_bool("DynamicExperimentalControl")

    openpilot_enabled_toggle = self.params.get_bool("OpenpilotEnabledToggle")
    controller_available = self.CI.CC is not None and openpilot_enabled_toggle and not self.CP.dashcamOnly
    self.CP.passive = not controller_available or self.CP.dashcamOnly
    if self.CP.passive:
      safety_config = structs.CarParams.SafetyConfig()
      safety_config.safetyModel = structs.CarParams.SafetyModel.noOutput
      self.CP.safetyConfigs = [safety_config]

    if self.CP.secOcRequired:
      # Copy user key if available
      try:
        with open("/cache/params/SecOCKey") as f:
          user_key = f.readline().strip()
          if len(user_key) == 32:
            self.params.put("SecOCKey", user_key, block=True)
      except Exception:
        pass

      secoc_key = self.params.get("SecOCKey")
      if secoc_key is not None:
        saved_secoc_key = bytes.fromhex(secoc_key.strip())
        if len(saved_secoc_key) == 16:
          self.CP.secOcKeyAvailable = True
          self.CI.CS.secoc_key = saved_secoc_key
          if controller_available:
            self.CI.CC.secoc_key = saved_secoc_key
        else:
          cloudlog.warning("Saved SecOC key is invalid")

    # Write previous route's CarParams
    prev_cp = self.params.get("CarParamsPersistent")
    if prev_cp is not None:
      self.params.put("CarParamsPrevRoute", prev_cp, block=True)

    # Write CarParams for controls and radard
    cp_bytes = self.CP.to_bytes()
    self.params.put("CarParams", cp_bytes, block=True)
    self.params.put("CarParamsCache", cp_bytes)
    self.params.put("CarParamsPersistent", cp_bytes)

    # Write CarParamsSP for controls
    # convert to pycapnp representation for caching and logging
    self.CP_SP_capnp = convert_to_capnp(self.CP_SP)
    cp_sp_bytes = self.CP_SP_capnp.to_bytes()
    self.params.put("CarParamsSP", cp_sp_bytes, block=True)
    self.params.put("CarParamsSPCache", cp_sp_bytes)
    self.params.put("CarParamsSPPersistent", cp_sp_bytes)

    self.v_cruise_helper = VCruiseHelper(self.CP, self.CP_SP)

    self.is_metric = self.params.get_bool("IsMetric")
    self.experimental_mode = self.params.get_bool("ExperimentalMode")

    # [Orbit] Mando remoto v2. El lector del plano de estado crea su propio SubMaster de un
    # solo servicio de forma perezosa, en el primer poll: msgq NO es thread-safe y el poll
    # tiene que ocurrir en el hilo de card (state_update), no en params_thread.
    self._orbit_link = OrbitCommandLink(etiqueta="card") if _ORBIT_MANDO else None
    self._orbit_auth = None
    self._orbit_now_mono = 0.0
    self._orbit_speed_mod = None      # modulo orbit_speed_ultra_simple, cacheado
    self._orbit_speed_roto = False    # el import fallo: no reintentarlo en cada ciclo
    self._orbit_ultimo_error_mono = 0.0
    # Veredictos de cruise_delta: los encola state_update y los escribe params_thread, al
    # que se despierta en cuanto hay uno (sin esperar a su siguiente vuelta de 100 ms).
    self._orbit_veredictos = deque(maxlen=8)
    self._orbit_despertar = threading.Event()
    self._orbit_aviso_veredicto = False

    # card is driven by can recv, expected at 100Hz
    self.rk = Ratekeeper(100, print_delay_threshold=None)

    # log fingerprint in sentry
    sunnypilot_interfaces.log_fingerprint(self.CP)

  def _orbit_poll(self) -> None:
    """Lee el plano de estado del mando UNA vez por ciclo. Nunca lanza.

    Se llama desde state_update, es decir desde el hilo de card: msgq no es thread-safe y
    este SubMaster se crea perezosamente en el primer poll, en el hilo que lo usa.
    """
    self._orbit_now_mono = time.monotonic()
    if self._orbit_link is None:
      self._orbit_auth = None
      return
    try:
      self._orbit_auth = self._orbit_link.poll()
    except Exception:
      self._orbit_auth = None

  def _orbit_speed_module(self):
    """Modulo orbit_speed_ultra_simple, importado una sola vez.

    Si el import falla se marca roto y no se reintenta: hacerlo en cada ciclo de 100 Hz
    seria recorrer sys.path cien veces por segundo desde el core de tiempo real.
    """
    if self._orbit_speed_mod is None and not self._orbit_speed_roto:
      try:
        import openpilot.orbit.orbit_speed_ultra_simple as mod
        self._orbit_speed_mod = mod
      except Exception:
        self._orbit_speed_roto = True
    return self._orbit_speed_mod

  def _orbit_leer_flags_velocidad(self) -> None:
    """Consume los flags one-shot de `cruise_delta` desde el hilo de params (10 Hz, NO-RT).

    Se limpian SIEMPRE, se ejecute la orden o no: un flag que sobrevive a un rechazo es una
    orden que se ejecuta sola en cuanto los gates se ponen verdes un rato despues, que es
    justo lo que el TTL existe para impedir.
    """
    subir = self.params.get_bool("orbit_speed_increase")
    bajar = self.params.get_bool("orbit_speed_decrease")
    if not (subir or bajar):
      return
    # SE BORRA la clave en vez de escribir False. put_bool(block=False) encola en un hilo
    # async y la escritura puede aterrizar DESPUES de que el router arme la siguiente orden:
    # ese False tardio se comeria un cruise_delta que el router ya dio por aceptado.
    # Params.remove es sincrono y get_bool sobre una clave inexistente devuelve False.
    self.params.remove("orbit_speed_increase")
    self.params.remove("orbit_speed_decrease")
    mod = self._orbit_speed_module()
    if mod is None:
      return
    mod.orbit_speed_increase = bool(subir)
    mod.orbit_speed_decrease = bool(bajar)

  def _orbit_publicar_veredicto(self) -> None:
    """Escribe en OrbitCmdResult el veredicto de cruise_delta que encolo state_update.

    Aqui y no en el bucle de 100 Hz por lo mismo que los flags: es disco. put() sin block
    solo encola en el hilo de escritura de Params, asi que este hilo no espera a un fsync
    (que puede pasar del segundo) antes de leer el siguiente flag. La clave es STRING: va
    el JSON como str, nunca el dict (put() lanza TypeError con un tipo que no es el suyo).

    UNO por vuelta y SOLO con el hueco vacio: OrbitCmdResult es un solo hueco que el router
    lee y borra (cada 20 ms mientras espera) y que tambien escribe desire_helper. Escribir
    encima de un veredicto sin leer lo perdia, y su orden acababa en NO_RESULT con la
    consigna ya movida; esperando, sale en la vuelta siguiente. Se saca de la cola ANTES de
    escribir: si put() falla, ese veredicto se pierde (NO_RESULT, que es honesto) en vez de
    reintentarse y acabar, ya rancio, cerrando por verbo una pulsacion posterior.
    """
    if not self._orbit_veredictos or self.params.get("OrbitCmdResult"):
      return
    veredicto = self._orbit_veredictos.popleft()
    self.params.put("OrbitCmdResult", json.dumps(veredicto, separators=(",", ":")))

  def _orbit_cruise_delta(self, CS: car.CarState) -> None:
    """[Orbit] VERBO `cruise_delta` (seccion 6): sube o baja la consigna de crucero por orden
    remota. Vive aqui porque VCruiseHelper se movio a card.py.

    QUE CAMBIA RESPECTO A LO ANTERIOR
     * Los flags ya no se leen ni se limpian AQUI: eran cuatro accesos a /data/params por
       ciclo (dos get_bool + dos put_bool) dentro de un bucle a 100 Hz en SCHED_FIFO sobre
       el core 4. Ahora los atiende params_thread, que corre a 10 Hz y a SCHED_OTHER.
     * Se pasa el carControl REAL en vez de un objeto falso con solo `longActive`. El
       consumidor v2 tambien mira `enabled`, y un objeto sin ese atributo lo daba por
       False: el verbo habria quedado rechazado SIEMPRE con GATE_ENGAGED.
     * Hace falta autoridad viva del plano de estado (modo copiloto, gates ENGAGED y
       LONG_ACTIVE y deadman sin vencer). El propio modulo reevalua los gates con este
       carControl y este carState, en el ciclo en que actua.
     * El veredicto (aplicada, o rechazada con su motivo) se ENCOLA y lo escribe
       params_thread: el router cierra el ACK con el. Antes el ACK decia `applied` en cuanto
       habia un flag en disco y el rechazo solo llegaba a este log.
    """
    try:
      self._orbit_poll()
      mod = self._orbit_speed_module()
      if mod is not None and (mod.orbit_speed_increase or mod.orbit_speed_decrease):
        consumidor = mod.orbit_speed_ultra_simple
        motivo = consumidor.process_speed_commands(
          self.sm['carControl'], CS, self.v_cruise_helper,
          autoridad=self._orbit_auth, now_mono=self._orbit_now_mono)
        if motivo:
          self._orbit_veredictos.append(consumidor.veredicto(motivo, self._orbit_now_mono))
          self._orbit_despertar.set()
        if motivo not in ("", "OK"):
          cloudlog.warning(f"card: [Orbit] cruise_delta rechazado: {motivo}")
    except Exception:
      # Acotado en el tiempo: esto corre a 100 Hz y una excepcion que se repita cada ciclo
      # convertiria el hilo de card en un generador de swaglog.
      if (self._orbit_now_mono - self._orbit_ultimo_error_mono) > 5.0:
        self._orbit_ultimo_error_mono = self._orbit_now_mono
        cloudlog.exception("card: [Orbit] excepcion en cruise_delta (ignorada: no puede tocar el control)")

  def state_update(self) -> tuple[car.CarState, custom.CarStateSP, structs.RadarDataT | None]:
    """carState update loop, driven by can"""

    can_strs = messaging.drain_sock_raw(self.can_sock, wait_for_one=True)
    can_list = can_capnp_to_list(can_strs)

    # Update carState from CAN
    CS, CS_SP = self.CI.update(can_list)
    CS_SP = convert_to_capnp(CS_SP)

    # Update radar tracks from CAN
    RD: structs.RadarDataT | None = self.RI.update(can_list)

    self.sm.update(0)

    can_rcv_valid = len(can_strs) > 0

    # Check for CAN timeout
    if not can_rcv_valid:
      self.can_rcv_cum_timeout_counter += 1

    if can_rcv_valid and REPLAY:
      self.can_log_mono_time = messaging.log_from_bytes(can_strs[0]).logMonoTime

    self.v_cruise_helper.update_speed_limit_assist(self.is_metric, self.sm['longitudinalPlanSP'])
    self.v_cruise_helper.update_v_cruise(CS, self.sm['carControl'].enabled, self.is_metric)
    if self.sm['carControl'].enabled and not self.CC_prev.enabled:
      # Use CarState w/ buttons from the step selfdrived enables on
      self.v_cruise_helper.initialize_v_cruise(self.CS_prev, self.experimental_mode, self.dynamic_experimental_control)

    # [Orbit] VERBO `cruise_delta`: ver _orbit_cruise_delta.
    self._orbit_cruise_delta(CS)

    # TODO: mirror the carState.cruiseState struct?
    CS.vCruise = float(self.v_cruise_helper.v_cruise_kph)
    CS.vCruiseCluster = float(self.v_cruise_helper.v_cruise_cluster_kph)

    return CS, CS_SP, RD

  def state_publish(self, CS: car.CarState, CS_SP: custom.CarStateSP, RD: structs.RadarDataT | None):
    """carState and carParams publish loop"""

    # carParams - logged every 50 seconds (> 1 per segment)
    if self.sm.frame % int(50. / DT_CTRL) == 0:
      cp_send = messaging.new_message('carParams')
      cp_send.valid = True
      cp_send.carParams = self.CP
      self.pm.send('carParams', cp_send)

    # publish new carOutput
    co_send = messaging.new_message('carOutput')
    co_send.valid = self.sm.all_checks(['carControl'])
    co_send.carOutput.actuatorsOutput = self.last_actuators_output
    self.pm.send('carOutput', co_send)

    # kick off controlsd step while we actuate the latest carControl packet
    cs_send = messaging.new_message('carState')
    cs_send.valid = CS.canValid
    cs_send.carState = CS
    cs_send.carState.canErrorCounter = self.can_rcv_cum_timeout_counter
    cs_send.carState.cumLagMs = -self.rk.remaining * 1000.
    self.pm.send('carState', cs_send)

    if RD is not None:
      tracks_msg = messaging.new_message('liveTracks')
      tracks_msg.valid = not any(RD.errors.to_dict().values())
      tracks_msg.liveTracks = RD
      self.pm.send('liveTracks', tracks_msg)

    # carParamsSP - logged every 50 seconds (> 1 per segment)
    if self.sm.frame % int(50. / DT_CTRL) == 0:
      cp_sp_send = messaging.new_message('carParamsSP')
      cp_sp_send.valid = True
      cp_sp_send.carParamsSP = self.CP_SP_capnp
      self.pm.send('carParamsSP', cp_sp_send)

    cs_sp_send = messaging.new_message('carStateSP')
    cs_sp_send.valid = CS.canValid
    cs_sp_send.carStateSP = CS_SP
    self.pm.send('carStateSP', cs_sp_send)

  def controls_update(self, CS: car.CarState, CC: car.CarControl, CC_SP: custom.CarControlSP):
    """control update loop, driven by carControl"""

    if not self.initialized_prev:
      # Initialize CarInterface, once controls are ready
      # TODO: this can make us miss at least a few cycles when doing an ECU knockout
      self.CI.init(self.CP, self.CP_SP, *self.can_callbacks)
      # signal pandad to switch to car safety mode
      self.params.put_bool("ControlsReady", True)

    if self.sm.all_alive(['carControl']):
      # send car controls over can
      now_nanos = self.can_log_mono_time if REPLAY else int(time.monotonic() * 1e9)
      self.last_actuators_output, can_sends = self.CI.apply(CC, convert_carControlSP(CC_SP), now_nanos)
      self.pm.send('sendcan', can_list_to_can_capnp(can_sends, msgtype='sendcan', valid=CS.canValid))

      self.CC_prev = CC

  def step(self):
    CS, CS_SP, RD = self.state_update()

    self.state_publish(CS, CS_SP, RD)

    initialized = (not any(e.name == EventName.selfdriveInitializing for e in self.sm['onroadEvents']) and
                   self.sm.seen['onroadEvents'])
    if not self.CP.passive and initialized:
      self.controls_update(CS, self.sm['carControl'], self.sm['carControlSP'])

    self.initialized_prev = initialized
    self.CS_prev = CS
    self.CS_SP_prev = CS_SP

  def params_thread(self, evt):
    while not evt.is_set():
      self.is_metric = self.params.get_bool("IsMetric")
      self.experimental_mode = self.params.get_bool("ExperimentalMode") and self.CP.openpilotLongitudinalControl

      # sunnypilot
      self.dynamic_experimental_control = self.params.get_bool("DynamicExperimentalControl")
      self.v_cruise_helper.read_custom_set_speed_params()

      # [Orbit] Flags one-shot de `cruise_delta`. Se leen y se limpian AQUI (SCHED_OTHER,
      # 10 Hz) y no en el bucle de 100 Hz: hacerlo alli eran dos open()+read() de
      # /data/params por cada trama CAN, mas dos put_bool cuyo hilo async hereda la
      # prioridad FIFO del core 4 y mete fsync en el camino de tiempo real. Este hilo
      # tampoco puede tocar cereal (msgq no es thread-safe): solo pone el flag en RAM y
      # quien decide y actua es state_update, en el hilo de card.
      try:
        self._orbit_leer_flags_velocidad()
      except Exception:
        pass
      # [Orbit] Veredicto de cruise_delta hacia el router. Try propio: si fallara la
      # escritura, los flags se tienen que seguir consumiendo igual.
      try:
        self._orbit_publicar_veredicto()
      except Exception:
        if not self._orbit_aviso_veredicto:
          self._orbit_aviso_veredicto = True
          cloudlog.exception("card: [Orbit] no se pudo escribir OrbitCmdResult: cruise_delta cerrara con NO_RESULT")

      # 0.1 s, o menos si state_update encola un veredicto: esperar a la vuelta siguiente
      # sumaba hasta 100 ms entre `executing` y `applied`, y la app solo pinta una fase
      # intermedia si dura 700 ms (kRetencionIntermedia): cada ms de aqui es margen que se
      # come el jitter de red antes de que asome "En curso" entre "Enviando" y "Hecho".
      if self._orbit_despertar.wait(0.1):
        self._orbit_despertar.clear()

  def card_thread(self):
    e = threading.Event()
    t = threading.Thread(target=self.params_thread, args=(e, ))
    try:
      t.start()
      while True:
        self.step()
        self.rk.monitor_time()
    finally:
      e.set()
      t.join()


def main():
  config_realtime_process(4, Priority.CTRL_HIGH)
  car = Car()
  car.card_thread()


if __name__ == "__main__":
  main()
