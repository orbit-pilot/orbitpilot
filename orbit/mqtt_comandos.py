#!/usr/bin/env python3
"""Cliente MQTT de MANDO de ORBIT: suscripcion, puente v1->v2 y ACK.

TODO comando -- venga del namespace v2 (orbit/v2/cmd/<dongle>) o de un topic v1 de la
epoca de telemetry_config/ -- entra por el MISMO sitio: CommandRouter. Este fichero ya no
escribe ningun Param de actuador desde el callback de paho.

    orbit/v2/cmd/<dongle>  --------------------.
                                                >-- CommandRouter -- cola -- handler
    telemetry_config/<dongle>/<v1>  -- puente --'      (gates, modo, TTL, seq, ACK)

El puente v1 (seccion 13 del diseno, migracion) traduce topic+payload a (verbo, args) y
llama a router.submit_local(), que FABRICA UN SOBRE v2 de verdad. No hay un segundo
camino con reglas propias: si lo hubiera, el v1 seria el agujero por el que entra lo que
el contrato v2 rechaza (que es exactamente como el payload 'false' acababa disparando un
cambio de carril).

Lo que NO pasa por el router, y por que: `jetson_config`, `camera_config`, `enroll_ack` y
`speed_increment` no son verbos del catalogo de la seccion 6 -- son configuracion e
identidad, no ordenes de conduccion, y ninguno mueve un actuador. Estan enumerados uno a
uno en el docstring de on_message con lo que hace cada uno.
"""
import functools
import json
import time
import threading
import paho.mqtt.client as mqtt
from openpilot.common.params import Params
from openpilot.orbit import config_broker
from openpilot.common.swaglog import cloudlog
import os

from openpilot.orbit import config_v2 as cfg2
from openpilot.orbit.command_router import CommandRouter
from openpilot.orbit.command_spec import TOPIC_CAPS, TOPIC_CMD, ahora_epoch_ms, get_spec
from openpilot.orbit.command_state import get_command_plane

# Politica del reintento del connect() inicial. La COMPARTEN los dos hilos de conexion
# (mqtt_envio_general.py la importa de aqui) para que telemetria y mandos no puedan
# divergir. Antes era un 5 s plano: con el broker caido eran 2 lineas por cliente cada
# 5 s en el rlog, indefinidamente.
#   fallo 1 -> 5 s, fallo 2 -> 10 s, fallo 3 -> 20 s, del 4 en adelante -> 60 s (tope).
CONN_RETRY_SECS = (5.0, 10.0, 20.0, 60.0)
# config_mqtt.json se distribuye con "broker": "" y paho rechaza connect("") al instante
# ("Invalid host."): no hay nada que reintentar. El hilo se queda ESPERANDO -- no sale:
# nadie mas volveria a intentarlo, la reconexion automatica de paho solo existe tras un
# primer connect() bueno -- y mira cada tanto si ya hay broker. La relectura en caliente
# del JSON relanza el hilo en cuanto la UI escribe uno; esta espera es la red de seguridad.
CONN_SIN_BROKER_SECS = 30.0


def espera_reintento(fallos_previos: int) -> float:
  """Segundos a esperar tras un connect() fallido: 5, 10, 20 y luego 60 fijo.

  `fallos_previos` es cuantos connect() habian fallado YA antes de este (0 en el primero).
  """
  return CONN_RETRY_SECS[min(fallos_previos, len(CONN_RETRY_SECS) - 1)]


# Roles de la cuenta ORBIT del dueno que el firmware acepta en el enroll_ack. Otro valor
# (o ninguno, backend viejo) borra OrbitOwnerRole: la UI no pinta un rol que no conoce.
ROLES_DUENO = ("user", "developer", "superadmin")
PARAM_OWNER_ROLE = "OrbitOwnerRole"


@functools.cache
def tipo_dispositivo() -> str | None:
  """Modelo del comma (tici | tizi | mici | pc) para caps y el anuncio de enrolamiento.

  Cacheado tambien cuando falla: un devicetree ilegible no se relee (ni se loguea) en
  cada tick. None = desconocido; quien lo use omite la clave en vez de inventarla.
  """
  try:
    from openpilot.system.hardware import HARDWARE
    return HARDWARE.get_device_type() or None
  except Exception:
    cloudlog.exception("[Orbit] no se pudo leer el tipo de dispositivo")
    return None


class MQTTComandos:
  def __init__(self, plane=None):
    self.base_path = os.path.dirname(os.path.abspath(__file__))
    # Fichero real de la config de conexion (en el comma, /data/orbit_config_mqtt.json:
    # fuera del arbol git, sobrevive al updater). Se lee siempre por config_broker.
    self.jsonConfig = config_broker.ruta_config()
    self.params = Params()
    self._refrescar_dongle()
    # Plano de mando (GateMonitor + estado compartido). Se INYECTA por referencia -- el
    # mismo patron que _link_camera_to_comandos -- y por defecto se coge el singleton del
    # proceso, que es el que tica en el hilo supervisado del manager. Ver el docstring de
    # CommandPlane para por que el objeto sobrevive a los reinicios de su hilo.
    self.plane = plane if plane is not None else get_command_plane()
    self.router = None
    # Version del firmware para el descriptor de capacidades. Se lee UNA vez: leerla en
    # on_connect seria I/O de disco en el hilo de red.
    try:
      self._fw = self.params.get("Version") or ""
    except Exception:
      self._fw = ""
    self._caps_huella = None
    self.conectado = False
    self.stop_event = threading.Event()
    # Hilo UNICO de conexion + su senal de relevo (ver _relanzar_hilo_conexion).
    # Antes cada llamada a init_mqtt/reload_broker arrancaba un hilo nuevo SIN
    # avisar al anterior: dos hilos llamando connect() sobre el MISMO cliente
    # cierran el socket recien abierto y dejan MUDO el canal de mandos.
    self._conn_thread = None
    self._conn_stop = threading.Event()
    # Serializa el connect(): el relevo por join() puede caducar (paho corta el
    # connect a los 5 s, pero un DNS lento puede pasarse), y aqui NO hay bucle
    # propio donde reintentar un relevo aplazado, asi que el hilo nuevo se lanza
    # igualmente y es este lock + la recomprobacion de `stop` DENTRO de el lo que
    # impide que el hilo viejo pise la conexion del nuevo.
    self._conn_lock = threading.Lock()
    # Archivo para guardar mensajes MQTT para modo debug
    self.debug_file = "/tmp/mqtt_debug_messages.txt"
    self.max_messages = 30  # Máximo de mensajes a guardar (reducido para ahorrar memoria)
    self.messages_lock = threading.Lock()
    # Inicializar debug_enabled leyendo el parámetro al inicio
    self.debug_enabled = self.params.get_bool("modo_debug")
    # Plazo interno de 2 s: monotono, no epoch (ademas time.time esta prohibido en el arbol).
    self._last_debug_check = time.monotonic()
    self.camera_sender = None  # Referencia al CameraSender (se establece desde MQTTEnvioGeneral)
    self.load_config()
    # El router se construye ANTES de abrir el socket: on_connect se suscribe a su topic
    # y publica el descriptor de capacidades, y ese callback puede dispararse en cuanto
    # init_mqtt arranca el hilo de conexion.
    self.init_router()
    self.init_mqtt()

  def _refrescar_dongle(self):
    """Relee DongleId y recalcula si el namespace de mandos es acotable.

    Params.get() ya devuelve str. Sin DongleId real NO se puede acotar el
    namespace de mandos: el literal "DongleID" es un comodin que comparten
    TODOS los comma sin registrar, asi que un frenazo dirigido a uno lo
    ejecutarian todos los que esten sin cobertura de registro. Se marca
    invalido y on_connect no se suscribe a ningun topic de mando.

    Se relee tambien en on_connect: en el PRIMER arranque el registro con comma
    tarda segundos, y calcularlo solo en __init__ dejaba el mando mudo PARA
    SIEMPRE (on_connect hacia return antes de suscribirse) hasta que el manager
    recreara MQTTEnvioGeneral entero.
    """
    try:
      dongle = self.params.get("DongleId")
    except Exception:
      dongle = None
    dongle = dongle.strip() if isinstance(dongle, str) else ""
    self.DongleID = dongle if dongle else "DongleID"
    self.dongle_valido = bool(dongle) and dongle != "DongleID"

  # ------------------------------------------------------------------ router v2

  def init_router(self):
    """Construye el CommandRouter y registra los verbos que ESTE firmware sabe ejecutar.

    Protegido entero: si el router no se puede construir, `self.router` queda en None y
    el dispatch descarta TODO mando (fail-closed). Un mando sin router no tiene quien le
    evalue los gates, y ejecutarlo "por si acaso" es justamente el agujero que este
    fichero existe para cerrar.
    """
    try:
      self.router = CommandRouter(
        self.DongleID if self.dongle_valido else "",
        gates=self.plane.gates,
        store=self.plane.store,
        publish=self._publicar_mqtt,
      )
      for verbo, fn in self._verbos().items():
        self.router.register_handler(verbo, fn)
      self.router.start()
      # El boton FISICO de desarme de la pantalla acaba en el mismo ejecutor que el verbo
      # disarm_all: un solo sitio que apaga los actuadores, dos disparadores.
      self.plane.set_on_disarm(self._desarmar_todo)
    except Exception:
      cloudlog.exception("[Orbit] no se pudo construir el CommandRouter: el mando queda MUDO (fail-closed)")
      self.router = None

  def _verbos(self):
    """Verbos del catalogo (command_spec.COMMANDS) con ejecutor real en este firmware.

    Los que NO estan aqui se publican como `unsupported: no_handler` en el descriptor de
    capacidades y responden UNSUPPORTED_VERB: la app no pinta el boton en vez de pintar
    uno que no hace nada (seccion 3.5).
    """
    return {
      "disarm_all": self._h_disarm_all,
      "set_mode": self._h_set_mode,
      "lane_change": self._h_lane_change,
      "cruise_delta": self._h_cruise_delta,
      "cruise_button": self._h_cruise_button,
      "assisted_decel": self._h_assisted_decel,
      "torque_mode": self._h_torque_mode,
      "steering_pulse": self._h_steering_pulse,
      "healthcheck": self._h_healthcheck,
    }

  def _publicar_mqtt(self, topic, payload, qos=1, retain=False):
    """Salida del router (ACK y capacidades). paho.publish() es thread-safe, asi que la
    llaman indistintamente el hilo de red y el worker de comandos."""
    self.mqttc.publish(topic, payload, qos=qos, retain=retain)

  def maybe_publish_caps(self, forzar: bool = False) -> bool:
    """Publica orbit/v2/caps/<dongle> RETENIDO y con qos 1 (seccion 3.1).

    Retenido porque la app tiene que saber que sabe hacer este coche NADA MAS
    suscribirse, sin esperar a que el coche vuelva a hablar; qos 1 porque perderlo deja a
    la app pintando de memoria.

    Se republica cuando cambia el contenido -- tipicamente al aparecer carParams, que
    offroad no existe y deja marca y plataforma vacias -- y no en cada tick: un retenido
    reescrito 1 vez por segundo es 1 escritura por segundo en el broker para siempre.
    """
    if self.router is None or not self.conectado or not self.dongle_valido:
      return False
    try:
      payload = self.router.capabilities_payload(fw=self._fw)
      # Modelo del comma para que backend y app distingan comma 4 (mici) de 3X (tizi).
      if tipo := tipo_dispositivo():
        payload["device_type"] = tipo
      # La huella ignora ts_ms: si no, cambiaria en cada tick y republicaria siempre.
      huella = json.dumps({k: v for k, v in payload.items() if k != "ts_ms"}, sort_keys=True)
      if not forzar and huella == self._caps_huella:
        return False
      self._publicar_mqtt(TOPIC_CAPS.format(self.DongleID), json.dumps(payload, separators=(",", ":")),
                          qos=1, retain=True)
      self._caps_huella = huella
      verbos = sorted(payload.get("verbs", {}))
      cloudlog.warning(f"[Orbit] capacidades publicadas: brand={payload.get('brand')!r} platform={payload.get('platform')!r} verbos={verbos}")
      return True
    except Exception:
      cloudlog.exception("[Orbit] no se pudieron publicar las capacidades")
      return False

  # ----------------------------------------------------------- ejecutores de verbo
  # Corren en el hilo worker del router (NUNCA en el de red) y solo despues de que el
  # router haya validado tipo, rango, modo, gates, TTL, secuencia y ritmo. Aqui ya no se
  # valida nada de eso: se aplica. Si uno lanza, el router publica FAILED/INTERNAL.

  def _desarmar_todo(self):
    """Devuelve todos los actuadores remotos a neutro. Lo comparten el verbo disarm_all y
    el boton fisico de la pantalla del comma."""
    from openpilot.orbit.orbit_control_ultra_simple import disarm_all_actuators
    tocados = disarm_all_actuators(self.params)
    cloudlog.warning(f"[Orbit] disarm_all: actuadores devueltos a neutro {tocados}")
    return tocados

  def _h_disarm_all(self, cmd):
    self._desarmar_todo()

  def _h_set_mode(self, cmd):
    """Cambia el modo de mando (observador / copiloto / maniobra).

    Sin este ejecutor el subsistema entero estaba muerto: NADIE escribia OrbitCommandMode,
    asi que el modo efectivo era siempre 0 (observador) y todo verbo salvo disarm_all
    contestaba MODE. El plano lo persiste y el router lo lee en el tick de 10 Hz.

    BANCO NO SE ALCANZA POR AQUI. Ni siquiera hace falta comprobarlo en el handler: el
    esquema del verbo no lo ofrece como opcion, asi que el router lo rechaza antes con
    RANGE. El modo fisico solo se arma en la pantalla del comma (seccion 4.1).

    El modo CADUCA: `expira_s` de la tabla (copiloto 900 s, maniobra 120 s). Un modo sin
    caducidad se queda encendido para siempre y el gate de modo deja de significar nada.
    """
    if self.plane is None or getattr(self.plane, "store", None) is None:
      raise RuntimeError("no hay plano de estado: el modo no se puede cambiar")
    destino = str(cmd.args.get("target_mode", "")).strip()
    spec = get_spec(cmd.verb)
    expira = float((spec.limits.get("expira_s") or {}).get(destino, 0.0)) if spec else 0.0
    modo = self.plane.store.set_mode(destino, expira_s=expira)
    cloudlog.warning(f"[Orbit] modo de mando -> {modo.name}"
                     + (f" (caduca en {expira:.0f} s)" if expira > 0 else ""))

  def _h_lane_change(self, cmd):
    """Un unico cambio de carril. desire_helper aplica ademas SUS gates en el ciclo en
    que actua (velocidad minima, latActive, angulo muerto): defensa en profundidad."""
    izquierda = cmd.args.get("direction") == "left"
    clave = "ForceLaneChangeLeft" if izquierda else "ForceLaneChangeRight"
    opuesta = "ForceLaneChangeRight" if izquierda else "ForceLaneChangeLeft"
    # El opuesto SIEMPRE a False primero: dos sentidos armados a la vez es una maniobra
    # encadenada, y el verbo declara encadenable=False.
    # block=True: el consumidor (desire_helper) hace remove() SINCRONO al consumir el
    # flag. Con la escritura diferida, un remove() que gane la carrera borra un flag que
    # aun no estaba en disco y la orden se ejecuta DOS veces (reproducido 300/300 con el
    # consumidor sin margen, 0/500 con block=True). Esto corre en el worker del router,
    # fuera del hilo RT: esperar al fsync es gratis aqui.
    self.params.put_bool(opuesta, False, True)
    self.params.put_bool(clave, True, True)

  # Paso minimo que sabe aplicar este firmware. El consumidor (OrbitSpeedUltraSimple)
  # acota el incremento a [1, 5] km/h, asi que por debajo de 1 km/h no hay forma de
  # ejecutar la orden SIN aplicar mas de lo pedido -- y aumentar la autoridad por encima
  # de lo que se pidio es justo lo que el diseno no permite. Se dice y no se hace.
  PASO_CRUCERO_MINIMO_KPH = 1.0

  def _h_cruise_delta(self, cmd):
    """Paso de crucero. El consumidor (OrbitSpeedUltraSimple, en el proceso de card)
    autoconsume el flag y aplica el incremento acotado a +-5 km/h.

    La magnitud viaja por `orbit_speed_increment` porque es el UNICO canal que el
    consumidor lee hoy. Sin escribirla, `delta_kph` seria un argumento decorativo: el
    coche moveria el paso configurado en la pantalla y no el que pidio la orden -- otra
    vez un control que dice una cosa y hace otra.

    EFECTO LATERAL CONOCIDO: `orbit_speed_increment` es PERSISTENT, asi que una orden
    remota deja fijado el tamano del paso que muestra la pantalla del comma. Se acepta
    mientras ese sea el unico canal; la salida limpia es que el consumidor lea la
    magnitud del propio plano de estado, y eso vive en otro fichero.
    """
    delta = float(cmd.args.get("delta_kph", 0.0))
    magnitud = abs(delta)
    if magnitud < self.PASO_CRUCERO_MINIMO_KPH:
      minimo = self.PASO_CRUCERO_MINIMO_KPH
      raise ValueError(f"delta_kph={delta} por debajo del paso minimo aplicable ({minimo} km/h): no se aplica nada")
    magnitud = min(5.0, magnitud)
    try:
      actual = self.params.get("orbit_speed_increment", return_default=True)
      if actual is None or abs(float(actual) - magnitud) > 0.01:
        # Param tipado FLOAT: put() exige float nativo (put(str) = TypeError silencioso).
        self.params.put("orbit_speed_increment", magnitud)
    except Exception:
      cloudlog.exception("[Orbit] no se pudo fijar el incremento de crucero")
    clave = "orbit_speed_increase" if delta > 0 else "orbit_speed_decrease"
    # block=True: mismo motivo que en _h_lane_change (el consumidor hace remove()).
    self.params.put_bool(clave, True, True)

  def _h_cruise_button(self, cmd):
    """Cancelar crucero: el boton de panico de la app.

    Es el UNICO boton grande que el diseno deja como accion de panico, y no por
    comodidad: cancelar BAJA autoridad (desengancha) mientras que una frenada remota la
    aumenta, y esta implementado en las 12 marcas. Una frenada pedida desde el movil llega
    con segundos de latencia a una situacion que el conductor ya ha visto.

    El esquema del verbo solo acepta 'cancel'; 'resume' y 'set' no estan declarados.
    """
    if str(cmd.args.get("button", "")).strip() != "cancel":
      raise ValueError("cruise_button solo implementa 'cancel'")
    # block=True: controlsd lo consume y lo limpia; con la escritura en vuelo podria
    # limpiarlo antes de que llegue a disco y perderse la cancelacion.
    self.params.put_bool("OrbitCruiseCancel", True, True)

  def _h_assisted_decel(self, cmd):
    """Deceleracion asistida acotada (sustituye a brutebreak). El rango [-2.5,-1.0] ya lo
    ha impuesto el router con el esquema del verbo; aqui solo se aplica."""
    accel = float(cmd.args["accel"])
    # Param tipado FLOAT.
    # La intensidad se escribe ANTES y con block=True: si el flag llegase primero,
    # controlsd leeria el flag activo con la intensidad de la orden ANTERIOR.
    self.params.put("brutebreak_intensidad", accel, True)
    self.params.put_bool("brutebreak_active", True, True)

  def _h_torque_mode(self, cmd):
    """Modo de torque del volante. Solo se llega aqui en modo banco y con armado FISICO:
    el router lo exige por spec.requiere_armado_banco.

    Volver al modo 0 NO pasa por aqui: baja autoridad y se atiende como disarm_all, que
    es el unico verbo que no se puede bloquear (seccion 2).
    """
    modo = int(cmd.args["mode"])
    apply_target = cmd.args.get("apply_target")
    if modo == 3:
      if apply_target not in ("curvature", "torque"):
        # Sin saber DONDE se aplica el esquive no se puede aplicar. Lanzar aqui es lo
        # correcto: el router lo convierte en ACK failed/INTERNAL con el motivo.
        raise ValueError("mode=3 exige apply_target 'curvature' o 'torque'")
      self.params.put("JetsonObstacleApplyTarget", apply_target)
    # SteerTorqueMode es INT: put() exige int nativo.
    self.params.put("SteerTorqueMode", modo)
    cloudlog.warning(f"[Orbit] SteerTorqueMode -> {modo} (apply_target={apply_target!r})")

  def _h_steering_pulse(self, cmd):
    """Pulso de direccion (banco). El modulo aplica su propio tope de duracion y de
    velocidad; el sentido sale del signo del par pedido."""
    from openpilot.orbit.orbit_steering_pulse import set_steering_pulse
    torque = float(cmd.args["torque"])
    direccion = "right" if torque >= 0 else "left"
    if not set_steering_pulse(direccion, duration_ms=cmd.args.get("duration_ms", 200),
                              magnitude=abs(torque)):
      raise RuntimeError("set_steering_pulse rechazo el pulso")

  def _h_healthcheck(self, cmd):
    """Diagnostico remoto. No se puede tocar cereal desde este hilo (msgq no es
    thread-safe): se deja la peticion en un Param y MQTTEnvioGeneral.loop() la contesta
    en SU hilo con deviceState/pandaStates/managerState."""
    # Param tipado STRING.
    self.params.put("OrbitHealthcheckRequest", str(ahora_epoch_ms() // 1000))

  def load_config(self):
    # Plantilla del arbol + lo persistido en /data por encima (ver orbit/config_broker.py).
    config = config_broker.leer_config()
    self.broker_address = config.get("broker", "localhost")
    # broker_port antes se ignoraba aqui (1883 a fuego): en un broker con puerto
    # no estandar los comandos morian mientras la telemetria si salia.
    self.broker_port = int(config.get("broker_port", 1883))
    # Credenciales MQTT opcionales (broker con auth). Vacio/ausente = anonimo.
    self.mqtt_username = (config.get("username") or "").strip() or None
    self.mqtt_password = config.get("password") or None

  def init_mqtt(self):
    self.mqttc = mqtt.Client()
    if self.mqtt_username:
      self.mqttc.username_pw_set(self.mqtt_username, self.mqtt_password)
    self.mqttc.max_queued_messages_set(0)  # No encolar mensajes en RAM si no hay conexión
    self.mqttc.on_connect = self.on_connect
    self.mqttc.on_disconnect = self.on_disconnect
    self.mqttc.on_message = self.on_message
    self.mqttc.reconnect_delay_set(min_delay=1, max_delay=30)
    self._relanzar_hilo_conexion()

  def _relanzar_hilo_conexion(self):
    """Arranca el UNICO hilo de conexion, relevando antes al anterior.

    Mismo bug que ya se arreglo en el fichero gemelo (mqtt_envio_general.py):
    si el broker no responde, el hilo inicial se queda en su bucle de reintento
    y al cambiar la IP desde Ajustes se lanzaba un SEGUNDO hilo sin avisar al
    primero. Cuando el nuevo conectaba y el viejo despertaba, el connect() del
    viejo sobre el MISMO objeto cliente hacia _sock_close() y _out_packet.clear():
    cerraba el socket recien abierto. Aqui es PEOR que en el gemelo porque lo que
    queda mudo es el canal de MANDOS (incluida la cancelacion remota).

    Diferencia deliberada con el gemelo: alli, si el join caduca, el relevo se
    APLAZA (_conn_pendiente) y lo reintenta el loop de 10 Hz. MQTTComandos no
    tiene loop propio -- reload_broker solo se llama cuando cambia el JSON --, asi
    que aplazar dejaria el mando mudo hasta el siguiente cambio de configuracion.
    Se lanza igualmente y la exclusion la garantiza _conn_lock + la recomprobacion
    de `stop` dentro del lock en setup_mqtt.
    """
    viejo = self._conn_thread
    if viejo is not None and viejo.is_alive():
      self._conn_stop.set()
      # 6 s = la espera de 5 s entre reintentos (ya interrumpible) mas el margen
      # del connect() en curso, que paho corta a los 5 s (_connect_timeout).
      viejo.join(timeout=6.0)
      if viejo.is_alive():
        cloudlog.warning("[Bemposta] MQTTComandos: hilo de conexion anterior aun vivo; el relevo se apoya en _conn_lock")
    self._conn_stop = threading.Event()
    self._conn_thread = threading.Thread(target=self.setup_mqtt, args=(self._conn_stop,),
                                         daemon=True, name="OrbitMQTTComandosConnect")
    self._conn_thread.start()

  def setup_mqtt(self, stop=None):
    """Bucle de conexion inicial. Corre en el hilo unico de conexion; `stop` es
    su senal de relevo (la pone _relanzar_hilo_conexion antes de sustituirlo).

    El estado del reintento (fallos seguidos, aviso de 'sin broker') es LOCAL a
    proposito: cada relevo (reload_broker tras escribir un broker desde Ajustes)
    arranca limpio en 5 s. El hilo NO sale mientras no conecte (ver
    CONN_SIN_BROKER_SECS); el supervisor del manager no lo vigila."""
    if stop is None:
      stop = self._conn_stop
    fallos = 0
    sin_broker_avisado = False
    while not self.stop_event.is_set() and not stop.is_set():
      if not str(self.broker_address or "").strip():
        # config_mqtt.json de fabrica ("broker": ""): paho lanza 'Invalid host.' al
        # instante y esto giraba cada 5 s llenando el rlog de todos los dispositivos
        # sin configurar. Un aviso y a esperar. Va ANTES de coger _conn_lock: la
        # espera larga nunca se hace con el lock cogido (bloquearia al relevo).
        if not sin_broker_avisado:
          cloudlog.warning("[Bemposta] MQTTComandos: broker no configurado; esperando configuracion (Ajustes -> Servidor Orbit)")
          sin_broker_avisado = True
        if stop.wait(CONN_SIN_BROKER_SECS):
          break
        continue
      try:
        cloudlog.warning(f"[Bemposta] MQTTComandos conectando a broker {self.broker_address}:{self.broker_port}")
        with self._conn_lock:
          # Recomprobar DENTRO del lock: si nos relevaron mientras esperabamos,
          # este connect() cerraria el socket que acaba de abrir el hilo nuevo.
          if self.stop_event.is_set() or stop.is_set():
            return
          self.mqttc.connect(self.broker_address, self.broker_port, 60)
          if not self.conectado:
            self.mqttc.loop_start()
            self.conectado = True
        break
      except Exception as e:
        espera = espera_reintento(fallos)
        fallos += 1
        cloudlog.warning(f"[Bemposta] MQTTComandos NO pudo conectar a {self.broker_address}:{self.broker_port}: {e}. Reintento en {espera:g}s")
        # Espera INTERRUMPIBLE: con time.sleep(5) el relevo tardaba hasta 5 s en
        # notarse, y ese es justo el hueco en el que se solapaban los dos hilos.
        if stop.wait(espera):
          break

  def reload_broker(self, new_broker):
    """Reconecta en caliente cuando cambian el broker, el puerto o las
    credenciales en config_mqtt.json (lo llama MQTTEnvioGeneral al detectarlo)."""
    # broker_port entra en la tupla de comparacion: mqtt_envio_general SI detecta
    # el cambio de puerto y llama aqui, pero como no lo comparabamos saliamos por
    # el return de "sin cambios" y los comandos se quedaban colgados del broker
    # viejo mientras la telemetria ya hablaba con el nuevo.
    viejo = (self.broker_address, self.broker_port, self.mqtt_username, self.mqtt_password)
    try:
      self.load_config()  # re-lee broker, puerto y credenciales del JSON
    except Exception as e:
      # Un config_mqtt.json corrupto (escritura a medias desde la UI del comma)
      # propagaba la excepcion y se llevaba por delante el subsistema de comandos
      # entero. Restauramos la config anterior y seguimos con el broker que ya
      # funcionaba en vez de morir.
      (self.broker_address, self.broker_port, self.mqtt_username, self.mqtt_password) = viejo
      cloudlog.warning(f"[Bemposta] MQTTComandos config_mqtt.json ilegible ({e}), mantengo la config anterior")
      return
    if new_broker:
      self.broker_address = new_broker
    if (self.broker_address, self.broker_port, self.mqtt_username, self.mqtt_password) == viejo:
      return
    cloudlog.warning(f"[Bemposta] MQTTComandos broker/credenciales -> {self.broker_address}:{self.broker_port}, reconectando")
    try:
      self.mqttc.loop_stop()
    except Exception:
      pass
    try:
      self.mqttc.disconnect()
    except Exception:
      pass
    self.conectado = False
    # username=None -> vuelve a anonimo.
    self.mqttc.username_pw_set(self.mqtt_username, self.mqtt_password)
    self._relanzar_hilo_conexion()

  def on_connect(self, client, userdata, flags, rc):
    # Cuerpo COMPLETO en try/except: paho corre las callbacks con
    # suppress_exceptions=False, asi que una excepcion aqui (p.ej. leer DongleId)
    # sale hasta _thread_main, cuyo finally pone _thread=None y mata el hilo de
    # red EN SILENCIO: el mando quedaria mudo marcando 'conectado'.
    try:
      self._on_connect(client, userdata, flags, rc)
    except Exception:
      cloudlog.exception("[Bemposta] MQTTComandos on_connect fallo (el hilo de red habria muerto en silencio)")

  def _on_connect(self, client, userdata, flags, rc):
    if rc == 0:
      self.conectado = True
      # Recalcular AQUI, no solo en __init__: en el primer arranque el registro
      # con comma puede tardar y el dongle aparece despues de construirnos.
      self._refrescar_dongle()
      if not self.dongle_valido:
        # Ver _refrescar_dongle: sin DongleId real el namespace de mandos es comun
        # a todos los comma sin registrar. Mejor mudo que obedeciendo ordenes
        # ajenas. La proxima reconexion vuelve a intentarlo.
        cloudlog.error("[Bemposta] MQTTComandos SIN DongleId valido: NO me suscribo a ningun topic de mando (namespace comun)")
        return
      # --- namespace v2 (contrato congelado, seccion 3.1). qos 1: el mando no se pierde.
      # El retenido esta PROHIBIDO en este topic y lo rechaza el router incondicionalmente.
      if self.router is not None:
        # El router puede haberse construido sin dongle (primer arranque, con el registro
        # aun en curso). Ahora si lo hay: se le pone, y con el su topic de mando y el de ACK.
        self.router.dongle_id = self.DongleID
        client.subscribe(self.router.topic_cmd, qos=1)
      else:
        cloudlog.error("[Orbit] sin CommandRouter: NO me suscribo a orbit/v2/cmd (el mando queda mudo)")

      # --- namespace v1 (LEGACY, seccion 13: se apaga por verbo al cerrar la migracion).
      # Se sigue escuchando, pero ya no ejecuta nada por su cuenta: cada mensaje se
      # traduce a un sobre v2 y entra por el mismo router (ver _v1_al_router).
      topics = [
        f"telemetry_config/{self.DongleID}/left",           # Cambio carril izquierda
        f"telemetry_config/{self.DongleID}/right",          # Cambio carril derecha
        f"telemetry_config/{self.DongleID}/control",        # Control básico (forward, break, tright, tleft)
        f"telemetry_config/{self.DongleID}/speed_up",       # Comando aumentar velocidad (formato servidor)
        f"telemetry_config/{self.DongleID}/speed_down",     # Comando disminuir velocidad (formato servidor)
        f"telemetry_config/{self.DongleID}/speed_increment", # Configuración del incremento de velocidad (futuro)
        f"telemetry_config/{self.DongleID}/overtake",        # Adelantamiento automático (detecta BSM automáticamente)
        f"telemetry_config/{self.DongleID}/brutebreak",      # Frenado de emergencia brusco
        f"telemetry_config/{self.DongleID}/camera_config",   # Configuración de cámara desde app ORBIT
        f"telemetry_config/{self.DongleID}/steer_torque_mode", # Modo de torque del volante (por dongle_id)
        f"telemetry_config/{self.DongleID}/enroll_ack",      # Ack de enrolamiento ORBIT (backend → firmware)
        f"telemetry_config/{self.DongleID}/healthcheck"      # Peticion de diagnostico remoto (backend/app → firmware)
      ]

      for topic in topics:
        client.subscribe(topic, qos=0)

      # --- configuracion deseada (seccion 8). qos 1 y RETENIDO A PROPOSITO: es estado,
      # no una orden, y el coche tiene que recibirlo al conectar sin preguntarle a nadie.
      # Sustituye a telemetry_config/<dongle>/jetson_config, que ya no se escucha (ver
      # el docstring de on_message): ese topic reescribia jetson_ip sin validar nada.
      client.subscribe(cfg2.TOPIC_CFG_DESIRED.format(self.DongleID), qos=1)
      cloudlog.warning(f"[Bemposta] MQTTComandos CONECTADO (rc={rc}), suscrito a comandos v2+v1 y a cfg/desired para dongle={self.DongleID}")

      # Descriptor de capacidades: forzado en cada conexion porque el retenido vive en EL
      # BROKER, y este puede ser otro (cambio de IP desde Ajustes) o haberlo perdido.
      self.maybe_publish_caps(forzar=True)

  def on_disconnect(self, client, userdata, rc):
    self.conectado = False
    cloudlog.warning(f"[Bemposta] MQTTComandos DESCONECTADO del broker {self.broker_address} (rc={rc})")
    # print("🔌 MQTT Comandos desconectado. Reintentando...")  # Comentado para reducir uso de memoria

  def save_debug_message(self, topic, payload):
    """Guarda un mensaje MQTT para el modo debug. Optimizado para reducir uso de memoria."""
    # Solo guardar si el modo debug está activo
    try:
      # Verificar el estado del modo debug (cada 2 segundos para respuesta más rápida)
      current_time = time.monotonic()
      if not hasattr(self, '_last_debug_check') or current_time - self._last_debug_check > 2.0:
        self.debug_enabled = self.params.get_bool("modo_debug")
        self._last_debug_check = current_time

      if not self.debug_enabled:
        return  # No guardar si el modo debug no está activo
    except Exception:
      return  # Si hay error, no guardar

    try:
      timestamp = time.strftime("%H:%M:%S", time.localtime())
      # Truncar payload si es muy largo (reducido a 50 caracteres para ahorrar memoria)
      payload_display = payload[:50] if len(payload) > 50 else payload
      # Formato: [timestamp] topic\npayload
      # El panel espera este formato exacto: primera línea con timestamp y topic, segunda línea con payload
      # Los mensajes se separan con \n\n cuando se escriben al archivo
      message = f"[{timestamp}] {topic}\n{payload_display}"

      with self.messages_lock:
        # Método optimizado: leer solo las últimas líneas necesarias
        messages = []
        if os.path.exists(self.debug_file):
          try:
            # Leer el archivo de forma más eficiente con límite de tamaño
            file_size = os.path.getsize(self.debug_file)
            # Si el archivo es muy grande (>100KB), truncarlo
            if file_size > 100 * 1024:
              # Leer solo las últimas líneas sin cargar todo el archivo
              with open(self.debug_file, 'rb') as f:
                f.seek(max(0, file_size - 50 * 1024))  # Leer solo los últimos 50KB
                content = f.read().decode('utf-8', errors='ignore')
                lines = content.split('\n')
            else:
              with open(self.debug_file, encoding='utf-8') as f:
                lines = f.readlines()

            # Procesar desde el final hacia atrás
            i = len(lines) - 1
            temp_messages = []
            while i >= 0 and len(temp_messages) < self.max_messages:
              if lines[i].strip().startswith('['):
                if i > 0:
                  temp_messages.insert(0, (lines[i-1].strip() + '\n' + lines[i].strip()).strip())
                  i -= 2
                else:
                  i -= 1
              else:
                i -= 1
            messages = temp_messages
          except Exception:
            messages = []

        # Agregar nuevo mensaje
        messages.append(message.strip())

        # Mantener solo los últimos max_messages
        if len(messages) > self.max_messages:
          messages = messages[-self.max_messages:]

        # Escribir de vuelta (solo si hay mensajes)
        if messages:
          try:
            with open(self.debug_file, 'w', encoding='utf-8') as f:
              f.write('\n\n'.join(messages))
          except Exception:
            # Si falla la escritura, intentar crear el directorio si no existe
            try:
              os.makedirs(os.path.dirname(self.debug_file), exist_ok=True)
              with open(self.debug_file, 'w', encoding='utf-8') as f:
                f.write('\n\n'.join(messages))
            except Exception:
              pass  # Si sigue fallando, ignorar silenciosamente
    except Exception:
      pass  # Silenciar errores para no afectar el flujo principal

  # Topic de ESTADO idempotente cuyo publicador manda retained A PROPOSITO para el
  # cold-start: el backend publica enroll_ack con retain=True y tiene un test que lo fija.
  # No actua sobre el coche y lleva anti-eco. Todo lo demas del namespace v1 son MANDOS y
  # no puede venir retenido.
  #
  # `/jetson_config` SALIO de aqui y ademas dejo de estar suscrito (ver on_connect y el
  # docstring de on_message): aceptaba retenido, no pasaba por el router y reescribia
  # jetson_ip, que decide de que maquina vienen los offsets de direccion del modo 3. Lo
  # sustituye el sobre `orbit/v2/cfg/desired/<dongle>` (seccion 8).
  RETAIN_PERMITIDO = ("/enroll_ack",)

  # steer_torque_mode SI actua sobre el coche y por eso salio de la lista de
  # arriba: su ejecutor escribe SteerTorqueMode, y controlsd pone
  # actuators.torque = -1.0 EN CADA CICLO con modo 2 mientras latActive, mientras
  # que el modo 1 conmuta la fuente de direccion a la Jetson. Como el param es
  # PERSISTENT y la app publica con retain:true, el retenido se re-entregaba en
  # CADA reconexion y en cada reinicio del subsistema: par maximo remoto sin que
  # nadie mande nada. Se admite el retenido SOLO cuando BAJA autoridad (modo 0),
  # que es la unica direccion que el diseno (§2) permite siempre. El cold-start
  # legitimo ya lo cubre el propio comma con _publish_state_snapshot_retained().
  RETAIN_SOLO_SI_BAJA_AUTORIDAD = ("/steer_torque_mode",)

  # ------------------------------------------------------------------ entrada MQTT

  def on_message(self, client, userdata, msg):
    """Callback de paho: HILO DE RED. Aqui no se ejecuta ningun comando.

    Dos caminos, un solo destino:

      * orbit/v2/cmd/<dongle> -> CommandRouter.on_message() tal cual.
      * topic v1 (telemetry_config/<dongle>/...) -> _v1_al_router(), que lo traduce a
        (verbo, args) y fabrica un SOBRE v2 con router.submit_local(). A partir de ahi es
        indistinguible de un mando v2: mismos gates, mismo modo, mismo TTL, mismo ACK.

    Y un tercer camino que no es mando: `orbit/v2/cfg/desired/<dongle>` (seccion 8) va al
    buzon de configuracion (config_v2.get_bus()), que es RAM pura. Quien lo APLICA es el
    hilo del loop de mqtt_envio_general, porque aplicar toca Params, un JSON con flock y el
    CameraSender, y nada de eso puede pasar en el hilo de red.

    EL TOPIC QUE MURIO. `telemetry_config/<dongle>/jetson_config` ya no se escucha: no esta
    en la lista de suscripcion de on_connect ni en RETAIN_PERMITIDO, y su handler dejo de
    existir. Aceptaba retenido, no pasaba por el router y reescribia orbit/config_jetson.json
    campo a campo sin validar ninguno, jetson_ip incluido -- que es DE QUE MAQUINA viene
    JetsonObstaclePulse, es decir de que maquina vienen los offsets de direccion del modo 3
    (COMMA+JETSON), el modo de producto, que NO exige armado de banco cuando se elige en la
    pantalla del comma. Sus anti-eco (source=="comma_ui", _version) los ponia el propio
    emisor, asi que no eran una barrera. Ahora la IP solo entra por el sobre de
    configuracion, donde se valida el RANGO (privada/loopback/link-local) y la version no
    retrocede. Esto NO autentica a nadie -- D1 deja el broker abierto y sin TLS -- pero
    acota a un vocabulario cerrado lo que se puede escribir.

    Los TRES topics v1 que NO son verbos y por tanto no pasan por el router, con lo que
    hace cada uno (revisado uno a uno; ninguno mueve un actuador):

      /camera_config    enciende/apaga el envio de imagenes y su frecuencia. Es privacidad
                        y ancho de banda, no conduccion. El interruptor local de la
                        pantalla lo sigue mandando (seccion 9, innegociable).
      /enroll_ack       marca el dispositivo como reclamado/liberado (OrbitClaimed,
                        OrbitOwner, OrbitOwnerRole, codigo de emparejamiento). Es
                        identidad.
      /speed_increment  guarda el tamano del paso de crucero (param FLOAT). No mueve el
                        coche por si mismo, y el consumidor lo acota ademas a [1, 5] km/h,
                        que es el limite por orden que declara el verbo cruise_delta.
    """
    try:
      topic = msg.topic
      payload = msg.payload.decode(errors="ignore").strip()

      # Guardar mensaje para modo debug (tambien los que se descartan abajo, para
      # que el panel muestre que el mensaje llego y no parezca perdido)
      self.save_debug_message(topic, payload)

      # --- v2 cfg: configuracion deseada (seccion 8). Aqui el RETENIDO ES LO NORMAL --es
      # estado, no una orden-- asi que va ANTES del filtro de retenidos del namespace v1.
      # Este hilo solo parsea y deja el sobre en el buzon: aplicar toca Params, un JSON con
      # flock y el CameraSender, y eso es del hilo del loop.
      if isinstance(topic, str) and topic.startswith(cfg2.TOPIC_CFG_DESIRED.format("")):
        if not payload:
          # Payload vacio = borrado del retenido, no "configuracion sin claves".
          cloudlog.warning(f"[Bemposta] cfg/desired vacio en {topic} (borrado de retenido), ignorado")
          return
        cfg2.get_bus().recibir(payload)
        return

      # --- v2: el router hace sus propios filtros (retain, payload vacio, dongle ajeno).
      if isinstance(topic, str) and topic.startswith(TOPIC_CMD.format("")):
        if self.router is None:
          cloudlog.error("[Orbit] llego un mando v2 y no hay router: descartado (fail-closed)")
          return
        self.router.on_message(client, userdata, msg)
        return

      # --- v1: los filtros de retenido y de payload vacio son de este lado, porque el
      # namespace legacy SI tiene topics donde un retenido es legitimo.
      retenido = bool(msg.retain)
      if retenido and not topic.endswith(self.RETAIN_PERMITIDO) \
         and not topic.endswith(self.RETAIN_SOLO_SI_BAJA_AUTORIDAD):
        cloudlog.warning(f"[Bemposta] MQTTComandos descarta mensaje RETENIDO en {topic} (un mando no se repite)")
        return

      # Payload de longitud cero es el gesto MQTT estandar para BORRAR un
      # retenido: el broker lo reentrega como mensaje normal y aqui caia en los
      # except/else que ACTIVAN el comando (speed_up y speed_down activaban
      # literalmente dentro de su except json.JSONDecodeError, y left/right en su
      # else final). Borrar un retenido no es una orden de conduccion.
      if not payload:
        cloudlog.warning(f"[Bemposta] MQTTComandos descarta payload vacio en {topic} (borrado de retenido)")
        return

      # --- topics v1 de CONFIGURACION (no son verbos: ver el docstring).
      if topic.endswith("/jetson_config"):
        # Ni suscrito ni atendido. Aqui solo puede llegar por una suscripcion con comodin
        # que hoy no existe; el rechazo explicito esta para que, si alguien la anade, el
        # agujero no vuelva solo y quede en el log.
        cloudlog.warning(f"[Bemposta] {topic} IGNORADO: la config de la Jetson va por orbit/v2/cfg/desired (§8)")
        return
      if topic.endswith("/camera_config"):
        self.handle_camera_config(payload)
        return
      if topic.endswith("/enroll_ack"):
        self.handle_enroll_ack(payload)
        return
      if topic.endswith("/speed_increment"):
        self.handle_speed_increment_config(payload)
        return

      # --- todo lo demas es MANDO: al router, siempre.
      self._v1_al_router(topic, payload, retenido)

    except Exception:
      cloudlog.exception("[Orbit] MQTTComandos.on_message fallo (el hilo de red habria muerto en silencio)")

  # -------------------------------------------------- puente v1 -> v2 (seccion 13)

  def _v1_al_router(self, topic, payload, retenido=False):
    """Traduce un mando v1 a (verbo, args) y lo mete por el router.

    Devuelve el Resultado del router, o None si el payload no se reconocio (se descarta
    sin tocar nada, como ya hacian las listas blancas) o si no hay router.

    Nada de esto escribe un Param: quien lo escribe es el ejecutor del verbo, y solo
    despues de que el router haya dicho que si.
    """
    if self.router is None:
      cloudlog.error(f"[Orbit] mando v1 en {topic} descartado: no hay CommandRouter (fail-closed)")
      return None

    traduccion = None
    if topic.endswith("/left"):
      traduccion = self._v1_lane_change(payload, "ForceLaneChangeLeft", "ForceLaneChangeRight", "left")
    elif topic.endswith("/right"):
      traduccion = self._v1_lane_change(payload, "ForceLaneChangeRight", "ForceLaneChangeLeft", "right")
    elif topic.endswith("/speed_up"):
      traduccion = self._v1_paso_velocidad(payload, "speed_up", self.SPEED_UP_PLANOS, +1)
    elif topic.endswith("/speed_down"):
      traduccion = self._v1_paso_velocidad(payload, "speed_down", self.SPEED_DOWN_PLANOS, -1)
    elif topic.endswith("/control"):
      traduccion = self._v1_cruceta(payload)
    elif topic.endswith("/overtake"):
      traduccion = self._v1_overtake(payload)
    elif topic.endswith("/brutebreak"):
      traduccion = self._v1_brutebreak(payload)
    elif topic.endswith("/steer_torque_mode"):
      traduccion = self._v1_steer_torque_mode(payload, retenido)
    elif topic.endswith("/healthcheck"):
      traduccion = ("healthcheck", {})
    else:
      cloudlog.warning(f"[Orbit] topic v1 sin traduccion: {topic}")
      return None

    if traduccion is None:
      cloudlog.warning(f"[Bemposta] MQTTComandos DESCARTA {topic}: payload no reconocido ({payload[:60]!r})")
      return None

    verbo, args = traduccion
    return self.router.submit_local(verbo, args, actor={"via": "v1", "topic": topic})

  # Decisiones posibles de un payload de cambio de carril.
  LC_ACTIVAR = "activar"
  LC_CANCELAR = "cancelar"
  LC_TOGGLE = "toggle"

  @staticmethod
  def decidir_lane_change(payload, clave):
    """Lista BLANCA de payloads de cambio de carril.

    Devuelve LC_ACTIVAR, LC_CANCELAR, LC_TOGGLE o None (= DESCARTAR).

    Antes solo se descartaba el JSON {clave: <no bool>}: cualquier otro JSON
    valido (0, [], "x") o un dict sin la clave ({"foo": 1}) dejaba explicit=None,
    caia al fallback de string plano, no era igual a "false" y terminaba en el
    else que ACTIVA el giro. Es decir: basura -> cambio de carril.

    Reglas, en este orden:
      - "true"/"false" (con espacios o mayusculas): formato plano del backend.
        Se comprueban ANTES del JSON porque tambien son JSON valido y el
        resultado seria el mismo. "false" CANCELA: bajar autoridad nunca se
        descarta (diseno §2 y §3.4), aunque llegue con cualquier envoltorio.
      - dict con la clave y valor bool: formato de la app. bool("false") es True,
        por eso el valor tiene que ser bool de verdad y no truthy.
      - todo lo demas: None (descartar).
    """
    texto = payload.strip().lower()
    if texto == "false":
      return MQTTComandos.LC_CANCELAR
    if texto == "true":
      # Comportamiento historico del string plano: si el sentido OPUESTO ya esta
      # armado, cancelar los dos en vez de encadenar maniobras.
      return MQTTComandos.LC_TOGGLE

    try:
      data = json.loads(payload)
    except (json.JSONDecodeError, ValueError, TypeError):
      return None

    if not isinstance(data, dict) or clave not in data:
      return None
    v = data[clave]
    if not isinstance(v, bool):
      return None
    return MQTTComandos.LC_ACTIVAR if v else MQTTComandos.LC_CANCELAR

  def _v1_lane_change(self, payload, clave, opuesta, direccion):
    """/left y /right -> verbo lane_change, o disarm_all si el payload CANCELA.

    Por que una cancelacion se traduce a disarm_all y no a "lane_change con false": en v2
    no existe el verbo que cancela otro verbo. Bajar autoridad es UNA cosa y tiene UN
    verbo, que es el unico que no se puede bloquear por modo, gate ni TTL (seccion 2). Si
    la cancelacion viajara como lane_change, un gate en rojo la RECHAZARIA -- es decir, no
    se podria cancelar justo cuando mas falta hace. El efecto es mas amplio que el de v1
    (apaga todos los actuadores remotos, no solo los dos flags de carril), y eso es
    aceptable en la unica direccion que siempre lo es: hacia abajo.
    """
    decision = self.decidir_lane_change(payload, clave)
    if decision is None:
      return None
    if decision == self.LC_CANCELAR:
      return ("disarm_all", {})
    if decision == self.LC_ACTIVAR:
      return ("lane_change", {"direction": direccion})
    # LC_TOGGLE: string plano "true" del backend. Con el sentido opuesto ya armado, el
    # comportamiento historico es cancelar los dos en vez de encadenar maniobras.
    try:
      opuesto_armado = bool(self.params.get_bool(opuesta))
    except Exception:
      opuesto_armado = False
    if opuesto_armado:
      return ("disarm_all", {})
    return ("lane_change", {"direction": direccion})

  # Lista blanca de payloads planos para los pasos de crucero. El "aceptamos
  # cualquier payload como valido para mantener compatibilidad" de antes convertia
  # CUALQUIER cosa que llegara al topic (incluido un JSON de otro verbo, un "0" o
  # basura del broker) en un paso de velocidad, porque el except capturaba el
  # AttributeError de data.get sobre un no-dict y activaba dentro del propio except.
  SPEED_UP_PLANOS = ("1", "+1", "true")
  SPEED_DOWN_PLANOS = ("1", "-1", "true")

  @staticmethod
  def acepta_paso_velocidad(payload, clave, planos):
    """True solo si el payload esta en la lista blanca del verbo.

    - string plano exacto (sin espacios, sin distinguir mayusculas) de `planos`;
    - o JSON objeto con {clave: true} (formato de la app).
    Todo lo demas es False = descartar.
    """
    if payload.strip().lower() in planos:
      return True
    try:
      data = json.loads(payload)
    except (json.JSONDecodeError, ValueError, TypeError):
      return False
    return isinstance(data, dict) and data.get(clave) is True

  # Paso de crucero por defecto si el param no se puede leer. Es el tope por orden que
  # declara el verbo cruise_delta (+-5 km/h): no se puede pedir mas desde v1 que desde v2.
  PASO_CRUCERO_DEFECTO_KPH = 5.0

  def _v1_paso_velocidad(self, payload, clave, planos, signo):
    """/speed_up y /speed_down -> verbo cruise_delta.

    v1 no manda magnitud: manda "un paso". La magnitud sale del param
    orbit_speed_increment, acotada a [1, 5] km/h, que es el limite por orden del verbo.
    El presupuesto de +-20 km/h por minuto lo aplica el router, no esto.
    """
    if not self.acepta_paso_velocidad(payload, clave, planos):
      return None
    try:
      paso = self.params.get("orbit_speed_increment", return_default=True)
      paso = float(paso) if paso is not None else self.PASO_CRUCERO_DEFECTO_KPH
    except Exception:
      paso = self.PASO_CRUCERO_DEFECTO_KPH
    paso = max(1.0, min(5.0, abs(paso)))
    return ("cruise_delta", {"delta_kph": signo * paso})

  # Verbos de la cruceta, RETIRADOS por la seccion 6 del diseno ("Se retiran: forward,
  # break, tright, tleft"). Se siguen traduciendo -- y no ignorando -- para que el emisor
  # reciba un ACK UNSUPPORTED_VERB con el nombre del verbo en vez de silencio: un boton
  # que no responde es indistinguible de un coche que no esta.
  CRUCETA_V1 = ("forward", "break", "tright", "tleft")

  def _v1_cruceta(self, payload):
    """/control -> el verbo retirado que pidan, para que el router conteste
    UNSUPPORTED_VERB. Ninguno de los cuatro existe ya en command_spec.COMMANDS."""
    plano = payload.strip().lower()
    if plano in self.CRUCETA_V1:
      return (plano, {})
    try:
      data = json.loads(payload)
    except (json.JSONDecodeError, ValueError, TypeError):
      return None
    if not isinstance(data, dict):
      return None
    for verbo in self.CRUCETA_V1:
      if data.get(verbo) is True:
        return (verbo, {})
    return None

  def _v1_overtake(self, payload):
    """/overtake -> verbo `overtake`, que command_spec declara no implementado.

    El adelantamiento v1 armaba un cambio de carril a la izquierda SIN maquina de estados:
    no volvia al carril, no comprobaba trafico en sentido contrario y ni siquiera si
    existia carril izquierdo. La seccion 6 lo deja en "o se implementa la maquina de
    estados o se retira el HUD que miente", asi que aqui se traduce al verbo y el router
    contesta UNSUPPORTED_VERB. Apagarlo (enabled:false) SI hace algo: baja autoridad, y
    eso es disarm_all.
    """
    plano = payload.strip().lower()
    if plano == "false":
      return ("disarm_all", {})
    if plano == "true":
      return ("overtake", {})
    try:
      data = json.loads(payload)
    except (json.JSONDecodeError, ValueError, TypeError):
      return None
    if not isinstance(data, dict):
      return None
    if data.get("enabled") is False:
      return ("disarm_all", {})
    if data.get("enabled") is True:
      return ("overtake", {})
    return None

  # Tope de deceleracion del verbo assisted_decel (command_spec: accel en [-2.5, -1.0]).
  DECEL_MIN = -2.5
  DECEL_MAX = -1.0

  def _v1_brutebreak(self, payload):
    """/brutebreak -> verbo assisted_decel (o disarm_all al apagar).

    v1 aceptaba [-10, -1] m/s2 sin modo, sin gates y sin TTL. El contrato acota el rango a
    [-2.5, -1.0] en via publica (decision por defecto de la seccion 0: una frenada que
    llega con segundos de latencia, ordenada por quien no ve lo que ve el coche, no evita
    un peligro, lo crea). Una intensidad v1 fuera de rango se ACOTA en vez de rechazarse:
    el resultado es siempre MENOS autoridad de la pedida, que es la unica direccion que el
    diseno permite sin preguntar. Lo que si se rechaza es un payload no reconocido.
    """
    plano = payload.strip().lower()
    if plano in ("false", "0", "off"):
      return ("disarm_all", {})
    if plano in ("true", "1", "on"):
      return ("assisted_decel", {"accel": self.DECEL_MIN})
    try:
      data = json.loads(payload)
    except (json.JSONDecodeError, ValueError, TypeError):
      return None
    if not isinstance(data, dict):
      return None
    if data.get("enabled") is False or data.get("brutebreak") is False:
      return ("disarm_all", {})
    if not (data.get("enabled") is True or data.get("brutebreak") is True):
      return None
    accel = self.DECEL_MIN
    if "intensidad_frenado" in data:
      try:
        accel = float(data["intensidad_frenado"])
      except (TypeError, ValueError):
        return None
      if accel > 0:
        # Una deceleracion positiva es una aceleracion: no es este verbo.
        return None
      acotado = max(self.DECEL_MIN, min(self.DECEL_MAX, accel))
      if acotado != accel:
        cloudlog.warning(f"[Orbit] brutebreak v1 con {accel} m/s2 acotado a {acotado} (contrato assisted_decel)")
      accel = acotado
    return ("assisted_decel", {"accel": accel})

  def handle_speed_increment_config(self, payload):
    """Configuracion del tamano del paso de crucero (topic v1 /speed_increment).

    NO es un verbo: no mueve el coche. Solo dice cuanto vale "un paso" cuando llegue un
    cruise_delta v1, y el consumidor lo acota ademas a [1, 5] km/h, que es el tope por
    orden del verbo. Rango aceptado 1-50 por compatibilidad con la app vieja.
    """
    try:
      increment = float(payload.strip())
      # Validar rango (1-50 km/h)
      if 1.0 <= increment <= 50.0:
        # Param tipado FLOAT: hay que escribir float, no str (put(str) lanza TypeError).
        self.params.put("orbit_speed_increment", increment)
    except (ValueError, Exception):
      pass  # Error silenciado para no afectar al flujo principal


  def _v1_steer_torque_mode(self, payload, retenido=False):
    """/steer_torque_mode -> verbo torque_mode (o disarm_all si vuelve al modo 0).

    Topic v1:
      telemetry_config/{dongle_id}/steer_torque_mode

    Payload:
      {"dongle_id": "...", "steer_torque_mode": 0|1|2|3,
       "apply_target": "curvature"|"torque",   # obligatorio con mode == 3
       "source": "app" | "comma_ui"}

    Modos: 0 MODELO COMMA · 1 JETSON · 2 TEST MAX · 3 COMMA+JETSON (esquive).

    Que cambia respecto de v1. Antes esto escribia SteerTorqueMode directamente desde el
    hilo de red, con un unico gate ad-hoc (el modo 2 exigia OrbitBenchArmed) puesto a mano
    en la fase de contencion. Ahora los modos 1, 2 y 3 son el verbo `torque_mode`, que
    command_spec declara de modo BANCO: exigen armado FISICO en la pantalla del comma, con
    caducidad vigente, y ademas ENGAGED. El modo 1 le da el volante a la Jetson y el 3 le
    deja sumar offsets de esquive: los tres mueven el volante, asi que los tres van por la
    misma puerta. El modo 0 NO va por ahi: apagarlo BAJA autoridad y se atiende como
    disarm_all, que es el unico verbo que no se puede bloquear (seccion 2) -- si tambien
    exigiera banco, un TEST MAX armado por error no se podria apagar en remoto.

    `retenido` = el broker nos lo re-entrego (reconexion o reinicio del subsistema). En
    ese caso solo se acepta el modo 0: ver RETAIN_SOLO_SI_BAJA_AUTORIDAD.
    """
    try:
      data = json.loads(payload)
    except (json.JSONDecodeError, ValueError, TypeError):
      return None
    if not isinstance(data, dict):
      return None

    # Defensa en profundidad: el docstring de v1 prometia "por dongle_id" y el codigo
    # nunca lo comprobaba. Un payload dirigido a OTRO comma no cambia el volante de este.
    dongle_msg = data.get("dongle_id")
    if dongle_msg and dongle_msg != self.DongleID:
      cloudlog.warning(f"[Bemposta] steer_torque_mode descartado: dongle_id ajeno {dongle_msg!r} != {self.DongleID!r}")
      return None

    # Anti-eco: el propio comma publica este topic retenido con source="comma_ui".
    if data.get("source") == "comma_ui":
      return None

    if "steer_torque_mode" not in data:
      return None
    try:
      mode = int(data["steer_torque_mode"])
    except (ValueError, TypeError):
      return None
    if mode not in (0, 1, 2, 3):
      return None

    if retenido and mode != 0:
      # Un retenido se re-entrega en CADA reconexion: volveria a dar el volante a la
      # Jetson (o par maximo) sin que nadie mande nada en ese momento.
      cloudlog.warning(f"[Bemposta] steer_torque_mode RETENIDO con mode={mode} descartado (solo se acepta retenido el modo 0)")
      return None

    if mode == 0:
      return ("disarm_all", {})

    args = {"mode": mode}
    if mode == 3:
      apply_target = data.get("apply_target")
      if apply_target not in ("curvature", "torque"):
        # Sin apply_target no se sabe DONDE aplicar el esquive: descartar, no adivinar.
        cloudlog.warning(f"[Bemposta] steer_torque_mode mode=3 sin apply_target valido ({apply_target!r}), descartado")
        return None
      args["apply_target"] = apply_target
    return ("torque_mode", args)


  def handle_enroll_ack(self, payload):
    """Maneja el ack de enrolamiento ORBIT enviado por el backend.

    Topic:
      - telemetry_config/{dongle_id}/enroll_ack

    Payloads esperados (contrato compartido con el backend):
      reclamo:  {"claimed": true, "user_id": <int>, "user_name": <str|null>,
                 "user_email": <str|null>, "user_role": <str>, "ts": <epoch>}
      liberado: {"claimed": false, "ts": <epoch>}

    Al recibir claimed=true marcamos el dispositivo como reclamado
    (OrbitClaimed persiste tras reboot), guardamos el dueño en OrbitOwner y
    borramos el código de emparejamiento para dejar de anunciarlo y ocultar
    el QR. Con claimed=false (unclaim desde la app/backend) se revierte todo
    y se dispara OrbitEnrollRegen para que el announce loop rote el código y
    vuelva a anunciar de inmediato. Backends viejos pueden no mandar
    user_name/user_email: se cae a user_id. No hace falta anti-eco: el
    firmware nunca publica enroll_ack.

    user_role (user | developer | superadmin) va a OrbitOwnerRole solo para
    que la UI lo ENSENE: no abre ni cierra ningun gate del coche. Ausente o
    desconocido -> se borra. Con claimed=false se borra junto a OrbitOwner.
    """
    try:
      import json as json_mod
      data = json_mod.loads(payload)

      claimed = data.get("claimed")
      if claimed is True:
        self.params.put_bool("OrbitClaimed", True)
        self.params.remove("OrbitPairingCode")
        owner = data.get("user_name") or data.get("user_email")
        if not owner and data.get("user_id") is not None:
          owner = f"usuario {data.get('user_id')}"
        if owner:
          self.params.put("OrbitOwner", owner)
        rol = data.get("user_role")
        self._poner_rol_dueno(rol if rol in ROLES_DUENO else None)
        cloudlog.warning(f"[ORBIT ENROLL] Dispositivo reclamado (user_id={data.get('user_id')}, owner={owner!r}, ts={data.get('ts')})")
        print("[ORBIT ENROLL] Dispositivo reclamado, OrbitClaimed=True")
      elif claimed is False:
        self.params.put_bool("OrbitClaimed", False)
        self.params.remove("OrbitOwner")
        self.params.remove("OrbitPairingCode")
        self.params.put_bool("OrbitEnrollRegen", True)
        self._poner_rol_dueno(None)
        cloudlog.warning(f"[ORBIT ENROLL] Dispositivo liberado (unclaim, ts={data.get('ts')})")
        print("[ORBIT ENROLL] Dispositivo liberado, OrbitClaimed=False")
      else:
        print(f"[ORBIT ENROLL] enroll_ack sin claimed valido, ignorado: {data}")

    except Exception as e:
      print(f"[ORBIT ENROLL] ERROR handle_enroll_ack: {e}")

  def _poner_rol_dueno(self, rol):
    """Escribe (o borra con None) OrbitOwnerRole SOLO si cambia: el backend republica el
    enroll_ack retenido en cada reconexion y no hace falta reescribir el disco cada vez."""
    try:
      if (self.params.get(PARAM_OWNER_ROLE) or None) == rol:
        return
      if rol:
        self.params.put(PARAM_OWNER_ROLE, rol)
      else:
        self.params.remove(PARAM_OWNER_ROLE)
    except Exception:
      cloudlog.exception("[ORBIT ENROLL] no se pudo actualizar OrbitOwnerRole")

  def set_camera_sender(self, camera_sender):
    """Establece la referencia al CameraSender para control remoto desde la app."""
    self.camera_sender = camera_sender

  def handle_camera_config(self, payload):
    """Maneja la configuración de cámara recibida desde la app ORBIT.

    Topic: telemetry_config/{dongle_id}/camera_config

    Payload esperado (campos opcionales):
    {
      "dongle_id": "abc123",
      "timestamp": "2026-03-10T14:30:00.000Z",
      "image_sending_enabled": true|false,
      "send_frequency_seconds": 1|2|5|10|30|60,
      "save_images": true|false  (informativo, no afecta al comma)
    }
    """
    if self.camera_sender is None:
      return

    try:
      data = json.loads(payload)

      # Mensajes descriptivos para el panel debug antes de aplicar
      if "image_sending_enabled" in data:
        enabled = data["image_sending_enabled"]
        self.save_debug_message(
          f"telemetry_config/{self.DongleID}/cam_envio",
          "true" if enabled else "false"
        )

      if "send_frequency_seconds" in data:
        freq = data["send_frequency_seconds"]
        self.save_debug_message(
          f"telemetry_config/{self.DongleID}/cam_freq",
          str(freq) + "s"
        )

      if "save_images" in data:
        save = data["save_images"]
        self.save_debug_message(
          f"telemetry_config/{self.DongleID}/cam_guardar",
          "true" if save else "false"
        )

      if "camera_type" in data:
        ct = data["camera_type"]
        self.save_debug_message(
          f"telemetry_config/{self.DongleID}/cam_tipo",
          str(ct)
        )

      # Mapear preferred_camera_type -> camera_type para compatibilidad con la app
      if "preferred_camera_type" in data and "camera_type" not in data:
        data["camera_type"] = data["preferred_camera_type"]

      self.camera_sender.apply_config(data)
    except (json.JSONDecodeError, Exception):
      pass

  def start(self):
    """Inicia el cliente MQTT de comandos."""
    # El cliente ya se inicia automáticamente en el hilo

  def stop(self):
    """Detiene el cliente MQTT de comandos."""
    self.stop_event.set()
    # Despertar tambien al hilo de conexion: si esta dentro de stop.wait(5.0)
    # solo mira SU senal, y stop_event no lo saca de la espera.
    self._conn_stop.set()
    # El worker del router es un hilo aparte: sin esto, el supervisor del manager crearia
    # una instancia nueva y quedarian DOS workers ejecutando verbos sobre el mismo coche.
    if getattr(self, "router", None) is not None:
      try:
        self.router.stop()
      except Exception:
        cloudlog.exception("[Orbit] no se pudo parar el worker del CommandRouter")
    self.mqttc.disconnect()

if __name__ == "__main__":
  comandos = MQTTComandos()
  comandos.start()

  try:
    while True:
      time.sleep(1)
  except KeyboardInterrupt:
    comandos.stop()

