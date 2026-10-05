#pragma once

#include <string>
#include <unordered_map>

#include "cereal/gen/cpp/log.capnp.h"

inline static std::unordered_map<std::string, ParamKeyAttributes> keys = {
    {"AccessToken", {CLEAR_ON_MANAGER_START | DONT_LOG, STRING}},
    {"AdbEnabled", {PERSISTENT | BACKUP, BOOL}},
    {"AlwaysOnDM", {PERSISTENT | BACKUP, BOOL}},
    {"ApiCache_Device", {PERSISTENT, STRING}},
    {"ApiCache_FirehoseStats", {PERSISTENT, JSON}},
    {"AssistNowToken", {PERSISTENT, STRING}},
    {"AthenadPid", {PERSISTENT, INT}},
    {"AthenadUploadQueue", {PERSISTENT, JSON}},
    {"AthenadRecentlyViewedRoutes", {PERSISTENT, STRING}},
    {"BootCount", {PERSISTENT, INT}},
    {"CalibrationParams", {PERSISTENT, BYTES}},
    {"CameraDebugExpGain", {CLEAR_ON_MANAGER_START, STRING}},
    {"CameraDebugExpTime", {CLEAR_ON_MANAGER_START, STRING}},
    {"CarBatteryCapacity", {PERSISTENT, INT}},
    {"CarParams", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, BYTES}},
    {"CarParamsCache", {CLEAR_ON_MANAGER_START, BYTES}},
    {"CarParamsPersistent", {PERSISTENT, BYTES}},
    {"CarParamsPrevRoute", {PERSISTENT, BYTES}},
    {"CompletedTrainingVersion", {PERSISTENT, STRING, "0"}},
    {"ControlsReady", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, BOOL}},
    {"CurrentBootlog", {PERSISTENT, STRING}},
    {"CurrentRoute", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, STRING}},
    {"DisableLogging", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, BOOL}},
    {"DisablePowerDown", {PERSISTENT | BACKUP, BOOL}},
    {"DisableUpdates", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"DisengageOnAccelerator", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"DongleId", {PERSISTENT, STRING}},
    {"DoReboot", {CLEAR_ON_MANAGER_START, BOOL}},
    {"DoShutdown", {CLEAR_ON_MANAGER_START, BOOL}},
    {"DoUninstall", {CLEAR_ON_MANAGER_START, BOOL}},
    {"DriverTooDistracted", {CLEAR_ON_MANAGER_START | CLEAR_ON_IGNITION_ON, BOOL}},
    {"AlphaLongitudinalEnabled", {PERSISTENT | DEVELOPMENT_ONLY | BACKUP, BOOL}},
    {"ExperimentalMode", {PERSISTENT | BACKUP, BOOL}},
    {"ExperimentalModeConfirmed", {PERSISTENT | BACKUP, BOOL}},
    {"FirmwareQueryDone", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, BOOL}},
    {"ForcePowerDown", {PERSISTENT, BOOL}},
    {"GitBranch", {PERSISTENT, STRING}},
    {"GitCommit", {PERSISTENT, STRING}},
    {"GitCommitDate", {PERSISTENT, STRING}},
    {"GitDiff", {PERSISTENT, STRING}},
    {"GithubSshKeys", {PERSISTENT | BACKUP, STRING}},
    {"GithubUsername", {PERSISTENT | BACKUP, STRING}},
    {"GitRemote", {PERSISTENT, STRING}},
    {"GsmApn", {PERSISTENT | BACKUP, STRING}},
    {"GsmMetered", {PERSISTENT | BACKUP, BOOL, "1"}},
    {"GsmRoaming", {PERSISTENT | BACKUP, BOOL}},
    {"HardwareSerial", {PERSISTENT, STRING}},
    {"HasAcceptedTerms", {PERSISTENT, STRING, "0"}},
    {"InstallDate", {PERSISTENT, TIME}},
    {"IsDriverViewEnabled", {CLEAR_ON_MANAGER_START, BOOL}},
    {"IsEngaged", {PERSISTENT, BOOL}},
    {"IsLdwEnabled", {PERSISTENT | BACKUP, BOOL}},
    {"IsMetric", {PERSISTENT | BACKUP, BOOL}},
    {"IsOffroad", {CLEAR_ON_MANAGER_START, BOOL}},
    {"IsOnroad", {PERSISTENT, BOOL}},
    {"IsRhdDetected", {PERSISTENT, BOOL}},
    {"IsReleaseBranch", {CLEAR_ON_MANAGER_START, BOOL}},
    {"IsTakingSnapshot", {CLEAR_ON_MANAGER_START, BOOL}},
    {"IsTestedBranch", {CLEAR_ON_MANAGER_START, BOOL}},
    {"JoystickDebugMode", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, BOOL}},
    {"LanguageSetting", {PERSISTENT | BACKUP, STRING, "en"}},
    {"LastAthenaPingTime", {CLEAR_ON_MANAGER_START, INT}},
    {"LastGPSPosition", {PERSISTENT, STRING}},
    {"LastManagerExitReason", {CLEAR_ON_MANAGER_START, STRING}},
    {"LastOffroadStatusPacket", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, JSON}},
    {"LastAgnosPowerMonitorShutdown", {CLEAR_ON_MANAGER_START, STRING}},
    {"LastPowerDropDetected", {CLEAR_ON_MANAGER_START, STRING}},
    {"LastUpdateException", {CLEAR_ON_MANAGER_START, STRING}},
    {"LastUpdateRouteCount", {PERSISTENT, INT, "0"}},
    {"LastUpdateTime", {PERSISTENT, TIME}},
    {"LastUpdateUptimeOnroad", {PERSISTENT, FLOAT, "0.0"}},
    {"LiveDelay", {PERSISTENT | BACKUP, BYTES}},
    {"LiveParameters", {PERSISTENT, JSON}},
    {"LiveParametersV2", {PERSISTENT, BYTES}},
    {"LivestreamEncoderBitrate", {CLEAR_ON_MANAGER_START | DONT_LOG, INT}},
    {"LiveTorqueParameters", {PERSISTENT | DONT_LOG, BYTES}},
    {"LocationFilterInitialState", {PERSISTENT, BYTES}},
    {"LateralManeuverMode", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, BOOL}},
    {"LongitudinalManeuverMode", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, BOOL}},
    {"LongitudinalPersonality", {PERSISTENT | BACKUP, INT, std::to_string(static_cast<int>(cereal::LongitudinalPersonality::STANDARD))}},
    {"NetworkMetered", {PERSISTENT | BACKUP, BOOL}},
    {"ObdMultiplexingChanged", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, BOOL}},
    {"ObdMultiplexingEnabled", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, BOOL}},
    {"Offroad_CarUnrecognized", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, JSON}},
    {"Offroad_ConnectivityNeeded", {CLEAR_ON_MANAGER_START, JSON}},
    {"Offroad_ConnectivityNeededPrompt", {CLEAR_ON_MANAGER_START, JSON}},
    {"Offroad_ExcessiveActuation", {PERSISTENT, JSON}},
    {"Offroad_IsTakingSnapshot", {CLEAR_ON_MANAGER_START, JSON}},
    {"Offroad_NeosUpdate", {CLEAR_ON_MANAGER_START, JSON}},
    {"Offroad_NoFirmware", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, JSON}},
    {"Offroad_OrbitInstallIncomplete", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, JSON}},  // ORBIT: submodulos vacios / modelos LFS sin descargar
    {"Offroad_Recalibration", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, JSON}},
    {"Offroad_TemperatureTooHigh", {CLEAR_ON_MANAGER_START, JSON}},
    {"Offroad_UnregisteredHardware", {CLEAR_ON_MANAGER_START, JSON}},
    {"Offroad_UpdateFailed", {CLEAR_ON_MANAGER_START, JSON}},
    {"Offroad_DriverMonitoringUncertain", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, JSON}},
    {"OnroadCycleRequested", {CLEAR_ON_MANAGER_START, BOOL}},
    {"OpenpilotEnabledToggle", {PERSISTENT | BACKUP, BOOL, "1"}},
    {"PandaHeartbeatLost", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, BOOL}},
    {"PrimeType", {PERSISTENT, INT}},
    {"RecordAudio", {PERSISTENT | BACKUP, BOOL}},
    {"RecordAudioFeedback", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"RecordFront", {PERSISTENT | BACKUP, BOOL}},
    {"RecordFrontLock", {PERSISTENT, BOOL}},  // for the internal fleet
    {"SecOCKey", {PERSISTENT | DONT_LOG | BACKUP, STRING}},
    {"ShowDebugInfo", {PERSISTENT, BOOL}},
    {"RouteCount", {PERSISTENT, INT, "0"}},
    {"SnoozeUpdate", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, BOOL}},
    {"SshEnabled", {PERSISTENT | BACKUP, BOOL}},
    {"TermsVersion", {PERSISTENT, STRING}},
    {"TorqueBar", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"TrainingVersion", {PERSISTENT, STRING}},
    {"UbloxAvailable", {PERSISTENT, BOOL}},
    {"UpdateAvailable", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, BOOL}},
    {"UpdateFailedCount", {CLEAR_ON_MANAGER_START, INT}},
    {"UpdaterAvailableBranches", {PERSISTENT, STRING}},
    {"UpdaterCurrentDescription", {CLEAR_ON_MANAGER_START, STRING}},
    {"UpdaterCurrentReleaseNotes", {CLEAR_ON_MANAGER_START, BYTES}},
    {"UpdaterFetchAvailable", {CLEAR_ON_MANAGER_START, BOOL}},
    {"UpdaterNewDescription", {CLEAR_ON_MANAGER_START, STRING}},
    {"UpdaterNewReleaseNotes", {CLEAR_ON_MANAGER_START, BYTES}},
    {"UpdaterState", {CLEAR_ON_MANAGER_START, STRING}},
    {"UpdaterTargetBranch", {CLEAR_ON_MANAGER_START, STRING}},
    {"UpdaterLastFetchTime", {PERSISTENT, TIME}},
    {"UptimeOffroad", {PERSISTENT, FLOAT, "0.0"}},
    {"UptimeOnroad", {PERSISTENT, FLOAT, "0.0"}},
    {"UsbGpuPresent", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, BOOL}},
    {"UsbGpuCompiled", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, BOOL}},
    {"Version", {PERSISTENT, STRING}},

    // --- sunnypilot params --- //
    {"ApiCache_DriveStats", {PERSISTENT, JSON}},
    {"AutoLaneChangeBsmDelay", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"AutoLaneChangeTimer", {PERSISTENT | BACKUP, INT, "0"}},
    {"BlinkerLateralReengageDelay", {PERSISTENT | BACKUP, INT, "0"}},  // seconds
    {"BlinkerMinLateralControlSpeed", {PERSISTENT | BACKUP, INT, "20"}},  // MPH or km/h
    {"BlinkerPauseLateralControl", {PERSISTENT | BACKUP, INT, "0"}},
    {"Brightness", {PERSISTENT | BACKUP, INT, "0"}},
    {"CarList", {PERSISTENT, JSON}},
    {"CarParamsSP", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, BYTES}},
    {"CarParamsSPCache", {CLEAR_ON_MANAGER_START, BYTES}},
    {"CarParamsSPPersistent", {PERSISTENT, BYTES}},
    {"CarPlatformBundle", {PERSISTENT | BACKUP, JSON}},
    {"ChevronInfo", {PERSISTENT | BACKUP, INT, "4"}},
    {"CompletedSunnylinkConsentVersion", {PERSISTENT, STRING, "0"}},
    {"CustomAccIncrementsEnabled", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"CustomAccLongPressIncrement", {PERSISTENT | BACKUP, INT, "5"}},
    {"CustomAccShortPressIncrement", {PERSISTENT | BACKUP, INT, "1"}},
    {"DeviceBootMode", {PERSISTENT | BACKUP, INT, "0"}},
    {"DevUIInfo", {PERSISTENT | BACKUP, INT, "0"}},
    {"EnableCopyparty", {PERSISTENT | BACKUP, BOOL}},
    {"EnableGithubRunner", {PERSISTENT | BACKUP, BOOL}},
    {"GreenLightAlert", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"GithubRunnerSufficientVoltage", {CLEAR_ON_MANAGER_START , BOOL}},
    {"HasAcceptedTermsSP", {PERSISTENT, STRING, "0"}},
    {"HideVEgoUI", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"IntelligentCruiseButtonManagement", {PERSISTENT | BACKUP , BOOL}},
    {"InteractivityTimeout", {PERSISTENT | BACKUP, INT, "0"}},
    {"IsDevelopmentBranch", {CLEAR_ON_MANAGER_START, BOOL}},
    {"IsReleaseSpBranch", {CLEAR_ON_MANAGER_START, BOOL}},
    {"LastGPSPositionLLK", {PERSISTENT, STRING}},
    {"LeadDepartAlert", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"MaxTimeOffroad", {PERSISTENT | BACKUP, INT, "1800"}},
    {"ModelRunnerTypeCache", {CLEAR_ON_ONROAD_TRANSITION, INT}},
    {"OffroadMode", {CLEAR_ON_MANAGER_START, BOOL}},
    {"ForceOnroad", {CLEAR_ON_MANAGER_START, BOOL}},   // ORBIT modo banco: onroad sin coche (port del Force Drive State de StarPilot); un reinicio lo apaga
    {"Offroad_TiciSupport", {CLEAR_ON_MANAGER_START, JSON}},
    {"OnroadScreenOffBrightness", {PERSISTENT | BACKUP, INT, "0"}},
    {"OnroadScreenOffBrightnessMigrated", {PERSISTENT | BACKUP, STRING, "0.0"}},
    {"OnroadScreenOffTimer", {PERSISTENT | BACKUP, INT, "15"}},
    {"OnroadScreenOffTimerMigrated", {PERSISTENT | BACKUP, STRING, "0.0"}},
    {"OnroadUploads", {PERSISTENT | BACKUP, BOOL, "1"}},
    {"QuickBootToggle", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"QuietMode", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"RainbowMode", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"RocketFuel", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"ShowAdvancedControls", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"ShowTurnSignals", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"StandstillTimer", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"TrueVEgoUI", {PERSISTENT | BACKUP, BOOL, "0"}},

    // MADS params
    {"Mads", {PERSISTENT | BACKUP, BOOL, "1"}},
    {"MadsMainCruiseAllowed", {PERSISTENT | BACKUP, BOOL, "1"}},
    {"MadsSteeringMode", {PERSISTENT | BACKUP, INT, "0"}},
    {"MadsUnifiedEngagementMode", {PERSISTENT | BACKUP, BOOL, "1"}},

    // Model Manager params
    {"ModelManager_ActiveBundle", {PERSISTENT, JSON}},
    {"ModelManager_ClearCache", {CLEAR_ON_MANAGER_START, BOOL}},
    {"ModelManager_DownloadIndex", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, INT}},
    {"ModelManager_Favs", {PERSISTENT | BACKUP, STRING}},
    {"ModelManager_LastSyncTime", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, INT, "0"}},
    {"ModelManager_ModelsCache", {PERSISTENT | BACKUP, JSON}},

    // Neural Network Lateral Control
    {"NeuralNetworkLateralControl", {PERSISTENT | BACKUP, BOOL, "0"}},

    // sunnylink params
    {"EnableSunnylinkUploader", {PERSISTENT | BACKUP, BOOL}},
    {"LastSunnylinkPingTime", {CLEAR_ON_MANAGER_START, INT}},
    {"ParamsVersion", {PERSISTENT, INT}},
    {"SunnylinkCache_Roles", {PERSISTENT, STRING}},
    {"SunnylinkCache_Users", {PERSISTENT, STRING}},
    {"SunnylinkDongleId", {PERSISTENT, STRING}},
    {"SunnylinkdPid", {PERSISTENT, INT}},
    {"SunnylinkEnabled", {PERSISTENT, BOOL, "1"}},
    {"SunnylinkTempFault", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, BOOL, "0"}},

    // Backup Manager params
    {"BackupManager_CreateBackup", {PERSISTENT, BOOL}},
    {"BackupManager_RestoreVersion", {PERSISTENT, STRING}},

    // sunnypilot car specific params
    {"HyundaiLongitudinalTuning", {PERSISTENT | BACKUP, INT, "0"}},
    {"SubaruStopAndGo", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"SubaruStopAndGoManualParkingBrake", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"TeslaCoopSteering", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"ToyotaEnforceStockLongitudinal", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"ToyotaStopAndGoHack", {PERSISTENT | BACKUP, BOOL, "0"}},

    {"DynamicExperimentalControl", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"BlindSpot", {PERSISTENT | BACKUP, BOOL, "0"}},

    // sunnypilot model params
    {"CameraOffset", {PERSISTENT | BACKUP, FLOAT, "0.0"}},
    {"LagdToggle", {PERSISTENT | BACKUP, BOOL, "1"}},
    {"LagdToggleDelay", {PERSISTENT | BACKUP, FLOAT, "0.2"}},
    {"LagdValueCache", {PERSISTENT, FLOAT, "0.2"}},
    {"LaneTurnDesire", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"LaneTurnValue", {PERSISTENT | BACKUP, FLOAT, "19.0"}},
    {"PlanplusControl", {PERSISTENT | BACKUP, FLOAT, "1.0"}},

    // mapd
    {"MapAdvisorySpeedLimit", {CLEAR_ON_ONROAD_TRANSITION, FLOAT}},
    {"MapdVersion", {PERSISTENT, STRING}},
    {"MapSpeedLimit", {CLEAR_ON_ONROAD_TRANSITION, FLOAT, "0.0"}},
    {"NextMapSpeedLimit", {CLEAR_ON_ONROAD_TRANSITION, JSON}},
    {"Offroad_OSMUpdateRequired", {CLEAR_ON_MANAGER_START, JSON}},
    {"OsmDbUpdatesCheck", {CLEAR_ON_MANAGER_START, BOOL}},  // mapd database update happens with device ON, reset on boot
    {"OSMDownloadBounds", {PERSISTENT, STRING}},
    {"OsmDownloadedDate", {PERSISTENT, STRING, "0.0"}},
    {"OSMDownloadLocations", {PERSISTENT, JSON}},
    {"OSMDownloadProgress", {CLEAR_ON_MANAGER_START, JSON}},
    {"OsmLocal", {PERSISTENT, BOOL}},
    {"OsmLocationName", {PERSISTENT, STRING}},
    {"OsmLocationTitle", {PERSISTENT, STRING}},
    {"OsmLocationUrl", {PERSISTENT, STRING}},
    {"OsmStateName", {PERSISTENT, STRING, "All"}},
    {"OsmStateTitle", {PERSISTENT, STRING}},
    {"OsmWayTest", {PERSISTENT, STRING}},
    {"RoadName", {CLEAR_ON_ONROAD_TRANSITION, STRING}},
    {"RoadNameToggle", {PERSISTENT | BACKUP, BOOL, "0"}},

    // Speed Limit
    {"SpeedLimitMode", {PERSISTENT | BACKUP, INT, "1"}},
    {"SpeedLimitOffsetType", {PERSISTENT | BACKUP, INT, "0"}},
    {"SpeedLimitPolicy", {PERSISTENT | BACKUP, INT, "3"}},
    {"SpeedLimitValueOffset", {PERSISTENT | BACKUP, INT, "0"}},

    // Smart Cruise Control
    {"MapTargetVelocities", {CLEAR_ON_ONROAD_TRANSITION, STRING}},
    {"SmartCruiseControlMap", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"SmartCruiseControlVision", {PERSISTENT | BACKUP, BOOL, "0"}},

    // Torque lateral control custom params
    {"CustomTorqueParams", {PERSISTENT | BACKUP , BOOL}},
    {"EnforceTorqueControl", {PERSISTENT | BACKUP, BOOL}},
    {"LiveTorqueParamsToggle", {PERSISTENT | BACKUP , BOOL}},
    {"LiveTorqueParamsRelaxedToggle", {PERSISTENT | BACKUP , BOOL}},
    {"TorqueControlTune", {PERSISTENT | BACKUP, FLOAT, "0.0"}},
    {"TorqueParamsOverrideEnabled", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"TorqueParamsOverrideFriction", {PERSISTENT | BACKUP, FLOAT, "0.1"}},
    {"TorqueParamsOverrideLatAccelFactor", {PERSISTENT | BACKUP, FLOAT, "2.5"}},

    // ============================================================================
    // SIC-UEM / Orbit (TFG) — Jetson torque, esquive, overtake, MQTT, telemetría
    // ============================================================================
    // Selector de torque lateral y bridge Jetson (ZMQ)
    {"SteerTorqueMode", {PERSISTENT, INT, "0"}},                       // 0=Comma 1=Jetson 2=TestMax 3=Comma+Jetson
    {"JetsonTorque", {CLEAR_ON_MANAGER_START, STRING}},                // torque normalizado [-1,1] recibido de la Jetson
    {"JetsonTorqueTimestamp", {CLEAR_ON_MANAGER_START, STRING}},       // wall-clock del último torque (watchdog)
    {"JetsonTorqueGain", {PERSISTENT, STRING}},                        // DEPRECATED
    {"JetsonDeadZone", {PERSISTENT, FLOAT, "0.02"}},                   // dead-zone normalizada
    {"CommaSteerTorque", {CLEAR_ON_MANAGER_START, STRING}},            // torque del modelo Comma (diagnóstico UI)
    {"AppliedSteerTorque", {CLEAR_ON_MANAGER_START, STRING}},          // torque final aplicado (diagnóstico UI)
    {"SteerTorqueModeMqttPayload", {CLEAR_ON_MANAGER_START, STRING}},  // sync modo torque vía MQTT (JSON serializado: se escribe con json.dumps)
    {"JetsonConfigChanged", {CLEAR_ON_MANAGER_START, BOOL}},           // flag recarga config_jetson.json
    // Los cuatro *MqttPayload de aqui son params-BUZON: la UI (o controlsd) escribe un JSON
    // y mqtt_envio_general lo republica tal cual. Su mitad de ENTRADA ya murio: el topic
    // telemetry_config/<dongle>/jetson_config no se escucha (aceptaba retenido, no pasaba
    // por el CommandRouter y reescribia jetson_ip sin validar). La configuracion entra
    // ahora por orbit/v2/cfg/desired/<dongle> y sale por cfg/reported (§8), y esta reserva
    // el estado real leyendo los DESTINOS, no estos buzones. La publicacion legacy se
    // mantiene mientras dure la migracion (§13, publicacion dual v1+v2).
    {"JetsonConfigMqttPayload", {CLEAR_ON_MANAGER_START, STRING}},     // sync config jetson vía MQTT (JSON serializado)
    // Modo 3 (COMMA+JETSON, esquive de obstáculos)
    {"JetsonObstaclePulse", {CLEAR_ON_MANAGER_START, STRING}},         // JSON crudo (serializado) del último pulso de la Jetson
    {"JetsonObstacleTimestamp", {CLEAR_ON_MANAGER_START, STRING}},     // wall-clock del último pulso
    {"JetsonObstacleStatus", {CLEAR_ON_MANAGER_START, STRING}},        // DODGING_LEFT/RIGHT/HOLD/CANCELED_DRIVER/BSM_BLOCKED_*
    {"JetsonObstacleStatusMqttPayload", {CLEAR_ON_MANAGER_START, STRING}},
    {"JetsonObstacleApplyTargetMqttPayload", {CLEAR_ON_MANAGER_START, STRING}},
    {"JetsonObstacleMaxAngle", {PERSISTENT, FLOAT, "25.0"}},           // grados de offset para |intensity|=1
    {"JetsonObstacleMaxCurv", {PERSISTENT, FLOAT, "0.030"}},           // curvatura 1/m de offset para |intensity|=1
    {"JetsonObstacleApplyTarget", {PERSISTENT, STRING, "curvature"}},  // "curvature" | "torque"
    // Cambio de carril por MQTT + overtake
    // ForceLaneChange*: comandos one-shot (se consumen y auto-limpian en desire_helper).
    // CLEAR_ON_MANAGER_START: un "1" residual de antes de un reinicio NO debe disparar
    // un cambio de carril inesperado en el siguiente trayecto.
    {"ForceLaneChangeLeft", {CLEAR_ON_MANAGER_START, BOOL}},
    {"ForceLaneChangeRight", {CLEAR_ON_MANAGER_START, BOOL}},
    {"c_carril", {PERSISTENT, BOOL}},
    {"cambiar_a_izq", {PERSISTENT, BOOL}},
    {"cambiar_a_der", {PERSISTENT, BOOL}},
    {"ForceLeftBlinker", {PERSISTENT, BOOL}},
    {"GirarALaDerecha", {PERSISTENT, BOOL}},
    {"bsmLaneChangeStatus", {CLEAR_ON_MANAGER_START, STRING}},
    {"overtakeStatus", {CLEAR_ON_MANAGER_START, STRING}},
    {"overtakingActive", {CLEAR_ON_MANAGER_START, BOOL}},              // dedup: una sola entrada (era doble en origen)
    {"waitingToReturn", {PERSISTENT, BOOL}},
    {"returningRight", {PERSISTENT, BOOL}},
    {"OvertakeTargetSpeedKph", {CLEAR_ON_MANAGER_START, FLOAT, "0"}},
    {"sic_adelantar", {PERSISTENT | CLEAR_ON_MANAGER_START, BOOL}},
    {"sic_adelantar_bsm", {PERSISTENT | CLEAR_ON_MANAGER_START, BOOL}},     // DEPRECATED compat
    {"sic_adelantar_nobsm", {PERSISTENT | CLEAR_ON_MANAGER_START, BOOL}},   // DEPRECATED compat
    {"overtake_distancia_activacion", {PERSISTENT, FLOAT, "50"}},
    {"overtake_tiempo_carril_izq", {PERSISTENT, FLOAT, "15"}},
    {"overtake_incremento_velocidad", {PERSISTENT, FLOAT, "15"}},
    {"ActivateEvent", {PERSISTENT, BOOL}},
    // Frenado / longitudinal
    {"brutebreak_active", {CLEAR_ON_MANAGER_START, BOOL}},
    {"brutebreak_intensidad", {PERSISTENT, FLOAT, "-3.5"}},
    // Velocidad
    {"Velocidad_C1", {PERSISTENT, STRING}},
    {"Velocidad_C2", {PERSISTENT, STRING}},
    {"Velocidad_C3", {PERSISTENT, STRING}},
    {"Velocidad_C4", {PERSISTENT, STRING}},
    {"vel_adel", {CLEAR_ON_MANAGER_START, STRING}},
    {"orbit_speed_increment", {PERSISTENT, FLOAT, "5"}},
    // Comandos de bajo nivel (fallback bools escritos por mqtt_comandos)
    {"orbit_forward", {CLEAR_ON_MANAGER_START, BOOL}},
    {"orbit_break", {CLEAR_ON_MANAGER_START, BOOL}},
    {"orbit_tright", {CLEAR_ON_MANAGER_START, BOOL}},
    {"orbit_tleft", {CLEAR_ON_MANAGER_START, BOOL}},
    {"orbit_speed_increase", {CLEAR_ON_MANAGER_START, BOOL}},
    {"orbit_speed_decrease", {CLEAR_ON_MANAGER_START, BOOL}},
    {"orbit_steering_pulse", {CLEAR_ON_MANAGER_START, STRING}},   // pulso giro cruceta, 5 campos: "direction:start_ms_pared:start_mono:dur_inicial_ms:magnitud". La expiracion se deriva de start_mono + duracion (reloj MONOTONO: un salto del reloj de pared no puede alargar el pulso). Cruza barrera de proceso a controlsd.
    // Toggles UI / telemetría
    {"modo_debug", {PERSISTENT | BACKUP, BOOL}},
    {"show_blindspot", {PERSISTENT, BOOL}},
    {"carState_toggle", {PERSISTENT, BOOL}},
    {"carControl_toggle", {PERSISTENT, BOOL}},
    {"controlsState_toggle", {PERSISTENT, BOOL}},
    {"liveCalibration_toggle", {PERSISTENT, BOOL}},
    {"gpsLocationExternal_toggle", {PERSISTENT, BOOL}},
    {"gpsLocation_toggle", {PERSISTENT, BOOL}},
    {"drivingModelData_toggle", {PERSISTENT, BOOL}},
    {"radarState_toggle", {PERSISTENT, BOOL}},
    // Submenu ORBIT > Telemetria (orbit/telemetria_grupos.py): interruptor por canal v2. Sin configurar = encendido; solo
    // se apaga con un False EXPLICITO. Los canales v1 reutilizan los <canal>_toggle de arriba.
    {"tel2_pos_toggle", {PERSISTENT, BOOL}},
    {"tel2_vehicle_toggle", {PERSISTENT, BOOL}},
    {"tel2_perception_toggle", {PERSISTENT, BOOL}},
    {"tel2_openpilot_toggle", {PERSISTENT, BOOL}},
    {"tel2_road_toggle", {PERSISTENT, BOOL}},
    {"tel2_health_toggle", {PERSISTENT, BOOL}},
    {"tel2_event_toggle", {PERSISTENT, BOOL}},
    {"tel2_trip_toggle", {PERSISTENT, BOOL}},
    // Degradar a perfil AHORRO cuando la red movil esta marcada como de pago (GsmMetered). Sin configurar = APAGADO: GsmMetered
    // vale "1" de fabrica y con esto encendido todo comma con SIM perdia percepcion, alertas y pedales al salir de casa.
    {"OrbitAhorroRedMovil", {PERSISTENT, BOOL}},
    // Navegación (distancias de maniobra) + sender UEM
    {"roundabout_distance", {PERSISTENT, STRING}},
    {"intersection_distance", {PERSISTENT, STRING}},
    {"merge_distance", {PERSISTENT, STRING}},
    {"turn_distance", {PERSISTENT, STRING}},
    {"off_road_distance", {PERSISTENT, STRING}},
    {"on_road_distance", {PERSISTENT, STRING}},
    {"sender_uem_up", {PERSISTENT, BOOL}},
    {"sender_uem_down", {PERSISTENT, BOOL}},
    {"sender_uem_left", {PERSISTENT, BOOL}},
    {"sender_uem_right", {PERSISTENT, BOOL}},
    // Enrolamiento de dispositivo ORBIT (QR)
    {"OrbitClaimed", {PERSISTENT, BOOL}},                                 // dispositivo reclamado (fuente de verdad, sobrevive reboot)
    {"OrbitPairingCode", {CLEAR_ON_MANAGER_START, STRING}},              // código efímero actual para renderizar el QR
    {"OrbitEnrollExpiry", {CLEAR_ON_MANAGER_START, STRING}},             // issued-at/expiry (ms epoch) para countdown opcional
    {"OrbitConnected", {CLEAR_ON_MANAGER_START, BOOL}},                  // conexión MQTT viva (lo escribe mqtt_envio_general)
    {"OrbitLastPublish", {CLEAR_ON_MANAGER_START, STRING}},              // epoch (s) del último publish de telemetría
    {"OrbitOwner", {PERSISTENT, STRING}},                                // nombre/email del usuario que reclamó el dispositivo
    {"OrbitOwnerRole", {PERSISTENT, STRING}},                            // rol ORBIT de ese usuario: user | developer | superadmin (llega en el enroll_ack; vacio = desconocido)
    {"OrbitEnrollRegen", {CLEAR_ON_MANAGER_START, BOOL}},                // trigger: la UI pide rotar el código/QR ya
    {"OrbitHealthcheckRequest", {CLEAR_ON_MANAGER_START, STRING}},       // trigger: comando MQTT de diagnóstico remoto pendiente de responder
    {"OrbitCmdResult", {CLEAR_ON_MANAGER_START, STRING}},
    {"OrbitSteerModeLocal", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, BOOL}},   // Seleccion PRESENCIAL del modo de volante en la pantalla del comma. NO es OrbitBenchArmed: armar el banco habilita los verbos FISICOS por MQTT a cualquiera que publique en el broker, y elegir "Jetson" delante del coche no puede significar eso. Lo escribe solo la UI; lo lee controlsd para permitir los modos 1 y 2 sin armado remoto. Se limpia al arrancar el manager y al pasar a offroad.                // RESULTADO real de un verbo, escrito por el CONSUMIDOR que decide (desire_helper, controlsd, card) y leido por command_router para cerrar el ACK. JSON: {v,verb,id,phase,reason,detail,ts_ms,mono_ms}. El router NO puede saber si una maniobra se aplico: solo escribe un Param, y el consumidor puede rechazarla por sus propios gates.
    {"OrbitCruiseCancel", {CLEAR_ON_MANAGER_START, BOOL}},               // Disparo one-shot del verbo cruise_button {button:"cancel"}: el BOTON DE PANICO. Lo consume controlsd, que sostiene CC.cruiseControl.cancel durante ~0.5 s (un solo ciclo no llega a salir por CAN) y lo limpia. BAJA autoridad: desengancha, no engancha.
    {"OrbitPrivacyMute", {PERSISTENT, BOOL}},                            // Interruptor maestro LOCAL de privacidad (pantalla del comma). PERSISTENT a proposito: si el conductor silencio el coche, un reinicio no puede volver a encender la camara ni la posicion. Lo escribe solo la UI; lo leen camera_sender (envio de imagenes) y mqtt_envio_general (canales de posicion).
    // Mando remoto v2 (contrato orbit/v2/*)
    // Ver docs/superpowers/specs/2026-08-23-orbit-mando-remoto-v2-design.md, §4-§6.
    // El PLANO DE ESTADO del mando (modo vivo, verbo activo, gates, deadman) NO vive
    // aquí: va en el struct cereal OrbitCommandState (§5). Params.put es mkstemp+fsync
    // y a 10 Hz ya provocó commIssue en este árbol (por eso existe _defer_param_put en
    // controlsd). Aquí solo están los ajustes/armados que escribe la PANTALLA FÍSICA
    // del comma y los disparos que deben sobrevivir a la muerte del publicador cereal.
    // NINGUNA clave de modo/mando es PERSISTENT a propósito: el precedente de
    // SteerTorqueMode persistente es exactamente por lo que manager.py:91-100 tuvo que
    // añadir un fail-safe de arranque (un modo peligroso guardado en disco revive solo
    // tras un reinicio, sin que nadie lo pida). La única PERSISTENT de aquí abajo es
    // OrbitGlobalRetainPurged, que no manda sobre el coche: es la marca de una limpieza
    // hecha en el broker.
    // OJO al leer/escribir: put() exige el tipo NATIVO del param (put("1") sobre un
    // INT lanza TypeError) y get() devuelve None si la clave no está escrita — el
    // default de abajo solo sale con get(key, return_default=True).
    {"OrbitCommandMode", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, INT, "0"}},   // 0=observador 1=copiloto 2=maniobra 3=banco (§4.1). put() exige int NATIVO
    {"OrbitBenchArmed", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, BOOL}},        // armado del modo banco: SOLO desde la pantalla física del comma (§4.1)
    {"OrbitBenchExpiry", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, STRING}},     // caducidad del armado de banco: epoch ms como TEXTO (no INT: 1.7e12 no cabe en el std::stoi de 32 bits del lado C++), igual que OrbitEnrollExpiry
    {"OrbitDisarmAll", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, BOOL}},         // verbo disarm_all (§6): único sin modo ni gate. Redundante con cereal A PROPÓSITO: bajar autoridad no puede depender de que el publicador de cereal siga vivo
    {"OrbitGlobalRetainPurged", {PERSISTENT, STRING}},                                        // "host:puerto" del broker donde ya se borraron los retenidos rancios de */global (lo escribe mqtt_envio_general; PERSISTENT para no repetirlo en cada arranque, con el broker dentro para rehacerlo si cambia)
    // Configuracion deseada vs reportada (contrato orbit/v2/cfg/*, §8 del diseno).
    // Tres fuentes de verdad divergentes (Params aqui, tablas en el backend,
    // SharedPreferences en el movil) se reconcilian con un sobre versionado:
    // {version, ts_ms, source, values}. Gana la version mas alta y el EMPATE lo gana
    // `comma_ui`, porque el coche es el unico lado que puede tener a alguien delante sin
    // red. Las dos claves de abajo son PERSISTENT y NO son mando: no arman nada ni mueven
    // ningun actuador (el vocabulario de config_v2.py excluye a proposito SteerTorqueMode
    // y JetsonObstacleApplyTarget, que son SOLO REPORTE porque cambiarlos mueve el
    // volante y eso es el verbo torque_mode). Guardan el CONTADOR, no la autoridad: sin
    // ellas la version se reiniciaria en cada arranque y un `desired` retenido viejo del
    // broker le ganaria al estado real del coche.
    {"OrbitConfigDesired", {PERSISTENT, STRING}},                                             // ultimo sobre cfg/desired ACEPTADO (JSON serializado). Solo para que la version no retroceda tras un reinicio
    {"OrbitConfigReported", {PERSISTENT, STRING}},                                            // ultimo sobre cfg/reported PUBLICADO (JSON serializado). Lo escribe mqtt_envio_general tras aplicar y publicar
    {"OrbitTelemetryProfile", {PERSISTENT, STRING, "normal"}},                                // perfil de telemetria v2: "ahorro" | "normal" | "diagnostico" (§7). Lo lee mqtt_envio_general._maybe_reload_perfil, que hasta ahora se caia a normal porque la clave no estaba registrada. La degradacion por red de pago y la caducidad de 15 min del diagnostico NO dependen de este param: viven en el motor, que es quien tiene el reloj
};
