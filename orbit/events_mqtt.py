#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import time
from datetime import datetime, UTC
import threading
from typing import Optional, Dict

import paho.mqtt.client as mqtt

from openpilot.common.params import Params
from openpilot.orbit import config_broker, telemetria_grupos as grupos

try:
  from openpilot.common.swaglog import cloudlog
except ImportError:  # herramientas sueltas / PC sin zmq
  import logging
  cloudlog = logging.getLogger("orbit.events_mqtt")


# Cooldown en segundos antes de re-enviar el mismo evento
EVENT_COOLDOWN_SECONDS = 12  # 12 segundos (entre 10-15 como se discutió)

# Cooldown más largo para eventos críticos de "TAKE CONTROL" para evitar spam
TAKE_CONTROL_COOLDOWN_SECONDS = 30  # 30 segundos para eventos de "TAKE CONTROL"

# Diccionario para trackear último envío de cada evento (basado en alert_type)
_event_last_sent: Dict[str, float] = {}  # alert_type -> timestamp último envío
_MAX_EVENT_HISTORY = 50  # Máximo de eventos en el historial para evitar crecimiento indefinido

# Cliente MQTT persistente
_mqtt_client: Optional[mqtt.Client] = None
_mqtt_connected = False
_mqtt_client_lock = threading.Lock()
_mqtt_broker: Optional[str] = None
_mqtt_port: Optional[int] = None
_mqtt_username: Optional[str] = None
_mqtt_password: Optional[str] = None


# Caché de la config del broker: _ensure_mqtt_client (que llama a _load_broker)
# se ejecuta en el hilo RT de selfdrived a 100 Hz por cada alerta que pasa el
# filtro, y leer+parsear el JSON en cada llamada metía I/O de disco en el loop
# de tiempo real (patrón commIssue). Con TTL de 5 s los cambios de config se
# siguen recogiendo casi en tiempo real sin tocar disco a 100 Hz.
_BROKER_CFG_TTL_S = 5.0
_broker_cfg_cache: Optional[tuple[str, int, Optional[str], Optional[str]]] = None
_broker_cfg_cache_ts = 0.0


def _load_broker() -> tuple[str, int, Optional[str], Optional[str]]:
  global _broker_cfg_cache, _broker_cfg_cache_ts
  now = time.monotonic()
  if _broker_cfg_cache is not None and (now - _broker_cfg_cache_ts) < _BROKER_CFG_TTL_S:
    return _broker_cfg_cache

  # Plantilla del arbol + lo persistido en /data por encima (orbit/config_broker.py):
  # la IP de la pantalla no vuelve a "" con cada OTA. Sigue siendo disco -> sigue cacheado.
  try:
    cfg = config_broker.leer_config()
    broker = cfg.get("broker", "localhost")
    port = int(cfg.get("broker_port", 1883))
    # Credenciales MQTT opcionales (broker con auth). Vacio/ausente = anonimo.
    username = (cfg.get("username") or "").strip() or None
    password = cfg.get("password") or None
    result = (broker, port, username, password)
  except Exception:
    result = ("localhost", 1883, None, None)

  _broker_cfg_cache = result
  _broker_cfg_cache_ts = now
  return result


_dongle_id_cache: Optional[str] = None


def _get_dongle_id() -> str:
  # El DongleId no cambia durante una sesión; cacheamos para no construir un Params()
  # ni leer disco en cada alerta (send_alert se llama por ciclo de selfdrived a 100 Hz).
  global _dongle_id_cache
  if _dongle_id_cache is None:
    raw = Params().get("DongleId")
    _dongle_id_cache = raw if raw else "UnregisteredDevice"
  return _dongle_id_cache


def _on_mqtt_connect(client, userdata, flags, rc):
  global _mqtt_connected
  _mqtt_connected = (rc == 0)


def _on_mqtt_disconnect(client, userdata, rc):
  global _mqtt_connected
  _mqtt_connected = False


def _ensure_mqtt_client():
  """Inicializa (o reutiliza) un cliente MQTT persistente."""
  # _mqtt_connected debe estar en la declaracion global: sin ella, el
  # `_mqtt_connected = False` del except creaba una variable LOCAL muerta
  # y el flag global quedaba sin resetear.
  global _mqtt_client, _mqtt_connected, _mqtt_broker, _mqtt_port, _mqtt_username, _mqtt_password

  broker, port, username, password = _load_broker()

  # Sin broker configurado (plantilla de fabrica, "broker": "") no hay nada que abrir:
  # paho lanzaria 'Invalid host.' y, como el cliente quedaba a None, CADA alerta que
  # pasara el filtro construia un Client nuevo para volver a fallar. Se avisa UNA vez
  # (por valor) y se espera a que la pantalla escriba una IP; _load_broker la recoge.
  if not (broker or "").strip():
    _avisar_una_vez("sin_broker", "[Orbit] events_mqtt: sin broker configurado, los eventos no salen")
    return None

  with _mqtt_client_lock:
    needs_reinit = (
      _mqtt_client is None or
      broker != _mqtt_broker or
      port != _mqtt_port or
      username != _mqtt_username or
      password != _mqtt_password
    )

    if needs_reinit:
      try:
        if _mqtt_client is not None:
          _mqtt_client.loop_stop()
          _mqtt_client.disconnect()
      except Exception:
        pass

      try:
        client = mqtt.Client()
        client.max_queued_messages_set(1)  # como maximo un mensaje QoS>0 pendiente
        client.on_connect = _on_mqtt_connect
        client.on_disconnect = _on_mqtt_disconnect
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        # Credenciales MQTT opcionales (broker con auth). None = anonimo.
        if username:
          client.username_pw_set(username, password)
        # Publicar el estado inicial ANTES de arrancar el hilo. loop_start puede
        # ejecutar on_connect inmediatamente; escribir False despues pisaba ese
        # True y dejaba una conexion viva descartando eventos indefinidamente.
        _mqtt_connected = False
        _mqtt_broker = broker
        _mqtt_port = port
        _mqtt_username = username
        _mqtt_password = password
        _mqtt_client = client
        client.connect_async(broker, port, keepalive=60)
        client.loop_start()
        cloudlog.warning(f"[Orbit] events_mqtt: conectando a {broker}:{port}")
      except Exception as e:
        # Antes este fallo era mudo: el cliente quedaba a None y nadie se enteraba de
        # que los eventos no salian. Una linea por valor de broker, no una por alerta.
        _avisar_una_vez(f"fallo:{broker}:{port}", f"[Orbit] events_mqtt: no se pudo abrir el cliente MQTT hacia {broker}:{port}: {e}")
        _mqtt_connected = False
        _mqtt_client = None

  return _mqtt_client


_avisos_dados: set = set()


def _avisar_una_vez(clave: str, texto: str) -> None:
  """Un aviso por clave y proceso: esto corre en el hilo de 100 Hz de selfdrived y un
  fallo persistente (sin broker, broker inalcanzable) no puede convertirse en un log
  por ciclo."""
  if clave in _avisos_dados:
    return
  _avisos_dados.add(clave)
  try:
    cloudlog.warning(texto)
  except Exception:
    pass


def warmup() -> None:
  """Abre el cliente MQTT por adelantado (llamar UNA vez, fuera del bucle de 100 Hz).

  connect_async es asincrono: sin esto, la PRIMERA alerta que pasa el filtro es la que
  crea el cliente y se pierde siempre (send_event_full exige conexion viva), y con ella
  cualquier otra del mismo ciclo. Con el cliente abierto desde el arranque de selfdrived,
  la primera alerta real del viaje ya sale. Nunca lanza."""
  try:
    _ensure_mqtt_client()
  except Exception:
    pass


def mirror_alerts(alerts, prev_types: frozenset) -> frozenset:
  """Espejo MQTT de las alertas de un ciclo de selfdrived (100 Hz).

  Devuelve el conjunto de `alert_type` vivos. Solo se publica cuando ese conjunto CAMBIA
  respecto a `prev_types`; el resto de ciclos esto es una comprension y una comparacion
  de frozensets, sin I/O ni locks. El filtro de relevancia y el cooldown por alert_type
  siguen viviendo en send_event_full.

  Esta logica estaba inline en selfdrived y llevaba MUERTA desde el 20 de agosto de 2026
  (commit 5c028bc2a): hacia `frozenset(...).discard("")`, que es un AttributeError
  (frozenset es inmutable, no tiene discard), dentro de un `except Exception: pass`. Ni
  una alerta salio por MQTT desde entonces, sin una linea de log. Aqui el conjunto se
  construye ya sin vacios, y el llamante registra la excepcion en vez de tragarsela.
  """
  tipos = frozenset(t for t in (getattr(a, "alert_type", "") or "" for a in alerts) if t)
  if tipos == prev_types:
    return prev_types
  enviadas = True
  for a in alerts:
    if getattr(a, "alert_type", ""):
      enviadas = send_alert(a) and enviadas
  # connect_async tarda: si aun no habia enlace no memorizamos el conjunto.
  # El siguiente ciclo vuelve a intentar exactamente las mismas alertas y las
  # publica en cuanto on_connect confirme la conexion.
  return tipos if enviadas else prev_types


def _should_filter_event(title: str, message: str, priority: int, event_name: Optional[str], alert_type: Optional[str]) -> bool:
  """Filtra eventos según criterios específicos para reducir saturación MQTT.

  Solo se envían eventos que cumplan AL MENOS UNA de estas condiciones:
  1. Título contiene "TAKE CONTROL" o "Take Control"
  2. Título contiene "BRAKE" o "Pay Attention"
  3. Evento relacionado con cambio de carril (event_name contiene "laneChange")
  4. Prioridad >= 3 (MID, HIGH, HIGHEST)

  Args:
    title: Título del evento
    message: Mensaje del evento
    priority: Prioridad del evento (0-5)
    event_name: Nombre del evento (ej: "laneChange")
    alert_type: Tipo completo de alerta (ej: "laneChange/warning")

  Returns:
    True si el evento debe enviarse, False si debe filtrarse
  """
  # Normalizar strings para comparación case-insensitive
  title_upper = (title or "").upper()
  message_upper = (message or "").upper()
  event_name_lower = (event_name or "").lower()
  alert_type_lower = (alert_type or "").lower()

  # 1. Verificar eventos de "TAKE CONTROL"
  if "TAKE CONTROL" in title_upper:
    return True

  # 2. Verificar eventos de "BRAKE" o "Pay Attention"
  if "BRAKE" in title_upper or "PAY ATTENTION" in title_upper:
    return True

  # 3. Verificar eventos de cambio de carril
  if "lanechange" in event_name_lower or "lanechange" in alert_type_lower:
    return True

  # 4. Verificar prioridad >= 3 (MID, HIGH, HIGHEST)
  if priority >= 3:
    return True

  # Si no cumple ninguna condición, filtrar el evento
  return False


def _should_send_event(alert_type: str, title: Optional[str] = None) -> bool:
  """Verifica si se debe enviar un evento basado en el cooldown.

  Los eventos de "TAKE CONTROL" tienen un cooldown más largo para evitar spam.

  Args:
    alert_type: Tipo de alerta (ej: "controlsLagging/warning")
    title: Título del evento (opcional, para detectar eventos de "TAKE CONTROL")

  Returns:
    True si se debe enviar, False si está en cooldown
  """
  if not alert_type:
    return False

  current_time = time.time()
  last_sent = _event_last_sent.get(alert_type, 0)

  # Determinar el cooldown apropiado según el tipo de evento
  title_upper = (title or "").upper()
  is_take_control = "TAKE CONTROL" in title_upper

  # Usar cooldown más largo para eventos de "TAKE CONTROL"
  cooldown_seconds = TAKE_CONTROL_COOLDOWN_SECONDS if is_take_control else EVENT_COOLDOWN_SECONDS

  # Si nunca se ha enviado o ha pasado el cooldown, permitir envío
  if last_sent == 0 or (current_time - last_sent) >= cooldown_seconds:
    _event_last_sent[alert_type] = current_time

    # Limpiar eventos antiguos para evitar crecimiento indefinido de memoria
    if len(_event_last_sent) > _MAX_EVENT_HISTORY:
      # Eliminar los eventos más antiguos (más de 1 hora)
      cutoff_time = current_time - 3600
      keys_to_remove = [k for k, v in _event_last_sent.items() if v < cutoff_time]
      for k in keys_to_remove:
        del _event_last_sent[k]
    return True

  return False


def send_event_full(title: str,
                    message: str,
                    priority: int,
                    dongle_id: Optional[str] = None,
                    event_name: Optional[str] = None,
                    event_type: Optional[str] = None,
                    alert_type: Optional[str] = None) -> bool:
  """Envía un evento completo por MQTT con toda su información.

  Args:
    title: Título del evento
    message: Mensaje del evento
    priority: Prioridad del evento (0-5)
    dongle_id: ID del dispositivo (opcional)
    event_name: Nombre del evento (ej: "controlsLagging")
    event_type: Tipo del evento (ej: "warning")
    alert_type: Tipo completo de alerta (ej: "controlsLagging/warning")
  """
  did = dongle_id or _get_dongle_id()
  topic = f"telemetry_mqtt/{did}/event"

  # Algunas alertas de sistema (p. ej. engagement) no tienen texto visible.
  # El nombre de maquina sigue siendo un evento valido y evita tarjetas vacias.
  title_stripped = (title or "").strip()
  message_stripped = (message or "").strip()
  if not title_stripped and not message_stripped:
    title = (event_name or (alert_type or "").split("/", 1)[0] or "evento").strip()

  # Construir alert_type si no se proporciona
  if not alert_type:
    if event_name and event_type:
      alert_type = f"{event_name}/{event_type}"
    elif event_name:
      alert_type = event_name
    else:
      alert_type = "unknown/unknown"

  # Submenu ORBIT > Telemetria: con el grupo "Eventos" apagado no se publica. Devuelve True
  # (atendido) y no False, o mirror_alerts lo reintentaria en cada ciclo de selfdrived.
  if not grupos.activo(Params(), grupos.param_v2("event")):
    return True

  client = _ensure_mqtt_client()

  # Si no hay cliente o la conexión aún no está establecida, no enviar para
  # evitar colas. IMPORTANTE: este check va ANTES de consumir el cooldown —
  # antes, un evento ocurrido sin conexión (siempre el primero tras arrancar,
  # porque connect_async es asíncrono) se perdía Y además suprimía los
  # reenvíos del mismo alert_type durante 12/30 s.
  if client is None or not _mqtt_connected:
    return False

  # Verificar cooldown antes de enviar (con cooldown extendido para "TAKE CONTROL")
  if not _should_send_event(alert_type, title=title):
    # Ya se entrego este mismo tipo hace unos segundos: cuenta como atendido.
    return True

  # Payload completo con toda la información del evento
  payload = {
    "dongle_id": did,
    "event_name": event_name or "",
    "event_type": event_type or "",
    "alert_type": alert_type,
    "title": title or "",
    "message": message or "",
    "priority": priority,
    # Mismo formato de cable que utcnow().isoformat()+"Z" (utcnow está deprecado en 3.12)
    "timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
  }

  try:
    # Publicar utilizando el cliente persistente (QoS 0 para máximo rendimiento)
    info = client.publish(topic, json.dumps(payload), qos=0)
    if getattr(info, "rc", mqtt.MQTT_ERR_SUCCESS) != mqtt.MQTT_ERR_SUCCESS:
      # La conexion puede caer entre el flag y publish(). No se consume el
      # cooldown: mirror_alerts debe reintentarlo tras el siguiente on_connect.
      _event_last_sent.pop(alert_type, None)
      return False
    return True
  except Exception:
    # Error silencioso para no afectar el loop de control
    _event_last_sent.pop(alert_type, None)
    return False


def send_alert(alert) -> bool:
  """Send an Events.Alert-like object (envía evento completo con cooldown).

  Expects attributes: alert_text_1, alert_text_2, priority, alert_type, event_name, event_type
  """
  try:
    # Obtener información del alert
    alert_type = getattr(alert, "alert_type", "") or ""
    title = getattr(alert, "alert_text_1", "") or ""
    message = getattr(alert, "alert_text_2", "") or ""
    priority = getattr(alert, "priority", 0)

    # Extraer event_name y event_type desde alert_type si está en formato "eventName/eventType"
    event_name = None
    event_type = None
    if alert_type and "/" in alert_type:
      parts = alert_type.split("/", 1)
      event_name = parts[0] if len(parts) > 0 else None
      event_type = parts[1] if len(parts) > 1 else None
    elif alert_type:
      event_name = alert_type

    # Validar que tenemos alert_type
    if not alert_type:
      return False

    # Enviar evento completo con cooldown
    return send_event_full(
      title=title,
      message=message,
      priority=priority,
      event_name=event_name,
      event_type=event_type,
      alert_type=alert_type
    )

  except Exception:
    # No re-lanzar para no afectar el loop de control
    return False