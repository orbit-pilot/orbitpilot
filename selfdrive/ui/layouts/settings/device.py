import os
import math

from cereal import messaging, log
from openpilot.common.basedir import BASEDIR
from openpilot.common.params import Params, UnknownKeyName
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.ui.onroad.driver_camera_dialog import DriverCameraDialog
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui.layouts.onboarding import TrainingGuide
from openpilot.selfdrive.ui.widgets.pairing_dialog import PairingDialog
from openpilot.selfdrive.ui.widgets.orbit_enroll_dialog import OrbitEnrollDialog
from openpilot.selfdrive.ui.widgets.orbit_mando import PARAM_OWNER_ROLE, rol_etiqueta
from openpilot.system.ui.lib.application import FontWeight, gui_app
from openpilot.system.ui.lib.multilang import multilang, tr, tr_noop
from openpilot.system.ui.widgets import Widget, DialogResult
from openpilot.system.ui.widgets.button import ButtonStyle
from openpilot.system.ui.widgets.confirm_dialog import ConfirmDialog, alert_dialog
from openpilot.system.ui.widgets.html_render import HtmlModal
from openpilot.system.ui.widgets.list_view import text_item, button_item, dual_button_item
from openpilot.system.ui.widgets.option_dialog import MultiOptionDialog
from openpilot.system.ui.widgets.scroller_tici import Scroller

if gui_app.sunnypilot_ui():
  from openpilot.system.ui.sunnypilot.widgets.list_view import button_item_sp as button_item

# Description constants
DESCRIPTIONS = {
  'pair_device': tr_noop("Pair your device with comma connect (connect.comma.ai) and claim your comma prime offer."),
  'driver_camera': tr_noop("Preview the driver facing camera to ensure that driver monitoring has good visibility. (vehicle must be off)"),
  'reset_calibration': tr_noop("ORBIT requires the device to be mounted within 4° left or right and within 5° up or 9° down."),
  'review_guide': tr_noop("Review the rules, features, and limitations of ORBIT"),
}


class DeviceLayout(Widget):
  def __init__(self):
    super().__init__()

    self._params = Params()
    self._select_language_dialog: MultiOptionDialog | None = None
    self._fcc_dialog: HtmlModal | None = None
    self._training_guide: TrainingGuide | None = None

    items = self._initialize_items()
    self._scroller = Scroller(items, line_separator=True, spacing=0)

    ui_state.add_offroad_transition_callback(self._offroad_transition)

  def _initialize_items(self):
    self._pair_device_btn = button_item(lambda: tr("Pair Device"), lambda: tr("PAIR"), lambda: tr(DESCRIPTIONS['pair_device']),
                                        callback=lambda: gui_app.push_widget(PairingDialog()))
    self._pair_device_btn.set_visible(lambda: not ui_state.prime_state.is_paired())

    self._orbit_enroll_btn = button_item(lambda: tr("Vincular con ORBIT"), lambda: tr("VINCULAR"),
                                         lambda: tr("Escanea el QR desde la app ORBIT para reclamar este dispositivo."),
                                         callback=lambda: gui_app.push_widget(OrbitEnrollDialog()),
                                         button_style=ButtonStyle.PRIMARY)
    self._orbit_enroll_btn.set_visible(lambda: not self._orbit_claimed())

    self._orbit_account_row = text_item(lambda: tr("Cuenta ORBIT"), self._orbit_account_text)
    self._orbit_account_row.set_visible(self._orbit_claimed)

    self._orbit_unlink_btn = button_item(lambda: tr("Desvincular ORBIT"), lambda: tr("QUITAR"),
                                         lambda: tr("Reinicia el enlace ORBIT en este dispositivo. Si la cuenta sigue vinculada en el servidor, " +
                                                    "se volverá a vincular sola; para desvincularla del todo usa la app ORBIT."),
                                         callback=self._orbit_unlink_prompt, button_style=ButtonStyle.DANGER)
    self._orbit_unlink_btn.set_visible(self._orbit_claimed)

    self._reset_calib_btn = button_item(lambda: tr("Reset Calibration"), lambda: tr("RESET"), lambda: tr(DESCRIPTIONS['reset_calibration']),
                                        callback=self._reset_calibration_prompt, button_style=ButtonStyle.DANGER)
    self._reset_calib_btn.set_description_opened_callback(self._update_calib_description)

    self._power_off_btn = dual_button_item(lambda: tr("Reboot"), lambda: tr("Power Off"),
                                           left_callback=self._reboot_prompt, right_callback=self._power_off_prompt)

    items = [
      text_item(lambda: tr("Dongle ID"), self._params.get("DongleId") or (lambda: tr("N/A"))),
      text_item(lambda: tr("Serial"), self._params.get("HardwareSerial") or (lambda: tr("N/A"))),
      self._pair_device_btn,
      self._orbit_enroll_btn,
      self._orbit_account_row,
      self._orbit_unlink_btn,
      button_item(lambda: tr("Driver Camera"), lambda: tr("PREVIEW"), lambda: tr(DESCRIPTIONS['driver_camera']),
                  callback=lambda: gui_app.push_widget(DriverCameraDialog()), enabled=ui_state.is_offroad),
      self._reset_calib_btn,
      button_item(lambda: tr("Review Training Guide"), lambda: tr("REVIEW"), lambda: tr(DESCRIPTIONS['review_guide']),
                  self._on_review_training_guide, enabled=ui_state.is_offroad),
      button_item(lambda: tr("Regulatory"), lambda: tr("VIEW"), callback=self._on_regulatory, enabled=ui_state.is_offroad),
      button_item(lambda: tr("Change Language"), lambda: tr("CHANGE"), callback=self._show_language_dialog),
      self._power_off_btn,
    ]
    return items

  def _offroad_transition(self):
    self._power_off_btn.action_item.right_button.set_visible(ui_state.is_offroad())

  # ORBIT enrollment params are registered by the orbit daemon; tolerate older manifests.
  def _orbit_claimed(self) -> bool:
    try:
      return self._params.get_bool("OrbitClaimed")
    except UnknownKeyName:
      return False

  def _orbit_owner(self) -> str:
    try:
      return self._params.get("OrbitOwner") or ""
    except UnknownKeyName:
      return ""

  def _orbit_role(self) -> str:
    try:
      return rol_etiqueta(self._params.get(PARAM_OWNER_ROLE))
    except UnknownKeyName:
      return ""

  # Dueño y rol juntos (contrato C2): "Ana • desarrollador"; el rol se omite si no se conoce.
  def _orbit_account_text(self) -> str:
    return " • ".join(x for x in (self._orbit_owner(), self._orbit_role()) if x) or tr("N/A")

  def _orbit_unlink_prompt(self):
    def perform_unlink(result: DialogResult):
      if result != DialogResult.CONFIRM:
        return
      try:
        self._params.put_bool("OrbitClaimed", False)
        self._params.remove("OrbitOwner")
        self._params.remove(PARAM_OWNER_ROLE)
        # Fuerza un codigo nuevo: el de antes se borro al reclamar y, sin esto, el QR
        # sale vacio hasta que caduque el TTL del codigo en memoria (600 s).
        self._params.put_bool("OrbitEnrollRegen", True)
      except UnknownKeyName:
        cloudlog.exception("OrbitClaimed/OrbitOwner not registered")

    dialog = ConfirmDialog(tr("¿Seguro que quieres reiniciar el enlace ORBIT de este dispositivo?"),
                           tr("Desvincular"), callback=perform_unlink)
    gui_app.push_widget(dialog)

  def show_event(self):
    super().show_event()
    self._scroller.show_event()

  def _render(self, rect):
    self._scroller.render(rect)

  def _show_language_dialog(self):
    def handle_language_selection(result: DialogResult):
      if result == DialogResult.CONFIRM and self._select_language_dialog:
        selected_language = multilang.languages[self._select_language_dialog.selection]
        multilang.change_language(selected_language)
        self._update_calib_description()
      self._select_language_dialog = None

    self._select_language_dialog = MultiOptionDialog(tr("Select a language"), multilang.languages, multilang.codes[multilang.language],
                                                     option_font_weight=FontWeight.UNIFONT, callback=handle_language_selection)
    gui_app.push_widget(self._select_language_dialog)

  def _reset_calibration_prompt(self):
    if ui_state.engaged:
      gui_app.push_widget(alert_dialog(tr("Disengage to Reset Calibration")))
      return

    def reset_calibration(result: DialogResult):
      # Check engaged again in case it changed while the dialog was open
      if ui_state.engaged or result != DialogResult.CONFIRM:
        return

      self._params.remove("CalibrationParams")
      self._params.remove("LiveTorqueParameters")
      self._params.remove("LiveParameters")
      self._params.remove("LiveParametersV2")
      self._params.remove("LiveDelay")
      self._params.put_bool("OnroadCycleRequested", True, block=True)
      self._update_calib_description()

    dialog = ConfirmDialog(tr("Are you sure you want to reset calibration?"), tr("Reset"), callback=reset_calibration)
    gui_app.push_widget(dialog)

  def _update_calib_description(self):
    desc = tr(DESCRIPTIONS['reset_calibration'])

    calib_bytes = self._params.get("CalibrationParams")
    if calib_bytes:
      try:
        calib = messaging.log_from_bytes(calib_bytes, log.Event).liveCalibration

        if calib.calStatus != log.LiveCalibrationData.Status.uncalibrated:
          pitch = math.degrees(calib.rpyCalib[1])
          yaw = math.degrees(calib.rpyCalib[2])
          desc += tr(" Your device is pointed {:.1f}° {} and {:.1f}° {}.").format(abs(pitch), tr("down") if pitch > 0 else tr("up"),
                                                                                  abs(yaw), tr("left") if yaw > 0 else tr("right"))
      except Exception:
        cloudlog.exception("invalid CalibrationParams")

    lag_perc = 0
    lag_bytes = self._params.get("LiveDelay")
    if lag_bytes:
      try:
        lag_perc = messaging.log_from_bytes(lag_bytes, log.Event).liveDelay.calPerc
      except Exception:
        cloudlog.exception("invalid LiveDelay")
    if lag_perc < 100:
      desc += tr("<br><br>Steering lag calibration is {}% complete.").format(lag_perc)
    else:
      desc += tr("<br><br>Steering lag calibration is complete.")

    torque_bytes = self._params.get("LiveTorqueParameters")
    if torque_bytes:
      try:
        torque = messaging.log_from_bytes(torque_bytes, log.Event).liveTorqueParameters
        # don't add for non-torque cars
        if torque.useParams:
          torque_perc = torque.calPerc
          if torque_perc < 100:
            desc += tr(" Steering torque response calibration is {}% complete.").format(torque_perc)
          else:
            desc += tr(" Steering torque response calibration is complete.")
      except Exception:
        cloudlog.exception("invalid LiveTorqueParameters")

    desc += "<br><br>"
    desc += tr("ORBIT is continuously calibrating, resetting is rarely required. " +
               "Resetting calibration will restart ORBIT if the car is powered on.")

    self._reset_calib_btn.set_description(desc)

  def _reboot_prompt(self):
    if ui_state.engaged:
      gui_app.push_widget(alert_dialog(tr("Disengage to Reboot")))
      return

    def perform_reboot(result: DialogResult):
      if not ui_state.engaged and result == DialogResult.CONFIRM:
        self._params.put_bool("DoReboot", True)

    dialog = ConfirmDialog(tr("Are you sure you want to reboot?"), tr("Reboot"), callback=perform_reboot)
    gui_app.push_widget(dialog)

  def _power_off_prompt(self):
    if ui_state.engaged:
      gui_app.push_widget(alert_dialog(tr("Disengage to Power Off")))
      return

    def perform_power_off(result: DialogResult):
      if not ui_state.engaged and result == DialogResult.CONFIRM:
        self._params.put_bool("DoShutdown", True)

    dialog = ConfirmDialog(tr("Are you sure you want to power off?"), tr("Power Off"), callback=perform_power_off)
    gui_app.push_widget(dialog)

  def _on_regulatory(self):
    if not self._fcc_dialog:
      self._fcc_dialog = HtmlModal(os.path.join(BASEDIR, "selfdrive/assets/offroad/fcc.html"))
    gui_app.push_widget(self._fcc_dialog)

  def _on_review_training_guide(self):
    if not self._training_guide:
      self._training_guide = TrainingGuide()
    gui_app.push_widget(self._training_guide)
