"""Contrato de la telemetria v1 sobre el namespace v2 (seccion 7 del diseno de mando v2).

Lo que se prueba aqui es lo que, si se rompe, NO da un fallo ruidoso sino un consumo o un
dato equivocado en silencio:

  - que las rutas del descriptor existan de verdad en el cereal de ESTE build (un campo
    renombrado upstream se convierte hoy en un campo que desaparece del cable sin avisar);
  - que el codigo de evento salga de la tabla del descriptor y NO del ordinal del enum de
    cereal, que cambia entre rebases;
  - que el `dongle_id` y el nombre del canal NO viajen en el payload (van en el topic);
  - que la omision por defecto declarado sea LOSSLESS: reconstruir con los defectos del
    descriptor devuelve exactamente lo que se habria enviado sin omitir;
  - que los perfiles, la degradacion por red de pago y la caducidad de los 15 minutos del
    diagnostico funcionen con el reloj monotono que entra por parametro;
  - que la lista blanca v1 de canales.json no mencione claves que el capnp no tiene (una
    clave mal escrita ahi vacia un campo que el backend si lee).
"""
import json
import math
import os
import time

import pytest

from cereal import messaging
from openpilot.orbit import telemetria_v1 as tel
from openpilot.orbit.telemetria_v1 import (CANALES, EVENTOS_CODIGO, MotorTelemetria, PERFIL_AHORRO, PERFIL_DIAG,
                                           PERFIL_NORMAL, codigo_evento, descriptor_backend, extraer,
                                           firma_telemetria, topic_telemetria)

DONGLE = "0123456789abcdef"


# --------------------------------------------------------------------------- utilidades

def lector(servicio, n=None, **campos):
  """Mensaje cereal real con los campos pedidos (rutas punteadas) ya puestos."""
  msg = messaging.new_message(servicio) if n is None else messaging.new_message(servicio, n)
  cuerpo = getattr(msg, servicio)
  for ruta, valor in campos.items():
    destino = cuerpo
    tramos = ruta.split(".")
    for tramo in tramos[:-1]:
      destino = destino[int(tramo)] if tramo.isdigit() else getattr(destino, tramo)
    setattr(destino, tramos[-1], valor)
  return getattr(msg.as_reader(), servicio)


def solo_datos(mensajes):
  return {canal: sobre["d"] for canal, sobre in mensajes}


def fuentes_minimas(**extra):
  base = {"carState": lector("carState")}
  base.update(extra)
  return base


# --------------------------------------------------------------------------- descriptor

def test_todas_las_rutas_del_descriptor_existen_en_el_cereal_de_este_build():
  """Un campo renombrado upstream desaparece del cable SIN avisar: _leer devuelve None y
  el campo simplemente se omite. Este test convierte ese silencio en un fallo."""
  huerfanos = []
  for canal in CANALES.values():
    if canal.disparo in ("evento", "sesion"):
      continue                                   # payload propio, no lista blanca
    for campo in canal.campos:
      if campo.origen == "":
        continue                                 # calculado
      servicio = campo.fuente
      if servicio == "gps":
        servicio = "gpsLocationExternal"         # alias interno del canal pos
      try:
        obj = lector(servicio, 1) if servicio == "pandaStates" else lector(servicio)
      except Exception as e:                     # pragma: no cover
        huerfanos.append(f"{canal.nombre}.{campo.nombre}: servicio {servicio} no construible ({e})")
        continue
      if tel._leer(obj, campo.origen) is None:
        huerfanos.append(f"{canal.nombre}.{campo.nombre} -> {servicio}.{campo.origen}")
  assert not huerfanos, "rutas del descriptor que no existen en este cereal: " + ", ".join(huerfanos)


def test_los_servicios_del_descriptor_son_servicios_cereal_reales():
  from cereal.services import SERVICE_LIST
  assert set(tel.SERVICIOS) <= set(SERVICE_LIST)


def test_el_tipo_del_defecto_declarado_concuerda_con_el_tipo_del_campo():
  """`valor == defecto` es la regla de omision. Con un defecto de otro tipo la comparacion
  o no casa nunca (el campo deja de ahorrar) o casa de mas (False == 0 en Python)."""
  esperado = {"float": float, "int": int, "bool": bool, "enum": str, "str": str, "list[str]": list}
  malos = []
  for canal in CANALES.values():
    for campo in canal.campos:
      if campo.defecto is tel._SIN_DEFECTO:
        continue
      tipo = esperado.get(campo.tipo)
      if tipo is None or not isinstance(campo.defecto, tipo) or (campo.tipo != "bool" and isinstance(campo.defecto, bool)):
        malos.append(f"{canal.nombre}.{campo.nombre}={campo.defecto!r} para tipo {campo.tipo}")
  assert not malos, malos


def test_no_hay_nombres_de_campo_repetidos_dentro_de_un_canal():
  for canal in CANALES.values():
    nombres = [c.nombre for c in canal.campos]
    assert len(nombres) == len(set(nombres)), canal.nombre


def test_el_descriptor_serializa_a_json_y_la_firma_es_estable():
  d = descriptor_backend()
  json.dumps(d)                                  # lo consume el backend tal cual
  assert firma_telemetria() == firma_telemetria()
  assert set(d["canales"]) == set(CANALES)
  assert d["perfiles"] == list(tel.PERFILES)


def test_el_topic_lleva_dongle_y_canal_y_ninguno_va_en_el_payload():
  assert topic_telemetria(DONGLE, "vehicle") == f"orbit/v2/tel/{DONGLE}/vehicle"
  motor = MotorTelemetria()
  mensajes = motor.tick(10.0, fuentes_minimas(), ts_ms=1)
  assert mensajes
  for canal, sobre in mensajes:
    crudo = json.dumps(sobre)
    assert DONGLE not in crudo and "dongle_id" not in crudo
    assert f'"{canal}"' not in crudo             # el nombre del canal tampoco se repite


# --------------------------------------------------------------------------- eventos

def test_el_codigo_de_evento_no_depende_del_ORDINAL_del_enum_de_cereal():
  """La tabla es el contrato. Si alguien la generase del enum, insertar un evento en medio
  en un rebase renumeraria todo lo que va detras y los historicos quedarian mal etiquetados."""
  from cereal import log
  ordinales = {n: i for i, n in enumerate(log.OnroadEvent.EventName.schema.enumerants)}
  # 'canError' es el primero del enum (ordinal 0) y su codigo ORBIT es 1: si alguien
  # sustituye la tabla por el ordinal, este assert cae.
  assert ordinales["canError"] == 0
  assert codigo_evento("canError") == 1
  distintos = [n for n, o in ordinales.items() if n in EVENTOS_CODIGO and EVENTOS_CODIGO[n] != o]
  assert distintos, "todos los codigos coinciden con el ordinal: la tabla no aporta nada"


def test_los_codigos_de_evento_son_unicos():
  vistos = {}
  for nombre, cod in EVENTOS_CODIGO.items():
    assert cod not in vistos, f"{nombre} y {vistos.get(cod)} comparten el codigo {cod}"
    vistos[cod] = nombre


def test_un_evento_que_este_firmware_no_conoce_sale_con_codigo_0_y_su_nombre():
  assert codigo_evento("eventoDeUnRebaseFuturo") == 0
  assert codigo_evento(None) == 0


def test_los_eventos_de_cereal_que_faltan_en_la_tabla_se_publican_igual():
  from cereal import log, custom
  base = set(log.OnroadEvent.EventName.schema.enumerants)
  sp = set(custom.OnroadEventSP.EventName.schema.enumerants)
  faltan = (base | sp) - set(EVENTOS_CODIGO)
  # No es un error tener eventos nuevos sin codigo (salen con cod=0), pero si hay que
  # enterarse: este test documenta la deuda en vez de dejarla invisible.
  assert not faltan, f"eventos de cereal sin codigo ORBIT (saldrian con cod=0): {sorted(faltan)}"


def test_solo_se_publican_los_FLANCOS_del_evento_no_su_presencia_continua():
  motor = MotorTelemetria()
  ev = lector("onroadEvents", 1)
  fuentes = fuentes_minimas(onroadEvents=ev)
  primera = solo_datos(motor.tick(10.0, fuentes, ts_ms=1))
  assert primera["event"]["ev"] == [{"cod": 1, "nom": "canError", "sev": "info", "on": True}]
  # Mismo evento, siguiente ciclo: no se repite.
  assert "event" not in solo_datos(motor.tick(11.0, fuentes, ts_ms=2))
  # Desaparece: flanco de bajada.
  sin_ev = fuentes_minimas(onroadEvents=lector("onroadEvents", 0))
  bajada = solo_datos(motor.tick(12.0, sin_ev, ts_ms=3))
  assert bajada["event"]["ev"] == [{"cod": 1, "nom": "canError", "sev": "info", "on": False}]


def test_sin_fuente_de_eventos_NO_se_deducen_bajadas():
  """onroadEvents va a 1 Hz: perder un ciclo no puede publicar un 'off' de todo."""
  motor = MotorTelemetria()
  motor.tick(10.0, fuentes_minimas(onroadEvents=lector("onroadEvents", 1)), ts_ms=1)
  fuera = solo_datos(motor.tick(11.0, fuentes_minimas(), ts_ms=2))
  assert "event" not in fuera


def test_la_severidad_sale_de_los_flancos_de_la_INSTANCIA_no_de_una_tabla_por_nombre():
  motor = MotorTelemetria()
  ev = lector("onroadEvents", 1, **{"0.immediateDisable": True})
  fuera = solo_datos(motor.tick(10.0, fuentes_minimas(onroadEvents=ev), ts_ms=1))
  assert fuera["event"]["ev"][0]["sev"] == "critico"


# --------------------------------------------------------------------------- saneado y redondeo

def test_el_redondeo_es_el_declarado_y_la_unidad_la_publicada():
  motor = MotorTelemetria()
  cs = lector("carState", vEgo=22.345678, steeringAngleDeg=-3.456789, aEgo=0.3456789)
  d = solo_datos(motor.tick(10.0, {"carState": cs}, ts_ms=1))["vehicle"]
  assert d["speed_kph"] == round(22.345678 * 3.6, 1)     # m/s -> km/h, 1 decimal
  assert d["steer_deg"] == -3.5
  assert d["accel_ms2"] == 0.35


def test_un_float_no_finito_viaja_como_null_explicito_no_como_campo_ausente():
  motor = MotorTelemetria(PERFIL_DIAG)
  cst = lector("controlsState", desiredCurvature=float("nan"))
  fuentes = fuentes_minimas(radarState=lector("radarState"), controlsState=cst)
  d = solo_datos(motor.tick(10.0, fuentes, ts_ms=1))["perception"]
  assert "desired_curvature" in d and d["desired_curvature"] is None
  # Y el payload entero sigue siendo JSON valido con allow_nan=False.
  json.dumps(tel.sanea_no_finitos(d), allow_nan=False)


def test_una_fuente_que_no_esta_viva_OMITE_sus_campos_no_los_manda_a_cero():
  motor = MotorTelemetria(PERFIL_DIAG)
  # perception con radarState pero SIN drivingModelData: las lineas de carril no aparecen.
  fuentes = fuentes_minimas(radarState=lector("radarState", **{"leadOne.status": True, "leadOne.dRel": 30.0}))
  d = solo_datos(motor.tick(10.0, fuentes, ts_ms=1))["perception"]
  assert d["lead_dist_m"] == 30.0
  assert "lane_left_m" not in d and "lane_left_prob" not in d


# --------------------------------------------------------------------------- omision por defecto

def test_la_omision_por_defecto_es_LOSSLESS_reconstruyendo_con_el_descriptor():
  """El consumidor rellena los campos ausentes con el `defecto` del descriptor. Si eso no
  devuelve exactamente lo que se habria enviado sin omitir, la optimizacion pierde datos."""
  cs = lector("carState", vEgo=13.0, leftBlinker=True, doorOpen=True, gearShifter="drive")
  canal = CANALES["vehicle"]
  enviado = extraer(canal, {"carState": cs}, PERFIL_NORMAL)
  # Sin omision: mismo camino, ignorando los defectos.
  completo = {}
  for campo in canal.campos_de(PERFIL_NORMAL):
    valor = tel.coacciona(campo, tel._leer(cs, campo.origen))
    if valor is not tel._AUSENTE:
      completo[campo.nombre] = valor
  reconstruido = dict(enviado)
  for campo in canal.campos_de(PERFIL_NORMAL):
    if campo.nombre not in reconstruido and campo.defecto is not tel._SIN_DEFECTO:
      reconstruido[campo.nombre] = campo.defecto
  assert reconstruido == completo
  assert len(enviado) < len(completo), "la omision por defecto no esta ahorrando nada"


def test_el_par_del_volante_a_cero_viaja_en_perfil_normal():
  """Sin defecto a proposito: un firmware anterior (era de perfil diagnostico) no lo manda
  nunca en normal, asi que la app no puede reconstruir un 0.0 omitido sin inventarselo.
  Omitido, la fila 'Torque del volante' salia en '-' con el coche aparcado."""
  cs = {"carState": lector("carState", steeringTorque=0.0)}
  assert extraer(CANALES["vehicle"], cs, PERFIL_NORMAL)["steer_torque_driver"] == 0.0
  assert "steer_torque_driver" not in extraer(CANALES["vehicle"], cs, PERFIL_AHORRO)


def test_la_confianza_de_linea_a_cero_viaja_en_perfil_normal():
  """Sin defecto, como el par: un 0 % (sin lineas) tiene que llegar como 0.0 y no como
  ausente, que la app pinta como '-' (sin dato)."""
  dm = {"drivingModelData": lector("drivingModelData", **{"laneLineMeta.leftProb": 0.0, "laneLineMeta.rightProb": 0.0})}
  d = extraer(CANALES["perception"], dm, PERFIL_NORMAL)
  assert (d["lane_left_prob"], d["lane_right_prob"]) == (0.0, 0.0)


def test_los_intermitentes_salen_del_coche():
  """Es de las senales mas pedidas y hoy no sale del coche con un contrato propio."""
  motor = MotorTelemetria()
  cs = lector("carState", leftBlinker=True, rightBlinker=False)
  d = solo_datos(motor.tick(10.0, {"carState": cs}, ts_ms=1))["vehicle"]
  assert d["blink_left"] is True
  assert "blink_right" not in d                  # False = defecto declarado, se omite
  campos = {c.nombre for c in CANALES["vehicle"].campos_de(PERFIL_AHORRO)}
  assert {"blink_left", "blink_right"} <= campos, "los intermitentes tienen que salir hasta en AHORRO"


# --------------------------------------------------------------------------- perfiles

def test_ahorro_recorta_campos_y_apaga_perception():
  ahorro = CANALES["vehicle"].campos_de(PERFIL_AHORRO)
  normal = CANALES["vehicle"].campos_de(PERFIL_NORMAL)
  diag = CANALES["vehicle"].campos_de(PERFIL_DIAG)
  assert len(ahorro) < len(normal) < len(diag)
  assert math.isinf(CANALES["perception"].periodo(PERFIL_AHORRO))
  assert not math.isinf(CANALES["perception"].periodo(PERFIL_NORMAL))


def test_la_red_de_pago_degrada_a_ahorro_incluso_desde_diagnostico():
  motor = MotorTelemetria(PERFIL_DIAG)
  metered = lector("deviceState", networkMetered=True, started=False)
  motor.tick(10.0, fuentes_minimas(deviceState=metered), ts_ms=1)
  assert motor.perfil == PERFIL_AHORRO
  assert motor.perfil_pedido == PERFIL_DIAG      # lo PEDIDO no se pierde: vuelve al salir


def test_el_diagnostico_se_apaga_solo_a_los_15_minutos_en_el_propio_dispositivo():
  motor = MotorTelemetria(PERFIL_DIAG, ahora=0.0)
  motor.tick(1.0, fuentes_minimas(), ts_ms=1)
  assert motor.perfil == PERFIL_DIAG
  motor.tick(tel.DIAG_TTL_S + 1.0, fuentes_minimas(), ts_ms=2)
  assert motor.perfil == PERFIL_NORMAL
  assert motor.perfil_pedido == PERFIL_NORMAL
  assert motor.diag_expirado is True             # bandera para que la UI deje de mentir


def test_pedir_un_perfil_invalido_no_cambia_nada():
  motor = MotorTelemetria()
  assert motor.pedir_perfil("turbo", 0.0) == PERFIL_NORMAL
  assert motor.pedir_perfil(None, 0.0) == PERFIL_NORMAL


# --------------------------------------------------------------------------- cadencias

def test_vehicle_va_a_2_Hz_en_normal_y_no_mas_rapido():
  motor = MotorTelemetria()
  f = fuentes_minimas()
  assert "vehicle" in solo_datos(motor.tick(10.00, f, ts_ms=1))
  assert "vehicle" not in solo_datos(motor.tick(10.25, f, ts_ms=2))
  assert "vehicle" in solo_datos(motor.tick(10.50, f, ts_ms=3))


def test_openpilot_solo_se_publica_cuando_CAMBIA_mas_keepalive():
  motor = MotorTelemetria()
  ss = lector("selfdriveState")
  f = fuentes_minimas(selfdriveState=ss)
  assert "openpilot" in solo_datos(motor.tick(10.0, f, ts_ms=1))
  assert "openpilot" not in solo_datos(motor.tick(20.0, f, ts_ms=2))
  # Cambia -> sale
  f2 = fuentes_minimas(selfdriveState=lector("selfdriveState", enabled=True, active=True))
  assert "openpilot" in solo_datos(motor.tick(30.0, f2, ts_ms=3))
  # Sin cambios pero pasado el keepalive -> sale igual
  keep = CANALES["openpilot"].keepalive(PERFIL_NORMAL)
  assert "openpilot" in solo_datos(motor.tick(30.0 + keep + 1.0, f2, ts_ms=4))


def test_un_canal_on_change_publica_el_payload_VACIO_cuando_todo_vuelve_al_defecto():
  """Descartar el vacio dejaria al consumidor pintando el ultimo estado NO por defecto:
  openpilot pasando de 'actuando' a 'apagado' se quedaria en 'actuando' para siempre."""
  motor = MotorTelemetria()
  activo = fuentes_minimas(selfdriveState=lector("selfdriveState", enabled=True, active=True))
  motor.tick(10.0, activo, ts_ms=1)
  apagado = fuentes_minimas(selfdriveState=lector("selfdriveState"))
  fuera = solo_datos(motor.tick(12.0, apagado, ts_ms=2))
  assert "openpilot" in fuera
  assert "enabled" not in fuera["openpilot"] and "active" not in fuera["openpilot"]


def test_el_seq_es_monotono_POR_CANAL():
  motor = MotorTelemetria()
  f = fuentes_minimas()
  seqs = []
  for i in range(4):
    for canal, sobre in motor.tick(10.0 + i, f, ts_ms=i):
      if canal == "vehicle":
        seqs.append(sobre["seq"])
  assert seqs == [1, 2, 3, 4]


# --------------------------------------------------------------------------- pos

def _gps(lat, lon, rumbo=0.0, acc=3.0, fix=True):
  return lector("gpsLocationExternal", latitude=lat, longitude=lon, bearingDeg=rumbo,
                horizontalAccuracy=acc, hasFix=fix, satelliteCount=12)


def test_pos_se_decima_por_distancia_y_por_curvatura():
  motor = MotorTelemetria()
  f = fuentes_minimas(gpsLocationExternal=_gps(40.0, -3.0))
  assert "pos" in solo_datos(motor.tick(10.0, f, ts_ms=1))
  # 20 m mas alla y mismo rumbo: por debajo del minimo de 50 m en NORMAL -> no sale.
  cerca = fuentes_minimas(gpsLocationExternal=_gps(40.00018, -3.0))
  assert "pos" not in solo_datos(motor.tick(12.0, cerca, ts_ms=2))
  # Mismo sitio pero girando 20 grados: la muestra que salva la curva SI sale.
  girando = fuentes_minimas(gpsLocationExternal=_gps(40.00018, -3.0, rumbo=20.0))
  assert "pos" in solo_datos(motor.tick(14.0, girando, ts_ms=3))


def test_pos_respeta_el_suelo_de_ritmo():
  motor = MotorTelemetria()
  motor.tick(10.0, fuentes_minimas(gpsLocationExternal=_gps(40.0, -3.0)), ts_ms=1)
  lejos = fuentes_minimas(gpsLocationExternal=_gps(41.0, -3.0))
  assert "pos" not in solo_datos(motor.tick(10.4, lejos, ts_ms=2))   # < 1 s en NORMAL


def test_un_fix_inutil_no_gasta_datos_pero_el_keepalive_sigue_saliendo():
  motor = MotorTelemetria()
  motor.tick(10.0, fuentes_minimas(gpsLocationExternal=_gps(40.0, -3.0)), ts_ms=1)
  basura = fuentes_minimas(gpsLocationExternal=_gps(41.0, -3.0, acc=500.0))
  assert "pos" not in solo_datos(motor.tick(15.0, basura, ts_ms=2))
  keep = CANALES["pos"].keepalive(PERFIL_NORMAL)
  assert "pos" in solo_datos(motor.tick(10.0 + keep + 1.0, basura, ts_ms=3))


def test_sin_fix_no_se_publica_traza_salvo_keepalive():
  motor = MotorTelemetria()
  motor.tick(10.0, fuentes_minimas(gpsLocationExternal=_gps(40.0, -3.0)), ts_ms=1)
  sin_fix = fuentes_minimas(gpsLocationExternal=_gps(41.0, -3.0, fix=False))
  assert "pos" not in solo_datos(motor.tick(15.0, sin_fix, ts_ms=2))


def test_sin_fix_el_0_0_del_receptor_no_viaja_como_posicion():
  """El ublox del comma 4 sin fix manda lat=lon=0: eso no es una posicion."""
  motor = MotorTelemetria()
  d = solo_datos(motor.tick(10.0, fuentes_minimas(gpsLocationExternal=_gps(0.0, 0.0, fix=False)), ts_ms=1))["pos"]
  assert "lat" not in d and "lon" not in d and d["fix"] is False   # sale como "vivo y sin posicion"
  # Al recuperar el fix la posicion sale en el siguiente periodo, sin esperar al keepalive.
  d = solo_datos(motor.tick(11.0, fuentes_minimas(gpsLocationExternal=_gps(40.0, -3.0)), ts_ms=2))["pos"]
  assert d["lat"] == 40.0
  # Sin fix pero con una posicion real (la ultima que conoce el modem) si viaja.
  d = solo_datos(MotorTelemetria().tick(10.0, fuentes_minimas(gpsLocationExternal=_gps(40.0, -3.0, fix=False)), ts_ms=1))["pos"]
  assert d["lat"] == 40.0 and d["fix"] is False


def test_qcomgpsd_da_los_satelites_solo_si_cuadran_y_caben_en_el_Int8():
  """El 3X (qcomgpsd) no rellenaba satelliteCount y `sats` no llegaba nunca a la app.

  Pasa por el desempaquetado REAL de position_report: una clave mal escrita en el helper
  tumbaria qcomgpsd en cada fix (el bucle de main() no tiene try)."""
  import struct
  from openpilot.system.qcomgpsd.qcomgpsd import satelites_en_solucion
  from openpilot.system.qcomgpsd.structs import dict_unpacker, parse_struct, position_report
  fmt, nombres = parse_struct(position_report)
  desempaqueta, _ = dict_unpacker(position_report)

  def sats(gps, glo, bds):
    campos = dict.fromkeys(nombres, 0)
    for c, (usados, vistos) in zip(("Gps", "Glo", "Bds"), (gps, glo, bds), strict=True):
      campos[f"u_Num{c}SvsUsed"], campos[f"u_Total{c}Svs"] = usados, vistos
    return satelites_en_solucion(desempaqueta(struct.pack(fmt, *(campos[n] for n in nombres))))

  assert sats((9, 12), (6, 8), (7, 15)) == 22
  assert sats((13, 12), (6, 8), (7, 15)) == 0            # usados > vistos: dato u offset roto
  assert sats((100, 200), (20, 30), (10, 20)) == 0       # 130 no cabe en satelliteCount (Int8)
  msg = messaging.new_message("gpsLocation")
  msg.gpsLocation.satelliteCount = sats((60, 60), (60, 60), (7, 7))   # 127, el tope exacto
  assert msg.gpsLocation.satelliteCount == 127


def test_sin_fuente_de_posicion_el_canal_pos_no_existe():
  """Es lo que hace efectivo el interruptor de privacidad: el emisor retira la fuente."""
  motor = MotorTelemetria()
  assert "pos" not in solo_datos(motor.tick(10.0, fuentes_minimas(), ts_ms=1))


# --------------------------------------------------------------------------- viaje

def test_el_viaje_abre_y_cierra_con_deviceState_started_y_sella_el_trip_id():
  motor = MotorTelemetria()
  onroad = lector("deviceState", started=True)
  cs = lector("carState", vEgo=20.0)
  mensajes = motor.tick(10.0, {"deviceState": onroad, "carState": cs}, ts_ms=1)
  d = solo_datos(mensajes)
  assert d["trip"] == {"ev": "start"}
  trip_id = motor.trip_id
  assert trip_id and all(sobre.get("trip") == trip_id for _, sobre in mensajes)

  for i in range(1, 11):                          # 10 s a 20 m/s = 200 m
    motor.tick(10.0 + i, {"deviceState": onroad, "carState": cs}, ts_ms=1 + i)

  offroad = lector("deviceState", started=False)
  fin = solo_datos(motor.tick(21.0, {"deviceState": offroad, "carState": lector("carState")}, ts_ms=99))
  resumen = fin["trip"]
  assert resumen["ev"] == "end"
  # 10 s y no 11: la duracion es la INTEGRAL del tiempo observado onroad (t=10..20), igual
  # que dist_km y engaged_s. El tick de cierre ya llega con started=False, y regalarle su
  # segundo entero a la duracion descuadraria engaged_pct contra su propio numerador.
  assert resumen["dur_s"] == 10
  assert resumen["dist_km"] == pytest.approx(0.2, abs=0.01)
  assert resumen["v_max_kph"] == pytest.approx(72.0, abs=0.1)
  assert motor.trip_id is None


def test_el_resumen_cuenta_los_desenganches_solo_en_movimiento():
  motor = MotorTelemetria()
  onroad = lector("deviceState", started=True)
  moviendo = lector("carState", vEgo=20.0)
  activo = lector("selfdriveState", active=True)
  inactivo = lector("selfdriveState")
  motor.tick(10.0, {"deviceState": onroad, "carState": moviendo, "selfdriveState": activo}, ts_ms=1)
  motor.tick(11.0, {"deviceState": onroad, "carState": moviendo, "selfdriveState": activo}, ts_ms=2)
  motor.tick(12.0, {"deviceState": onroad, "carState": moviendo, "selfdriveState": inactivo}, ts_ms=3)
  # Ahora parado: apagar openpilot al aparcar NO es una intervencion.
  parado = lector("carState", vEgo=0.0)
  motor.tick(13.0, {"deviceState": onroad, "carState": parado, "selfdriveState": activo}, ts_ms=4)
  motor.tick(14.0, {"deviceState": onroad, "carState": parado, "selfdriveState": inactivo}, ts_ms=5)
  fin = solo_datos(motor.tick(15.0, {"deviceState": lector("deviceState"), "carState": parado}, ts_ms=6))
  assert fin["trip"]["n_desenganches"] == 1


def test_el_cierre_de_viaje_no_escupe_una_bajada_por_cada_evento_abierto():
  motor = MotorTelemetria()
  onroad = lector("deviceState", started=True)
  ev = lector("onroadEvents", 1)
  motor.tick(10.0, {"deviceState": onroad, "carState": lector("carState"), "onroadEvents": ev}, ts_ms=1)
  fin = solo_datos(motor.tick(11.0, {"deviceState": lector("deviceState"), "carState": lector("carState")}, ts_ms=2))
  assert "trip" in fin and "event" not in fin


# --------------------------------------------------------------------------- lista blanca v1

def test_canales_json_tiene_lista_blanca_en_los_ocho_canales():
  """El mecanismo existia desde el primer dia con la lista VACIA en los ocho, que es lo que
  hacia que se publicara el to_dict() entero: 16,4 MB/h."""
  ruta = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "canales.json")
  with open(ruta) as f:
    canales = json.load(f)["canales"]
  assert len(canales) == 8
  for c in canales:
    assert c["keys_importantes"], f"{c['canal']} sigue publicando el mensaje entero"


def test_las_claves_de_la_lista_blanca_v1_existen_en_el_capnp_de_este_build():
  """Una clave mal escrita aqui no da error: vacia en silencio un campo que el backend lee."""
  ruta = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "canales.json")
  with open(ruta) as f:
    canales = json.load(f)["canales"]
  malas = []
  for c in canales:
    nombre = c["canal"]
    # Se comprueba contra el ESQUEMA, no contra el to_dict() de un mensaje recien creado:
    # pycapnp omite del dict los campos de puntero (structs y listas anidadas) que siguen
    # a nulo, asi que un mensaje vacio no tiene ni 'cruiseState' ni 'leadOne' ni
    # 'laneLineMeta' y el test daria por inexistentes justo las claves anidadas.
    campos = set(lector(nombre).schema.fieldnames)
    for k in c["keys_importantes"]:
      if k not in campos:
        malas.append(f"{nombre}.{k}")
  assert not malas, f"claves de canales.json que no existen en el capnp: {malas}"


def test_el_filtro_v1_deja_pasar_solo_la_lista_blanca_y_sigue_poniendo_el_dongle():
  """`enviar_datos_importantes` es el filtro legacy. Se llama sin construir el emisor (que
  levanta paho, SubMaster y CameraSender): solo necesita dos atributos."""
  from openpilot.orbit.mqtt_envio_general import MQTTEnvioGeneral

  class Falso:
    keys_importantes_por_canal = {"carState": ["vEgo", "leftBlinker"]}
    DongleID = DONGLE

  fuera = MQTTEnvioGeneral.enviar_datos_importantes(Falso(), "carState",
                                                   {"vEgo": 1.0, "leftBlinker": True, "wheelSpeeds": {"fl": 1}})
  assert fuera == {"vEgo": 1.0, "leftBlinker": True, "dongle_id": DONGLE}


# --------------------------------------------------------------------------- compactado de floats

def test_compactar_a_9_cifras_no_cambia_el_valor_de_un_float32():
  """Es el argumento entero del recorte: 9 cifras significativas es la precision con la que
  un IEEE-754 binary32 va y vuelve exacto, y en cereal casi todo es Float32."""
  import struct
  for crudo in (-3.456789, 22.345678, 0.0021345, 1234.5678, 0.0, -0.00001234):
    f32 = struct.unpack("f", struct.pack("f", crudo))[0]
    recortado = tel.compacta_floats(f32)
    assert struct.pack("f", recortado) == struct.pack("f", f32), crudo
  # Y nunca se come digitos de la parte entera (un epoch en ms tiene 13).
  assert tel.compacta_floats(1755960000000.0) == 1755960000000.0


def test_compactar_respeta_los_no_finitos_para_que_el_saneado_los_vea():
  assert math.isnan(tel.compacta_floats(float("nan")))
  assert tel.sanea_no_finitos(tel.compacta_floats(float("inf"))) is None


# --------------------------------------------------------------------------- forma de las fuentes de evento

def test_onroadEventsSP_es_un_struct_con_lista_dentro_y_sus_eventos_SI_se_publican():
  """`onroadEvents` es List(OnroadEvent) y `onroadEventsSP` es un struct con `events`
  dentro. Iterar el struct como lista lanza TypeError, y en un bucle defensivo eso
  significa que los ~26 eventos de sunnypilot no salen NUNCA sin que nadie se entere."""
  from cereal import messaging as m
  assert tel.FUENTES_EVENTO["onroadEvents"] == ""
  assert tel.FUENTES_EVENTO["onroadEventsSP"] == "events"
  # La forma declarada tiene que ser la del capnp de este build.
  assert "events" in m.new_message("onroadEventsSP").onroadEventsSP.schema.fieldnames

  sp = m.new_message("onroadEventsSP")
  eventos = sp.onroadEventsSP.init("events", 1)
  eventos[0].name = "lkasEnable"
  eventos[0].enable = True
  motor = MotorTelemetria()
  fuera = solo_datos(motor.tick(10.0, fuentes_minimas(onroadEventsSP=sp.as_reader().onroadEventsSP), ts_ms=1))
  assert fuera["event"]["ev"] == [{"cod": 201, "nom": "lkasEnable", "sev": "info", "on": True}]


# --------------------------------------------------------------------------- emisor (cableado)

class _ClienteFalso:
  def __init__(self, rc=0):
    self.publicado = []
    self.rc = rc          # rc != 0 = "el mensaje NO salio" (tipico MQTT_ERR_NO_CONN)

  def is_connected(self):
    return True

  def publish(self, topic, payload, qos=0, retain=False):
    self.publicado.append((topic, payload, qos, retain))
    rc = self.rc
    class _Info:
      pass
    _Info.rc = rc
    return _Info()


class _SubMasterFalso:
  def __init__(self, datos):
    self.data = datos
    self.alive = dict.fromkeys(datos, True)
    # `recv_frame` es lo que mira el camino v1 para saber si llego algo NUEVO desde su
    # ultima publicacion (el tick va a 4 Hz y v1 publica a 1 Hz). Todo lo de `datos` cuenta
    # como llegado; el arranque de verdad (recv_frame=0, seen=False) lo prueba el SubMaster
    # real en test_v1_no_publica_el_mensaje_CERO_de_un_servicio_que_nunca_llego.
    self.recv_frame = dict.fromkeys(datos, 1)
    self.seen = dict.fromkeys(datos, True)

  def __getitem__(self, k):
    return self.data[k]


class _ParamsFalsos:
  def __init__(self, mute=False):
    self.mute = mute

  def get_bool(self, k):
    return self.mute if k == "OrbitPrivacyMute" else False

  def get(self, k):
    raise KeyError(k)          # clave sin registrar: el caso REAL de OrbitTelemetryProfile


class _SpoolFalso:
  """Spool en RAM con la misma superficie que usa el emisor. Los tests no tocan /data.

  `acepta=False` imita el spool DESACTIVADO (disco lleno, base corrupta) o un canal
  excluido: guardar() devuelve False, que es la senal por la que el emisor revierte el
  sello del canal on-change.
  """

  def __init__(self, activo=True, acepta=True):
    self.activo = activo
    self.acepta = acepta
    self.en_ram = []      # (canal, cuerpo, dongle) aun sin volcar
    self.en_disco = []    # ya volcadas por flush()
    self.perdidos = {}    # aceptadas y luego MUERTAS sin publicarse, por canal
    self.purgas = 0

  def guardar(self, canal, cuerpo, ts_ms=None, dongle=""):
    if not self.activo or not self.acepta:
      return False
    self.en_ram.append((canal, cuerpo, dongle))
    return True

  def flush(self):
    self.en_disco.extend(self.en_ram)
    n, self.en_ram = len(self.en_ram), []
    return n

  def hay_pendientes(self):
    return bool(self.en_ram or self.en_disco)

  def drenar(self, publicar, dongle=None, privacidad=False, **kwargs):
    from openpilot.orbit.spool import CANALES_POSICION, MuestraSpool
    quedan, corte = [], False
    for canal, cuerpo, dng in self.en_disco:
      if corte:
        quedan.append((canal, cuerpo, dng))
        continue
      if privacidad and canal in CANALES_POSICION:
        continue          # descartada: ni se publica ni se conserva
      cuerpo_marcado = json.dumps({**json.loads(cuerpo), "backfill": True})
      if publicar(MuestraSpool(canal=canal, cuerpo=cuerpo_marcado, ts_ms=0, dongle=dng)):
        continue
      corte = True
      quedan.append((canal, cuerpo, dng))
    self.en_disco = quedan

  def purgar_posicion(self):
    from openpilot.orbit.spool import CANALES_POSICION
    self.purgas += 1
    n = 0
    for cola in (self.en_ram, self.en_disco):
      quedan = [f for f in cola if f[0] not in CANALES_POSICION]
      n += len(cola) - len(quedan)
      cola[:] = quedan
    return n

  def perder(self, canal):
    """Lo que hace el Spool de verdad cuando una fila YA ACEPTADA muere: eviccion de RAM,
    lote rechazado con el disco al tope, poda del disco o apagado en caliente."""
    self.perdidos[canal] = self.perdidos.get(canal, 0) + 1

  def canales_perdidos(self):
    perdidos, self.perdidos = self.perdidos, {}
    return perdidos

  @property
  def guardadas(self):
    return self.en_ram + self.en_disco


def _emisor(datos, mute=False, spool=None):
  """Emisor sin __init__: construirlo de verdad levanta paho, SubMaster y CameraSender."""
  from openpilot.orbit.mqtt_envio_general import MQTTEnvioGeneral
  e = object.__new__(MQTTEnvioGeneral)
  e.conectado = True
  e.dongle_valido = True
  e.DongleID = DONGLE
  e.mqttc = _ClienteFalso()
  e.sm = _SubMasterFalso(datos)
  e.servicios_v2 = list(datos)
  e.motor_v2 = MotorTelemetria(PERFIL_NORMAL, 0.0)
  e.params = _ParamsFalsos(mute)
  e._last_perfil_check = 0.0
  e.PERFIL_RELOAD_SECS = 5.0
  e._perfil_avisado = False
  e._privacy_ts = 0.0
  e._privacy_cache = False
  e._privacy_avisado = False
  e._last_rc_log = 0.0
  e.RC_LOG_SECS = 30.0
  # El spool se inyecta ya construido: _spool() devuelve _spool_obj sin llamar a
  # get_spool(), asi que ningun test abre SQLite en /data.
  e._spool_obj = _SpoolFalso() if spool is None else spool
  e._spool_roto = False
  e._last_spool = -1e9
  e.SPOOL_SECS = 1.0
  return e


def test_el_emisor_publica_la_telemetria_v2_en_el_topic_del_contrato():
  datos = {"carState": lector("carState", vEgo=20.0, leftBlinker=True)}
  e = _emisor(datos)
  e._ciclo_v2(10.0)
  topics = [t for t, *_ in e.mqttc.publicado]
  assert f"orbit/v2/tel/{DONGLE}/vehicle" in topics
  cuerpo = json.loads(next(p for t, p, *_ in e.mqttc.publicado if t.endswith("/vehicle")))
  assert cuerpo["v"] == 2 and cuerpo["sv"] == 1 and cuerpo["seq"] == 1
  assert cuerpo["d"]["blink_left"] is True
  assert DONGLE not in json.dumps(cuerpo)


def test_una_clave_de_perfil_sin_registrar_no_rompe_nada_y_deja_el_perfil_en_normal():
  e = _emisor({"carState": lector("carState", vEgo=20.0)})
  e._ciclo_v2(10.0)
  assert e.motor_v2.perfil == PERFIL_NORMAL
  assert e._perfil_avisado is True            # avisa UNA vez, no en cada tick


def test_el_interruptor_de_privacidad_silencia_la_posicion_y_deja_el_resto():
  datos = {"carState": lector("carState", vEgo=20.0),
           "gpsLocationExternal": lector("gpsLocationExternal", latitude=40.0, longitude=-3.0, hasFix=True)}
  libre = _emisor(datos, mute=False)
  libre._ciclo_v2(10.0)
  assert any(t.endswith("/pos") for t, *_ in libre.mqttc.publicado)

  callado = _emisor(datos, mute=True)
  callado._ciclo_v2(10.0)
  topics = [t for t, *_ in callado.mqttc.publicado]
  assert not any(t.endswith("/pos") for t in topics)
  assert any(t.endswith("/vehicle") for t in topics), "el mute es de POSICION, no de todo"


def test_una_fuente_que_no_esta_alive_no_llega_al_motor():
  datos = {"carState": lector("carState", vEgo=20.0), "deviceState": lector("deviceState", started=True)}
  e = _emisor(datos)
  e.sm.alive["deviceState"] = False
  assert "deviceState" not in e._fuentes_v2()
  e._ciclo_v2(10.0)
  # Sin deviceState no se abre viaje: el `started` de un mensaje que nunca llego es False.
  assert not any(t.endswith("/trip") for t, *_ in e.mqttc.publicado)


# ------------------------------------------------------- A2: el estado no se da por entregado

def _estado_openpilot(activo):
  """Fuentes con `selfdriveState` y `deviceState` vivos: el canal `openpilot` es on-change."""
  return {"carState": lector("carState", vEgo=20.0),
          "selfdriveState": lector("selfdriveState", enabled=activo, active=activo)}


def test_un_publish_que_no_sale_NO_sella_el_estado_del_canal_on_change():
  """Sin esto, un rc!=0 dejaba el canal callado hasta el keepalive (60 s en NORMAL).

  El spool desactivado es el caso peor a proposito: sin red de reenvio, la unica forma de
  no perder el cambio de estado es que el motor lo vuelva a proponer.
  """
  e = _emisor(_estado_openpilot(True), spool=_SpoolFalso(acepta=False))
  e.mqttc.rc = 1                                    # el publish no sale
  e._ciclo_v2(10.0)
  assert any(t.endswith("/openpilot") for t, *_ in e.mqttc.publicado)

  # Siguiente periodo del canal (1 s en NORMAL) con el MISMO estado y el enlace ya bueno:
  # tiene que volver a salir, porque el anterior nunca llego.
  e.mqttc = _ClienteFalso()
  e.sm = _SubMasterFalso(_estado_openpilot(True))
  e._ciclo_v2(11.5)
  assert any(t.endswith("/openpilot") for t, *_ in e.mqttc.publicado), \
    "el estado se dio por entregado sin haberse publicado"


def test_un_publish_que_SI_sale_sella_el_estado_y_no_se_repite():
  e = _emisor(_estado_openpilot(True))
  e._ciclo_v2(10.0)
  assert any(t.endswith("/openpilot") for t, *_ in e.mqttc.publicado)
  e.mqttc = _ClienteFalso()
  e.sm = _SubMasterFalso(_estado_openpilot(True))
  e._ciclo_v2(11.5)
  assert not any(t.endswith("/openpilot") for t, *_ in e.mqttc.publicado), \
    "un canal on-change no puede republicar un estado que no ha cambiado"


def test_lo_que_no_sale_se_guarda_en_el_spool_en_vez_de_perderse():
  """`event` y `trip` son mensajes UNICOS: un rc!=0 los borraba de la historia."""
  datos = {"carState": lector("carState", vEgo=20.0),
           "deviceState": lector("deviceState", started=True)}
  spool = _SpoolFalso()
  e = _emisor(datos, spool=spool)
  e.mqttc.rc = 1
  e._ciclo_v2(10.0)
  canales = [c for c, *_ in spool.guardadas]
  assert "trip" in canales, "el arranque de viaje se perdio sin dejar rastro"
  assert all(d == DONGLE for _, _, d in spool.guardadas)


def test_si_el_spool_conserva_la_muestra_el_sello_se_MANTIENE_y_no_se_duplica():
  """Revertir ademas de spoolear publicaria dos veces el mismo estado."""
  spool = _SpoolFalso()
  e = _emisor(_estado_openpilot(True), spool=spool)
  e.mqttc.rc = 1
  e._ciclo_v2(10.0)
  assert any(c == "openpilot" for c, *_ in spool.guardadas)
  e.mqttc = _ClienteFalso()
  e.sm = _SubMasterFalso(_estado_openpilot(True))
  e._ciclo_v2(11.5)
  # Lo unico que puede volver a salir es el REENVIO de esa misma muestra (marcado como
  # backfill). Una publicacion viva del mismo estado seria el duplicado que se quiere evitar.
  vivos = [t for t, pl, *_ in e.mqttc.publicado
           if t.endswith("/openpilot") and not json.loads(pl).get("backfill")]
  assert not vivos, "el estado se publico dos veces: una diferida y otra viva"


def test_sin_enlace_no_se_intenta_publicar_pero_el_motor_sigue_tickeando():
  """Antes se salia ANTES de tickear: los flancos y el `dt` del viaje del corte se perdian."""
  datos = {"carState": lector("carState", vEgo=20.0),
           "deviceState": lector("deviceState", started=True)}
  spool = _SpoolFalso()
  e = _emisor(datos, spool=spool)
  e.conectado = False
  e._ciclo_v2(10.0)
  assert e.mqttc.publicado == [], "sin enlace no se intenta el publish"
  assert e.motor_v2.trip_id is not None, "el viaje tiene que abrirse aunque no haya broker"
  assert [c for c, *_ in spool.guardadas], "lo que no sale tiene que quedar encolado"


def test_al_volver_el_enlace_se_reenvia_lo_diferido_marcado_como_backfill():
  datos = {"carState": lector("carState", vEgo=20.0)}
  spool = _SpoolFalso()
  e = _emisor(datos, spool=spool)
  e.conectado = False
  e._ciclo_v2(10.0)
  assert spool.hay_pendientes()

  e.conectado = True
  e.mqttc = _ClienteFalso()
  e._last_spool = -1e9
  e._ciclo_v2(11.0)
  diferidas = [json.loads(pl) for t, pl, *_ in e.mqttc.publicado if json.loads(pl).get("backfill")]
  assert diferidas, "el spool no se dreno al volver el enlace"
  assert not spool.hay_pendientes()


def test_el_reenvio_diferido_no_toca_la_presencia():
  """Un backfill es de hace un rato: si marcase presencia pintaria conectado un coche apagado."""
  import inspect
  from openpilot.orbit.mqtt_envio_general import MQTTEnvioGeneral
  for metodo in (MQTTEnvioGeneral._publicar_diferida, MQTTEnvioGeneral._mantener_spool):
    # Se mira el CODIGO, no el docstring: el docstring nombra estos simbolos justamente
    # para explicar por que no se tocan.
    cuerpo = inspect.getsource(metodo).replace(metodo.__doc__ or "", "")
    for prohibido in ("OrbitLastPublish", "OrbitConnected", "_publish_presence_online",
                      "_last_heartbeat", "conectado ="):
      assert prohibido not in cuerpo, f"{metodo.__name__} toca la presencia ({prohibido})"


def test_sin_identidad_no_se_publica_NI_se_encola():
  """Una muestra grabada bajo el literal 'DongleID' se atribuiria al vehiculo fantasma."""
  spool = _SpoolFalso()
  e = _emisor({"carState": lector("carState", vEgo=20.0)}, spool=spool)
  e.dongle_valido = False
  e.conectado = False
  e._ciclo_v2(10.0)
  assert e.mqttc.publicado == []
  assert not spool.hay_pendientes()


def test_revertir_solo_deshace_lo_sellado_en_el_tick_en_curso():
  motor = MotorTelemetria(PERFIL_NORMAL, 0.0)
  motor.tick(10.0, _estado_openpilot(True), ts_ms=1)
  assert motor.revertir("openpilot") is True
  assert motor.revertir("openpilot") is False, "no se puede revertir dos veces el mismo sello"
  # Un canal que no es on-change no sella estado: revertir es un no-op declarado.
  assert motor.revertir("vehicle") is False


# ------------------------------------------------------- A3: la privacidad cubre la posicion derivada

def test_el_interruptor_de_privacidad_calla_tambien_el_nombre_de_la_via():
  """`road` publica road_name y los limites: es posicion, aunque sea derivada."""
  datos = {"carState": lector("carState", vEgo=20.0),
           "liveMapDataSP": lector("liveMapDataSP", roadName="Calle Mayor",
                                   speedLimit=13.888, speedLimitValid=True)}
  libre = _emisor(datos, mute=False)
  libre._ciclo_v2(10.0)
  cuerpos = [json.loads(pl) for t, pl, *_ in libre.mqttc.publicado if t.endswith("/road")]
  assert cuerpos and cuerpos[0]["d"]["road_name"] == "Calle Mayor"

  callado = _emisor(datos, mute=True)
  callado._ciclo_v2(10.0)
  topics = [t for t, *_ in callado.mqttc.publicado]
  assert not any(t.endswith("/road") for t in topics)
  assert "Calle Mayor" not in json.dumps(callado.mqttc.publicado)
  assert any(t.endswith("/vehicle") for t in topics), "el mute es de POSICION, no de todo"


def test_con_el_mute_puesto_la_posicion_no_se_queda_escrita_en_el_spool():
  datos = {"carState": lector("carState", vEgo=20.0),
           "gpsLocationExternal": lector("gpsLocationExternal", latitude=40.0, longitude=-3.0, hasFix=True),
           "liveMapDataSP": lector("liveMapDataSP", roadName="Calle Mayor")}
  spool = _SpoolFalso()
  e = _emisor(datos, mute=True, spool=spool)
  e.conectado = False
  e._ciclo_v2(10.0)
  encolado = json.dumps(spool.guardadas)
  assert "Calle Mayor" not in encolado
  assert "pos" not in [c for c, *_ in spool.guardadas]


def test_el_canal_pos_esta_excluido_del_spool_por_defecto(tmp_path):
  """Si se spoolease, una posicion no publicada quedaria escrita en disco esperando red."""
  from openpilot.orbit.spool import CANALES_NO_SPOOLEADOS, Spool
  assert "pos" in CANALES_NO_SPOOLEADOS
  s = Spool(ruta=str(tmp_path / "spool"))
  try:
    assert s.activo, s.motivo
    assert s.guardar("pos", '{"d":{"lat":40.0}}') is False
    assert s.guardar("vehicle", '{"d":{"speed_kph":10.0}}') is True
  finally:
    s.cerrar()


def _emisor_v1(canales, mute=False):
  """Emisor recortado para ejercitar SOLO el bucle de canales de `_ciclo_v1`.

  Los pasos de mantenimiento (broker, toggles, enrolamiento) se anulan a proposito: cada
  uno abre ficheros o Params y no tienen nada que ver con lo que se prueba aqui.
  """
  datos = {c: lector(c) for c in canales}
  e = _emisor(datos, mute=mute)
  e._maybe_reload_broker = lambda: None
  e._maybe_reload_canales = lambda: None
  e._maybe_announce_enroll = lambda: None
  e._camera_pendiente = False
  e._v1_frame = {}
  e._last_heartbeat = time.monotonic()      # el latido no toca en este tick
  e.HEARTBEAT_SECS = 3.0
  e.enabled_items = [{"canal": c, "topic": "telemetry_mqtt/{0}/" + c} for c in canales]
  e.keys_importantes_por_canal = {c: ["latitude", "longitude", "vEgo"] for c in canales}
  return e


# ------------------------------------------------------- A1: la lista blanca v1

def _lista_blanca_v1():
  ruta = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "canales.json")
  with open(ruta) as f:
    return {c["canal"]: c["keys_importantes"] for c in json.load(f)["canales"]}


def test_la_lista_blanca_v1_lleva_las_senales_de_cabina_que_lee_la_app():
  """La app las lee de PRIMER NIVEL y el filtro v1 tambien filtra de primer nivel.

  `cabin_state.dart` (_legadoCarState) hace cs['doorOpen'] y cs['seatbeltUnlatched'], y
  ademas los usa como MARCAS del canal; sin ellos en la lista blanca el panel de Cabina no
  puede pintar puerta abierta ni cinturon suelto por el camino v1, aunque el dato exista.
  """
  claves = _lista_blanca_v1()["carState"]
  for k in ("vEgo", "gearShifter", "standstill", "doorOpen", "seatbeltUnlatched",
            "leftBlinker", "rightBlinker", "gasPressed", "brakePressed", "steeringPressed"):
    assert k in claves, f"carState.{k} lo lee la app y la lista blanca v1 lo tira"


def test_la_lista_blanca_v1_de_gps_lleva_hasFix():
  """El backend (`guardar_parcial`) y la app (`_legadoPos`) no toman la posicion v1 de un mensaje
  con `hasFix` false: el modem del 3X publica coordenadas antes del fix, a kilometros del coche,
  y la ficha y el listado prefieren la columna v1 a la ultima posicion v2. Sin `hasFix` en la
  lista blanca esas guardas no ven nada y el punto malo pasa."""
  blanca = _lista_blanca_v1()
  for canal in ("gpsLocation", "gpsLocationExternal"):
    assert "hasFix" in blanca[canal], f"{canal}: sin hasFix se guardan posiciones sin fix"


def test_el_grupo_deprecated_NO_entra_en_la_lista_blanca_v1():
  """Guardia contra "arreglar" el banner de alerta metiendo el grupo entero.

  La app lee controlsState['deprecated']['alertText1'], pero en ESTE build nadie escribe
  ese grupo: el unico publicador vivo de controlsState es selfdrive/controls/controlsd.py
  y solo pone curvature, desiredCurvature, longControlState, forceDecel, los *AccelCmd,
  los monotimes y lateralControlState. Anadir 'deprecated' son ~950 B por ciclo de valores
  por defecto y el banner seguiria apagado: la alerta viva vive en selfdriveState
  (selfdrived.py) y llega a la app por el canal v2 `openpilot` (alert_text1).

  Si un rebase empieza a rellenar el grupo, este test cae y toca volver a decidir.
  """
  for canal, claves in _lista_blanca_v1().items():
    assert "deprecated" not in claves, f"{canal} publica el grupo deprecated entero"
  raiz = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
  with open(os.path.join(raiz, "selfdrive", "controls", "controlsd.py")) as f:
    fuente = f.read()
  assert "cs.deprecated" not in fuente, \
    "controlsd ya rellena controlsState.deprecated: revisa la decision de canales.json"


# ------------------------------------------------------- A3: la privacidad tapa tambien el v1

def test_el_interruptor_de_privacidad_calla_los_canales_v1_de_posicion():
  """El camino LEGACY sigue vivo durante la migracion y lleva latitude/longitude crudas."""
  canales = ["gpsLocation", "gpsLocationExternal", "carState"]
  libre = _emisor_v1(canales, mute=False)
  libre._ciclo_v1()
  topics = [t for t, *_ in libre.mqttc.publicado]
  assert any(t.endswith("/gpsLocation") for t in topics)
  assert any(t.endswith("/gpsLocationExternal") for t in topics)

  callado = _emisor_v1(canales, mute=True)
  callado._ciclo_v1()
  topics = [t for t, *_ in callado.mqttc.publicado]
  assert not any("gpsLocation" in t for t in topics), \
    "con OrbitPrivacyMute puesto el camino v1 seguia publicando la posicion"
  assert any(t.endswith("/carState") for t in topics), "el mute es de POSICION, no de todo"


# ------------------------------------------------------- A4: v1 publica solo lo que ha llegado

def _llega(sm, servicio, **campos):
  """Entrega un mensaje al SubMaster DE VERDAD por la misma puerta que usa update()."""
  msg = messaging.new_message(servicio)
  cuerpo = getattr(msg, servicio)
  for k, v in campos.items():
    setattr(cuerpo, k, v)
  sm.update_msgs(time.monotonic(), [msg.as_reader()])


def _canales_v1_publicados(e):
  canales = {c["canal"] for c in e.enabled_items}
  return [t.rsplit("/", 1)[1] for t, *_ in e.mqttc.publicado if t.rsplit("/", 1)[1] in canales]


def test_v1_no_publica_el_mensaje_CERO_de_un_servicio_que_nunca_llego():
  """Al arrancar, el SubMaster de verdad tiene recv_frame=0 y un mensaje TODO A CERO por
  servicio. El camino v1 lo publicaba: en un 3X gpsLocationExternal no llega nunca, y cada
  arranque machacaba en el backend la ultima posicion buena con (0,0)."""
  canales = ["gpsLocation", "gpsLocationExternal", "carState"]
  e = _emisor_v1(canales)
  e.sm = messaging.SubMaster(canales)        # el de verdad y sin publicador: nada ha llegado
  assert e.sm.recv_frame["gpsLocationExternal"] == 0 and not e.sm.seen["gpsLocationExternal"]
  e._ciclo_v1()
  assert _canales_v1_publicados(e) == []

  _llega(e.sm, "gpsLocation", latitude=40.1, longitude=-3.1)
  e._ciclo_v1()
  e._ciclo_v1()                              # sin nada nuevo no se repite
  assert _canales_v1_publicados(e) == ["gpsLocation"]
  cuerpo = json.loads(next(p for t, p, *_ in e.mqttc.publicado if t.endswith("/gpsLocation")))
  assert cuerpo["latitude"] == pytest.approx(40.1)


def test_recrear_el_SubMaster_no_deja_mudo_el_camino_v1():
  """Tocar un toggle de canal recrea el SubMaster y sus recv_frame vuelven a empezar en 0.
  Con los del SubMaster anterior guardados, el v1 se callaba hasta alcanzarlos."""
  e = _emisor_v1(["carState"])
  e.lista_suscripciones = ["carState"]
  e._v1_frame = {"carState": 5000}           # lo publicado con el SubMaster anterior
  e.init_submaster()
  _llega(e.sm, "carState", vEgo=3.0)         # frame 0 del SubMaster nuevo
  e._ciclo_v1()
  assert _canales_v1_publicados(e) == ["carState"]


def test_el_cableado_del_spool_funciona_contra_el_Spool_DE_VERDAD(tmp_path):
  """El resto de pruebas usan un doble; esta usa `orbit.spool.Spool` sobre un directorio
  temporal, para que un cambio de firma entre los dos modulos no pase inadvertido."""
  from openpilot.orbit.spool import Spool
  datos = {"carState": lector("carState", vEgo=20.0),
           "deviceState": lector("deviceState", started=True)}
  s = Spool(ruta=str(tmp_path / "spool"))
  assert s.activo, s.motivo
  try:
    e = _emisor(datos, spool=s)
    e.conectado = False
    e._ciclo_v2(10.0)                 # sin enlace: todo al spool
    assert e.mqttc.publicado == []
    assert s.hay_pendientes()

    e.conectado = True
    e.mqttc = _ClienteFalso()
    e._last_spool = -1e9
    e._ciclo_v2(11.0)                 # vuelve el enlace: se drena
    cuerpos = [json.loads(pl) for _, pl, *_ in e.mqttc.publicado]
    reenviados = [c for c in cuerpos if c.get("backfill") is True]
    assert reenviados, "el spool real no se dreno"
    assert all("ts_ms" in c for c in reenviados)
    assert not s.hay_pendientes()
  finally:
    s.cerrar()


# ------------------------------------- B2: la privacidad tambien manda sobre lo ENCOLADO

def _datos_de_via(calle="Calle Mayor"):
  return {"carState": lector("carState", vEgo=20.0),
          "liveMapDataSP": lector("liveMapDataSP", roadName=calle)}


def test_el_mute_pulsado_DESPUES_de_encolar_no_publica_la_traza_al_volver_la_cobertura(tmp_path):
  """La secuencia real, con el Spool DE VERDAD: el coche circula sin cobertura y encola
  `road` con el nombre de la via; el conductor pulsa el interruptor; vuelve la cobertura.
  Retirar liveMapDataSP en la captura no ve nada de esto, porque lo encolado ya estaba
  escrito antes de pulsar. El test existente solo cubria el mute puesto ANTES de encolar.
  """
  from openpilot.orbit.spool import Spool
  s = Spool(ruta=str(tmp_path / "spool"))
  assert s.activo, s.motivo
  try:
    e = _emisor(_datos_de_via(), spool=s)
    e.conectado = False
    e._ciclo_v2(10.0)                       # sin cobertura: la via se queda encolada
    assert s.hay_pendientes()

    e.params.mute = True                    # el conductor pulsa el interruptor...
    e._privacy_ts = 0.0                     # ...y el cache de 1 s no puede taparlo
    e.conectado = True
    e.mqttc = _ClienteFalso()
    e._last_spool = -1e9
    e._ciclo_v2(11.0)                       # vuelve la cobertura

    topics = [t for t, *_ in e.mqttc.publicado]
    assert "Calle Mayor" not in json.dumps(e.mqttc.publicado), \
      "se publico la traza que estaba encolada de antes del mute"
    assert not any(t.endswith("/road") for t in topics)
    assert any(t.endswith("/vehicle") for t in topics), "el mute es de POSICION, no de todo"
  finally:
    s.cerrar()


def test_con_el_mute_puesto_la_traza_encolada_se_borra_aunque_no_vuelva_la_cobertura(tmp_path):
  """Sin enlace no se drena, asi que filtrar solo al drenar dejaria la traza en disco
  esperando: bastaria con quitar el mute antes de recuperar cobertura para que saliera."""
  from openpilot.orbit.spool import Spool
  s = Spool(ruta=str(tmp_path / "spool"))
  assert s.activo, s.motivo
  try:
    e = _emisor(_datos_de_via("Gran Via"), spool=s)
    e.conectado = False
    e._ciclo_v2(10.0)
    assert dict(s._con.execute("SELECT canal, COUNT(*) FROM cola GROUP BY 1").fetchall()).get("road")

    e.params.mute = True
    e._privacy_ts = 0.0
    e._last_spool = -1e9
    e._ciclo_v2(11.0)                       # sigue SIN cobertura

    filas = dict(s._con.execute("SELECT canal, COUNT(*) FROM cola GROUP BY 1").fetchall())
    assert "road" not in filas, "la via encolada sigue en disco esperando a que se quite el mute"
    assert filas.get("vehicle"), "el mute es de POSICION, no de todo"
  finally:
    s.cerrar()


# ------------------------------- A1: que el spool la acepte no es garantia de que salga

def test_una_muestra_que_el_spool_acepta_y_luego_pierde_deshace_el_sello():
  """`guardar()` devolviendo True solo dice que la fila entro en la cola. Si muere despues
  (eviccion de RAM, lote rechazado con el disco al tope, poda, apagado en caliente) el
  sello se quedaba puesto y el canal daba el estado por entregado: `openpilot` 60 s callado
  en NORMAL pintando "actuando" un coche ya desenganchado, y `road` para siempre en AHORRO,
  donde no hay keepalive que lo rescate."""
  spool = _SpoolFalso()
  e = _emisor(_estado_openpilot(True), spool=spool)
  e.mqttc.rc = 1                                   # el publish vivo no sale
  e._ciclo_v2(10.0)
  assert any(c == "openpilot" for c, *_ in spool.guardadas)

  # El spool la acepto y luego no pudo conservarla (aqui, la poda del disco).
  spool.en_disco = [f for f in spool.en_disco if f[0] != "openpilot"]
  spool.perder("openpilot")

  e.mqttc = _ClienteFalso()
  e.sm = _SubMasterFalso(_estado_openpilot(True))
  e._ciclo_v2(11.5)                                # el mantenimiento recoge la perdida
  assert spool.perdidos == {}, "el emisor no vacio el registro de perdidas del spool"

  e.mqttc = _ClienteFalso()
  e.sm = _SubMasterFalso(_estado_openpilot(True))
  e._ciclo_v2(13.0)
  vivos = [t for t, pl, *_ in e.mqttc.publicado
           if t.endswith("/openpilot") and not json.loads(pl).get("backfill")]
  assert vivos, "el estado se dio por entregado: la muestra se perdio y el canal se callo"


def test_las_perdidas_se_recogen_aunque_el_spool_este_desactivado():
  """_desactivar() tira la cola de RAM: justo cuando el spool deja de funcionar es cuando
  mas falta hace enterarse de lo que se llevo por delante."""
  spool = _SpoolFalso(activo=False)
  spool.perder("road")
  e = _emisor(_estado_openpilot(True), spool=spool)
  e._ciclo_v2(10.0)
  assert spool.perdidos == {}


def test_invalidar_sello_vale_fuera_del_tick_que_sello_y_revertir_no():
  """La perdida se descubre al volcar o al podar, que es DESPUES del tick que sello, y
  `revertir` solo deshace lo sellado en el tick en curso (`_hash_previo` se limpia)."""
  motor = MotorTelemetria(PERFIL_NORMAL, 0.0)
  motor.tick(10.0, _estado_openpilot(True), ts_ms=1)
  motor.tick(11.5, _estado_openpilot(True), ts_ms=2)      # otro tick: el sello ya no es del tick
  assert motor.revertir("openpilot") is False

  assert motor.invalidar_sello("openpilot") is True
  # Sin sello que olvidar es un no-op declarado, igual que revertir.
  assert motor.invalidar_sello("openpilot") is False
  assert motor.invalidar_sello("vehicle") is False

  salida = motor.tick(13.0, _estado_openpilot(True), ts_ms=3)
  assert any(c == "openpilot" for c, _ in salida), "el estado no se volvio a proponer"
