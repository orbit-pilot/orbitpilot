#!/usr/bin/env python3
import json
import time
import secrets
import threading
import paho.mqtt.client as mqtt
import cereal.messaging as messaging
from cereal.services import SERVICE_LIST
from openpilot.common.params import Params
from openpilot.orbit import config_broker
from openpilot.common.swaglog import cloudlog
import os
from .mqtt_comandos import CONN_SIN_BROKER_SECS, MQTTComandos, espera_reintento, tipo_dispositivo
from .camera_sender import CameraSender
from .command_state import get_command_plane
from . import telemetria_v1 as tel2
from . import spool as spool_mod
from . import config_v2 as cfg2


# Namespaces MQTT de presencia. v1 (telemetry_mqtt/...) es LEGACY: es el que
# consume la app de hoy y muere al cerrar la migracion. v2 (orbit/v2/...) es el
# contrato congelado del diseno de mando remoto v2 (seccion 3.1: retenido, qos 0).
# Durante la migracion se publica en AMBOS (seccion 13, publicacion dual v1+v2).
TOPIC_PRESENCE_V1 = "telemetry_mqtt/{}/presence"
TOPIC_PRESENCE_V2 = "orbit/v2/presence/{}"

# Retenidos rancios que quedaron en el broker de la epoca de los topics */global
# (mandos a TODA la flota, ya retirados del firmware). Ver _purge_retenidos_legacy.
TOPICS_GLOBAL_LEGACY = (
  "steer_torque_mode/global",
  "jetson_config/global",
  "jetson_obstacle_status/global",
)

# Canales LEGACY v1 que publican POSICION EN CRUDO (latitude/longitude en el payload de
# telemetry_mqtt/<dongle>/<canal>). El interruptor maestro de privacidad los silencia
# igual que silencia el canal v2 `pos` y la camara: el camino v1 sigue vivo durante la
# migracion y sin este filtro OrbitPrivacyMute dejaba de tapar justamente lo que dice
# donde esta el coche.
CANALES_V1_POSICION = frozenset({"gpsLocation", "gpsLocationExternal"})


def _epoch_ms() -> int:
  """Instante de PARED en epoch milisegundos enteros (seccion 3.2 del diseno v2).

  Existe para no repetir el noqa: time.time esta prohibido en este arbol porque casi
  siempre lo que se quiere es un plazo, y un plazo con reloj de pared salta cuando entra
  el NTP. Aqui si se quiere pared: son sellos que fecha la app o el backend. Los plazos
  internos de este fichero usan time.monotonic().
  """
  return time.time_ns() // 1_000_000


# Saneado de no finitos. La IMPLEMENTACION vive en telemetria_v1 (modulo puro) para que
# el camino v1 y el v2 no puedan divergir: json.dumps() emite por defecto los literales
# NaN/Infinity, que NO son JSON valido (RFC 8259), el parser del backend revienta y se
# pierde el mensaje ENTERO, de forma intermitente y sin ningun rastro porque el publish
# sale con rc=0. Que en este arbol hay no finitos esta confirmado por las defensas que ya
# existen aguas arriba (controlsd.py filtra la curvatura con math.isfinite, calibrationd.py
# comprueba np.isnan). El alias se mantiene porque es el nombre por el que lo importan los
# tests y el script de medida.
_sanea_no_finitos = tel2.sanea_no_finitos


class MQTTEnvioGeneral:
  def __init__(self):
    # Cadencia del camino LEGACY v1 (telemetry_mqtt/<dongle>/<canal>). NO es el ritmo del
    # bucle: el bucle pasa a TICK_SECS para poder sostener los canales de 2 Hz del
    # contrato v2 (seccion 7), y todo el camino v1 sigue corriendo detras de una compuerta
    # de 1 Hz. Subir v1 a 4 Hz habria multiplicado por cuatro justo el trafico que este
    # trabajo viene a bajar.
    self.velocidadActualizacion = 1
    # Tick base del bucle. 0,25 s da los 2 Hz de `vehicle` y `perception` con margen y
    # deja el ritmo del bucle en manos del sleep y no del poll del SubMaster.
    self.TICK_SECS = 0.25
    self._last_v1 = 0.0
    # Ultimo recv_frame del SubMaster publicado por cada canal v1: con el tick a 4 Hz y la
    # publicacion v1 a 1 Hz, `sm.updated` (que solo mira el tick actual) dejaria mudos los
    # canales cuyo mensaje llego en un tick que no publica.
    self._v1_frame = {}
    self.base_path = os.path.dirname(os.path.abspath(__file__))
    # Fichero que se VIGILA para la recarga en caliente: en el comma es
    # /data/orbit_config_mqtt.json (fuera del arbol git, sobrevive al updater), en el PC
    # la plantilla orbit/config_mqtt.json. El contenido se lee siempre por config_broker.
    self.jsonConfig = config_broker.ruta_config()
    self.jsonCanales = os.path.join(self.base_path, "canales.json")
    self.espera = 0.5
    self.pause_event = threading.Event()
    self.pause_event.set()
    self.stop_event = threading.Event()
    self.params = Params()
    # Identidad del dispositivo. Params.get() ya devuelve str.
    # SIN DongleId real NO hay identidad: el literal "DongleID" no es un id, es
    # un namespace COMPARTIDO por todos los comma sin registrar. Publicar ahi
    # GPS, velocidad y JPEG de camara junta a TODOS los dispositivos sin
    # registrar en un unico vehiculo fantasma llamado "DongleID" (el backend v2
    # toma topic_parts[1] como identidad autoritativa). Mientras el dongle no
    # sea valido este proceso NO publica telemetria, ni heartbeat, ni camara,
    # ni snapshots de configuracion: la UNICA salida permitida es el anuncio de
    # enrolamiento, que es justo el mecanismo por el que el dispositivo consigue
    # identidad. La ENTRADA de mandos ya estaba cerrada en mqtt_comandos.py.
    self.DongleID = "DongleID"
    self.dongle_valido = False
    # Arranque diferido del CameraSender al hilo del loop (ver _start_camera_sender).
    self._camera_pendiente = False
    # Log rate-limitado del estado "sin identidad": el loop corre a 1 Hz y sin
    # limite llenaria el log (y la flash) mientras el dispositivo no se registre.
    self._last_sin_dongle_log = 0.0
    self.SIN_DONGLE_LOG_SECS = 60.0
    self._refresh_dongle()
    self.conectado = False
    self.params.put_bool("OrbitConnected", False)
    self._last_heartbeat = 0.0
    self.HEARTBEAT_SECS = 3.0
    # Log de publish fallido con rate limit: el bucle de canales corre a 1 Hz
    # sobre N canales, asi que sin limite un broker caido llena el log (y la
    # flash) con la misma linea cientos de veces por minuto.
    self._last_rc_log = 0.0
    self.RC_LOG_SECS = 30.0
    # Hilo UNICO de conexion + su senal de relevo (ver _relanzar_hilo_conexion).
    self._conn_thread = None
    self._conn_stop = threading.Event()
    self._conn_pendiente = False
    # Deteccion de 'conectado pero mudo' para healthy(): instante (monotonic)
    # del primer sintoma, para exigir que persista antes de pedir un reinicio.
    self._mudo_desde = None
    self.MUDO_GRACE_SECS = 15.0
    self._last_toggles_check = time.monotonic()   # plazo interno: monotono, no epoch
    self.TOGGLES_RELOAD_SECS = 5.0
    # Estado del anuncio de enrolamiento ORBIT (QR). Ver _maybe_announce_enroll.
    self._last_enroll = 0.0
    self._enroll_issued_at = 0.0
    self._pairing_code = None
    self.ENROLL_ANNOUNCE_SECS = 30.0
    self.ENROLL_TTL_S = 600
    # Diagnostico remoto (healthcheck): SubMaster propio de este hilo, perezoso.
    self._diag_sm = None
    # Motor de la telemetria v2 (seccion 7). Es puro: decide QUE canal toca y con que
    # campos, y devuelve mensajes; publicar es cosa de este fichero.
    self.motor_v2 = tel2.MotorTelemetria(tel2.PERFIL_NORMAL, time.monotonic())
    self._last_perfil_check = 0.0
    self.PERFIL_RELOAD_SECS = 5.0
    self._perfil_avisado = False
    # Interruptor maestro LOCAL de privacidad: cache de 1 s, mismo patron que
    # CameraSender._privacidad_silenciada. Tiene que hacer efecto EN CALIENTE.
    self._privacy_ts = 0.0
    self._privacy_cache = False
    self._privacy_avisado = False
    # Cola persistente de telemetria diferida (orbit/spool.py). Se abre PEREZOSAMENTE, en
    # el hilo del loop: crea /data/orbit_spool y una base SQLite, y el constructor de esta
    # clase corre en el hilo del manager. Si no se puede abrir, el propio Spool se
    # desactiva y guardar() pasa a ser un no-op: la telemetria viva no depende de el.
    self._spool_obj = None
    self._spool_roto = False
    self._last_spool = 0.0
    self.SPOOL_SECS = 1.0
    # Configuracion deseada/reportada (seccion 8, orbit/config_v2.py). El sobre `desired`
    # lo deja mqtt_comandos en el buzon desde el hilo de RED; aplicarlo y publicar
    # `reported` es de ESTE hilo, porque toca Params, un JSON con flock y el CameraSender.
    self._cfg_deseado = None
    self._cfg_reportado = None
    # ...y si ese `reported` llego a SALIR por el cable. Son dos preguntas distintas y
    # confundirlas costaba la garantia de la seccion 8: `_cfg_reportado` es lo que el coche
    # SABE de si mismo (avanza al APLICAR, tambien sin cobertura, y es con lo que
    # gana_a_todos defiende un cambio local del `desired` retenido); esto de aqui es solo
    # la deuda con el broker, y lo unico que decide es si hay que reintentar el publish.
    # Arranca en False a proposito: tras un reinicio el retenido del broker puede ser de
    # otro arranque, o no estar, asi que el primer ciclo republica lo que se releyo.
    self._cfg_reportado_publicado = False
    # Rechazos del ultimo `desired` aceptado. Son ESTADO, no un subproducto del tick que
    # aplica: si se vaciaran en el tick siguiente, el `reported` saldria limpio, el backend
    # (con version por encima de la deseada) adoptaria el documento entero y el ajuste
    # pedido desapareceria del deseado sin haberse aplicado jamas, con la app pintando
    # "Al dia" encima. Viven mientras viva su causa (_cfg_rechazos_vivos).
    self._cfg_rechazos = {}
    self._cfg_cargado = False
    self._last_cfg = 0.0
    # 5 s y no 1: reconstruir `reported` son ~14 lecturas de Params mas un JSON, y esto
    # solo tiene que reconciliar ajustes, no seguir un actuador.
    self.CFG_SECS = 5.0
    self.load_config()
    self.cargar_canales()
    self.init_submaster()
    self.init_mqtt()
    self.init_comandos()
    self.init_camera_sender()
    self._link_camera_to_comandos()

  def load_config(self):
    # Plantilla del arbol + lo persistido en /data por encima (ver orbit/config_broker.py):
    # la IP que el usuario escribio en la pantalla no vuelve a "" con cada OTA.
    config = config_broker.leer_config()
    self.broker_address = config.get("broker", "localhost")
    self.broker_port = int(config.get("broker_port", 1883))
    # Credenciales MQTT opcionales (broker con auth). Vacio/ausente = anonimo.
    self.mqtt_username = (config.get("username") or "").strip() or None
    self.mqtt_password = config.get("password") or None
    try:
      self._cfg_mtime = os.path.getmtime(self.jsonConfig)
    except OSError:
      self._cfg_mtime = None

  def _maybe_reload_broker(self):
    """Relee config_mqtt.json en caliente. Si el usuario cambia la IP del broker
    desde la UI del comma (Ajustes -> Servidor Orbit), reconecta SIN reiniciar
    openpilot. Antes la IP se leia una sola vez al arrancar y el cambio no surtia
    efecto hasta un reinicio -> causa tipica de 'cambie la IP y sigue sin salir'."""
    # Reintento del relevo si el hilo de conexion anterior aun no habia muerto
    # cuando se pidio el cambio (ver _relanzar_hilo_conexion).
    if self._conn_pendiente:
      self._relanzar_hilo_conexion()
    try:
      mtime = os.path.getmtime(self.jsonConfig)
    except OSError:
      return
    if mtime == self._cfg_mtime:
      return
    self._cfg_mtime = mtime
    try:
      cfg = config_broker.leer_config()
      new_broker = cfg.get("broker", self.broker_address)
      new_port = int(cfg.get("broker_port", self.broker_port))
      new_username = (cfg.get("username") or "").strip() or None
      new_password = cfg.get("password") or None
    except Exception:
      return
    if (new_broker == self.broker_address and new_port == self.broker_port
        and new_username == self.mqtt_username and new_password == self.mqtt_password):
      return
    antes = f"{self.broker_address}:{self.broker_port}"
    cloudlog.warning(f"[Bemposta] broker/credenciales cambiados {antes} -> {new_broker}:{new_port}, reconectando (sin reiniciar openpilot)")
    self.broker_address, self.broker_port = new_broker, new_port
    self.mqtt_username, self.mqtt_password = new_username, new_password
    try:
      self.mqttc.loop_stop()
    except Exception:
      pass
    try:
      self.mqttc.disconnect()
    except Exception:
      pass
    self.conectado = False
    # Reaplicar credenciales antes de reconectar (username=None -> anonimo).
    self.mqttc.username_pw_set(self.mqtt_username, self.mqtt_password)
    self._relanzar_hilo_conexion()
    try:
      if getattr(self, "comandos_mqtt", None):
        self.comandos_mqtt.reload_broker(new_broker)
    except Exception:
      pass

  def cargar_canales(self):
    with open(self.jsonCanales) as f:
      data = json.load(f)
    enabled = [item for item in data["canales"] if item.get("enable") == 1]
    # Solo suscribir a canales que sean servicios cereal reales en este build
    # ('navInstruction' ya no existe en el sunnypilot nuevo). Evita el KeyError
    # del SubMaster y el acceso posterior self.sm[canal].
    self.enabled_items = [item for item in enabled if item["canal"] in SERVICE_LIST]
    dropped = [item["canal"] for item in enabled if item["canal"] not in SERVICE_LIST]
    if dropped:
      print(f"[Bemposta] canales sin servicio cereal, ignorados: {dropped}")
    # Respetar los toggles de UI del panel de canales ORBIT (param f"{canal}_toggle"). Su
    # consumidor original (SicMqttHilo2) fue retirado y los toggles quedaron
    # huerfanos; aqui volvemos a honrarlos SIN cambiar el comportamiento por
    # defecto: un canal solo se excluye si su toggle esta EXPLICITAMENTE a False.
    # Si el param esta sin configurar (None) o a True, se mantiene habilitado
    # (default = habilitado -> comportamiento actual intacto). El heartbeat de
    # carState no pasa por aqui, asi que la PRESENCIA del dispositivo sobrevive
    # aunque se desactive el canal carState.
    def _canal_habilitado(nombre):
      key = f"{nombre}_toggle"
      try:
        raw = self.params.get(key)
        if raw is None:
          return True  # sin configurar -> habilitado por defecto
        return bool(self.params.get_bool(key))  # solo excluye si el usuario lo puso a False
      except Exception:
        return True  # clave no registrada / error -> habilitado
    self.enabled_items = [item for item in self.enabled_items if _canal_habilitado(item["canal"])]
    self.keys_importantes_por_canal = {
      item["canal"]: item.get("keys_importantes", [])
      for item in self.enabled_items
    }
    # Suscripciones = canales v1 habilitados + servicios que alimentan la telemetria v2.
    # Los toggles `<canal>_toggle` del panel ORBIT siguen gobernando SOLO el camino v1
    # (son toggles de topic v1, y el diseno los manda a la app en F4): un canal v2 no
    # desaparece porque se apague su homonimo v1, y `vehicle` no depende de que
    # `carState_toggle` este puesto. Se filtra por SERVICE_LIST porque no todos los
    # servicios existen en todos los builds de sunnypilot.
    v2 = [s for s in tel2.SERVICIOS if s in SERVICE_LIST]
    self.servicios_v2 = v2
    vistos = set()
    self.lista_suscripciones = [
      c for c in ([item["canal"] for item in self.enabled_items] + v2)
      if not (c in vistos or vistos.add(c))
    ]

  def _maybe_reload_canales(self):
    """Re-evalua en caliente los toggles de canal del panel ORBIT
    (param f"{canal}_toggle"). cargar_canales() solo corria en __init__, asi que
    activar/desactivar un canal desde la UI no surtia efecto hasta reiniciar
    openpilot. Lecturas de Params baratas cada TOGGLES_RELOAD_SECS; si cambia la
    lista de canales se reconstruye el SubMaster (mismo hilo que lo consume)."""
    now = time.monotonic()
    if (now - self._last_toggles_check) < self.TOGGLES_RELOAD_SECS:
      return
    self._last_toggles_check = now
    try:
      antes = self.lista_suscripciones
      self.cargar_canales()
      if self.lista_suscripciones != antes:
        cloudlog.warning(f"[Bemposta] toggles de canal cambiados {antes} -> {self.lista_suscripciones}, recreando SubMaster")
        self.init_submaster()
    except Exception:
      cloudlog.exception("[Bemposta] _maybe_reload_canales fallo")

  def init_submaster(self):
    # SubMaster([]) lanza ValueError y tumbaria TODO el subsistema ORBIT del
    # manager (telemetria + comandos + camara; los eventos sobreviven porque
    # viven en selfdrived). Si el usuario desactiva todos los canales desde el
    # panel, mantenemos un SubMaster minimo con carState: no se publica como
    # canal (enabled_items sigue vacio) pero alimenta el heartbeat de presencia.
    self.sm = messaging.SubMaster(self.lista_suscripciones or ["carState"])

  def init_mqtt(self):
    self.mqttc = mqtt.Client()
    if self.mqtt_username:
      self.mqttc.username_pw_set(self.mqtt_username, self.mqtt_password)
    self.mqttc.max_queued_messages_set(0)  # No encolar mensajes en RAM si no hay conexión
    self.mqttc.on_connect = self.on_connect
    self.mqttc.on_disconnect = self.on_disconnect
    self.mqttc.reconnect_delay_set(min_delay=1, max_delay=30)
    self._set_will()
    self._relanzar_hilo_conexion()

  def _set_will(self):
    """(Re)arma el Last Will de presencia.

    Last Will: si el coche se apaga o se queda sin cobertura, el broker publica
    este retenido POR NOSOTROS. Antes no habia ni una llamada a will_set en todo
    orbit/ y la app deducia 'conectado' de que llegara telemetria en <10 s, lo
    que no distingue 'apagado' de 'sin cobertura'. Sin timestamp a proposito: el
    broker lo publica en un instante futuro desconocido, poner la hora de ahora
    seria mentir.

    El will se queda en el topic v1 porque es el que consume la app de hoy:
    moverlo a v2 dejaria a la app ya instalada sin aviso de muerte y un coche
    apagado se veria "conectado" para siempre. MQTT admite UN SOLO will por
    conexion, asi que orbit/v2/presence/<dongle> NO puede tener el suyo desde
    este cliente; su 'online' retenido se publica en on_connect y el propio
    payload v2 dice en "lwt_topic" donde vive el aviso de muerte autoritativo
    mientras dure la migracion dual (seccion 13 del diseno v2).

    Sin dongle valido NO se arma: el will tambien es un retenido y en el
    namespace comun "DongleID" seria un 'offline' compartido por toda la flota
    sin registrar.

    OJO: paho aplica el will en el SIGUIENTE CONNECT, no sobre la conexion viva.
    """
    # getattr: _refresh_dongle puede llamarnos desde __init__, antes de que
    # init_mqtt haya creado el cliente (entonces lo arma el propio init_mqtt).
    if not self.dongle_valido or getattr(self, "mqttc", None) is None:
      return
    try:
      self.mqttc.will_set(TOPIC_PRESENCE_V1.format(self.DongleID),
                          json.dumps({"online": False, "dongle_id": self.DongleID,
                                      "reason": "lwt", "schema_version": 1}),
                          qos=0, retain=True)
    except Exception as e:
      cloudlog.warning(f"[Bemposta] will_set fallo: {e}")

  def _refresh_dongle(self) -> bool:
    """Relee DongleId de Params y actualiza DongleID/dongle_valido.

    Se reevalua en cada on_connect (y en el loop mientras siga invalido) porque
    el dongle lo asigna el REGISTRO, despues del primer arranque: leerlo solo en
    __init__ dejaba al dispositivo silenciado hasta reiniciar openpilot aunque
    ya tuviera identidad.

    Devuelve True si acaba de pasar a valido.
    """
    try:
      d = self.params.get("DongleId")
    except Exception:
      return False
    d = d.strip() if isinstance(d, str) else ""
    valido = bool(d) and d != "DongleID"
    nuevo = d if valido else "DongleID"
    if valido == self.dongle_valido and nuevo == self.DongleID:
      return False

    self.DongleID = nuevo
    self.dongle_valido = valido

    if not valido:
      # DongleId borrado en caliente (des-registro). Callar tambien la camara:
      # sus JPEG llevan el dongle en el topic y acabarian en el namespace comun.
      cloudlog.error("[Bemposta] DongleId dejo de ser valido: telemetria y camara silenciadas")
      cam = getattr(self, "camera_sender", None)
      if cam is not None:
        try:
          cam.stop()
        except Exception:
          pass
        self.camera_sender = None
      return False

    cloudlog.warning(f"[Bemposta] DongleId valido ({nuevo}): habilitada la publicacion de telemetria y camara")
    self._set_will()
    cam = getattr(self, "camera_sender", None)
    if cam is not None:
      # El topic de camara lleva el dongle; si ya estaba corriendo hay que
      # moverlo al nuevo (atributo publico que fija su constructor).
      try:
        cam.dongle_id = self.DongleID
      except Exception:
        pass
    else:
      # Arranque diferido al hilo del loop: construir el CameraSender lee
      # ficheros de disco y levanta ZMQ, y _refresh_dongle tambien se llama
      # desde on_connect, que es el hilo de RED (un handler lento ahi tira el
      # PINGRESP y con el la conexion).
      self._camera_pendiente = True
    return True

  def init_comandos(self):
    """Inicializa el sistema de comandos MQTT.
    Protegido: si el cliente de ordenes falla al construirse, la telemetria
    (y por tanto la PRESENCIA del dispositivo en la app) NO debe caerse con el.
    Antes esto no estaba en try/except y una excepcion aqui abortaba todo el
    __init__ -> manager lo tragaba -> ni telemetria ni ordenes ni dispositivo."""
    try:
      # Plano de mando (GateMonitor + estado + publicador de orbitCommandState a 10 Hz).
      # Lo supervisa el manager como un hilo ORBIT mas; aqui solo se garantiza que este
      # en marcha para el caso de arrancar este modulo suelto (sin manager). start() es
      # idempotente y el objeto es el MISMO singleton que ve el manager, asi que esto no
      # duplica hilos ni deja al router apuntando a un GateMonitor huerfano.
      plane = get_command_plane()
      plane.start()
      self.comandos_mqtt = MQTTComandos(plane=plane)
      self.comandos_mqtt.start()
    except Exception:
      cloudlog.exception("[Bemposta] init_comandos fallo; la telemetria sigue sin ordenes")
      self.comandos_mqtt = None

  def init_camera_sender(self):
    """Inicializa el sistema de envío de imágenes de cámaras.
    La configuracion (enabled, frecuencia) se carga automaticamente
    desde /data/orbit_camera_config.json si existe."""
    self.camera_sender = None
    self._start_camera_sender()

  def _start_camera_sender(self):
    """Arranca el CameraSender, SOLO con dongle valido.

    Sus JPEG van a telemetry_mqtt/<dongle>/camera/...: sin identidad acabarian
    en el namespace comun "DongleID" (ver __init__), y ademas el envio puede
    venir ya activado de /data/orbit_camera_config.json, asi que un dispositivo
    sin registrar emitiria imagenes de la carretera nada mas arrancar.

    Se llama desde __init__ y desde el hilo del loop cuando el dongle pasa a
    valido en caliente; NUNCA desde el callback de paho (construirlo lee
    ficheros de disco y levanta ZMQ, y un handler lento en el hilo de red tira
    el PINGRESP y con el la conexion).
    """
    self._camera_pendiente = False
    if getattr(self, "camera_sender", None) is not None:
      return
    if not self.dongle_valido:
      cloudlog.warning("[Bemposta] sin DongleId valido: CameraSender NO arranca (los JPEG irian al namespace comun)")
      return
    try:
      self.camera_sender = CameraSender(
        mqtt_client=self.mqttc,
        dongle_id=self.DongleID,
        camera_type="road",
        interval_seconds=2.0,  # Default, se sobreescribe si hay config persistida
      )
      self.camera_sender.start()
    except Exception:
      cloudlog.exception("[Bemposta] arranque del CameraSender fallo; la telemetria sigue sin camara")
      self.camera_sender = None

  def _link_camera_to_comandos(self):
    """Conecta el CameraSender con MQTTComandos para permitir control remoto desde la app."""
    if getattr(self, 'camera_sender', None) is not None and getattr(self, 'comandos_mqtt', None) is not None:
      self.comandos_mqtt.set_camera_sender(self.camera_sender)

  def _relanzar_hilo_conexion(self):
    """Arranca el UNICO hilo de conexion, relevando antes al anterior.

    Antes: si el broker no respondia, el hilo inicial se quedaba en su bucle de
    reintento (sleep de 5 s) y al corregir la IP desde Ajustes se lanzaba un
    SEGUNDO hilo sin avisar al primero. Cuando el nuevo conectaba y el viejo
    despertaba, el connect() del viejo sobre el MISMO objeto cliente hacia
    _sock_close() y _out_packet.clear(): cerraba el socket recien abierto y el
    dispositivo se quedaba mudo hasta reiniciar.
    """
    viejo = self._conn_thread
    if viejo is not None and viejo.is_alive():
      self._conn_stop.set()
      # 6 s = la espera de 5 s entre reintentos (ya interrumpible) mas el
      # margen del connect() en curso, que paho corta a los 5 s.
      viejo.join(timeout=6.0)
      if viejo.is_alive():
        # No relanzar con el viejo todavia vivo: seria exactamente el doble
        # connect() que este metodo existe para evitar. Se reintenta en la
        # siguiente iteracion del loop.
        cloudlog.warning("[Bemposta] hilo de conexion anterior aun vivo; relevo aplazado")
        self._conn_pendiente = True
        return
    self._conn_pendiente = False
    self._conn_stop = threading.Event()
    self._conn_thread = threading.Thread(target=self.setup_mqtt, args=(self._conn_stop,),
                                         daemon=True, name="OrbitMQTTConnect")
    self._conn_thread.start()

  def setup_mqtt(self, stop=None):
    """Bucle de conexion inicial. Corre en el hilo unico de conexion; `stop` es
    su senal de relevo (la pone _relanzar_hilo_conexion antes de sustituirlo).

    El estado del reintento (fallos seguidos, aviso de 'sin broker') es LOCAL a
    proposito: cada relevo (_relanzar_hilo_conexion tras escribir un broker desde
    Ajustes) arranca limpio en 5 s. El hilo NO sale mientras no conecte: la
    reconexion automatica de paho solo existe tras un primer connect() bueno, y el
    supervisor del manager no vigila este hilo (is_alive mira el loop; healthy no
    mira _conn_thread). La politica (escalera 5/10/20/60 y espera sin broker) se
    importa de mqtt_comandos para que los dos clientes no diverjan."""
    if stop is None:
      stop = self._conn_stop
    fallos = 0
    sin_broker_avisado = False
    while not self.stop_event.is_set() and not stop.is_set():
      if not str(self.broker_address or "").strip():
        # config_mqtt.json de fabrica ("broker": ""): paho lanza 'Invalid host.' al
        # instante y esto giraba cada 5 s llenando el rlog de todos los dispositivos
        # sin configurar. Un aviso y a esperar a que la UI escriba el broker (la
        # relectura en caliente relanza este hilo; la espera es la red de seguridad).
        if not sin_broker_avisado:
          cloudlog.warning("[Bemposta] MQTTEnvioGeneral: broker no configurado; esperando configuracion (Ajustes -> Servidor Orbit)")
          sin_broker_avisado = True
        if stop.wait(CONN_SIN_BROKER_SECS):
          break
        continue
      try:
        cloudlog.warning(f"[Bemposta] MQTTEnvioGeneral conectando a broker {self.broker_address}:{self.broker_port}")
        self.mqttc.connect(self.broker_address, self.broker_port, 60)
        if not self.conectado:
          self.mqttc.loop_start()
          # Esperar un momento para que se establezca la conexión
          time.sleep(0.5)
        break
      except Exception as e:
        # Diagnostico clave: si el broker cambio de IP (IP domestica dinamica),
        # este es el log que lo delata. Antes estaba silenciado y no se veia nada.
        espera = espera_reintento(fallos)
        fallos += 1
        cloudlog.warning(f"[Bemposta] MQTTEnvioGeneral NO pudo conectar a {self.broker_address}:{self.broker_port}: {e}. Reintento en {espera:g}s")
        # Espera interrumpible: con time.sleep(5) el relevo tardaba hasta 5 s en
        # notarse y era cuando se solapaban los dos hilos de conexion.
        if stop.wait(espera):
          break

  def on_connect(self, client, userdata, flags, rc):
    # Cuerpo COMPLETO en try/except: el paho 2.1.0 vendorizado corre las
    # callbacks con suppress_exceptions=False, asi que cualquier excepcion aqui
    # sale hasta _thread_main, cuyo finally pone _thread=None y mata el hilo de
    # red EN SILENCIO. Como para entonces ya habiamos puesto conectado=True, el
    # loop seguia publicando contra un socket que nadie escribe y el dispositivo
    # quedaba mudo mostrando 'conectado' para siempre.
    try:
      if rc == 0:
        self.conectado = True
        self.params.put_bool("OrbitConnected", True)
        cloudlog.warning(f"[Bemposta] MQTTEnvioGeneral CONECTADO al broker {self.broker_address}:{self.broker_port} (rc={rc})")
        # Reevaluar la identidad en CADA conexion: el dongle lo asigna el
        # registro despues del primer arranque (una sola lectura de Params, no
        # bloquea el hilo de red).
        self._refresh_dongle()
        # Limpieza unica de los retenidos rancios de */global. Va aqui porque
        # solo se puede borrar un retenido con el socket abierto.
        self._purge_retenidos_legacy()
        if not self.dongle_valido:
          cloudlog.error("[Bemposta] SIN DongleId valido: no publico presencia ni telemetria (namespace comun), solo enrolamiento")
          return
        # Gemelo 'online' del Last Will (ver _set_will): retenido, para que la
        # app distinga 'apagado' de 'sin datos' sin esperar a la telemetria.
        self._publish_presence_online()
        # "Cold start" sync: publicar nuestro estado actual como mensaje
        # RETAINED para que cualquier app que se conecte despues lo reciba
        # inmediatamente (sin necesidad de que el usuario mueva nada).
        #
        # Lo hacemos con un pequeno delay para dar tiempo a que el otro
        # cliente MQTT (mqtt_comandos, que tiene las suscripciones) procese
        # los retained que el broker le pueda estar entregando del lado app
        # (caso: el usuario cambio algo en la app mientras el Comma estaba
        # offline). Asi publicamos DESPUES de haber aplicado esos cambios
        # y nuestro retained refleja el estado real.
        threading.Timer(1.5, self._publish_state_snapshot_retained).start()
      else:
        self.conectado = False
        self.params.put_bool("OrbitConnected", False)
        cloudlog.warning(f"[Bemposta] MQTTEnvioGeneral rechazado por broker (rc={rc})")
    except Exception:
      cloudlog.exception("[Bemposta] on_connect fallo (el hilo de red habria muerto en silencio)")

  def _publish_presence_online(self):
    """Publica el 'online' retenido de presencia en AMBOS namespaces.

    Migracion dual v1+v2 (seccion 13 del diseno v2): v1 es el que consume la app
    de hoy, v2 (orbit/v2/presence/<dongle>, retenido) es el namespace congelado
    del contrato nuevo (seccion 3.1). Ambos llevan schema_version para que el
    consumidor sepa que esta leyendo.

    El aviso de muerte (Last Will) solo puede vivir en UNO de los dos porque
    MQTT admite un will por conexion, y se queda en v1 para no romper la app ya
    instalada; por eso el payload v2 publica "lwt_topic" apuntando a el.

    Se llama en cada conexion Y como LATIDO cada HEARTBEAT_SECS (ver _ciclo_v1): esto es
    lo que sustituye a republicar el carState entero para decir "sigo vivo". Devuelve True
    si el 'online' v1 -- el que consume la app de hoy -- salio de verdad (rc==0).
    """
    if not self.dongle_valido:
      return False
    # epoch ms ENTERO: el contrato v2 (seccion 3.2) prohibe ISO-8601 en el cable
    # y exige que el instante sea siempre epoch en milisegundos.
    ts_ms = _epoch_ms()   # epoch de PARED: lo fecha la app
    v1_topic = TOPIC_PRESENCE_V1.format(self.DongleID)
    ok = False
    try:
      info = self.mqttc.publish(v1_topic,
                                json.dumps({"online": True, "dongle_id": self.DongleID,
                                            "timestamp": ts_ms, "schema_version": 1}),
                                qos=0, retain=True)
      ok = info.rc == mqtt.MQTT_ERR_SUCCESS
      if not ok:
        self._log_publish_rc("presencia v1", v1_topic, info.rc)
    except Exception as e:
      cloudlog.warning(f"[Bemposta] presence v1 (online) fallo: {e}")
    try:
      # Sin dongle_id en el payload: el contrato v2 lo toma del topic (seccion 7).
      self.mqttc.publish(TOPIC_PRESENCE_V2.format(self.DongleID),
                         json.dumps({"v": 2, "schema_version": 2, "online": True,
                                     "ts_ms": ts_ms, "lwt_topic": v1_topic}),
                         qos=0, retain=True)
    except Exception as e:
      cloudlog.warning(f"[Bemposta] presence v2 (online) fallo: {e}")
    return ok

  def _purge_retenidos_legacy(self):
    """Borra del broker los retenidos rancios de los topics */global.

    El firmware ya no publica NI escucha */global (se retiraron por ser mandos a
    TODA la flota: un topic sin dongle en la ruta lo obedecen todos los coches).
    Pero el broker conserva el ULTIMO valor retenido de cada uno y se lo entrega
    a cualquiera que se suscriba: un steer_torque_mode de hace meses sigue ahi y
    se lee como estado actual. El gesto MQTT para borrar un retenido es publicar
    payload VACIO con retain=True.

    qos=1: es una operacion de una sola vez y queremos que paho la reintente si
    la conexion se corta, no que se pierda como un qos=0 cualquiera.

    Gateado por OrbitGlobalRetainPurged, que guarda el broker DONDE ya se hizo:
    asi no se repite en cada reconexion (el motivo del gate) pero si se rehace
    al cambiar de broker, porque el broker nuevo tiene sus propios retenidos.
    """
    destino = f"{self.broker_address}:{self.broker_port}"
    try:
      if self.params.get("OrbitGlobalRetainPurged") == destino:
        return
    except Exception:
      pass
    ok = True
    for topic in TOPICS_GLOBAL_LEGACY:
      try:
        info = self.mqttc.publish(topic, None, qos=1, retain=True)  # payload vacio + retain = borrar
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
          ok = False
      except Exception as e:
        ok = False
        cloudlog.warning(f"[Bemposta] purga del retenido {topic} fallo: {e}")
    if not ok:
      return
    try:
      # STRING: put() exige el tipo nativo del param (un put(int) aqui seria
      # TypeError). No bloquea: putNonBlocking por defecto.
      self.params.put("OrbitGlobalRetainPurged", destino)
    except Exception as e:
      cloudlog.warning(f"[Bemposta] no pude marcar la purga de */global: {e}")
    cloudlog.warning(f"[Bemposta] retenidos legacy */global purgados en {destino}")

  def _publish_state_snapshot_retained(self):
    """Publica el estado actual de SteerTorqueMode y config_jetson con retain=True.

    Se llama una vez al conectar MQTT. El broker guarda estos mensajes y los
    entrega instantaneamente a cualquier subscriber futuro (p.ej. la app al
    lanzarse). source='comma_ui' para que el anti-eco de mqtt_comandos.py
    ignore el retained cuando le llegue a el mismo por su suscripcion.
    """
    if not self.conectado:
      return
    # Sin identidad estos retenidos irian a telemetry_config/DongleID/..., un
    # namespace compartido por todos los comma sin registrar (ver __init__).
    if not self.dongle_valido:
      return
    try:
      # --- SteerTorqueMode ---
      mode_raw = self.params.get("SteerTorqueMode")
      try:
        mode = int(mode_raw) if mode_raw else 0
      except (ValueError, TypeError):
        mode = 0
      steer_payload = {
        "dongle_id": self.DongleID,
        "steer_torque_mode": mode,
        "source": "comma_ui",
        "timestamp": _epoch_ms(),   # epoch de PARED: lo fecha la app, no es un plazo
      }
      steer_str = json.dumps(steer_payload)
      # SOLO la variante por dongle: un topic sin el dongle en la ruta
      # ("steer_torque_mode/global") es un mando a TODA la flota y la ACL del
      # broker no puede distinguir destinatarios dentro de el.
      self.mqttc.publish(f"telemetry_config/{self.DongleID}/steer_torque_mode", steer_str, qos=0, retain=True)
      print(f"[COLD-START SYNC] Retained SteerTorqueMode={mode} publicado")

      # --- config_jetson.json (IPs, puertos, calidad, enabled) ---
      config_path = os.path.join(self.base_path, "config_jetson.json")
      if os.path.exists(config_path):
        try:
          with open(config_path) as f:
            cfg = json.load(f)
        except Exception:
          cfg = {}
        # Propagar el _version EXACTO del disco. Asi, si este retained vuelve
        # a nosotros via handle_jetson_config, el anti-eco por _version lo
        # descartara porque sera igual al ya guardado en disco (no mayor).
        # Si no hay _version en el archivo (migracion), no lo inventamos
        # aqui para no pisarnos a nosotros mismos en el futuro.
        jetson_payload = {
          "dongle_id": self.DongleID,
          "jetson_enabled": cfg.get("jetson_enabled", False),
          "jetson_ip": cfg.get("jetson_ip", ""),
          "comma_ip": cfg.get("comma_ip", ""),
          "jetson_img_port": cfg.get("jetson_img_port", 5555),
          "jetson_torque_port": cfg.get("jetson_torque_port", 5556),
          "jpeg_quality": cfg.get("jpeg_quality", 80),
          "source": "comma_ui",
          "timestamp": _epoch_ms(),   # epoch de PARED: lo fecha la app, no es un plazo
        }
        if "_version" in cfg:
          jetson_payload["_version"] = str(cfg["_version"])
        jet_str = json.dumps(jetson_payload)
        # SOLO por dongle (ver arriba): "jetson_config/global" reconfiguraba la
        # Jetson de todos los vehiculos a la vez.
        self.mqttc.publish(f"telemetry_config/{self.DongleID}/jetson_config", jet_str, qos=0, retain=True)
        print(f"[COLD-START SYNC] Retained jetson_config publicado: {jetson_payload}")
    except Exception as e:
      print(f"[COLD-START SYNC] ERROR publicando snapshot: {e}")

  def on_disconnect(self, client, userdata, rc):
    # Mismo blindaje que on_connect: con suppress_exceptions=False una excepcion
    # aqui sale a _thread_main y mata el hilo de red, dejando la reconexion
    # automatica de paho sin nadie que la ejecute (mudo hasta reiniciar).
    try:
      self.conectado = False
      self.params.put_bool("OrbitConnected", False)
      cloudlog.warning(f"[Bemposta] MQTTEnvioGeneral DESCONECTADO del broker {self.broker_address} (rc={rc})")
    except Exception:
      cloudlog.exception("[Bemposta] on_disconnect fallo (el hilo de red habria muerto en silencio)")

  def start(self):
    # Guardar el handle: el supervisor de manager (manager_thread) vigila este
    # hilo y reinicia la instancia si muere, asi que necesita is_alive()/join().
    self.thread = threading.Thread(target=self.loop, daemon=True, name="MQTTEnvioGeneral")
    self.thread.start()

  def is_alive(self) -> bool:
    """True si el hilo principal (loop de telemetria) sigue vivo."""
    t = getattr(self, "thread", None)
    return t is not None and t.is_alive()

  def join(self, timeout=None):
    t = getattr(self, "thread", None)
    if t is not None:
      t.join(timeout)

  @staticmethod
  def _enlace_mudo(cli, cree_conectado) -> bool:
    """True si un cliente paho cree estar conectado pero ya no puede publicar.

    Dos sintomas: su hilo de red desaparecio (_thread=None, que es lo que deja
    el finally de _thread_main cuando una callback lanza) o el propio cliente
    se declara desconectado. El primero es el importante: is_connected() mira
    _state, que se queda en CONNECTED cuando el hilo muere, asi que por si solo
    NO detecta el caso 'mudo'.
    """
    if cli is None or not cree_conectado:
      return False
    try:
      if getattr(cli, "_thread", None) is None:
        return True
      return not cli.is_connected()
    except Exception:
      return False

  def healthy(self) -> bool:
    """True si los subsistemas internos que deben estar vivos lo estan.

    El supervisor de manager reinicia la instancia entera si esto devuelve
    False (p.ej. el hilo de camera_sender murio por una excepcion y la app
    se quedaba sin camara hasta reiniciar el dispositivo).
    """
    cam = getattr(self, "camera_sender", None)
    if cam is not None:
      cam_t = getattr(cam, "thread", None)
      if cam_t is not None and not cam_t.is_alive():
        return False

    # Enlace MQTT mudo (telemetria o comandos): hasta ahora el supervisor no
    # tenia forma de ver una conexion muerta y el dispositivo se quedaba sin
    # publicar ni obedecer hasta reiniciarlo a mano.
    # OJO: 'no conectado' a secas NO es enfermedad. Sin cobertura o con el
    # broker apagado es lo normal, y devolver False ahi reiniciaria la
    # instancia cada 30 s (matando camara y comandos) sin arreglar nada. Por
    # eso se exige que NOSOTROS creamos estar conectados, y que el sintoma
    # persista MUDO_GRACE_SECS para no cazar la ventana normal de reconexion.
    mudo = self._enlace_mudo(getattr(self, "mqttc", None), self.conectado)
    cmd = getattr(self, "comandos_mqtt", None)
    if cmd is not None:
      mudo = mudo or self._enlace_mudo(getattr(cmd, "mqttc", None), getattr(cmd, "conectado", False))
    if not mudo:
      self._mudo_desde = None
      return True
    now = time.monotonic()
    if self._mudo_desde is None:
      self._mudo_desde = now
    elif (now - self._mudo_desde) >= self.MUDO_GRACE_SECS:
      cloudlog.warning("[Bemposta] enlace MQTT mudo (se cree conectado pero no publica); pido reinicio")
      return False
    return True

  def stop(self):
    """Detiene el sistema MQTT completo."""
    self.stop_event.set()
    # Despertar YA al hilo de conexion (espera interrumpible de 5 s): el
    # supervisor de manager hace stop()+join(2 s) antes de recrear la
    # instancia, y un hilo viejo aun dentro de connect() pisaria el socket
    # del cliente nuevo.
    self._conn_stop.set()
    # comandos_mqtt puede ser None si init_comandos fallo: el atributo existe
    # (hasattr no basta) y None.stop() abortaria el resto del apagado.
    if getattr(self, 'comandos_mqtt', None) is not None:
      self.comandos_mqtt.stop()
    if hasattr(self, 'camera_sender') and self.camera_sender is not None:
      self.camera_sender.stop()
    # Volcar a disco lo que quede en RAM del spool: son muestras que no salieron y el
    # manager recrea esta instancia sin avisar. Se hace flush() y NO cerrar(): el spool es
    # un singleton de PROCESO (spool.get_spool()) y la instancia siguiente lo reutiliza;
    # cerrarlo lo dejaria con activo=False para siempre y guardar() seria un no-op el
    # resto de la vida del proceso.
    if getattr(self, '_spool_obj', None) is not None:
      try:
        self._spool_obj.flush()
      except Exception:
        cloudlog.exception("[Bemposta] flush de cierre del spool fallo")
    self.mqttc.disconnect()
    # print("🛑 Sistema MQTT detenido")  # Comentado para reducir uso de memoria

  def _maybe_publish_healthcheck(self):
    """Si la UI/app dejo una peticion (Param OrbitHealthcheckRequest), construye
    el informe de salud y lo publica una vez. Corre en el hilo de loop()."""
    try:
      req = self.params.get("OrbitHealthcheckRequest")
    except Exception:
      return
    if not req:
      return
    # Consumir la peticion de forma idempotente (aunque falle el publish).
    try:
      self.params.remove("OrbitHealthcheckRequest")
    except Exception:
      pass
    try:
      payload = self._build_healthcheck()
      self.mqttc.publish(f"telemetry_mqtt/{self.DongleID}/healthcheck",
                         json.dumps(payload), qos=0, retain=False)
    except Exception as e:
      cloudlog.warning(f"[ORBIT] healthcheck publish fallo: {e}")

  def _build_healthcheck(self):
    """Reune metricas de salud del dispositivo. El SubMaster de diagnostico se
    crea y consume en ESTE hilo (msgq no es thread-safe) y se calienta unos
    ciclos para captar datos frescos de servicios de baja frecuencia."""
    if self._diag_sm is None:
      self._diag_sm = messaging.SubMaster(['deviceState', 'pandaStates', 'managerState'])
    for _ in range(6):
      self._diag_sm.update(100)

    report = {
      "dongle_id": self.DongleID,
      "ts": _epoch_ms() // 1000,   # epoch de PARED: sella el informe para el backend
      "fw": self.params.get("Version") or "",
      "branch": self.params.get("GitBranch") or "",
      "commit": (self.params.get("GitCommit") or "")[:7],
    }

    try:
      if self._diag_sm.updated["deviceState"] or self._diag_sm.recv_frame["deviceState"] > 0:
        ds = self._diag_sm["deviceState"]
        cpu = list(ds.cpuTempC)
        gpu = list(ds.gpuTempC)
        report["device"] = {
          "cpu_temp_c": round(max(cpu), 1) if cpu else None,
          "gpu_temp_c": round(max(gpu), 1) if gpu else None,
          "max_temp_c": round(float(ds.maxTempC), 1),
          "thermal_status": str(ds.thermalStatus),
          "mem_used_pct": int(ds.memoryUsagePercent),
          "free_space_pct": round(float(ds.freeSpacePercent), 1),
          "network_type": str(ds.networkType),
          "network_strength": str(ds.networkStrength),
        }
    except Exception:
      pass

    try:
      pandas = list(self._diag_sm["pandaStates"])
      if pandas:
        ps = pandas[0]
        report["panda"] = {
          "voltage_mv": int(ps.voltage),
          "ignition": bool(ps.ignitionLine or ps.ignitionCan),
          "fault_status": str(ps.faultStatus),
          "faults": [str(f) for f in ps.faults],
          "safety_model": str(ps.safetyModel),
        }
    except Exception:
      pass

    try:
      procs = list(self._diag_sm["managerState"].processes)
      not_running = [p.name for p in procs if p.shouldBeRunning and not p.running]
      report["manager"] = {
        "process_count": len(procs),
        "not_running": not_running,
      }
    except Exception:
      pass

    # Frescura de enlaces ORBIT (ya en Params).
    try:
      last_pub = self.params.get("OrbitLastPublish")
      # OrbitLastPublish se escribe con epoch, asi que la edad se calcula con epoch.
      report["orbit_last_publish_age_s"] = (int(_epoch_ms() / 1000.0 - float(last_pub))
                                            if last_pub else None)
    except Exception:
      report["orbit_last_publish_age_s"] = None
    try:
      jt_ts = self.params.get("JetsonTorqueTimestamp")
      # JetsonTorqueTimestamp lo escribe zmq_client con epoch: misma base de tiempo.
      report["jetson_torque_age_s"] = (round(_epoch_ms() / 1000.0 - float(jt_ts), 1)
                                       if jt_ts else None)
    except Exception:
      report["jetson_torque_age_s"] = None

    return report

  def _log_publish_rc(self, que, topic, rc):
    """Loguea un publish fallido (rc != 0) como mucho cada RC_LOG_SECS.

    Sin rate limit esto seria una linea por canal y por ciclo (1 Hz): con el
    broker caido llenaria el log y la flash del dispositivo."""
    now = time.monotonic()
    if (now - self._last_rc_log) < self.RC_LOG_SECS:
      return
    self._last_rc_log = now
    cloudlog.warning(f"[Bemposta] publish de {que} en {topic} NO salio (rc={rc})")

  @staticmethod
  def _campos_no_serializables(datos):
    """Nombres de los campos que impiden serializar el mensaje, para el log.

    json.dumps solo dice 'Out of range float values are not JSON compliant',
    nunca QUE campo: sin esto no habia forma de saber que dato corrompe la
    telemetria de un canal."""
    malos = []
    if isinstance(datos, dict):
      for k, v in datos.items():
        try:
          json.dumps(v, allow_nan=False)
        except (ValueError, TypeError):
          malos.append(k)
    return malos or ["<desconocido>"]

  def loop(self):
    # Guard de nivel superior: el loop corre en un hilo daemon sin reinicio, asi
    # que cualquier excepcion no capturada dentro de una iteracion mataba la
    # telemetria EN SILENCIO para el resto de la sesion (los comandos seguian
    # vivos en su propio hilo -> sintoma confuso). Logueamos y reintentamos.
    while not self.stop_event.is_set():
      self.pause_event.wait()
      try:
        self._loop_once()
      except Exception:
        cloudlog.exception("[Bemposta] iteracion del loop de telemetria fallo; reintento")
        time.sleep(self.velocidadActualizacion)

  def _loop_once(self):
    """Un tick del bucle. DOS RITMOS: el camino v2 corre a TICK_SECS (4 Hz) y todo el
    camino LEGACY v1 detras de una compuerta de velocidadActualizacion (1 Hz).

    El poll del SubMaster pasa a NO bloquear (timeout 0). Antes el bucle lo usaba de
    metronomo (hasta 100 ms) ademas del sleep de 1 s, lo que daba los ~0,9 Hz que midio la
    auditoria; con dos ritmos el metronomo tiene que ser el sleep, o el tick corto se
    convierte en uno largo cada vez que no hay nada que recibir. Los sockets son
    conflate=True, asi que un poll no bloqueante siempre devuelve el ULTIMO mensaje de
    cada servicio: no se pierde nada por no esperar.
    """
    self.sm.update(0)
    ahora = time.monotonic()
    if (ahora - self._last_v1) >= self.velocidadActualizacion:
      self._last_v1 = ahora
      self._ciclo_v1()
    self._ciclo_v2(ahora)
    time.sleep(self.TICK_SECS)

  def _ciclo_v1(self):
      # Recoger en caliente un cambio de IP del broker hecho desde la UI.
      self._maybe_reload_broker()

      # Recoger en caliente los toggles de canal cambiados desde la UI (panel ORBIT).
      self._maybe_reload_canales()

      # Reevaluar la identidad mientras no la tengamos: el DongleId lo asigna el
      # registro DESPUES del primer arranque, y sin esto el dispositivo se
      # quedaba silenciado hasta reiniciar openpilot. Una lectura de Params por
      # iteracion (1 Hz) y SOLO mientras siga invalido.
      if not self.dongle_valido:
        self._refresh_dongle()

      # Arranque diferido del CameraSender cuando el dongle pasa a valido en
      # caliente: se hace aqui, en el hilo del loop, y nunca en el de red.
      if self._camera_pendiente:
        self._start_camera_sender()
        self._link_camera_to_comandos()

      # Enrolamiento ORBIT (QR): generar/rotar el codigo SIEMPRE (aunque no haya
      # broker) para que la UI pueda pintar el QR sin conexion; el publish va
      # gateado por conexion dentro del propio metodo. Best-effort.
      self._maybe_announce_enroll()

      # Verificar conexión antes de intentar enviar (evita encolar mensajes)
      # Usar verificación más simple: si está conectado según el callback
      is_connected = self.conectado
      # También verificar el estado real del cliente si está disponible
      if hasattr(self.mqttc, 'is_connected'):
        is_connected = is_connected and self.mqttc.is_connected()

      if not is_connected:
        # Sin conexión: no procesar ni encolar mensajes para evitar saturación de RAM
        # Log ocasional para debug (cada 50 iteraciones = ~50 segundos)
        if hasattr(self, '_no_connection_log_counter'):
          self._no_connection_log_counter += 1
        else:
          self._no_connection_log_counter = 0

        # Log eliminado para reducir uso de memoria

        return

      # Resetear contador si hay conexión
      if hasattr(self, '_no_connection_log_counter'):
        self._no_connection_log_counter = 0

      # SIN IDENTIDAD NO SE PUBLICA NADA (ver __init__): ni telemetria, ni
      # heartbeat de presencia, ni sicuem_torque, ni snapshots de config, ni
      # camara. Todo eso lleva el dongle en el topic y sin el iria al namespace
      # comun "DongleID", donde el backend v2 (que toma topic_parts[1] como
      # identidad) mezclaria a todos los dispositivos sin registrar en un unico
      # vehiculo fantasma. La UNICA excepcion es el anuncio de enrolamiento, que
      # ya se hizo arriba: es como el dispositivo consigue identidad.
      if not self.dongle_valido:
        ahora_mono = time.monotonic()
        if (ahora_mono - self._last_sin_dongle_log) >= self.SIN_DONGLE_LOG_SECS:
          self._last_sin_dongle_log = ahora_mono
          cloudlog.error("[Bemposta] sin DongleId valido: telemetria y camara silenciadas, solo anuncio de enrolamiento")
        return

      # Responder a una peticion de diagnostico remoto (healthcheck), si la hay.
      self._maybe_publish_healthcheck()

      # Descriptor de capacidades v2 (orbit/v2/caps/<dongle>, retenido). Se reintenta a
      # 1 Hz porque marca y plataforma salen de carParams, que offroad todavia no existe:
      # el descriptor publicado al conectar sale con los dos campos vacios y hay que
      # rehacerlo en cuanto el coche se identifica. Solo republica si CAMBIA.
      if getattr(self, "comandos_mqtt", None) is not None:
        try:
          self.comandos_mqtt.maybe_publish_caps()
        except Exception:
          cloudlog.exception("[Bemposta] maybe_publish_caps fallo")

      # Configuracion deseada/reportada (seccion 8). Best-effort como todo lo de aqui: la
      # reconciliacion de ajustes no puede tumbar el bucle de telemetria.
      try:
        self._mantener_config(time.monotonic())
      except Exception:
        cloudlog.exception("[Bemposta] mantenimiento de configuracion fallo")

      # Publicar Jetson config si fue cambiada desde la UI del Comma
      try:
        jetson_payload = self.params.get("JetsonConfigMqttPayload")
        if jetson_payload and len(jetson_payload) > 2:
          payload_str = jetson_payload
          print(f"[JETSON SYNC] Detectado JetsonConfigMqttPayload: {payload_str[:200]}")
          try:
            # retain=True: el broker guarda la ultima version de cada topic y
            # la entrega automaticamente a futuros subscribers. Asi la app al
            # lanzarse tiene el estado actual sin tener que preguntarle a nadie.
            # SOLO por dongle: "jetson_config/global" reconfiguraba la Jetson
            # de TODA la flota y la ACL del broker no puede distinguir
            # destinatarios dentro de un topic sin dongle en la ruta.
            result = self.mqttc.publish(f"telemetry_config/{self.DongleID}/jetson_config", payload_str, qos=0, retain=True)
            print(f"[JETSON SYNC] Publicado (retained) a telemetry_config/{self.DongleID}/jetson_config rc={result.rc}")
          except Exception as e:
            print(f"[JETSON SYNC] ERROR publicando MQTT: {e}")
          self.params.remove("JetsonConfigMqttPayload")
          print("[JETSON SYNC] Param JetsonConfigMqttPayload eliminado")
      except Exception as e:
        print(f"[JETSON SYNC] ERROR leyendo param: {e}")

      # Publicar SteerTorqueMode si fue cambiado desde la UI del Comma
      try:
        steer_mode_payload = self.params.get("SteerTorqueModeMqttPayload")
        if steer_mode_payload and len(steer_mode_payload) > 2:
          payload_str = steer_mode_payload
          print(f"[STEER MODE SYNC] Detectado payload: {payload_str[:200]}")
          try:
            # retain=True: ver comentario arriba en JetsonConfig.
            # SOLO por dongle (ver JetsonConfig): el topic global cambiaba el
            # modo de torque del volante de TODOS los vehiculos a la vez.
            result = self.mqttc.publish(f"telemetry_config/{self.DongleID}/steer_torque_mode", payload_str, qos=0, retain=True)
            print(f"[STEER MODE SYNC] Publicado (retained) a telemetry_config/{self.DongleID}/steer_torque_mode rc={result.rc}")
          except Exception as e:
            print(f"[STEER MODE SYNC] ERROR publicando MQTT: {e}")
          self.params.remove("SteerTorqueModeMqttPayload")
      except Exception as e:
        print(f"[STEER MODE SYNC] ERROR leyendo param: {e}")

      # Publicar JetsonObstacleApplyTarget si fue cambiado desde la UI del Comma
      try:
        apply_target_payload = self.params.get("JetsonObstacleApplyTargetMqttPayload")
        if apply_target_payload and len(apply_target_payload) > 2:
          payload_str = apply_target_payload
          print(f"[APPLY TARGET SYNC] Detectado payload: {payload_str[:200]}")
          try:
            # retain=True: que la app reciba el estado al reconectarse.
            result1 = self.mqttc.publish(f"telemetry_config/{self.DongleID}/jetson_apply_target", payload_str, qos=0, retain=True)
            print(f"[APPLY TARGET SYNC] Publicado (retained) a telemetry_config/{self.DongleID}/jetson_apply_target rc={result1.rc}")
          except Exception as e:
            print(f"[APPLY TARGET SYNC] ERROR publicando MQTT: {e}")
          self.params.remove("JetsonObstacleApplyTargetMqttPayload")
      except Exception as e:
        print(f"[APPLY TARGET SYNC] ERROR leyendo param: {e}")

      # Publicar JetsonObstacleStatus (modo 3 COMMA+JETSON) si cambió en controlsd
      try:
        obstacle_payload = self.params.get("JetsonObstacleStatusMqttPayload")
        if obstacle_payload and len(obstacle_payload) > 2:
          payload_str = obstacle_payload
          # controlsd no conoce el dongle: inyectarlo aqui para que el backend
          # sepa de que vehiculo es el estado (antes registraba "GLOBAL" como
          # dispositivo y la app derivaba deviceId="global"). Best-effort.
          try:
            _obs = json.loads(payload_str)
            if isinstance(_obs, dict) and "dongle_id" not in _obs:
              _obs["dongle_id"] = self.DongleID
              payload_str = json.dumps(_obs)
          except Exception:
            pass
          print(f"[OBSTACLE STATUS SYNC] Detectado payload: {payload_str[:200]}")
          try:
            # retain=False aquí: el status del esquive es transitorio, no
            # queremos que un suscriptor que se conecte tarde reciba un
            # "DODGING_RIGHT" de hace 10 minutos como si estuviera vivo.
            # SOLO por dongle (ver JetsonConfig): el topic global mezclaba el
            # estado de esquive de toda la flota en un unico canal sin dueno.
            result = self.mqttc.publish(f"telemetry_config/{self.DongleID}/jetson_obstacle_status", payload_str, qos=0, retain=False)
            print(f"[OBSTACLE STATUS SYNC] Publicado a telemetry_config/{self.DongleID}/jetson_obstacle_status rc={result.rc}")
          except Exception as e:
            print(f"[OBSTACLE STATUS SYNC] ERROR publicando MQTT: {e}")

          # Puente sicuem_torque: la app + backend consumen "sicuem_torque/<dongle>"
          # pero el firmware nunca lo publicaba. Lo emitimos aqui, a la misma cadencia
          # que el status de obstaculo (cuando controlsd publica un cambio). 'torque'
          # sale de la mejor fuente numerica disponible (param JetsonTorque, escrito por
          # zmq_client desde la Jetson); 'active'/'obstacle_detected' se derivan del status
          # de esquive (DODGING_*/BSM_* => detectado; "" => idle). Best-effort: cualquier
          # fallo se ignora y NUNCA rompe el loop.
          try:
            status = ""
            try:
              status = (json.loads(payload_str) or {}).get("status", "") or ""
            except Exception:
              status = ""
            obstacle_detected = bool(status)
            try:
              torque = float(self.params.get("JetsonTorque") or 0.0)
            except Exception:
              torque = 0.0
            active = obstacle_detected or (torque != 0.0)
            torque_payload = {
              "torque": torque,
              "active": active,
              "obstacle_detected": obstacle_detected,
              "dongle_id": self.DongleID,
            }
            self.mqttc.publish(f"sicuem_torque/{self.DongleID}", json.dumps(torque_payload), qos=0)
          except Exception as e:
            print(f"[SICUEM TORQUE SYNC] ERROR publicando MQTT: {e}")

          self.params.remove("JetsonObstacleStatusMqttPayload")
      except Exception as e:
        print(f"[OBSTACLE STATUS SYNC] ERROR leyendo param: {e}")

      privacidad = self._privacidad_silenciada()

      for canal in self.enabled_items:
        nombre = canal["canal"]
        # El interruptor maestro de privacidad tapa tambien el camino LEGACY: estos dos
        # canales llevan latitude/longitude en el payload y hasta ahora salian igual con
        # OrbitPrivacyMute puesto, con lo que apagar la posicion en el panel apagaba el
        # canal v2 `pos` y la camara pero no el v1. El resto de canales v1 sigue saliendo.
        if privacidad and nombre in CANALES_V1_POSICION:
          continue
        topic = canal["topic"].format(self.DongleID)

        # `sm.updated` solo dice si el mensaje llego EN ESTE tick. Con el tick base a 4 Hz
        # y la publicacion v1 a 1 Hz, la mayoria de las llegadas caen en ticks que no
        # publican y el canal se habria quedado mudo. `recv_frame` responde la pregunta
        # que de verdad importa: ha llegado algo NUEVO desde la ultima vez que publique
        # ESTE canal.
        frame = self.sm.recv_frame.get(nombre, 0)
        if nombre in self.sm.data and frame > self._v1_frame.get(nombre, -1):
          self._v1_frame[nombre] = frame
          datos = self.sm[nombre].to_dict()
          datos_filtrados = self.enviar_datos_importantes(nombre, datos)
          if datos_filtrados:
            # Verificar conexión nuevamente antes de cada publicación
            if self.conectado:
              # Verificar también el estado real si está disponible
              if hasattr(self.mqttc, 'is_connected') and not self.mqttc.is_connected():
                continue
              try:
                # Saneado + allow_nan=False: json.dumps emite por defecto el
                # literal NaN, que NO es JSON valido (RFC 8259), asi que un
                # solo desiredCurvature NaN corrompia el mensaje ENTERO de
                # forma intermitente y sin rastro (el publish salia con rc=0).
                # compacta_floats recorta la expansion DOBLE que json.dumps escribe de
                # cada Float32 ("-3.4567890167236328" por un angulo de volante) a las 9
                # cifras significativas con las que un binary32 va y vuelve exacto: el
                # consumidor v1 no puede notar la diferencia y el mensaje encoge ~40 %.
                cuerpo = json.dumps(tel2.compacta_floats(_sanea_no_finitos(datos_filtrados)), allow_nan=False)
              except (ValueError, TypeError):
                malos = self._campos_no_serializables(datos_filtrados)
                cloudlog.warning(f"[Bemposta] canal {nombre}: campos no serializables {malos}; mensaje descartado")
                continue
              try:
                info = self.mqttc.publish(topic, cuerpo, qos=0)
                # rc != 0 (tipico: MQTT_ERR_NO_CONN) significa que el mensaje no
                # salio. Antes se ignoraba y la telemetria parecia estar fluyendo.
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                  self._log_publish_rc(f"canal {nombre}", topic, info.rc)
              except Exception as e:
                # NO tocar self.conectado aqui: un fallo de serializacion
                # (json.dumps de un to_dict() con bytes/NaN, etc.) o un error
                # puntual de un canal NO significa que el socket MQTT este caido.
                # Si lo poniamos a False, como on_disconnect nunca disparaba, la
                # telemetria quedaba CONGELADA para siempre. Dejar que las callbacks
                # (on_disconnect) gestionen el estado real de la conexion.
                cloudlog.warning(f"[Bemposta] fallo publicando canal {nombre} en {topic}: {e}")
            # Si no hay conexión, simplemente no enviar (no encolar)

      # LATIDO DE PRESENCIA. Antes esto republicaba el `carState` ENTERO cada 3 s en
      # telemetry_mqtt/<dongle>/carState -- el topic de un canal de DATOS -- solo para que
      # la app, que deduce "conectado" de que le llegue telemetria en <10 s, no pintara el
      # coche desconectado con el vehiculo parado (offroad ningun canal se actualiza).
      # Costaba 1454 B x 1200 mensajes/h = 1,74 MB/h por decir "sigo vivo", y ademas
      # mentia: con carState_toggle desactivado publicaba {"dongle_id": ...} a secas en un
      # topic que la app modela como un carState, y pintaba el coche parado y sano.
      # Ahora el latido es la PRESENCIA de verdad (~180 B: 0,22 MB/h), retenida y en los
      # dos namespaces, y va al MISMO topic v1 que el Last Will, asi que un 'online' vivo
      # sobreescribe el 'offline' que dejo el broker al morir la conexion anterior.
      now = time.monotonic()
      if self.conectado and (now - self._last_heartbeat) >= self.HEARTBEAT_SECS:
        self._last_heartbeat = now
        if not (hasattr(self.mqttc, 'is_connected') and not self.mqttc.is_connected()):
          try:
            if self._publish_presence_online():
              # Marca de vida para la UI: epoch (s) del ultimo ciclo de publicacion.
              # Ligada a la cadencia del latido (3 s) para no anadir mas frecuencia de
              # escritura en Params. Va DENTRO del try y solo si el publish salio de
              # verdad (rc==0): antes estaba fuera, asi que orbit_panel.py y home.py
              # pintaban el enlace vivo aunque todos los publishes reventaran.
              #
              # OJO: epoch de PARED, NO `now` (que es monotonic y solo sirve para el
              # intervalo del latido). Este valor CRUZA PROCESOS: lo leen
              # orbit_panel.py:50 y home.py:664 restando contra time.time(). Escribir
              # monotonic aqui hacia que la UI calculase ~57 anos de antiguedad y
              # dejaba el chip de enlace permanentemente en rojo.
              self.params.put("OrbitLastPublish", str(_epoch_ms() // 1000))
          except Exception as e:
            cloudlog.warning(f"[Bemposta] latido de presencia fallo: {e}")

          # sicuem_torque a cadencia baja (unida al heartbeat). El backend/app
          # consumen sicuem_torque/<dongle> de forma CONTINUA, pero antes solo se
          # publicaba dentro del bloque JetsonObstacleStatusMqttPayload (transitorio,
          # solo al cambiar el status) -> la app veia el torque congelado. Aqui lo
          # republicamos a ritmo bajo SIEMPRE que haya un modo de torque Jetson/
          # obstaculo activo (SteerTorqueMode 1=JETSON, 3=COMMA+JETSON), leyendo el
          # ultimo torque del param JetsonTorque (escrito por zmq_client). No se
          # incluye 'confidence' porque no existe ninguna fuente para el en este
          # build (se omite; el backend ya lo trata como opcional). Best-effort:
          # cualquier fallo se ignora y NUNCA rompe el loop de telemetria.
          try:
            try:
              mode_raw = self.params.get("SteerTorqueMode")
              mode = int(mode_raw) if mode_raw else 0
            except (ValueError, TypeError):
              mode = 0
            if mode in (1, 3):
              try:
                torque = float(self.params.get("JetsonTorque") or 0.0)
              except Exception:
                torque = 0.0
              torque_payload = {
                "torque": torque,
                "active": True,
                "obstacle_detected": torque != 0.0,
                "dongle_id": self.DongleID,
              }
              self.mqttc.publish(f"sicuem_torque/{self.DongleID}", json.dumps(torque_payload), qos=0)
          except Exception as e:
            cloudlog.warning(f"[Bemposta] sicuem_torque (heartbeat) fallo: {e}")

  # ------------------------------------------------------------------ telemetria v2

  _PRIVACY_TTL_S = 1.0
  PARAM_PERFIL = "OrbitTelemetryProfile"

  def _privacidad_silenciada(self) -> bool:
    """Interruptor maestro LOCAL de privacidad (OrbitPrivacyMute).

    Mismo patron y mismo TTL que CameraSender._privacidad_silenciada: tiene que hacer
    efecto EN CALIENTE (si hubiera que reiniciar, el interruptor no serviria de nada justo
    cuando hace falta) sin abrir el param en cada tick. El diseno (seccion 9) declara
    innegociable que "dejar de emitir" cubra posicion Y camara, y hasta ahora la camara lo
    respetaba y la telemetria de posicion solo a medias.

    QUE TAPA, todo lo que dice DONDE ESTA el coche y nada mas:
      - el canal v2 `pos` (gpsLocation / gpsLocationExternal), en _fuentes_v2;
      - el canal v2 `road`, que se alimenta de liveMapDataSP y publica el NOMBRE DE LA
        VIA -- posicion derivada, pero posicion -- tambien en _fuentes_v2;
      - los canales LEGACY v1 gpsLocation y gpsLocationExternal (CANALES_V1_POSICION), que
        llevan latitude/longitude crudas y hasta ahora salian igual con el mute puesto.
    El resto de la telemetria sigue: esto no es un interruptor de "apagar el coche".
    """
    ahora = time.monotonic()
    if ahora - self._privacy_ts >= self._PRIVACY_TTL_S:
      self._privacy_ts = ahora
      try:
        self._privacy_cache = bool(self.params.get_bool("OrbitPrivacyMute"))
      except Exception:
        # Clave no registrada o disco: NO se silencia por error, pero se avisa una vez.
        if not self._privacy_avisado:
          self._privacy_avisado = True
          cloudlog.exception("[Bemposta] no se pudo leer OrbitPrivacyMute")
        self._privacy_cache = False
    return self._privacy_cache

  def _maybe_reload_perfil(self, ahora):
    """Relee el perfil de telemetria pedido (AHORRO / NORMAL / DIAGNOSTICO, seccion 7).

    La clave `OrbitTelemetryProfile` TODAVIA NO ESTA REGISTRADA en common/params_keys.h
    (fichero de otro agente en esta ronda), asi que hoy la lectura levanta UnknownKeyName y
    se cae al defecto NORMAL. La plomeria queda escrita y funciona el dia que se registre,
    igual que se hizo con OrbitPrivacyMute. El aviso sale UNA vez, no a 0,2 Hz.

    La degradacion por red de pago y la caducidad de los 15 minutos del diagnostico NO
    dependen de este param: viven dentro del motor, que es quien tiene el reloj.
    """
    if (ahora - self._last_perfil_check) < self.PERFIL_RELOAD_SECS:
      return
    self._last_perfil_check = ahora
    try:
      pedido = self.params.get(self.PARAM_PERFIL)
    except Exception:
      if not self._perfil_avisado:
        self._perfil_avisado = True
        cloudlog.warning(f"[Bemposta] {self.PARAM_PERFIL} no disponible; perfil de telemetria = normal")
      pedido = None
    if pedido and tel2.perfil_valido(pedido) and pedido != self.motor_v2.perfil_pedido:
      cloudlog.warning(f"[Bemposta] perfil de telemetria {self.motor_v2.perfil_pedido} -> {pedido}")
      self.motor_v2.pedir_perfil(pedido, ahora)
    if self.motor_v2.diag_expirado:
      # El motor ya volvio a NORMAL por su cuenta; aqui solo se refleja en Params para que
      # la UI no siga diciendo "diagnostico". put() exige tipo NATIVO: la clave es STRING.
      self.motor_v2.diag_expirado = False
      cloudlog.warning("[Bemposta] perfil diagnostico caducado a los 15 min, vuelta a normal")
      try:
        self.params.put(self.PARAM_PERFIL, tel2.PERFIL_NORMAL)
      except Exception:
        pass

  def _fuentes_v2(self) -> dict:
    """Lectores cereal VIVOS para el motor de telemetria.

    Un servicio que aun no ha llegado no se pasa: el SubMaster entrega en su lugar el
    mensaje CERO que construyo al arrancar (todo a valor por defecto), y publicarlo seria
    inventar un coche parado, frio y sano que no existe. `alive` es exactamente esa
    pregunta (recibido dentro de 10 periodos) y arranca en False porque recv_time es 0.
    """
    fuentes = {}
    for nombre in self.servicios_v2:
      try:
        if not self.sm.alive.get(nombre, False):
          continue
        fuentes[nombre] = self.sm[nombre]
      except Exception:
        continue
    if self._privacidad_silenciada():
      # Interruptor de privacidad: fuera TODA fuente que diga donde esta el coche. El
      # resto de la telemetria sigue (no es un interruptor de "apagar el coche", es de
      # "no emitir donde estoy") y los canales afectados se quedan sin fuente:
      #
      #   - gpsLocation / gpsLocationExternal alimentan el canal `pos` (lat/lon crudas);
      #   - liveMapDataSP alimenta el canal `road`, que NO es menos posicion por ser
      #     derivada: publica `road_name` -- el nombre de la calle por la que se va -- y
      #     el limite vigente, el proximo y la distancia a el. Ese canal es on-change, asi
      #     que la secuencia de nombres de via reconstruye el recorrido igual de bien que
      #     la traza. Antes solo se retiraban las dos fuentes GPS y el coche seguia
      #     diciendo por donde iba con el interruptor puesto.
      #
      # `pos` se queda ademas sin red de reenvio a proposito: el spool excluye ese canal
      # (spool.CANALES_NO_SPOOLEADOS), asi que una posicion no publicada no queda escrita
      # en disco esperando a que vuelva la cobertura.
      #
      # `road` SI se spoolea mientras el interruptor esta quitado, que es lo correcto (es
      # telemetria util) y por eso este filtro NO basta: lo encolado ANTES de pulsar el
      # interruptor sigue en la cola. De eso se encarga _mantener_spool, que pasa el mute
      # al drenaje y purga lo encolado que revele posicion.
      fuentes.pop("gpsLocationExternal", None)
      fuentes.pop("gpsLocation", None)
      fuentes.pop("liveMapDataSP", None)
    return fuentes

  # -------------------------------------------------------- configuracion deseada v2

  def _cfg_ruta_jetson(self) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_jetson.json")

  def _cfg_cargar_persistido(self) -> None:
    """Relee de Params los dos ultimos sobres. Una sola vez, en el hilo del loop.

    Sirve para que nada RETROCEDA tras un reinicio: ni la version -- un `desired` viejo
    retenido, que el broker re-entrega en cada reconexion, no puede ganarle al estado
    actual -- ni los rechazos, que si se perdieran dejarian al backend adoptando un
    `reported` limpio sobre un ajuste que nunca se aplico. Los VALORES ya estan donde
    tienen que estar (Params, config_jetson.json): aqui solo se recupera el veredicto.
    """
    if self._cfg_cargado:
      return
    self._cfg_cargado = True
    for clave, destino in (("OrbitConfigDesired", "_cfg_deseado"), ("OrbitConfigReported", "_cfg_reportado")):
      try:
        crudo = self.params.get(clave)
      except Exception:
        # Clave todavia no registrada en common/params_keys.h del binario instalado.
        crudo = None
      if crudo:
        # remoto=False: este sobre lo escribio este proceso, y el `reported` lleva
        # source=comma_ui, que parse_sobre rechaza cuando viene del cable.
        sobre = cfg2.parse_sobre(crudo, remoto=False)
        if sobre is not None:
          setattr(self, destino, sobre)

    # Los del CONTRATO se recalculan del propio `desired`: son funcion pura de el, asi que
    # no hace falta creerselos de nadie. Los de APLICACION no se pueden recalcular (la
    # causa estaba en el destino, no en el sobre) y por eso se recuperan del ultimo
    # `reported`, que es donde quedaron escritos.
    rechazos = {}
    if self._cfg_deseado is not None:
      contrato = cfg2.valida(self._cfg_deseado.values)[1]
      if self._cfg_reportado is not None and self._cfg_reportado.version >= self._cfg_deseado.version:
        # PERO no resucitan los que ya se podaron. Un `reported` con version >= la del
        # `desired` es POSTERIOR a el (_mantener_config nunca construye uno por debajo),
        # asi que sus rechazos son el veredicto ya podado. Sin este filtro, un rechazo de
        # rango que un cambio local mato volvia a la vida en el siguiente arranque y dejaba
        # esa clave otra vez vetada para el backend, que no adopta lo que sale rechazado.
        contrato = {n: m for n, m in contrato.items() if n in self._cfg_reportado.rechazos}
      rechazos.update(contrato)
    if self._cfg_reportado is not None:
      rechazos.update({n: m for n, m in self._cfg_reportado.rechazos.items()
                       if m in cfg2.MOTIVOS_DE_APLICACION})
    self._cfg_rechazos = rechazos

  def _cfg_guardar(self, clave: str, sobre) -> None:
    try:
      self.params.put(clave, sobre.a_json())
    except Exception:
      cloudlog.exception(f"[Bemposta] no se pudo persistir {clave}")

  def _cfg_valores_actuales(self) -> dict:
    """(valores, invalidos): estado REAL de cada clave, leido de su destino. TOCA DISCO.

    Es lo que hace que esto sea una reconciliacion y no otro buzon: `reported` no repite lo
    que se mando, dice lo que hay. Una clave cuyo destino no se puede leer NO sale en el
    sobre -- ausente es "no lo se", que es distinto de un valor inventado.
    """
    jetson = cfg2.leer_config_jetson(self._cfg_ruta_jetson())
    cam = getattr(self, "camera_sender", None)
    valores = {}
    for nombre, clave in cfg2.CLAVES.items():
      try:
        if clave.destino == cfg2.DEST_JETSON:
          if clave.dest_nombre in jetson:
            valores[nombre] = jetson[clave.dest_nombre]
        elif clave.destino == cfg2.DEST_PARAM:
          valor = self.params.get(clave.dest_nombre)
          if valor is not None:
            valores[nombre] = valor
        elif clave.destino == cfg2.DEST_CAMARA and cam is not None:
          if clave.dest_nombre == "image_sending_enabled":
            valores[nombre] = bool(cam.sending_enabled)
          elif clave.dest_nombre == "send_frequency_seconds":
            valores[nombre] = int(cam.interval_seconds)
          elif clave.dest_nombre == "camera_type":
            valores[nombre] = str(cam.camera_type)
      except Exception:
        continue   # clave no registrada, disco, o un CameraSender a medio arrancar
    # Se pasa por el validador para que `reported` no publique un valor que el propio
    # contrato rechazaria: un jetson_ip publico escrito por la UI o heredado de una version
    # anterior tiene que verse como lo que es, no colarse por la puerta de atras.
    limpios, sucios = cfg2.valida({k: v for k, v in valores.items() if k in cfg2.CLAVES_ESCRIBIBLES})
    for nombre in cfg2.CLAVES_SOLO_REPORTE:
      if nombre in valores:
        limpios[nombre] = valores[nombre]
    return limpios, sucios

  def _cfg_aplicar(self, sobre) -> dict:
    """Aplica un sobre `desired` YA GANADOR. Devuelve {clave: motivo} de lo no aplicado.

    Las claves del resultado son SIEMPRE las del CONTRATO, nunca las del destino: el plan
    esta indexado por destino (`OrbitTelemetryProfile`, `image_sending_enabled`) y un
    rechazo con ese nombre no lo reconoce ni la app -- que resuelve cada clave contra el
    vocabulario del contrato y tira lo que no reconoce -- ni el backend, que lo cruza
    contra `desired.values`. De ahi cfg2.clave_de_destino.

    TOCA DISCO (Params, config_jetson.json con flock, CameraSender): hilo del loop.
    """
    aceptados, rechazos = cfg2.valida(sobre.values)
    plan = cfg2.plan_aplicacion(aceptados)

    for clave_param, valor in plan.params.items():
      try:
        # put() EXIGE el tipo nativo del param: plan_aplicacion ya lo convierte.
        self.params.put(clave_param, valor)
      except Exception:
        cloudlog.exception(f"[Bemposta] cfg: no se pudo escribir el param {clave_param}")
        rechazos[cfg2.clave_de_destino(cfg2.DEST_PARAM, clave_param)] = cfg2.MOT_NO_APLICADO

    if plan.jetson:
      try:
        cambio, _final = cfg2.escribir_config_jetson(self._cfg_ruta_jetson(), plan.jetson)
        if cambio:
          # Mismo mecanismo que usaba el topic viejo: el CameraSender corre en su propio
          # hilo y recarga solo en su siguiente vuelta. Recargarlo desde aqui cerraria un
          # socket ZMQ que otro hilo puede estar usando.
          self.params.put_bool("JetsonConfigChanged", True)
      except Exception:
        cloudlog.exception("[Bemposta] cfg: no se pudo escribir config_jetson.json")
        for nombre in plan.jetson:
          rechazos[cfg2.clave_de_destino(cfg2.DEST_JETSON, nombre)] = cfg2.MOT_NO_APLICADO

    if plan.camara:
      cam = getattr(self, "camera_sender", None)
      if cam is None:
        for nombre in plan.camara:
          rechazos[cfg2.clave_de_destino(cfg2.DEST_CAMARA, nombre)] = cfg2.MOT_SIN_CAMARA
      else:
        try:
          # apply_config tiene sus propios filtros y el interruptor LOCAL de privacidad
          # gana siempre dentro de el: una peticion remota de encender la camara con el
          # mute puesto se ignora ahi, no aqui.
          cam.apply_config(plan.camara)
        except Exception:
          cloudlog.exception("[Bemposta] cfg: apply_config de camara fallo")
          for nombre in plan.camara:
            rechazos[cfg2.clave_de_destino(cfg2.DEST_CAMARA, nombre)] = cfg2.MOT_NO_APLICADO
    return rechazos

  def _cfg_rechazos_vivos(self, valores: dict) -> dict:
    """Los rechazos del ultimo `desired` cuya causa SIGUE en pie. RAM pura.

    Un rechazo que se borra solo es peor que no tenerlo: el `reported` siguiente sale
    limpio, el backend -- que adopta el documento entero en cuanto la version sube por
    encima de la deseada -- se queda sin el ajuste que pidio, y la app pinta "Al dia"
    sobre algo que no se aplico nunca. Asi que no se reconstruyen en cada tick, se PODAN:

      - los de APLICACION (no_aplicado, sin_camera_sender) tienen la causa en el DESTINO,
        y esa puede morir sola: si el valor pedido acaba puesto -- porque aparecio el
        CameraSender, porque la eMMC dejo de fallar, porque alguien lo puso a mano en la
        pantalla -- el rechazo desaparece o la app se queda pintando un error que ya no
        existe.
      - los de VALOR (tipo, rango, valor) tienen la causa dentro del sobre `desired`, pero
        NO por eso son eternos: "8.8.8.8 esta fuera de rango" es un veredicto sobre esa
        clave, y deja de ser la ultima palabra del coche en cuanto alguien la corrige EN
        LOCAL. Sin esta mitad el rechazo salia VIVO en el mismo `reported` que ya llevaba
        el valor bueno, y como el backend no adopta las claves rechazadas, la correccion
        hecha delante del coche no llegaba nunca -- que es justo lo que la regla del empate
        de la seccion 8 existe para garantizar.
      - los de CLAVE (solo_reporte, clave_desconocida) no hablan del valor: no hay cambio
        local que los pueda corregir (`steer_mode` no se escribe por aqui aunque se pida
        con el valor perfecto) y viven hasta que llegue otro `desired`.

    La poda es DEFINITIVA -- se sacan de `_cfg_rechazos`, no se filtran al vuelo -- porque
    si no reviven: en cuanto el `reported` nuevo lleva el valor corregido, la comparacion
    contra el `reported` anterior vuelve a dar "igual" y el rechazo reaparecia en el tick
    siguiente. Un rechazo intermitente es peor que no tenerlo.

    Se comparan los valores ya NORMALIZADOS por el contrato contra `valores`, que es lo
    que `_cfg_valores_actuales` acaba de leer del destino: un `jetson_ip` con espacios o un
    `speed_increment_kph` que llego como int no pueden contar como divergencia.
    """
    if not self._cfg_rechazos:
      return {}
    pedidos = cfg2.valida(self._cfg_deseado.values)[0] if self._cfg_deseado is not None else {}
    # Lo ULTIMO que el coche dijo de cada clave. Es la referencia de "no ha cambiado desde
    # que rechace": el rechazo se emitio junto a ese valor.
    ultimo = self._cfg_reportado.values if self._cfg_reportado is not None else None
    muertos = []
    for nombre, motivo in self._cfg_rechazos.items():
      if motivo in cfg2.MOTIVOS_DE_APLICACION:
        if nombre in pedidos and nombre in valores and valores[nombre] == pedidos[nombre]:
          muertos.append(nombre)
      elif motivo in cfg2.MOTIVOS_DE_VALOR:
        if ultimo is not None and nombre in ultimo and nombre in valores \
           and valores[nombre] != ultimo[nombre]:
          muertos.append(nombre)
    for nombre in muertos:
      self._cfg_rechazos.pop(nombre, None)
    return dict(self._cfg_rechazos)

  def _cfg_publicar(self, sobre) -> bool:
    """Publica `reported` retenido con qos 1. Devuelve si salio."""
    topic = cfg2.TOPIC_CFG_REPORTED.format(self.DongleID)
    try:
      info = self.mqttc.publish(topic, sobre.a_json(), qos=1, retain=True)
    except Exception as e:
      cloudlog.warning(f"[Bemposta] cfg/reported en {topic} fallo: {e}")
      return False
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
      self._log_publish_rc("cfg/reported", topic, info.rc)
      return False
    return True

  def _mantener_config(self, ahora):
    """Reconciliacion de configuracion deseada/reportada (seccion 8). A CFG_SECS.

    El problema que cierra: hoy la misma configuracion vive en Params, en las tablas del
    backend y en las SharedPreferences del movil, sin reconciliacion ninguna. Aqui hay un
    solo contrato: gana la version mas alta y el empate lo gana `comma_ui`, para que quien
    esta delante del coche pueda corregir sin pedirle permiso a una red que puede no
    existir.

    `reported` lleva SIEMPRE source=comma_ui porque es la palabra del coche sobre lo que
    hay, no un acuse del backend. Un `desired` que llegue con la misma version ya no vuelve
    a aplicarse (gana() lo dice) y por eso el retenido re-entregado en cada reconexion no
    hace nada.

    TOCA DISCO. Va en el hilo del loop, nunca en el callback de paho.
    """
    if not self.dongle_valido:
      return
    self._cfg_cargar_persistido()

    # 1) lo que haya dejado el hilo de red. Se atiende SIEMPRE, aunque no toque
    #    reconstruir `reported`: un ajuste pedido no puede esperar 5 s por el reloj.
    entrante = cfg2.get_bus().tomar()
    aplicado = False
    if entrante is not None:
      # Contra LOS DOS sobres locales, no solo contra el `desired`. El `desired` es siempre
      # source backend, asi que compararlo solo contra el dejaba la regla del empate sin
      # efecto ninguno: quien lleva el comma_ui es el `reported`, y es el que representa lo
      # que hay AHORA en el coche. Ver cfg2.gana_a_todos.
      if cfg2.gana_a_todos(entrante, self._cfg_deseado, self._cfg_reportado):
        self._cfg_rechazos = self._cfg_aplicar(entrante)
        self._cfg_deseado = entrante
        self._cfg_guardar("OrbitConfigDesired", entrante)
        aplicado = True
        cloudlog.warning(f"[Bemposta] cfg/desired v{entrante.version} de {entrante.source} aplicado; rechazos={self._cfg_rechazos}")
      else:
        cloudlog.warning(f"[Bemposta] cfg/desired v{entrante.version} ignorado: no gana al estado local")

    if not aplicado and (ahora - self._last_cfg) < self.CFG_SECS:
      return
    self._last_cfg = ahora

    # 2) que hay AHORA de verdad. Un cambio hecho en la pantalla del comma se ve aqui como
    #    diferencia y sube de version con source comma_ui, que es lo que sustituye a los
    #    params-buzon: la UI escribe su param y esto lo reporta, sin un JSON a mano.
    valores, invalidos = self._cfg_valores_actuales()
    # Los del ultimo `desired` que siguen siendo verdad, no los del tick que aplico: un
    # ajuste rechazado no puede desaparecer del `reported` sin que su causa haya muerto.
    rechazos = self._cfg_rechazos_vivos(valores)
    rechazos.update(invalidos)

    previo = self._cfg_reportado
    if previo is None or previo.values != valores or previo.rechazos != rechazos:
      version = entrante.version if (aplicado and entrante is not None) else None
      if version is None or (previo is not None and version <= previo.version):
        # Cambio local, o un `desired` cuya version no supera a la ultima reportada: sube.
        version = cfg2.siguiente_version(previo, self._cfg_deseado)
      sobre = cfg2.sobre_local(valores, rechazos, version=version)

      # EL REPORTED AVANZA AL APLICARSE, NO AL PUBLICARSE. Son dos cosas distintas y
      # atarlas rompia la seccion 8 justo cuando mas hace falta: sin cobertura el sobre no
      # salia, `_cfg_reportado` se quedaba como estaba, y al volver la red el `desired`
      # retenido -- que el broker re-entrega en CADA reconexion -- empataba contra un
      # reported viejo y deshacia el ajuste hecho en la pantalla. `gana_a_todos` no tenia
      # con que defenderlo. Y el empate a favor de comma_ui existe precisamente para el
      # coche SIN RED, que es el unico lado que puede tener a alguien delante.
      #
      # Se persiste tambien sin enlace, por lo mismo: la palabra del coche sobre lo que HAY
      # tiene que sobrevivir a un reinicio, no a un publish.
      self._cfg_reportado = sobre
      self._cfg_reportado_publicado = False
      self._cfg_guardar("OrbitConfigReported", sobre)
    elif self._cfg_reportado_publicado:
      return           # ni cambio ni deuda: no se republica un retenido identico

    # Lo que si depende del publish es la DEUDA con el broker: mientras el retenido no
    # tenga el ultimo `reported`, se reintenta cada CFG_SECS con el MISMO sobre (misma
    # version, mismo contenido), que es lo que el retenido tiene que acabar teniendo.
    if self.conectado and self._cfg_publicar(self._cfg_reportado):
      self._cfg_reportado_publicado = True

  # ------------------------------------------------------------------ spool diferido

  def _spool(self):
    """Instancia unica del spool, abierta en el PRIMER uso desde el hilo del loop.

    Devuelve None si no se pudo abrir. get_spool() no deberia lanzar (Spool se desactiva
    solo ante disco lleno o base corrupta), pero si lanzara, el fallo se recuerda para no
    reintentar la apertura a 4 Hz.
    """
    if self._spool_obj is None and not self._spool_roto:
      try:
        self._spool_obj = spool_mod.get_spool()
      except Exception:
        self._spool_roto = True
        cloudlog.exception("[Bemposta] no se pudo abrir el spool de telemetria")
    return self._spool_obj

  def _diferir(self, canal, payload):
    """La muestra NO salio por el cable: se guarda para reenviarla, o se deshace el sello.

    Dos redes, en este orden:

      1. el SPOOL (orbit/spool.py). Guarda en RAM (no toca disco, no bloquea) y el
         mantenimiento de 1 Hz lo vuelca y lo reenvia marcado como backfill. Es la unica
         red que tienen `event` y `trip`, que son mensajes UNICOS: un rc!=0 los borraba de
         la historia (el resumen de viaje entero incluido).
      2. si el spool no la acepta -- desactivado, canal excluido (`pos`), o lleno de
         criticos -- se REVIERTE el sello del canal on-change para que el motor vuelva a
         proponer el estado en su siguiente periodo. Sin esto un cambio de estado perdido
         no se reintentaba hasta el keepalive.

    Que el spool la acepte NO garantiza que salga: la fila puede morir despues (eviccion
    de RAM, lote rechazado con el disco al tope, poda, apagado en caliente). Esos caminos
    NO se ven desde aqui -- guardar() ya dijo True -- y los cierra _revertir_perdidas
    desde el mantenimiento, con lo que el spool anota en canales_perdidos().
    """
    guardada = False
    s = self._spool()
    if s is not None:
      try:
        guardada = bool(s.guardar(canal, payload, dongle=self.DongleID))
      except Exception:
        cloudlog.exception(f"[Bemposta] spool.guardar({canal}) fallo")
    if not guardada:
      self.motor_v2.revertir(canal)

  def _revertir_perdidas(self, s) -> None:
    """Deshace los sellos de lo que el spool ACEPTO y luego no pudo conservar.

    `guardar()` devolviendo True solo dice que la fila entro en la cola, no que vaya a
    salir por el cable, y _diferir la trata como si lo fuera: solo revierte cuando el
    spool la rechaza EN EL ACTO. Despues hay cuatro caminos que tiran una fila ya aceptada
    -- eviccion de RAM, rechazo del lote con el disco al tope, poda del disco y apagado en
    caliente del spool -- y por todos ellos el sello se quedaba puesto: el canal daba el
    estado por entregado y se callaba hasta el keepalive (`openpilot` 60 s en NORMAL
    pintando "actuando" un coche ya desenganchado; `road` NUNCA en AHORRO, que no tiene
    keepalive). El spool anota esas perdidas por canal y aqui se vacian.

    Se hace en el mantenimiento y no en el tick a proposito: la perdida se descubre cuando
    se vuelca o se poda, que es despues del tick que sello. Por eso no vale `revertir`
    (solo deshace lo sellado en el tick en curso) sino `invalidar_sello`.
    """
    try:
      perdidos = s.canales_perdidos()
    except Exception:
      cloudlog.exception("[Bemposta] no se pudo leer las perdidas del spool")
      return
    for canal, cuantas in perdidos.items():
      if self.motor_v2.invalidar_sello(canal):
        cloudlog.warning(f"[Bemposta] {cuantas} muestra(s) de {canal} perdidas: sello invalidado")
      elif canal in spool_mod.CANALES_CRITICOS:
        # `event` y `trip` son mensajes UNICOS: no hay sello que deshacer ni forma de
        # recuperarlos. Lo unico que se puede hacer es que no sea silencioso.
        cloudlog.error(f"[Bemposta] {cuantas} muestra(s) criticas de {canal} perdidas sin publicar")

  def _publicar_diferida(self, muestra) -> bool:
    """Republica UNA muestra del spool. Devuelve si SALIO de verdad (rc == 0).

    AQUI NO SE TOCA LA PRESENCIA: ni self.conectado, ni self._last_heartbeat, ni
    OrbitLastPublish. Un backfill es de hace un rato y no dice que el coche este conectado
    AHORA. El cuerpo ya lleva dentro "backfill": true y el ts_ms de CAPTURA (lo pone
    Spool._muestra), y el reenvio sale por orbit/v2/tel/, nunca por el namespace legacy.

    LO QUE ESTE LADO NO PUEDE ARREGLAR: la app de hoy marca contacto por la LLEGADA del
    mensaje, y lo hace tambien en el camino v2 (mqtt_service.dart, _registrarActividad()
    justo detras de ingerirCanal()), sin mirar el cuerpo. Mientras eso siga asi, drenar el
    spool pintara "visto ahora" un coche que puede llevar un rato apagado. Saltarse la
    presencia cuando backfill == true es cosa del backend y de la app; desde el firmware lo
    unico que se puede hacer es marcarlo en el cuerpo, que es lo que se hace.
    """
    topic = tel2.topic_telemetria(self.DongleID, muestra.canal)
    try:
      info = self.mqttc.publish(topic, muestra.cuerpo, qos=0)
    except Exception as e:
      cloudlog.warning(f"[Bemposta] reenvio diferido de {muestra.canal} en {topic} fallo: {e}")
      return False
    return info.rc == mqtt.MQTT_ERR_SUCCESS

  def _mantener_spool(self, ahora, enlace, drenar_ok=True):
    """Vuelca a disco lo encolado y, con enlace vivo, reenvia una tanda. A 1 Hz.

    TOCA DISCO: corre en el hilo del loop ORBIT y NUNCA en el callback de paho (que es el
    hilo de RED: un handler lento se come el PINGRESP y con el la conexion).

    EL INTERRUPTOR DE PRIVACIDAD TAMBIEN MANDA AQUI. Retirar las fuentes en la captura
    (_fuentes_v2) solo tapa lo que se captura DESPUES de pulsarlo; lo que ya estaba en la
    cola sigue en disco. La secuencia real es: el coche circula sin cobertura y encola N
    mensajes `road` con el nombre de la via, el conductor pulsa el interruptor, vuelve la
    cobertura y se publica la secuencia entera de nombres de calle -- la traza, por el
    mismo argumento que justifica retirar liveMapDataSP. Por eso el mute se pasa al drenar
    (lo de posicion se descarta en vez de publicarse) y ademas se purga lo encolado, que es
    lo unico que sirve cuando el mute se pulsa en mitad del corte de red: sin enlace no se
    drena, y esperar dejaria la traza en disco lista para salir en cuanto se quite el mute.

    TRES COSAS QUE ANTES DEJABAN EL INTERRUPTOR A MEDIAS:

      1. La purga estaba DENTRO del `if s.activo`. Un spool desactivado (disco lleno, base
         corrupta) cierra su conexion pero deja spool.db en la eMMC, y ni purgaba ni
         drenaba: las filas `road` ya escritas se quedaban ahi con el mute puesto hasta que
         reiniciara el proceso -- y si para entonces el conductor lo habia quitado, el
         arranque siguiente las publicaba enteras. Ahora la purga va PRIMERO y fuera del
         `if`; purgar_posicion() sabe abrir la base desactivada y, si ni eso, borrarla.
      2. El mute se leia UNA vez y se pasaba como bool, pero una tanda son hasta 200
         publicaciones seguidas: pulsarlo dentro de la tanda no la cortaba. Ahora se pasa
         el TESTIGO (el metodo, no su valor) y el spool lo vuelve a preguntar fila a fila.
         Se puede llamar 200 veces por tanda porque es un bool cacheado a 1 Hz.
      3. Esto se llamaba solo al final de _ciclo_v2, que retornaba antes por DOS caminos
         (sin identidad y con el tick del motor lanzando). Ahora va en un `finally` y el
         camino sin identidad lo llama aparte. Ver _ciclo_v2.

    `drenar_ok` False = se mantiene la cola pero NO se reenvia: es el caso sin identidad,
    donde publicar bajo el literal "DongleID" atribuiria la telemetria al vehiculo fantasma
    que comparten todos los comma sin registrar.
    """
    s = self._spool()
    if s is None:
      return
    if (ahora - self._last_spool) < self.SPOOL_SECS:
      return
    self._last_spool = ahora
    try:
      # Fuera del `if s.activo`: con el spool apagado el fichero sigue en el disco y esta
      # es la unica via que queda para que el interruptor llegue a el.
      if self._privacidad_silenciada():
        s.purgar_posicion()
      if s.activo:
        s.flush()
        if drenar_ok and enlace and s.hay_pendientes():
          # El testigo, no su valor: ver el punto 2 de arriba.
          s.drenar(self._publicar_diferida, dongle=self.DongleID,
                   privacidad=self._privacidad_silenciada)
    except Exception:
      cloudlog.exception("[Bemposta] mantenimiento del spool fallo")
    # Fuera del try: un spool que se acaba de desactivar (disco lleno, base corrupta) tira
    # su cola de RAM, y esas perdidas tambien hay que recogerlas.
    self._revertir_perdidas(s)

  def _ciclo_v2(self, ahora):
    """Publica la telemetria v2 del tick. Best-effort: NUNCA rompe el bucle.

    El motor tickea SIEMPRE que haya identidad, tambien sin enlace. Antes se salia antes
    de tickear y con eso un corte de cobertura se llevaba por delante tres cosas que no
    son "un dato perdido": el `dt` del viaje (el tope de 5 s por tick del motor recorta el
    hueco entero, asi que dur_s contaba de menos), los flancos de evento ocurridos durante
    el corte, y un viaje que empezara y acabara dentro del corte, que no dejaba rastro.
    Ahora lo que no sale por el cable va al spool y se reenvia marcado cuando vuelve.

    SIN IDENTIDAD no se publica NI SE GUARDA nada (ver __init__): una muestra grabada bajo
    el literal "DongleID" se atribuiria al vehiculo fantasma que comparten todos los comma
    sin registrar.

    EL MANTENIMIENTO DEL SPOOL VA EN UN `finally`. Este metodo tiene DOS salidas
    anticipadas -- sin identidad, y con el tick del motor lanzando -- y hasta ahora las dos
    se llevaban por delante _mantener_spool, que es quien purga la posicion encolada con el
    mute puesto. La segunda no estaba declarada en ninguna parte: un motor que falle
    siempre (una fuente cereal con un campo que ya no existe tras un rebase, por ejemplo)
    dejaba el interruptor de privacidad sin efecto sobre la cola, en silencio y para
    siempre. Con el `finally` la purga corre aunque el tick se caiga en cada ciclo.
    """
    if not self.dongle_valido:
      # Sin identidad no se publica ni se guarda nada nuevo, pero si este proceso llego a
      # abrir el spool antes de perderla, lo encolado sigue en disco y el interruptor tiene
      # que alcanzarlo. No se ABRE el spool aqui a proposito (self._spool_obj y no
      # self._spool()): crear /data/orbit_spool y una base SQLite para un dispositivo que
      # nunca ha tenido identidad no purga nada, solo escribe en la eMMC.
      if self._spool_obj is not None:
        self._mantener_spool(ahora, False, drenar_ok=False)
      return
    enlace = self.conectado
    if enlace and hasattr(self.mqttc, 'is_connected') and not self.mqttc.is_connected():
      enlace = False
    try:
      self._publicar_v2(ahora, enlace)
    finally:
      self._mantener_spool(ahora, enlace)

  def _publicar_v2(self, ahora, enlace):
    """Tick del motor y publicacion de sus mensajes. Lo llama _ciclo_v2 dentro del try."""
    try:
      self._maybe_reload_perfil(ahora)
      mensajes = self.motor_v2.tick(ahora, self._fuentes_v2())
    except Exception:
      cloudlog.exception("[Bemposta] motor de telemetria v2 fallo")
      return
    if self._privacidad_silenciada():
      # Segunda mitad del interruptor en la CAPTURA. _fuentes_v2 ya deja sin fuente a `pos`
      # y a `road`; `trip` no tiene fuente que quitar porque se alimenta de carState, asi
      # que se filtra aqui. No es posicion -- el resumen lleva dist_km, v_max_kph y
      # v_med_kph, ni una coordenada -- pero "43,2 km entre las 22:15 y las 22:47" leido
      # junto al sitio donde el coche duerme reconstruye el trayecto, y repetido a diario
      # dibuja la rutina. La seccion 9 pide "posicion y camara COMO MINIMO": ampliar cabe.
      # Se descarta, no se difiere: encolarlo dejaria el resumen en disco esperando a que
      # se quite el mute, que es exactamente lo que la purga del spool existe para evitar.
      antes = len(mensajes)
      mensajes = [(c, cuerpo) for c, cuerpo in mensajes if c not in spool_mod.CANALES_SILENCIADOS]
      if len(mensajes) != antes:
        cloudlog.warning(f"[Bemposta] privacidad: {antes - len(mensajes)} mensaje(s) de canal silenciado descartados en captura")
    for canal, cuerpo in mensajes:
      topic = tel2.topic_telemetria(self.DongleID, canal)
      try:
        # Mismo saneado + allow_nan=False que el camino v1: un solo float no finito
        # convierte el mensaje en algo que el parser del backend no puede leer, y el
        # publish sale con rc=0 igualmente (fallo intermitente y sin rastro).
        payload = json.dumps(tel2.sanea_no_finitos(cuerpo), allow_nan=False, separators=(",", ":"))
      except (ValueError, TypeError):
        # Irrecuperable: reintentarlo daria exactamente el mismo error, asi que NO se
        # difiere ni se revierte el sello (seria un bucle). Se pierde y queda en el log.
        cloudlog.warning(f"[Bemposta] canal v2 {canal}: payload no serializable, descartado")
        continue
      if not enlace:
        # Sin enlace ni se intenta el publish: directo al spool.
        self._diferir(canal, payload)
        continue
      salio = False
      try:
        info = self.mqttc.publish(topic, payload, qos=0)
        salio = info.rc == mqtt.MQTT_ERR_SUCCESS
        if not salio:
          self._log_publish_rc(f"canal v2 {canal}", topic, info.rc)
      except Exception as e:
        # NO tocar self.conectado aqui (ver el mismo razonamiento en el bucle v1): un
        # fallo puntual de un canal no significa que el socket MQTT este caido.
        cloudlog.warning(f"[Bemposta] fallo publicando canal v2 {canal} en {topic}: {e}")
      if not salio:
        self._diferir(canal, payload)

  def _maybe_announce_enroll(self):
    """Anuncia el codigo de enrolamiento ORBIT (QR) por MQTT mientras el
    dispositivo no este reclamado, y rota el codigo cuando expira su TTL.

    - Sale si OrbitClaimed (ya emparejado): el QR no debe reaparecer.
    - Exige dongle valido (lo reevalua _refresh_dongle desde el loop): con el
      literal de fallback "DongleID" no anuncia, porque emparejaria contra un id
      inexistente que ademas comparten todos los comma sin registrar.
    - Rota el codigo (CSPRNG, 8 chars mayusculas, sin O/0/1/I) al inicio y al
      expirar ENROLL_TTL_S; deja OrbitPairingCode / OrbitEnrollExpiry en Params
      para que la UI pinte el QR y una cuenta atras opcional.
    - Publica a telemetry_mqtt/<dongle>/enroll (qos=0, retain=False) cada
      ENROLL_ANNOUNCE_SECS.

    Best-effort: cualquier fallo se loguea y NUNCA rompe el loop de telemetria.
    """
    try:
      if self.params.get_bool("OrbitClaimed"):
        return
      if not self.dongle_valido:
        return
      # Epoch de PARED a proposito: el TTL del codigo se publica en OrbitEnrollExpiry y
      # en issued_at, y tanto la UI del comma como el backend los comparan con SU reloj de
      # pared. Un plazo monotono aqui seria incomparable fuera de este proceso.
      now = _epoch_ms() / 1000.0
      d = self.DongleID
      # Regeneracion manual (trigger OrbitEnrollRegen, lo pone la UI o el
      # unclaim): consumirlo y forzar rotacion + anuncio inmediatos.
      if self.params.get_bool("OrbitEnrollRegen"):
        self.params.remove("OrbitEnrollRegen")
        self._pairing_code = None
      # Rotacion del codigo: primera vez o TTL expirado. Se genera SIEMPRE (aunque
      # no haya broker) para que la UI pueda pintar el QR/codigo sin conexion.
      if self._pairing_code is None or (now - self._enroll_issued_at) >= self.ENROLL_TTL_S:
        # Re-chequear OrbitClaimed justo antes de escribir: cierra la carrera con
        # handle_enroll_ack (otro hilo) que borra OrbitPairingCode al reclamar.
        if self.params.get_bool("OrbitClaimed"):
          return
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        self._enroll_issued_at = now
        self._pairing_code = code
        # Anunciar el codigo nuevo YA: si se respetara la ventana de 30 s, el
        # backend seguiria validando contra el codigo viejo (bad_code) hasta
        # el proximo anuncio.
        self._last_enroll = 0.0
        self.params.put("OrbitPairingCode", code)
        self.params.put("OrbitEnrollExpiry", str(int((self._enroll_issued_at + self.ENROLL_TTL_S) * 1000)))
      # Anuncio periodico por MQTT: SOLO con conexion. Sin broker el codigo ya
      # esta disponible para la UI y se reintentara en la siguiente iteracion.
      if self.conectado and (now - self._last_enroll) >= self.ENROLL_ANNOUNCE_SECS:
        try:
          fw = self.params.get("Version") or ""
        except Exception:
          fw = ""
        payload = {
          "dongle_id": d,
          "pairing_code": self._pairing_code,
          "issued_at": int(self._enroll_issued_at * 1000),
          "ttl_s": self.ENROLL_TTL_S,
          "fw": fw,
        }
        # Modelo real (tici | tizi | mici | pc), el mismo que va en caps. Antes era un
        # "comma3x" fijo, falso en un comma 4. Si no se puede leer, se omite.
        if hw := tipo_dispositivo():
          payload["hw"] = hw
        self.mqttc.publish(f"telemetry_mqtt/{d}/enroll", json.dumps(payload), qos=0, retain=False)
        self._last_enroll = now
    except Exception as e:
      cloudlog.warning(f"[Bemposta] _maybe_announce_enroll fallo: {e}")

  def enviar_datos_importantes(self, canal, datos):
    claves = self.keys_importantes_por_canal.get(canal, [])

    # Si no hay claves definidas, usar datos directamente sin copiar
    if not claves:
      datos["dongle_id"] = self.DongleID
      return datos
    else:
      resultado = {k: datos[k] for k in claves if k in datos}
      resultado["dongle_id"] = self.DongleID
      return resultado

if __name__ == "__main__":
  sender = MQTTEnvioGeneral()
  sender.start()

  while not sender.conectado:
    time.sleep(0.5)

  while True:
    time.sleep(10)
