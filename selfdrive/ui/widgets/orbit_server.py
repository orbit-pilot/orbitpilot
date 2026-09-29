"""
ORBIT server reachability helpers.

Reads the Orbit server address through orbit/config_broker.py (template in the
tree + the user value persisted in /data, which survives OTA updates) and checks
whether it is reachable: a plain TCP connect to the MQTT broker (no MQTT
handshake) plus an HTTP GET to the backend /api/health endpoint.
Used by the home screen (live status) and the server-IP settings ("test") button.
"""
import socket
import threading
import urllib.request

from openpilot.orbit import config_broker

DEFAULT_BACKEND_PORT = 8010


def _read_config() -> dict:
  try:
    return config_broker.leer_config()
  except Exception:
    return {}


def read_broker() -> tuple[str, int]:
  """Return (ip, port) of the Orbit MQTT broker (template + persisted user value)."""
  data = _read_config()
  ip = data.get("broker") or ""
  try:
    port = int(data.get("broker_port", 1883) or 1883)
  except Exception:
    port = 1883
  return (ip if isinstance(ip, str) else ""), port


def read_backend_port() -> int:
  """Return the Orbit backend HTTP port (template + persisted user value)."""
  try:
    return int(_read_config().get("backend_port", DEFAULT_BACKEND_PORT) or DEFAULT_BACKEND_PORT)
  except Exception:
    return DEFAULT_BACKEND_PORT


def probe_server(ip: str, port: int, timeout: float = 2.0) -> bool:
  """True if a TCP connection to (ip, port) succeeds within `timeout`."""
  if not ip:
    return False
  try:
    with socket.create_connection((ip, int(port)), timeout=timeout):
      return True
  except Exception:
    return False


def probe_backend(ip: str, port: int, timeout: float = 2.0) -> bool:
  """True if GET http://ip:port/api/health answers within `timeout`."""
  if not ip:
    return False
  try:
    with urllib.request.urlopen(f"http://{ip}:{int(port)}/api/health", timeout=timeout) as resp:
      return 200 <= resp.status < 300
  except Exception:
    return False


def probar_servidor(ip: str, port: int, timeout: float = 3.0) -> tuple[bool, bool]:
  """(broker_ok, backend_ok): the "test connection" probes, shared by both UIs. Blocking."""
  return probe_server(ip, port, timeout=timeout), probe_backend(ip, read_backend_port(), timeout=timeout)


class ServerMonitor:
  """Background poller that keeps the current server IP and reachability
  (broker TCP + backend HTTP health)."""

  def __init__(self, interval: float = 5.0):
    self._interval = interval
    self._broker_ok = False
    self._backend_ok = False
    self._ip = ""
    self._lock = threading.Lock()
    self._stop = threading.Event()
    self._thread = threading.Thread(target=self._loop, name="orbit_server_monitor", daemon=True)
    self._thread.start()

  def _loop(self) -> None:
    while not self._stop.is_set():
      ip, port = read_broker()
      broker_ok = probe_server(ip, port)
      backend_ok = probe_backend(ip, read_backend_port())
      with self._lock:
        self._ip = ip
        self._broker_ok = broker_ok
        self._backend_ok = backend_ok
      if self._stop.wait(self._interval):
        break

  @property
  def broker_ok(self) -> bool:
    with self._lock:
      return self._broker_ok

  @property
  def backend_ok(self) -> bool:
    with self._lock:
      return self._backend_ok

  @property
  def connected(self) -> bool:
    with self._lock:
      return self._broker_ok

  @property
  def ip(self) -> str:
    with self._lock:
      return self._ip

  def stop(self) -> None:
    self._stop.set()
