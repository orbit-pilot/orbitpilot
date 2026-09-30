#!/usr/bin/env python3
"""Router de mando remoto ORBIT v2: sobre, filtros, ACK y ejecucion (secciones 3, 4 y 6).

ARQUITECTURA DE HILOS -- esto es lo primero que hay que entender del fichero:

    hilo de red de paho          cola (acotada)        hilo worker propio
    on_message() -> parse ...........................> _ejecutar() -> handler

El callback de paho corre en el HILO DE RED. Un handler lento ahi retrasa el
PINGRESP y el broker cierra la conexion: el sintoma es "el coche deja de responder
justo cuando le mandas algo pesado". Por eso aqui SOLO se parsea y se valida (todo
en RAM, sin disco, sin cereal, sin CAN) y lo que sobrevive se ENCOLA. Si la cola
esta llena se responde BUSY en vez de bloquear: perder un comando es recuperable,
perder la conexion no.

FILTROS, en orden (seccion 3.4 y tabla de la seccion 12):

    retain / payload vacio / JSON invalido / dongle ajeno .. DESCARTADO sin ACK
    v != 2 .................................................. TYPE
    verbo desconocido o sin handler ......................... UNSUPPORTED_VERB
    >>> disarm_all sale por aqui y se ejecuta SIEMPRE <<<
    id repetido (LRU de 512) ................................ DUPLICATE
    reloj sin sincronizar o desfase > 30 s .................. CLOCK
    ts_ms/seq/ttl_ms mal tipados ............................ TYPE
    TTL vencido ............................................. EXPIRED
    args fuera de tipo o de rango ........................... TYPE / RANGE
    eco de la publicacion dual v1+v2 ........................ DESCARTADO / DUPLICATE
    seq menor o igual que el ultimo aceptado del verbo ...... SUPERSEDED
    modo insuficiente o banco sin armado fisico ............. MODE
    gate en rojo ............................................ GATE_<nombre>
    presupuesto de ritmo agotado ............................ RANGE

Lo que se DESCARTA no lleva ACK a proposito: un retenido, un borrado de retenido o
un comando dirigido a otro dongle no son ordenes de este coche, y contestarlas seria
publicar en nuestro topic de ACK por orden de un tercero.
"""
import json
import queue
import threading
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from openpilot.common.swaglog import cloudlog
from openpilot.orbit import command_spec as spec_mod
from openpilot.orbit.command_gates import LINK_FRESH_MAX_S
from openpilot.orbit.command_spec import (CONTRACT_VERSION, TOPIC_ACK, TOPIC_CMD, Gate, Mode, Phase,
                                          ahora_epoch_ms, ahora_mono, gate_reason, get_spec, parse_mode,
                                          validar_args)

# Tope de la LRU de idempotencia (seccion 3.4).
LRU_MAX = 512

# Cola entre el hilo de red y el worker. 64 comandos es mucho mas de lo que un humano
# puede generar; si se llena es que algo esta inundando el broker y lo correcto es
# responder BUSY, no crecer en RAM (seccion 12, "saturar el broker").
QUEUE_MAX = 64

# Desfase maximo tolerado entre el ts_ms del emisor y el reloj local. La seccion 3.2 avisa
# en la app a partir de 5 s; aqui se RECHAZA a partir de 30 s, que es lo que separa el
# jitter de red de un reloj mal puesto. La prueba de la seccion 12 (reloj desfasado 5 min)
# cae de lleno en CLOCK y no en EXPIRED, que es lo que se quiere: el motivo tiene que
# decir la verdad al usuario.
CLOCK_SKEW_MAX_MS = 30_000

# Ventana del watchdog de ENLACE (seccion 5, el de segundos). No es el de actuador.
LINK_WINDOW_S = 5.0

# TTL por defecto si el sobre no lo trae y el verbo tampoco. Corto a proposito.
TTL_DEFECTO_MS = 2_000

# Ventana en la que dos sobres del MISMO verbo y los MISMOS argumentos llegados por
# CAMINOS DISTINTOS (el topic v2 y el puente v1) se consideran la MISMA accion del
# usuario y no dos ordenes.
#
# Existe por la publicacion DUAL de la seccion 13: las rutas legacy del backend publican
# el topic v1 y ademas el verbo v2 equivalente, con `id` distinto y contador de secuencia
# distinto, asi que ningun filtro de idempotencia los cruza. Consecuencias medidas: un
# cruise_delta gasta el DOBLE del presupuesto de ritmo, y -- peor -- si el sobre v2 se
# rechaza (gate rojo, modo insuficiente) el gemelo v1 se ejecuta igual, con lo que la app
# pinta "Rechazada" mientras el coche cambia de carril.
#
# 2 s es holgado para el desfase entre las dos publicaciones del mismo endpoint (van
# seguidas en el mismo proceso) y corto para dos pulsaciones reales del usuario. La
# supresion es SOLO entre caminos distintos: dos ordenes iguales seguidas por el MISMO
# camino son dos ordenes y se atienden como tales.
ECO_VENTANA_S = 2.0

# Un mensaje descartado o rechazado escribe UN log como mucho cada tanto. Sin esto, quien
# inunde el topic de mando (el broker es abierto, D1) hace que el hilo de RED se pase el
# rato escribiendo en swaglog, que es justo lo que la seccion 12 pide evitar al saturar.
LOG_CADA_S = 2.0


@dataclass
class Resultado:
  """Que paso con un mensaje. Lo devuelve handle_payload para los tests y los logs."""
  aceptado: bool = False
  descartado: bool = False
  reason: str = ""
  detail: str = ""
  cmd_id: str = ""
  verb: str = ""
  phase: str = ""


@dataclass
class Comando:
  """Sobre ya validado (seccion 3.2), listo para ejecutar."""
  cmd_id: str
  seq: int
  verb: str
  args: dict
  ts_ms: int
  ttl_ms: int | None
  mode: Mode | None
  actor: dict = field(default_factory=dict)
  spec: object = None
  recibido_mono: float = 0.0
  deadline_mono: float = 0.0   # 0.0 = sin TTL (solo disarm_all)
  crudo: dict = field(default_factory=dict)

  @property
  def caducado(self) -> bool:
    return self.deadline_mono > 0.0 and ahora_mono() > self.deadline_mono


class CommandRouter:
  """Un router por dispositivo. Se construye en el hilo ORBIT y se comparte con paho."""

  def __init__(self, dongle_id: str, gates=None, store=None, publish=None,
               queue_max: int = QUEUE_MAX, lru_max: int = LRU_MAX, publicar_received: bool = True):
    self.dongle_id = (dongle_id or "").strip()
    self.gates = gates
    self.store = store
    # publish(topic, payload, qos, retain) -> lo inyecta MQTTComandos con su cliente paho.
    self._publish = publish
    self._publicar_received = publicar_received

    self._handlers: dict = {}
    self._cola: queue.Queue = queue.Queue(maxsize=queue_max)
    self._lru_max = int(lru_max)
    self._vistos: OrderedDict = OrderedDict()   # cmd_id -> mono de la primera vez
    self._ultimo_seq: dict = {}                 # clave de fuente+verbo -> ultimo seq ACEPTADO
    self._seq_local = 0                         # contador de los sobres que fabricamos aqui
    self._ritmo: dict = {}                      # verbo -> deque[(mono, cantidad)]
    self._eco: dict = {}                        # verbo -> (clave_args, mono, fuente)
    # Verbo dueno de la ventana de actuador que este router abrio por ultima vez. Lo lleva
    # el router y no el plano porque el plano tiene UN solo deadline para todo el
    # subsistema (ver _deadline_efectivo).
    self._ventana_verb = ""
    self._lock = threading.Lock()

    self._worker: threading.Thread | None = None
    self._stop = threading.Event()
    self._ultimo_log: dict = {}

    # Verbos cuyo resultado real lo decide un consumidor en OTRO proceso (spec
    # .cierra_consumidor). cmd_id -> (verbo, mono limite). Mientras haya alguno, el hilo
    # de resultados lee OrbitCmdResult; con el diccionario vacio no toca el disco.
    self._pendientes: dict = {}
    self._hilo_resultados: threading.Thread | None = None
    self._params_res = None

  def _log(self, clave: str, mensaje: str) -> None:
    """Log acotado por clave. Corre en el hilo de red: nunca puede ser el cuello de botella."""
    ahora = ahora_mono()
    ultimo = self._ultimo_log.get(clave, 0.0)
    if ahora - ultimo < LOG_CADA_S:
      return
    if len(self._ultimo_log) > 64:
      self._ultimo_log.clear()
    self._ultimo_log[clave] = ahora
    cloudlog.warning(mensaje)

  # ------------------------------------------------------------------ configuracion

  @property
  def topic_cmd(self) -> str:
    return TOPIC_CMD.format(self.dongle_id)

  @property
  def topic_ack(self) -> str:
    return TOPIC_ACK.format(self.dongle_id)

  def register_handler(self, verb: str, fn) -> None:
    """Registra el ejecutor de un verbo. Un verbo de la tabla SIN handler responde
    UNSUPPORTED_VERB y se publica como no soportado en el descriptor de capacidades: es
    preferible a aceptar y no hacer nada, que es el boton que miente de la seccion 3.5."""
    if get_spec(verb) is None:
      raise ValueError(f"verbo '{verb}' no esta en COMMANDS (orbit/command_spec.py)")
    with self._lock:
      self._handlers[verb] = fn

  def registered_verbs(self) -> set:
    with self._lock:
      return set(self._handlers)

  def capabilities_payload(self, brand: str = "", platform: str = "", fw: str = "") -> dict:
    """Descriptor para orbit/v2/caps/<dongle>, con SOLO los verbos que de verdad tienen
    handler en este firmware.

    Marca y plataforma salen del GateMonitor (carParams), no de Params ni de un fichero:
    es la misma fuente que decide los gates, asi que la app no puede ver un descriptor
    que contradiga al coche. Offroad, carParams aun no se ha publicado y los dos campos
    salen vacios; el publicador los reintenta cuando aparecen (ver
    MQTTComandos.maybe_publish_caps).
    """
    marca = brand or (self.gates.brand if self.gates is not None else "")
    plataforma = platform or (getattr(self.gates, "platform", "") if self.gates is not None else "")
    return spec_mod.capabilities_payload(brand=marca, platform=plataforma, fw=fw,
                                         verbos_disponibles=self.registered_verbs())

  # ----------------------------------------------------------------- ciclo de vida

  def start(self) -> None:
    if self._worker is not None and self._worker.is_alive():
      return
    self._stop.clear()
    self._worker = threading.Thread(target=self._bucle_worker, daemon=True, name="OrbitCommandWorker")
    self._worker.start()
    if self._hilo_resultados is None or not self._hilo_resultados.is_alive():
      self._hilo_resultados = threading.Thread(target=self._bucle_resultados, daemon=True,
                                               name="OrbitCommandResult")
      self._hilo_resultados.start()

  def stop(self, timeout: float = 2.0) -> None:
    self._stop.set()
    for hilo in (self._worker, self._hilo_resultados):
      if hilo is not None and hilo.is_alive():
        hilo.join(timeout=timeout)

  # ------------------------------------------------------------------------- ACK

  def _ack(self, cmd_id: str, phase: str, reason: str = "", detail: str = "", verb: str = "") -> None:
    """Publica una fase del ACK en orbit/v2/ack/<dongle> (qos 1, sin retain).

    Nunca lanza: un fallo publicando el ACK no puede impedir que el comando se ejecute ni
    matar el hilo de red (paho corre las callbacks con suppress_exceptions=False).
    """
    if self.store is not None:
      try:
        self.store.note_ack(phase, reason)
      except Exception:
        pass
    if self._publish is None or not self.dongle_id:
      return
    payload = {
      "v": CONTRACT_VERSION,
      "id": cmd_id,
      "verb": verb,
      "phase": phase,
      "reason": reason or ("OK" if phase in (Phase.ACCEPTED, Phase.APPLIED) else ""),
      "detail": detail,
      "ts_ms": ahora_epoch_ms(),
      "mono_ms": int(ahora_mono() * 1000),
    }
    try:
      self._publish(self.topic_ack, json.dumps(payload, separators=(",", ":")), 1, False)
    except Exception:
      cloudlog.exception("[Orbit] no se pudo publicar el ACK")

  # ----------------------------------------------------------------- entrada MQTT

  def on_message(self, client, userdata, msg) -> Resultado:
    """Callback de paho. TODO el cuerpo va protegido: una excepcion aqui sale hasta
    _thread_main, cuyo finally pone _thread=None y mata el hilo de red EN SILENCIO."""
    try:
      return self.handle_payload(msg.topic, msg.payload, bool(getattr(msg, "retain", False)))
    except Exception:
      cloudlog.exception("[Orbit] CommandRouter.on_message fallo (el hilo de red habria muerto en silencio)")
      return Resultado(descartado=True, detail="excepcion en on_message")

  def handle_payload(self, topic, payload, retain: bool = False, fuente: str = "net") -> Resultado:
    """Parsea y valida EN EL HILO DE RED, y encola. No ejecuta nada.

    `fuente` dice de donde vino el sobre: "net" (orbit/v2/cmd/<dongle>) o el nombre del
    puente que lo fabrico ("v1"). Lo UNICO que cambia es el cajon de la ventana de
    secuencia: `seq` es "monotono por dongle" y lo lleva el backend, asi que mezclar su
    contador con el nuestro haria que uno de los dos se rechazara a si mismo con
    SUPERSEDED para siempre. Ningun filtro de seguridad -- modo, gates, TTL, reloj,
    idempotencia, ritmo -- mira este campo: v1 y v2 pasan exactamente por lo mismo.
    """

    # 1) retain: prohibido incondicionalmente en el namespace de mando (seccion 3.1).
    # Un mando retenido lo reentrega el broker en CADA reconexion: una frenada retenida
    # vuelve a frenar el coche cada vez que reaparece la cobertura.
    if retain:
      self._log("retain", "[Orbit] mando RETENIDO descartado (prohibido en orbit/v2/cmd)")
      return Resultado(descartado=True, detail="retain")

    # 2) payload vacio: es el gesto MQTT estandar para BORRAR un retenido, no una orden.
    if isinstance(payload, (bytes, bytearray)):
      texto = payload.decode("utf-8", errors="ignore")
    else:
      texto = payload if isinstance(payload, str) else ""
    texto = texto.strip()
    if not texto:
      return Resultado(descartado=True, detail="payload vacio")

    # 3) dongle ajeno: el topic manda. No hay ningun topic de mando sin <dongle>.
    dongle_topic = self._dongle_de_topic(topic)
    if dongle_topic is None or not self.dongle_id or dongle_topic != self.dongle_id:
      self._log("dongle", "[Orbit] mando para otro dongle descartado")
      return Resultado(descartado=True, detail="dongle ajeno")

    # 4) JSON
    try:
      crudo = json.loads(texto)
    except Exception:
      return Resultado(descartado=True, detail="json invalido")
    if not isinstance(crudo, dict):
      return Resultado(descartado=True, detail="el sobre no es un objeto")

    cmd_id = crudo.get("id")
    cmd_id = cmd_id.strip() if isinstance(cmd_id, str) else ""
    verb = crudo.get("verb")
    verb = verb.strip() if isinstance(verb, str) else ""

    # Sin id no hay idempotencia posible y QoS 1 es at-least-once: se descarta.
    if not cmd_id:
      return Resultado(descartado=True, detail="sobre sin id")

    # 5) version del contrato
    if crudo.get("v") != CONTRACT_VERSION:
      return self._rechazo(cmd_id, verb, "TYPE", f"version de contrato no soportada: {crudo.get('v')!r}")

    # 6) verbo
    spec = get_spec(verb)
    if spec is None:
      return self._rechazo(cmd_id, verb, "UNSUPPORTED_VERB", "verbo desconocido")
    with self._lock:
      tiene_handler = verb in self._handlers
    if not tiene_handler:
      return self._rechazo(cmd_id, verb, "UNSUPPORTED_VERB", "este firmware no implementa el verbo")

    # El ACK 'received' se publica AQUI y no antes: hasta este punto el mensaje podia ser
    # basura de cualquiera que conozca el dongle, y contestar a cada byte que llega
    # convierte nuestro propio enlace de subida en el amplificador de la inundacion.
    if self._publicar_received:
      self._ack(cmd_id, Phase.RECEIVED, verb=verb)

    args_crudos = crudo.get("args") or {}

    # El enlace esta vivo: llega un sobre bien formado para NOSOTROS. Alimenta LINK_FRESH
    # y el watchdog de enlace, tanto si el comando acaba aceptandose como si no.
    self._marcar_enlace()

    # ------------------------------------------------------------------------------
    # EXCEPCION EXPLICITA DEL CONTRATO (secciones 2 y 3.4).
    #
    # disarm_all se ejecuta SIEMPRE: aunque llegue fuera de orden (seq menor que el arm
    # que lo precedio), aunque el TTL haya expirado, aunque el reloj este mal, sea cual
    # sea el modo y esten los gates que esten. BAJAR AUTORIDAD NUNCA SE DESCARTA.
    #
    # Se salta a proposito hasta la comprobacion de duplicado: QoS 1 puede reentregarlo y
    # ejecutarlo dos veces, pero desarmar dos veces es desarmar. La alternativa -- tragarse
    # el segundo por DUPLICATE -- pierde el desarme si el primero se perdio en un buffer.
    # ------------------------------------------------------------------------------
    if spec.baja_autoridad:
      reason, detail, args = validar_args(spec, args_crudos)
      if reason is not None:
        # Ni siquiera esto lo bloquea: se ejecuta con los argumentos por defecto y se
        # deja constancia. disarm_all no tiene argumentos obligatorios.
        cloudlog.warning(f"[Orbit] disarm_all con args invalidos ({detail}): se ejecuta igual")
        args = {}
      cmd = Comando(cmd_id=cmd_id, seq=self._entero(crudo.get("seq"), 0), verb=verb, args=args,
                    ts_ms=self._entero(crudo.get("ts_ms"), 0), ttl_ms=None, mode=None,
                    actor=crudo.get("actor") or {}, spec=spec, recibido_mono=ahora_mono(),
                    deadline_mono=0.0, crudo=crudo)
      self._recordar_id(cmd_id)
      return self._encolar(cmd)

    # 7) idempotencia por id (LRU de 512). Es el control anti-reinyeccion: va antes que
    # el reloj y que el TTL para que un comando capturado y reinyectado diga DUPLICATE y
    # no un motivo colateral.
    if self._ya_visto(cmd_id):
      if fuente != "net":
        # Copia v1 de un sobre v2 que ya paso por aqui (mismo `origin_id`): el gemelo ya
        # tiene su veredicto y su ACK con ese id. Se descarta EN SILENCIO; publicar un
        # segundo ACK para el mismo id solo puede pisar en el registro de evidencia la
        # fase que de verdad valia.
        return Resultado(descartado=True, detail="eco v1 del mismo id ya procesado", verb=verb)
      return self._rechazo(cmd_id, verb, "DUPLICATE", "id ya procesado")

    # 7.bis) el evaluador de gates tiene que estar VIVO y con datos frescos.
    #
    # REGLA DURA: si el GateMonitor murio, se colgo o su hilo aun no ha ticado, sus datos
    # estan rancios y los gates se consideran EN ROJO -- nunca en verde. Se comprueba aqui
    # arriba, y no solo dentro de _comprobar_gates, por dos motivos: (1) el motivo del ACK
    # dice la verdad (INTERNAL, "el evaluador no publica") en vez de un CLOCK colateral, y
    # (2) alcanza tambien a los verbos que no declaran gates: sin plano de estado vivo
    # tampoco hay deadman, y un actuador sin deadman es un actuador pegado.
    if self._gates_rancios():
      return self._rechazo(cmd_id, verb, "INTERNAL",
                           "el evaluador de gates no publica datos frescos: gates en rojo")

    # 8) reloj. Preferible un coche que no obedece a uno que obedece una orden de hace
    # diez minutos (seccion 3.2).
    if not self._reloj_ok():
      return self._rechazo(cmd_id, verb, "CLOCK", "el reloj del dispositivo no esta sincronizado")

    ts_ms = crudo.get("ts_ms")
    if isinstance(ts_ms, bool) or not isinstance(ts_ms, int):
      # Ni float, ni cadena, ni ISO-8601. Hoy el campo temporal viaja como int, como
      # string y como ISO en el MISMO topic: eso es la enfermedad (seccion 2).
      return self._rechazo(cmd_id, verb, "TYPE", "ts_ms debe ser epoch en ms, entero")

    desfase_ms = ahora_epoch_ms() - ts_ms
    if abs(desfase_ms) > CLOCK_SKEW_MAX_MS:
      return self._rechazo(cmd_id, verb, "CLOCK", f"desfase de {desfase_ms} ms entre emisor y coche")

    seq = crudo.get("seq", 0)
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
      return self._rechazo(cmd_id, verb, "TYPE", "seq debe ser entero no negativo")

    ttl_ms = crudo.get("ttl_ms", spec.ttl_ms if spec.ttl_ms is not None else TTL_DEFECTO_MS)
    if isinstance(ttl_ms, bool) or not isinstance(ttl_ms, int) or ttl_ms <= 0:
      return self._rechazo(cmd_id, verb, "TYPE", "ttl_ms debe ser entero positivo en ms")
    # El TTL del sobre nunca puede ampliar el del catalogo: el limite lo pone el coche.
    if spec.ttl_ms is not None:
      ttl_ms = min(ttl_ms, spec.ttl_ms)

    # 9) TTL. Se calcula UNA vez con epoch (unico marco comun con el emisor) y a partir de
    # aqui el plazo vive en monotono, que no da saltos cuando entra el NTP (seccion 3.2).
    recibido_mono = ahora_mono()
    edad_ms = max(0, desfase_ms)        # un ts_ms en el futuro no alarga el TTL
    restante_ms = ttl_ms - edad_ms
    if restante_ms <= 0:
      return self._rechazo(cmd_id, verb, "EXPIRED", f"TTL de {ttl_ms} ms vencido hace {-restante_ms} ms",
                           phase=Phase.EXPIRED)

    # 10) argumentos
    reason, detail, args = validar_args(spec, args_crudos)
    if reason is not None:
      return self._rechazo(cmd_id, verb, reason, detail)

    # 10.bis) eco de la publicacion DUAL v1+v2 (seccion 13). Se comprueba aqui, con los
    # argumentos ya normalizados, porque comparar args crudos daria falsos negativos: el
    # mismo cambio de carril llega como {"direction":"left"} por los dos caminos, pero un
    # float 2 y un int 2 solo se igualan despues de validar.
    eco = self._eco_de_otro_camino(verb, args, fuente)
    if eco is not None:
      if fuente != "net":
        # El gemelo v2 ya tiene (o va a tener) su veredicto y su ACK con SU id. Este es
        # la copia v1 de la misma pulsacion: se descarta EN SILENCIO. Sin ACK a proposito:
        # publicar un segundo veredicto con un id que el backend no conoce solo puede
        # confundir al registro de evidencia.
        self._log("eco", f"[Orbit] copia v1 de un mando v2 ya atendido descartada (verb={verb})")
        return Resultado(descartado=True, detail=f"eco de '{verb}' por {eco}", verb=verb)
      # El puente v1 llego primero (el orden entre las dos publicaciones no esta
      # garantizado): la maniobra ya esta en curso, asi que esta copia NO se ejecuta y se
      # dice por que. DUPLICATE es el codigo honesto: la app lo traduce por "orden
      # repetida (ya se habia ejecutado)".
      return self._rechazo(cmd_id, verb, "DUPLICATE",
                           f"misma orden ya atendida por el camino '{eco}' hace menos de {ECO_VENTANA_S:g} s")
    self._anotar_eco(verb, args, fuente)

    # 11) ventana de secuencia. Un seq menor o igual que el ultimo ACEPTADO del mismo
    # verbo llega tarde: el comando bueno ya esta en la cola o ejecutado.
    clave_seq = verb if fuente == "net" else f"{fuente}:{verb}"
    with self._lock:
      ultimo = self._ultimo_seq.get(clave_seq)
    if ultimo is not None and seq <= ultimo:
      return self._rechazo(cmd_id, verb, "SUPERSEDED", f"seq {seq} <= {ultimo} ya aceptado para '{verb}'",
                           phase=Phase.SUPERSEDED)

    # 12) modo
    reason, detail = self._comprobar_modo(spec, crudo.get("mode"))
    if reason is not None:
      return self._rechazo(cmd_id, verb, reason, detail)

    # 13) gates
    reason, detail = self._comprobar_gates(spec, args)
    if reason is not None:
      return self._rechazo(cmd_id, verb, reason, detail)

    # 14) presupuesto de ritmo (p.ej. +-20 km/h por minuto en cruise_delta)
    reason, detail = self._comprobar_ritmo(spec, args, aplicar=True)
    if reason is not None:
      return self._rechazo(cmd_id, verb, reason, detail)

    cmd = Comando(cmd_id=cmd_id, seq=seq, verb=verb, args=args, ts_ms=ts_ms, ttl_ms=ttl_ms,
                  mode=parse_mode(crudo.get("mode")), actor=crudo.get("actor") or {}, spec=spec,
                  recibido_mono=recibido_mono, deadline_mono=recibido_mono + restante_ms / 1000.0,
                  crudo=crudo)

    self._recordar_id(cmd_id)
    with self._lock:
      self._ultimo_seq[clave_seq] = seq
    return self._encolar(cmd)

  # ------------------------------------------------- puente v1 -> sobre v2 (seccion 13)

  def submit_local(self, verb: str, args=None, actor=None, fuente: str = "v1",
                   ttl_ms: int | None = None, origin_id: str | None = None) -> Resultado:
    """Fabrica un SOBRE v2 y lo mete por el MISMO camino que un mando de red.

    Es el puente de la migracion (seccion 13). Lo llama el dispatch v1 por topic de
    MQTTComandos: en vez de escribir el Param del actuador a mano -- que era un segundo
    camino con reglas propias, es decir un agujero por el que se colaban las ordenes que
    el contrato v2 rechaza --, traduce el topic v1 a (verbo, args) y llama aqui. A partir
    de esta linea no hay diferencia entre v1 y v2: los mismos gates, el mismo modo, la
    misma idempotencia, el mismo TTL, la misma cola y el mismo ACK.

    Lo que NO puede aportar un mando v1 y por tanto se fabrica aqui:
      * `id`: v1 no lo trae, asi que se genera uno unico. Consecuencia honesta: un
        comando v1 repetido NO se detecta como DUPLICATE (no hay con que). El topic v1 se
        suscribe con qos 0, que no reentrega, y el retenido ya se filtra antes.
        `origin_id` es la salida a eso: si el emisor v1 puede decir con QUE id publico el
        sobre v2 gemelo (el backend lo sabe: es el mismo endpoint quien publica los dos,
        ver la publicacion dual de la seccion 13), se reutiliza ese id y entonces la LRU
        de idempotencia SI cruza las dos copias. Sin el, el cruce lo hace por parecido la
        ventana de eco (ECO_VENTANA_S), que es una heuristica y no una identidad.
      * `seq`: contador propio, en su propio cajon de la ventana de secuencia (ver el
        parametro `fuente` de handle_payload).
      * `ts_ms`: el instante de AHORA. El sobre nace aqui, no viaja: no hay desfase de
        reloj que medir contra el emisor, pero el filtro CLOCK sigue exigiendo que el
        reloj del dispositivo este sincronizado, igual que para un mando v2.
      * `mode`: no se declara. El emisor v1 no sabe en que modo esta el coche, y declarar
        uno seria mentir; el router comprueba igual el modo REAL contra spec.mode_min.

    Se llama desde el hilo de red de paho, asi que hace lo mismo que on_message: parsear,
    validar y encolar. Nunca ejecuta.
    """
    with self._lock:
      self._seq_local += 1
      seq = self._seq_local
    id_origen = origin_id.strip() if isinstance(origin_id, str) else ""
    sobre = {
      "v": CONTRACT_VERSION,
      "id": id_origen or f"{fuente}-{uuid.uuid4().hex}",
      "seq": seq,
      "verb": verb,
      "args": dict(args or {}),
      "ts_ms": ahora_epoch_ms(),
      "mono_ms": int(ahora_mono() * 1000),
      "mode": None,
      "actor": dict(actor or {"via": fuente}),
      "sig": None,
    }
    if ttl_ms is not None:
      sobre["ttl_ms"] = int(ttl_ms)
    try:
      texto = json.dumps(sobre, separators=(",", ":"))
    except Exception:
      cloudlog.exception("[Orbit] no se pudo serializar el sobre v1->v2")
      return Resultado(descartado=True, detail="sobre v1 no serializable", verb=verb)
    return self.handle_payload(self.topic_cmd, texto, retain=False, fuente=fuente)

  # ------------------------------------------------------------------------ ayudas

  def _dongle_de_topic(self, topic) -> str | None:
    if not isinstance(topic, str):
      return None
    prefijo = TOPIC_CMD.format("")
    if not topic.startswith(prefijo):
      return None
    resto = topic[len(prefijo):]
    return resto.strip("/") or None

  @staticmethod
  def _entero(valor, por_defecto: int) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int):
      return por_defecto
    return valor

  def _gates_rancios(self) -> bool:
    """True si no hay GateMonitor o su mascara esta rancia. Fail-closed."""
    if self.gates is None:
      return True
    try:
      return bool(getattr(self.gates, "stale", False))
    except Exception:
      return True

  def _reloj_ok(self) -> bool:
    """Sin GateMonitor NO se da por bueno el reloj: fail-closed. La unica orden que
    sobrevive a eso es disarm_all, que ni pasa por aqui."""
    if self.gates is None:
      return False
    try:
      return bool(self.gates.clock_synced)
    except Exception:
      return False

  def _marcar_enlace(self) -> None:
    if self.gates is not None:
      try:
        self.gates.note_link()
      except Exception:
        pass
    if self.store is not None:
      try:
        self.store.note_link(LINK_WINDOW_S)
      except Exception:
        pass

  def _modo_actual(self) -> Mode:
    if self.store is None:
      return Mode.OBSERVER
    try:
      return self.store.snapshot()["mode"]
    except Exception:
      return Mode.OBSERVER

  def _comprobar_modo(self, spec, modo_declarado) -> tuple[str | None, str]:
    actual = self._modo_actual()
    if spec.mode_min is Mode.BENCH:
      # EL MODO BANCO NO ESTA EN LA ESCALA. `set_mode` no lo ofrece a proposito (solo se
      # alcanza con armado FISICO, seccion 4.1), asi que compararlo numericamente contra
      # `actual` dejaba los verbos fisicos INALCANZABLES incluso con el banco armado en la
      # pantalla del coche: caian aqui antes de llegar a la comprobacion del armado, y el
      # dialogo de la pantalla prometia lo contrario. Quien concede banco es el armado, y
      # es lo que se comprueba unas lineas mas abajo.
      pass
    elif actual < spec.mode_min:
      return "MODE", f"'{spec.verb}' exige modo {spec.mode_min.name.lower()} y el coche esta en {actual.name.lower()}"

    # El sobre declara en que modo cree el emisor que esta el coche. Si el coche tiene
    # MENOS autoridad de la declarada, el emisor esta operando con una foto vieja.
    declarado = parse_mode(modo_declarado)
    if modo_declarado is not None and declarado is None:
      return "TYPE", f"campo mode no reconocido: {modo_declarado!r}"
    if declarado is not None and actual < declarado:
      return "MODE", f"el emisor asumia modo {declarado.name.lower()} y el coche esta en {actual.name.lower()}"

    # Modo banco: armado FISICO en la pantalla del comma, con caducidad vigente. Ningun
    # verbo fisico es alcanzable solo por MQTT (seccion 1, control compensatorio 2).
    if spec.requiere_armado_banco and not self._banco_armado():
      return "MODE", "el modo banco exige armado fisico en la pantalla del comma"
    return None, ""

  def _banco_armado(self) -> bool:
    if self.store is None:
      return False
    try:
      return bool(self.store.bench_armado_vigente)
    except Exception:
      return False

  def _comprobar_gates(self, spec, args) -> tuple[str | None, str]:
    if not spec.gates:
      return None, ""
    if self.gates is None:
      # Fail-closed: un verbo que declara precondiciones NO se ejecuta sin nadie que las
      # mire. Lo contrario seria que un fallo al crear el SubMaster abriera todos los gates.
      return "INTERNAL", "no hay GateMonitor: los gates quedan en rojo"
    if self._gates_rancios():
      # Se vuelve a mirar AQUI porque esta funcion tambien la llama _ejecutar(), y entre
      # aceptar y ejecutar el hilo del plano se puede haber caido.
      return "INTERNAL", "el evaluador de gates no publica datos frescos: gates en rojo"
    try:
      ok, fallados = self.gates.evaluate(spec, args)
    except Exception:
      cloudlog.exception("[Orbit] fallo evaluando gates")
      return "INTERNAL", "fallo evaluando gates"
    if ok:
      return None, ""
    fallados = self._sin_link_fresh_rancio(fallados)
    if not fallados:
      return None, ""
    # El primer gate rojo da el codigo; el detalle lleva todos, para que la app pueda
    # decir exactamente que falta en vez de "no se puede".
    return gate_reason(fallados[0]), "gates en rojo: " + ", ".join(g.name for g in fallados)

  def _sin_link_fresh_rancio(self, fallados: list) -> list:
    """Quita LINK_FRESH de los gates rojos si el enlace esta fresco AHORA MISMO.

    POR QUE EXISTE ESTO. El bit LINK_FRESH de la mascara lo calcula GateMonitor.update(),
    que corre en el hilo del plano a 10 Hz. handle_payload llama a note_link() y evalua
    los gates EN EL MISMO INSTANTE, con una mascara de hasta 100 ms antes: en el PRIMER
    mando de una sesion -- cuando aun no habia habido trafico -- el bit vale 0 y el ACK
    salia GATE_LINK_FRESH. Fail-closed, si, pero es un boton que falla siempre la primera
    vez y funciona la segunda, que es la peor clase de fallo para el usuario.
    No se relaja ninguna precondicion: se lee la MISMA fuente que alimenta el bit
    (el instante del ultimo trafico valido que guarda el GateMonitor) sin esperar al
    siguiente tick. Si el enlace no esta fresco de verdad, el gate sigue rojo.
    """
    if Gate.LINK_FRESH not in fallados:
      return fallados
    gates = self.gates
    if gates is None:
      return fallados
    try:
      edad = float(gates.link_age_s)
    except Exception:
      return fallados
    # La ventana tiene que ser la que el propio monitor tiene CONFIGURADA, no una copia:
    # los tests construyen GateMonitor(link_fresh_max_s=0.0) para comprobar que el gate
    # caduca, y usar aqui la constante del modulo lo reabriria a su espalda.
    ventana = getattr(gates, "_link_fresh_max_s", None)
    if ventana is None:
      ventana = LINK_FRESH_MAX_S
    if edad >= float(ventana):
      return fallados
    return [g for g in fallados if g is not Gate.LINK_FRESH]

  def _comprobar_ritmo(self, spec, args, aplicar: bool) -> tuple[str | None, str]:
    """Presupuesto acumulado por ventana (seccion 6: +-20 km/h por minuto en cruise_delta).

    El tope por orden no sirve de nada si se pueden encadenar veinte ordenes de +5 en dos
    segundos.
    """
    regla = (spec.limits or {}).get("rate")
    if not regla:
      return None, ""
    campo = regla.get("campo")
    presupuesto = float(regla.get("presupuesto", 0.0))
    ventana = float(regla.get("ventana_s", 60.0))
    if campo is None or presupuesto <= 0:
      return None, ""
    cantidad = abs(float(args.get(campo, 0.0) or 0.0))

    ahora = ahora_mono()
    with self._lock:
      historial = self._ritmo.setdefault(spec.verb, deque())
      while historial and (ahora - historial[0][0]) > ventana:
        historial.popleft()
      acumulado = sum(c for _, c in historial)
      if acumulado + cantidad > presupuesto:
        return "RANGE", f"presupuesto de '{campo}' agotado: {acumulado:g}+{cantidad:g} > {presupuesto:g} en {ventana:g} s"
      if aplicar:
        historial.append((ahora, cantidad))
    return None, ""

  # ------------------------------------------------------ eco de la publicacion dual

  @staticmethod
  def _clave_args(args: dict) -> str:
    """Huella estable de unos argumentos ya normalizados. Sin JSON: no hace falta y
    serializar en el hilo de red por cada mando es trabajo que no se paga."""
    return "|".join(f"{k}={args[k]!r}" for k in sorted(args))

  def _eco_de_otro_camino(self, verb: str, args: dict, fuente: str) -> str | None:
    """Fuente del gemelo si esta orden ya llego por OTRO camino hace menos de ECO_VENTANA_S.

    Solo cruza caminos distintos. Dos pulsaciones reales del usuario viajan siempre por
    el mismo camino, asi que esto no puede tragarse una orden legitima repetida.
    """
    clave = self._clave_args(args)
    ahora = ahora_mono()
    with self._lock:
      previo = self._eco.get(verb)
    if previo is None:
      return None
    clave_previa, cuando, fuente_previa = previo
    if fuente_previa == fuente or clave_previa != clave:
      return None
    if (ahora - cuando) > ECO_VENTANA_S:
      return None
    return fuente_previa

  def _anotar_eco(self, verb: str, args: dict, fuente: str) -> None:
    with self._lock:
      if len(self._eco) > 64:
        self._eco.clear()
      self._eco[verb] = (self._clave_args(args), ahora_mono(), fuente)

  def _ya_visto(self, cmd_id: str) -> bool:
    with self._lock:
      return cmd_id in self._vistos

  def _recordar_id(self, cmd_id: str) -> None:
    with self._lock:
      self._vistos[cmd_id] = ahora_mono()
      self._vistos.move_to_end(cmd_id)
      while len(self._vistos) > self._lru_max:
        self._vistos.popitem(last=False)

  def _rechazo(self, cmd_id: str, verb: str, reason: str, detail: str, phase: str = Phase.REJECTED) -> Resultado:
    self._log(f"rechazo:{reason}", f"[Orbit] mando rechazado verb={verb} reason={reason} detail={detail}")
    self._ack(cmd_id, phase, reason, detail, verb=verb)
    return Resultado(aceptado=False, reason=reason, detail=detail, cmd_id=cmd_id, verb=verb, phase=phase)

  def _encolar(self, cmd: Comando) -> Resultado:
    try:
      self._cola.put_nowait(cmd)
    except queue.Full:
      # No se bloquea el hilo de red bajo ningun concepto (seccion 12: saturar el broker).
      return self._rechazo(cmd.cmd_id, cmd.verb, "BUSY", "cola de comandos llena")
    self._ack(cmd.cmd_id, Phase.ACCEPTED, "OK", verb=cmd.verb)
    return Resultado(aceptado=True, reason="OK", cmd_id=cmd.cmd_id, verb=cmd.verb, phase=Phase.ACCEPTED)

  # -------------------------------------------------------------------- ejecucion

  # ------------------------------------------------- RESULTADO DEL CONSUMIDOR

  # Margen que se le da al consumidor por encima del TTL del verbo antes de declarar que
  # no contesto. El consumidor decide en su propio ciclo (desire_helper corre a 20 Hz
  # dentro de modeld), asi que el TTL pelado se quedaria corto por unos milisegundos.
  MARGEN_RESULTADO_S = 2.0
  # Cadencia del sondeo. Solo se lee el disco si hay algo pendiente, y entonces a 50 Hz: la
  # app solo pinta una fase intermedia si dura 700 ms (kRetencionIntermedia), y los 0-100 ms
  # que anadia el sondeo a 10 Hz eran margen que ya no quedaba para el jitter de red antes
  # de que "En curso" asomara entre "Enviando" y "Hecho". En reposo no se toca el disco.
  PERIODO_RESULTADO_S = 0.1
  PERIODO_RESULTADO_PENDIENTE_S = 0.02
  # Plazo que se concede a una maniobra YA EMPEZADA para terminar. En cuanto el consumidor
  # anuncia una fase intermedia (executing: "maniobra iniciada") la orden deja de estar
  # limitada por el TTL del sobre -- el TTL dice cuanto vale la orden EN VUELO, no cuanto
  # dura la maniobra -- y pasa a estarlo por esto. 15 s cubre LANE_CHANGE_TIME_MAX (10 s)
  # mas el fundido de salida y el margen del consumidor.
  #
  # POR QUE HACE FALTA. Medido en el coche (command_log del backend): un cambio de carril
  # con todo en verde cerraba como `failed/NO_RESULT`, y el unico que llego a cerrar con
  # el veredicto del consumidor lo hizo como `failed` con reason `OK` y detail "maniobra
  # iniciada". Dos fallos del mismo camino: (1) el consumidor firma el veredicto con el id
  # que ve en el plano de estado, y el plano lo BORRABA en cuanto el handler retornaba, asi
  # que el veredicto llegaba sin id y se tiraba; (2) el primer veredicto que llegaba
  # (`executing`) se trataba como terminal y como fallo.
  MARGEN_EJECUCION_S = 15.0

  # Fases que un consumidor puede anunciar SIN cerrar la orden, y las que la cierran.
  _FASES_INTERMEDIAS = (Phase.RECEIVED, Phase.ACCEPTED, Phase.EXECUTING)
  _FASES_TERMINALES = (Phase.APPLIED, Phase.REJECTED, Phase.FAILED, Phase.EXPIRED, Phase.SUPERSEDED)

  def _registrar_pendiente(self, cmd) -> None:
    """Deja el comando esperando el veredicto de su consumidor."""
    ttl_s = (cmd.ttl_ms or 3_000) / 1000.0
    limite = ahora_mono() + ttl_s + self.MARGEN_RESULTADO_S
    with self._lock:
      self._pendientes[cmd.cmd_id] = (cmd.verb, limite)

  def _params_resultado(self):
    """Params perezoso: importar arriba ataria los tests a un /data/params real."""
    if self._params_res is None:
      from openpilot.common.params import Params
      self._params_res = Params()
    return self._params_res

  def _consumir_resultado(self) -> dict | None:
    """Lee y BORRA OrbitCmdResult. Devuelve el JSON o None."""
    try:
      params = self._params_resultado()
      crudo = params.get("OrbitCmdResult")
      if not crudo:
        return None
      # Se consume: si no, el mismo veredicto cerraria tambien el comando siguiente.
      params.remove("OrbitCmdResult")
      if isinstance(crudo, bytes):
        crudo = crudo.decode("utf-8", "replace")
      dato = json.loads(crudo)
      return dato if isinstance(dato, dict) else None
    except Exception:
      self._log("resultado_lectura", "[Orbit] no se pudo leer OrbitCmdResult")
      return None

  def _bucle_resultados(self) -> None:
    """Cierra el ACK de los verbos cuyo veredicto lo da un consumidor de otro proceso.

    Sin este hilo, `cierra_consumidor` dejaria los comandos colgados en EXECUTING para
    siempre: el escritor de OrbitCmdResult existia y NO tenia lector.
    """
    while not self._stop.is_set():
      hay = False
      try:
        with self._lock:
          hay = bool(self._pendientes)
        if hay:
          dato = self._consumir_resultado()
          if dato is not None:
            self._cerrar_con_resultado(dato)
          self._caducar_pendientes()
      except Exception:
        cloudlog.exception("[Orbit] bucle de resultados")
      self._stop.wait(self.PERIODO_RESULTADO_PENDIENTE_S if hay else self.PERIODO_RESULTADO_S)

  def _correlacionar_pendiente(self, cmd_id: str, verbo: str) -> str | None:
    """Clave del pendiente al que pertenece un veredicto. Se llama CON el lock cogido.

    Con `id` se exige que sea uno de los nuestros: un id ajeno (otra sesion, un comando ya
    caducado) NO se correlaciona por verbo, porque seria cerrar nuestra orden con el
    veredicto de otra. SIN `id` se correlaciona por verbo con el pendiente mas antiguo de
    ese verbo: es el caso real del consumidor que leyo el flag antes de ver el cmdId en el
    plano de estado (desire_helper corre a 20 Hz y el plano publica a 10 Hz), y el de
    cruise_delta, que firma siempre sin id (spec.conserva_plano). lane_change no se
    encadena; cruise_delta si puede tener dos pulsaciones pendientes (dos moviles: la app no
    manda la segunda hasta cerrar la primera), y su consumidor las decide en el orden en que
    llegaron sus flags: con el mismo TTL, el limite mas cercano es el pendiente mas antiguo.
    """
    if cmd_id:
      return cmd_id if cmd_id in self._pendientes else None
    if not verbo:
      return None
    candidatos = [(limite, cid) for cid, (v, limite) in self._pendientes.items() if v == verbo]
    if not candidatos:
      return None
    return min(candidatos)[1]

  def _liberar_plano(self, cmd_id: str) -> None:
    """Cierra en el plano de estado el comando `cmd_id` SI sigue siendo el activo.

    Los verbos con spec.conserva_plano mantienen activeVerb/cmdId en el plano hasta que
    llega su veredicto (o vence el margen): es de ahi de donde el consumidor lee el id con
    el que firma. Si mientras tanto empezo otro comando, el plano ya es suyo y no se toca.
    Para los demas es un no-op: su handler ya cerro el plano al retornar.
    """
    if self.store is None:
      return
    try:
      if self.store.snapshot().get("cmd_id") == cmd_id:
        self.store.end_command()
    except Exception:
      pass

  def _cerrar_con_resultado(self, dato: dict) -> None:
    cmd_id = str(dato.get("id") or "").strip()
    verbo_dato = str(dato.get("verb") or "").strip()
    fase = str(dato.get("phase") or "").strip().lower()
    motivo = str(dato.get("reason") or "").strip()
    detalle = str(dato.get("detail") or "")[:512]
    terminal = fase in self._FASES_TERMINALES
    if not terminal and fase not in self._FASES_INTERMEDIAS:
      return  # fase desconocida: no se inventa un ACK
    with self._lock:
      clave = self._correlacionar_pendiente(cmd_id, verbo_dato)
      if clave is None:
        # Veredicto de un comando que ya caduco, o de otra sesion. No se inventa un ACK.
        return
      verbo = self._pendientes[clave][0]
      if terminal:
        self._pendientes.pop(clave, None)
      else:
        # La maniobra esta en marcha: el plazo pasa a ser el de la EJECUCION, no el del sobre.
        self._pendientes[clave] = (verbo, ahora_mono() + self.MARGEN_EJECUCION_S)
    if not terminal:
      self._ack(clave, fase, "OK" if fase == Phase.ACCEPTED else "", detalle, verb=verbo)
      # El consumidor ya ha visto el comando (firmo con su id o el router lo correlaciono
      # por verbo): el plano deja de anunciarlo como activo para no dar BUSY a los demas
      # consumidores durante la maniobra (OrbitAuthority.busy_with_other).
      self._liberar_plano(clave)
      return
    if fase == Phase.APPLIED and motivo in ("", "OK"):
      self._ack(clave, Phase.APPLIED, "OK", detalle, verb=verbo)
    elif fase in (Phase.EXPIRED, Phase.SUPERSEDED):
      self._ack(clave, fase, motivo or fase.upper(), detalle, verb=verbo)
    else:
      # rejected / failed del consumidor, o un "applied" con motivo de error (contradiccion:
      # fail-closed). Ya se publico EXECUTING, asi que el terminal es FAILED (seccion 3.3):
      # la app trata ambos como fallo y el motivo real viaja en `reason`.
      self._ack(clave, Phase.FAILED, motivo or "INTERNAL", detalle, verb=verbo)
    self._liberar_plano(clave)

  def _caducar_pendientes(self) -> None:
    ahora = ahora_mono()
    with self._lock:
      vencidos = [(cid, v) for cid, (v, lim) in self._pendientes.items() if ahora > lim]
      for cid, _ in vencidos:
        self._pendientes.pop(cid, None)
    for cid, verbo in vencidos:
      # El consumidor no contesto. NO se declara aplicado: lo unico cierto es que se dejo
      # un flag en disco y nadie confirmo que se ejecutara.
      self._ack(cid, Phase.FAILED, "NO_RESULT",
                "el consumidor no confirmo la maniobra dentro del plazo", verb=verbo)
      self._liberar_plano(cid)

  def _bucle_worker(self) -> None:
    while not self._stop.is_set():
      try:
        cmd = self._cola.get(timeout=0.2)
      except queue.Empty:
        continue
      try:
        self._ejecutar(cmd)
      except Exception:
        cloudlog.exception("[Orbit] excepcion ejecutando un comando")
      finally:
        self._cola.task_done()

  def drenar(self, limite: int = 0) -> int:
    """Ejecuta lo que haya en la cola en el hilo actual. Para tests y para arranques sin
    worker; en produccion se usa start()."""
    hechos = 0
    while limite <= 0 or hechos < limite:
      try:
        cmd = self._cola.get_nowait()
      except queue.Empty:
        break
      try:
        self._ejecutar(cmd)
      finally:
        self._cola.task_done()
      hechos += 1
    return hechos

  def _deadline_efectivo(self, cmd: Comando) -> float:
    """Ventana de actuador que se va a escribir en el plano de estado.

    EL PLANO TIENE UN SOLO `deadline_mono` PARA TODO EL SUBSISTEMA, y `begin_command` lo
    SOBREESCRIBE con el del verbo entrante. Eso significa que la ventana que abre un
    verbo la hereda cualquier consumidor que solo mire `deadlineMono`: tras un
    `torque_mode` habia minutos de ventana para todo lo demas, y al reves, un verbo corto
    recortaba la ventana de uno largo. Ninguna de las dos direcciones es lo que pidio
    nadie.

    Mitigacion mientras el deadline siga siendo unico: la ventana de un verbo NUNCA puede
    ser mas larga que la que ya estuviera abierta para OTRO verbo. Renovar el MISMO verbo
    si la refresca -- si no, un mando de banco renovado cada pocos segundos se apagaria
    solo al llegar al deadline del primero y no habria forma de mantenerlo.

    Es fail-safe en la unica direccion que el contrato permite: recortar la ventana ajena
    BAJA autoridad, y bajar autoridad siempre se acepta (seccion 2). Lo que no puede pasar
    es lo contrario -- que un verbo disfrute de una ventana mas larga que su propio TTL.

    EL ARREGLO DEFINITIVO ES UN DEADLINE POR VERBO en el plano de estado
    (orbit/command_state.py + struct OrbitCommandState de cereal/custom.capnp) y no cabe
    en este fichero: aqui no se puede publicar mas que un numero.
    """
    nuevo = float(cmd.deadline_mono)
    if self.store is None or self._ventana_verb in ("", cmd.verb):
      return nuevo
    try:
      vigente = float(self.store.snapshot().get("deadline_mono") or 0.0)
    except Exception:
      return nuevo
    if vigente <= ahora_mono():
      return nuevo      # no hay ventana viva de nadie: la de este verbo manda entera
    return min(nuevo, vigente)

  def _ejecutar(self, cmd: Comando) -> None:
    spec = cmd.spec

    # Reevaluacion TARDIA (defensa en profundidad, seccion 4.2): entre aceptar y ejecutar
    # pueden pasar el TTL entero, un desenganche, una pisada de freno o la caducidad del
    # armado de banco. El GateMonitor filtra pronto; aqui se decide tarde.
    if not spec.baja_autoridad:
      if cmd.caducado:
        self._ack(cmd.cmd_id, Phase.EXPIRED, "EXPIRED", "el TTL vencio en la cola", verb=cmd.verb)
        return
      reason, detail = self._comprobar_modo(spec, None)
      if reason is None:
        reason, detail = self._comprobar_gates(spec, cmd.args)
      if reason is not None:
        self._ack(cmd.cmd_id, Phase.REJECTED, reason, f"revalidacion antes de ejecutar: {detail}", verb=cmd.verb)
        return

    with self._lock:
      handler = self._handlers.get(cmd.verb)
    if handler is None:
      self._ack(cmd.cmd_id, Phase.REJECTED, "UNSUPPORTED_VERB", "el handler desaparecio", verb=cmd.verb)
      return

    if self.store is not None:
      try:
        # Solo los verbos que ABREN una ventana de actuador escriben el deadman. Ver
        # CommandSpec.arma_actuador: con un unico deadline en el plano, un verbo inocuo
        # de TTL largo alargaria la ventana del actuador que estuviera vivo.
        arma = getattr(spec, "arma_actuador", True)
        self.store.begin_command(cmd.verb, cmd.cmd_id, cmd.seq,
                                 self._deadline_efectivo(cmd) if arma else None)
        if arma:
          self._ventana_verb = cmd.verb
      except Exception:
        pass

    self._ack(cmd.cmd_id, Phase.EXECUTING, verb=cmd.verb)
    try:
      handler(cmd)
    except Exception as e:
      cloudlog.exception(f"[Orbit] handler de '{cmd.verb}' fallo")
      self._ack(cmd.cmd_id, Phase.FAILED, "INTERNAL", f"{type(e).__name__}: {e}", verb=cmd.verb)
      if self.store is not None:
        try:
          self.store.end_command()
        except Exception:
          pass
      return

    cierra_consumidor = bool(getattr(spec, "cierra_consumidor", False))
    if cierra_consumidor:
      # El handler solo dejo el flag en disco. Quien decide es el consumidor, y puede
      # rechazarlo: anunciar APPLIED aqui seria pintar un "Hecho" en la app por una
      # maniobra que quiza no ocurra nunca. Se queda en EXECUTING hasta que llegue
      # OrbitCmdResult, o hasta que venza el margen.
      self._registrar_pendiente(cmd)
    else:
      self._ack(cmd.cmd_id, Phase.APPLIED, "OK", verb=cmd.verb)
    if self.store is not None:
      try:
        # disarm_all deja el plano de estado en neutro AHORA (deadline a 0). El resto de
        # verbos conservan su deadline: el actuador puede seguir vivo hasta el deadman
        # aunque el handler ya haya vuelto (un hold de 1500 ms, por ejemplo). Quien lo
        # apaga al vencer es CommandStateStore.expire_if_due() en el tick de 10 Hz.
        if spec.baja_autoridad:
          self.store.clear_actuators()
          # La ventana se cerro entera: el siguiente verbo que arme actuador abre la suya
          # completa y no queda recortado por el deadline de un verbo ya desarmado.
          self._ventana_verb = ""
        elif not (cierra_consumidor and getattr(spec, "conserva_plano", False)):
          self.store.end_command()
        # Los verbos con conserva_plano (lane_change) MANTIENEN activeVerb/cmdId en el
        # plano: el consumidor firma su veredicto con ese id (desire_helper.
        # _orbit_consumir_flag) y con el borrado inmediato el veredicto llegaba sin id y
        # acababa en NO_RESULT. Lo cierra _liberar_plano al llegar el veredicto o al vencer
        # el margen. cruise_delta NO: mientras el plano anuncia un verbo, controlsd ve BUSY
        # en cada ciclo de un assisted_decel en marcha y lo suelta a neutro.
      except Exception:
        pass
