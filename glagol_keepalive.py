"""
Долгоживущие WebSocket-подключения к колонкам по локальному протоколу Glagol (LAN).

Поток на станцию запускается только при непустых ``device_token`` и ``ip`` в БД.
Состояние соединения доступно для UI. Если задан ``glagol_linked_object`` (имя объекта osysHome),
поля снимка публикуются в **фиксированные** свойства этого объекта через ``updateProperty``
(см. ``publish_glagol_snap_to_object``). Команды из методов объектов — ``glagol_command``,
см. ``plugins/YandexDevices/docs/Commands.md``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any, Dict, Optional, Set, Tuple

from app.database import session_scope


def _is_recv_timeout(exc: BaseException) -> bool:
    n = type(exc).__name__
    if "Timeout" in n:
        return True
    s = str(exc).lower()
    return "timed out" in s or "tempo" in s


def _glagol_update_prop(ob: str, prop: str, value: Any, source: str) -> None:
    from app.core.lib.object import updateProperty

    if value is not None:
        updateProperty(f"{ob}.{prop}", value, source)


def publish_glagol_features_to_object(
    linked_object: str,
    features: Any,
    source: str,
) -> None:
    """Один раз за сессию: ``glagol_features`` — JSON-массив имён ``supported_features``."""
    ob = (linked_object or "").strip()
    if not ob or not isinstance(features, list):
        return
    names = sorted({str(x) for x in features if x})
    if names:
        _glagol_update_prop(ob, "glagol_features", json.dumps(names, ensure_ascii=False), source)


def publish_glagol_snap_to_object(
    linked_object: str,
    snap: Dict[str, Any],
    source: str,
) -> None:
    """
    Публикация основных полей снимка Glagol в объект osysHome.

    Свойства: ``state``, ``volume``, ``muted``, ``alice_state``,
    ``media_title``, ``media_subtitle``, ``media_duration``, ``media_progress``,
    ``media_cover_url``. Расширенные поля — отдельно, по мере необходимости.
    """
    ob = (linked_object or "").strip()
    if not ob:
        return

    playing = snap.get("playing")
    if playing is True:
        _glagol_update_prop(ob, "state", "playing", source)
    elif playing is False:
        _glagol_update_prop(ob, "state", "idle", source)

    _glagol_update_prop(ob, "volume", snap.get("volume"), source)
    if snap.get("muted") is not None:
        _glagol_update_prop(ob, "muted", bool(snap.get("muted")), source)

    alice = snap.get("alice_state")
    if alice is not None:
        _glagol_update_prop(ob, "alice_state", str(alice), source)

    pl = snap.get("player")
    if isinstance(pl, dict):
        _glagol_update_prop(ob, "media_title", pl.get("title"), source)
        _glagol_update_prop(ob, "media_subtitle", pl.get("subtitle"), source)
        _glagol_update_prop(ob, "media_duration", pl.get("duration_sec"), source)
        _glagol_update_prop(ob, "media_progress", pl.get("progress_sec"), source)
        cv = pl.get("cover_url")
        if cv:
            _glagol_update_prop(ob, "media_cover_url", str(cv), source)


class GlagolStationWorker(threading.Thread):
    """Фоновый поток: одно WS-подключение к колонке, приём кадров ``state``, публикация в объект."""

    def __init__(self, plugin: Any, station_id: int):
        super().__init__(name=f"GlagolWS_{station_id}", daemon=True)
        self._plugin = plugin
        self._station_id = int(station_id)
        self._stop = threading.Event()
        self._ws_lock = threading.Lock()
        self._ws: Any = None

        self._status_lock = threading.Lock()
        self._phase = "starting"
        self._status_text = "Старт…"
        self._detail: Optional[str] = None
        self._frames_rx = 0
        self._frames_tx = 0
        self._last_rx_at: Optional[float] = None
        self._connected_since: Optional[float] = None
        self._snap_lock = threading.Lock()
        self._last_ui_snap: Optional[Dict[str, Any]] = None
        self._last_ws_status_at: float = 0.0
        self._pending_lock = threading.Lock()
        self._pending: Dict[str, Dict[str, Any]] = {}

    def _bump_frames(self, *, rx: int = 0, tx: int = 0) -> None:
        if not rx and not tx:
            return
        with self._status_lock:
            if rx:
                self._frames_rx += rx
                self._last_rx_at = time.time()
            if tx:
                self._frames_tx += tx
        self._notify_ws_status()

    def _notify_ws_snapshot(self, ui: Dict[str, Any]) -> None:
        try:
            self._plugin.sendDataToWebsocket(
                "glagol_snapshot",
                {"station_id": self._station_id, **ui},
            )
        except Exception:
            pass

    def _notify_ws_status(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_ws_status_at < 1.5:
            return
        self._last_ws_status_at = now
        try:
            self._plugin.sendDataToWebsocket(
                "glagol_ws_status",
                {"station_id": self._station_id, **self.public_status()},
            )
        except Exception:
            pass

    def _store_ui_snap(self, snap: Dict[str, Any]) -> None:
        """Кэш последнего снимка для карточки «Музыка» в админке (без ``raw_state``)."""
        ui: Dict[str, Any] = {"ok": True, "source": "keepalive", "updated_at": time.time()}
        for key, val in snap.items():
            if key != "raw_state":
                ui[key] = val
        with self._snap_lock:
            self._last_ui_snap = ui
        self._notify_ws_snapshot(ui)

    def _clear_ui_snap(self) -> None:
        with self._snap_lock:
            self._last_ui_snap = None

    def get_last_snapshot(self) -> Optional[Dict[str, Any]]:
        with self._snap_lock:
            if self._last_ui_snap is None:
                return None
            return dict(self._last_ui_snap)

    def request_stop(self) -> None:
        self._stop.set()
        with self._pending_lock:
            for pend in self._pending.values():
                pend["response"] = None
                pend["error"] = "stopped"
                pend["event"].set()
            self._pending.clear()
        with self._ws_lock:
            w = self._ws
        if w:
            try:
                w.close()
            except Exception:
                pass

    def is_connected(self) -> bool:
        with self._status_lock:
            return self._phase == "connected"

    def send_payload(
        self,
        token: str,
        payload: Dict[str, Any],
        *,
        timeout: float = 15.0,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Команда по уже открытому фоновому WebSocket (без второго подключения к колонке).
        """
        from plugins.YandexDevices.glagol_local import READ_TIMEOUT_SEC, _ws_send_json

        if not self.is_connected():
            return None, "keepalive not connected"
        req_id = str(uuid.uuid4())
        pending = {
            "event": threading.Event(),
            "response": None,
            "error": None,
        }
        with self._pending_lock:
            self._pending[req_id] = pending
        try:
            with self._ws_lock:
                ws = self._ws
            if not ws:
                return None, "no websocket"
            _ws_send_json(ws, token, payload, msg_id=req_id)
            self._bump_frames(tx=1)
            wait = min(float(timeout), READ_TIMEOUT_SEC)
            if not pending["event"].wait(wait):
                return None, "timeout waiting for response"
            if pending.get("error"):
                return None, str(pending["error"])
            return pending["response"], None
        except Exception as ex:
            return None, str(ex)
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)

    def _dispatch_pending_response(self, data: Dict[str, Any]) -> bool:
        rid = data.get("requestId") or data.get("id")
        if not rid:
            return False
        rid_s = str(rid)
        with self._pending_lock:
            pending = self._pending.get(rid_s)
        if pending is None:
            return False
        pending["response"] = data
        pending["event"].set()
        return True

    def public_status(self) -> Dict[str, Any]:
        with self._status_lock:
            return {
                "phase": self._phase,
                "text": self._status_text,
                "detail": self._detail,
                "frames_rx": self._frames_rx,
                "frames_tx": self._frames_tx,
                "last_rx_at": self._last_rx_at,
                "connected_since": self._connected_since,
            }

    def _set_status(self, phase: str, text: str, detail: Optional[str] = None) -> None:
        with self._status_lock:
            self._phase = phase
            self._status_text = text
            self._detail = detail
        self._notify_ws_status()

    def _bind_ws(self, ws: Any) -> None:
        with self._ws_lock:
            self._ws = ws

    def _clear_ws(self) -> None:
        with self._ws_lock:
            self._ws = None

    def _handshake_logged(self, ws: Any, token: str, title: str) -> None:
        """Первый кадр ``softwareVersion`` и короткий дренаж приветствия — с debug TX/RX."""
        from plugins.YandexDevices.glagol_local import READ_TIMEOUT_SEC

        try:
            from websocket._exceptions import WebSocketConnectionClosedException
        except ImportError:

            class WebSocketConnectionClosedException(Exception):
                pass

        log = self._plugin.logger
        ws.settimeout(READ_TIMEOUT_SEC)
        mid = str(uuid.uuid4())
        body = {
            "conversationToken": token,
            "id": mid,
            "payload": {"command": "softwareVersion"},
            "sentTime": int(round(time.time() * 1000)),
        }
        txt = json.dumps(body, ensure_ascii=False)
        log.debug("Glagol LAN [%s] TX %s", title, txt)
        ws.send(txt)
        self._bump_frames(tx=1)
        ws.settimeout(0.35)
        got_rx = False
        for _ in range(12):
            try:
                raw = ws.recv()
            except Exception as ex:
                if WebSocketConnectionClosedException and isinstance(
                    ex, WebSocketConnectionClosedException
                ):
                    raise
                break
            if not raw:
                break
            got_rx = True
            raw_str = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
            log.debug("Glagol LAN [%s] RX (handshake) %s", title, raw_str)
            self._bump_frames(rx=1)
            try:
                json.loads(raw_str)
            except json.JSONDecodeError:
                break
        if not got_rx:
            log.warning(
                "Glagol LAN [%s]: после softwareVersion нет ответа (проверьте токен — «Сформировать токен»)",
                title,
            )
        ws.settimeout(55.0)

    def _load_station_row(self) -> Optional[Dict[str, Any]]:
        from plugins.YandexDevices.glagol_local import parse_host_port
        from plugins.YandexDevices.models.YaStation import YaStation

        with session_scope() as session:
            st = session.query(YaStation).filter(YaStation.id == self._station_id).one_or_none()
            if not st:
                return None
            ip_field = (st.ip or "").strip()
            token = (st.device_token or "").strip()
            lo = (st.glagol_linked_object or "").strip()
            host, port = parse_host_port(ip_field)
            run = bool(host and token)
            return {
                "title": st.title or "",
                "host": host,
                "port": port,
                "token": token,
                "iot_id": st.iot_id,
                "platform": st.platform,
                "linked_object": lo,
                "run": run,
            }

    def run(self) -> None:
        log = self._plugin.logger
        fails = 0
        while not self._stop.is_set():
            cfg = self._load_station_row()
            if not cfg:
                self._set_status("stopped", "Станция удалена", None)
                return
            if not cfg["run"]:
                fails = 0
                self._set_status(
                    "idle",
                    "Ожидание IP и токена",
                    "Укажите LAN-IP и сформируйте токен устройства в карточке станции.",
                )
                if self._stop.wait(5.0):
                    return
                continue

            try:
                self._run_session(cfg)
                fails = 0
            except Exception as ex:
                fails += 1
                delay = 30 * min(fails - 1, 10) if fails > 1 else 5
                self._set_status("error", "Ошибка соединения", str(ex))
                log.warning(
                    "Glagol LAN: станция id=%s «%s» — сессия прервана: %s; пауза %ss",
                    self._station_id,
                    cfg.get("title"),
                    ex,
                    delay,
                    exc_info=log.isEnabledFor(logging.DEBUG),
                )
                if self._stop.wait(float(delay)):
                    return

        self._set_status("stopped", "Остановлено", None)

    def _run_session(self, cfg: Dict[str, Any]) -> None:
        from plugins.YandexDevices.glagol_local import (
            _open_glagol_ws,
            snapshot_from_glagol_state_dict,
        )

        log = self._plugin.logger
        host = cfg["host"]
        port = cfg["port"]
        token = cfg["token"]
        ensure = getattr(self._plugin, "_ensure_device_token", None)
        if ensure and cfg.get("iot_id"):
            fresh = ensure(
                self._station_id,
                cfg.get("iot_id"),
                cfg.get("platform"),
                token,
                refresh_if_expired=True,
            )
            if fresh:
                token = fresh.strip()
                cfg = {**cfg, "token": token}

        ws, conn_err = _open_glagol_ws(host, port, log)
        if not ws or conn_err:
            raise RuntimeError(str(conn_err) if conn_err else "не удалось открыть WebSocket")

        self._bind_ws(ws)
        self._set_status("connecting", "Подключение WebSocket…", f"{host}:{port}")
        last_sw: Optional[str] = None
        last_idle_ping = time.monotonic()

        try:
            self._handshake_logged(ws, token, cfg.get("title") or "")
            tconn = time.time()
            self._set_status("connected", "Подключено", f"{host}:{port}")
            with self._status_lock:
                self._connected_since = tconn
            log.info(
                "Glagol LAN: WebSocket установлен, станция id=%s «%s» (%s:%s)",
                self._station_id,
                cfg.get("title"),
                host,
                port,
            )

            while not self._stop.is_set():
                ws.settimeout(55.0)
                try:
                    raw = ws.recv()
                except Exception as recv_ex:
                    if _is_recv_timeout(recv_ex):
                        if time.monotonic() - last_idle_ping >= 50.0:
                            mid = str(uuid.uuid4())
                            ping_body = {
                                "conversationToken": token,
                                "id": mid,
                                "payload": {"command": "ping"},
                                "sentTime": int(round(time.time() * 1000)),
                            }
                            ping_txt = json.dumps(ping_body, ensure_ascii=False)
                            log.debug("Glagol LAN [%s] TX %s", cfg.get("title"), ping_txt)
                            try:
                                ws.send(ping_txt)
                                self._bump_frames(tx=1)
                            except Exception as ping_send_ex:
                                log.debug("Glagol LAN: idle ping send failed: %s", ping_send_ex)
                                raise
                            last_idle_ping = time.monotonic()
                        continue
                    raise

                last_idle_ping = time.monotonic()

                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue

                if self._dispatch_pending_response(data):
                    log.debug(
                        "Glagol LAN [%s] RX (cmd ack) %s",
                        cfg.get("title"),
                        json.dumps(data, ensure_ascii=False),
                    )
                    self._bump_frames(rx=1)
                    continue

                log.debug(
                    "Glagol LAN [%s] RX %s",
                    cfg.get("title"),
                    json.dumps(data, ensure_ascii=False),
                )
                self._bump_frames(rx=1)

                if isinstance(data.get("softwareVersion"), str):
                    last_sw = data["softwareVersion"]

                # publish_glagol_features_to_object — отключено; расширенные поля добавим позже

                st = data.get("state")
                if isinstance(st, dict):
                    snap = snapshot_from_glagol_state_dict(st)
                    if last_sw:
                        snap["software_version"] = last_sw
                    self._store_ui_snap(snap)
                    row = self._load_station_row()
                    if not row or not row["run"]:
                        log.info(
                            "Glagol LAN: станция id=%s — выход (сняты IP/токен)",
                            self._station_id,
                        )
                        return
                    pub = {
                        "station_id": self._station_id,
                        "station_title": row.get("title"),
                        "host": row["host"],
                        "port": row["port"],
                        "ts": time.time(),
                        **snap,
                    }
                    pub_txt = json.dumps(pub, ensure_ascii=False)
                    log.debug("Glagol LAN [%s] snapshot %s", row.get("title"), pub_txt)

                    lo = (row.get("linked_object") or "").strip()
                    if lo:
                        publish_glagol_snap_to_object(lo, snap, self._plugin.name)

                    if row["token"] != token or row["host"] != host or row["port"] != port:
                        log.info(
                            "Glagol LAN: станция id=%s — переподключение (сменились IP/порт/токен)",
                            self._station_id,
                        )
                        return

        finally:
            self._clear_ws()
            self._clear_ui_snap()
            try:
                ws.close()
            except Exception:
                pass
            with self._status_lock:
                self._connected_since = None
            self._set_status("reconnecting", "Переподключение…", None)


class GlagolWsRegistry:
    """Реестр фоновых потоков Glagol по станциям."""

    def __init__(self, plugin: Any):
        self._plugin = plugin
        self._log = plugin.logger
        self._lock = threading.Lock()
        self._workers: Dict[int, GlagolStationWorker] = {}
        self._shutdown = threading.Event()

    def sync_stations(self) -> None:
        if self._shutdown.is_set():
            return
        want: Set[int] = set()
        try:
            from plugins.YandexDevices.models.YaStation import YaStation

            with session_scope() as session:
                for st in session.query(YaStation).all():
                    if (st.ip or "").strip() and (st.device_token or "").strip():
                        want.add(int(st.id))
        except Exception as ex:
            self._log.warning("Glagol LAN: синхронизация списка станций: %s", ex, exc_info=True)
            return

        with self._lock:
            to_stop = [sid for sid in self._workers if sid not in want]
            for sid in to_stop:
                w = self._workers.pop(sid, None)
                if w:
                    w.request_stop()
                    w.join(timeout=5.0)
            for sid in want:
                if sid not in self._workers:
                    w = GlagolStationWorker(self._plugin, sid)
                    self._workers[sid] = w
                    w.start()

    def shutdown(self) -> None:
        self._shutdown.set()
        with self._lock:
            for sid, w in list(self._workers.items()):
                w.request_stop()
            for w in list(self._workers.values()):
                w.join(timeout=5.0)
            self._workers.clear()

    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {str(sid): w.public_status() for sid, w in self._workers.items()}

    def get_station_snapshot(self, station_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            w = self._workers.get(int(station_id))
            if w:
                return w.get_last_snapshot()
        return None

    def send_payload(
        self,
        station_id: int,
        token: str,
        payload: Dict[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
        """
        Returns:
            ``(response, error, used_keepalive)``. ``used_keepalive=False`` — вызывающий
            должен открыть отдельное соединение (``glagol_request``).
        """
        with self._lock:
            w = self._workers.get(int(station_id))
        if not w or not w.is_connected():
            return None, None, False
        resp, err = w.send_payload(token, payload)
        return resp, err, True

    def get_station_status(self, station_id: int) -> Dict[str, Any]:
        with self._lock:
            w = self._workers.get(int(station_id))
            if w:
                return w.public_status()
        return {
            "phase": "off",
            "text": "Не подключено",
            "detail": "Нет фонового потока: задайте IP и токен устройства для LAN Glagol.",
            "frames_rx": 0,
            "frames_tx": 0,
            "last_rx_at": None,
            "connected_since": None,
        }
