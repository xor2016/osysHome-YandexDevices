"""
Локальный протокол **Glagol** (Яндекс Колонка / Умный дом): JSON по WebSocket.

Общий формат **исходящего** кадра (один JSON на одно сообщение WS):

.. code-block:: json

    {
      "conversationToken": "<токен с quasar.yandex.net/glagol/token>",
      "id": "<uuid — идентификатор запроса>",
      "payload": { ... команда и параметры ... },
      "sentTime": <unix time в миллисекундах>
    }

**Входящие** кадры — тоже JSON. На запрос с полем ``id`` колонка может прислать ответ,
в котором идентификатор ищется в полях ``requestId`` или ``id``. Поле ``status`` часто
равно ``ok`` при успехе. Дополнительно может приходить ``vinsResponse`` (ответ голосового
контура Алисы): вложенная структура с карточками, репликой и т.п. — зависит от версии
прошивки и типа команды.

Типичные значения ``payload`` (поле ``command`` и сопутствующие ключи; не все колонки
поддерживают всё перечисленное):

- ``{"command": "sendText", "text": "..."}`` — передать фразу ассистенту (локальный TTS / сценарий).
- ``{"command": "softwareVersion"}`` — служебный пинг после подключения (часто шлют первым).
- ``{"command": "play"}`` / ``{"command": "stop"}`` — воспроизведение / остановка плеера.
- ``{"command": "next"}`` / ``{"command": "prev"}`` — трек вперёд / назад.
- ``{"command": "setVolume", "volume": <float>}`` — громкость.
- ``{"command": "rewind", "position": <секунды>}`` — перемотка.
- ``{"command": "repeat", "mode": "..."}`` / ``{"command": "shuffle", "enable": true|false}``.
- ``{"command": "playMusic", "id": "...", "type": "..."}`` — запуск трека/подборки по идентификатору.
- ``{"command": "serverAction", "serverActionEventPayload": {...}}`` — служебные действия
  (например сброс сессии диалога), формат зависит от прошивки.

Помимо ответов на команды, колонка присылает **поток состояний** в кадрах с полем ``state``:
``playing``, ``volume``, ``aliceState``, при необходимости ``playerState`` (название, исполнитель,
прогресс, ``extra.coverURI`` для обложки и т.д.). Для одноразового опроса используйте
``glagol_snapshot``; для управления — ``glagol_player_command`` / ``glagol_request``.

Модуль ``plugins/YandexDevices/requirements.txt`` задаёт зависимость ``websocket-client``;
при старте приложения она подтягивается из ``PluginsHelper`` вместе с остальными
зависимостями плагина.
"""

from __future__ import annotations

import json
import logging
import ssl
import time
import uuid
from typing import Any, Dict, Optional, Tuple

DEFAULT_GLAGOL_PORT = 1961
READ_TIMEOUT_SEC = 15.0
CONNECT_TIMEOUT_SEC = 8.0


def parse_host_port(ip_field: str, default_port: int = DEFAULT_GLAGOL_PORT) -> Tuple[Optional[str], int]:
    """Парсит IPv4 и опциональный порт из поля IP (``192.168.1.5`` или ``192.168.1.5:1961``)."""
    s = (ip_field or "").strip()
    if not s:
        return None, default_port
    if ":" in s:
        head, tail = s.rsplit(":", 1)
        if tail.isdigit() and "." in head and ":" not in head:
            return head.strip(), int(tail)
    return s, default_port


def _ws_send_json(ws: Any, token: str, payload: Dict[str, Any], msg_id: Optional[str] = None) -> str:
    mid = msg_id or str(uuid.uuid4())
    body = {
        "conversationToken": token,
        "id": mid,
        "payload": payload,
        "sentTime": int(round(time.time() * 1000)),
    }
    ws.send(json.dumps(body, ensure_ascii=False))
    return mid


def _open_glagol_ws(host: str, port: int, logger: logging.Logger) -> Tuple[Optional[Any], Optional[Exception]]:
    try:
        import websocket  # type: ignore
    except ImportError as e:
        logger.error(
            "Glagol: нет пакета websocket-client. Установите зависимости плагина "
            "(файл plugins/YandexDevices/requirements.txt при загрузке приложения или вручную: pip install -r …): %s",
            e,
        )
        return None, e

    last_err: Optional[Exception] = None
    for proto in ("wss", "ws"):
        url = f"{proto}://{host}:{port}"
        sslopt = {"cert_reqs": ssl.CERT_NONE} if proto == "wss" else None
        try:
            ws = websocket.create_connection(
                url,
                timeout=CONNECT_TIMEOUT_SEC,
                sslopt=sslopt,
            )
            return ws, None
        except Exception as ex:
            last_err = ex
            continue
    if last_err:
        log_fn = logger.error
        if isinstance(last_err, OSError):
            errno = getattr(last_err, "errno", None)
            winerr = getattr(last_err, "winerror", None)
            if winerr == 10061 or errno in (111, 61):  # ECONNREFUSED / «отверг запрос»
                log_fn = logger.warning
        log_fn("Glagol: не удалось подключиться к %s:%s — %s", host, port, last_err)
    return None, last_err


def _session_handshake(ws: Any, conversation_token: str, _logger: logging.Logger) -> None:
    """Первый пинг и короткий дренаж приветственных пакетов (как при типичном клиентском сценарии)."""
    ws.settimeout(READ_TIMEOUT_SEC)
    _ws_send_json(ws, conversation_token, {"command": "softwareVersion"})
    ws.settimeout(0.35)
    for _ in range(12):
        try:
            raw = ws.recv()
        except Exception:
            break
        if not raw:
            break
        try:
            json.loads(raw)
        except json.JSONDecodeError:
            break
    ws.settimeout(READ_TIMEOUT_SEC)


def glagol_request(
    host: str,
    port: int,
    conversation_token: str,
    payload: Dict[str, Any],
    logger: logging.Logger,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Одна команда Glagol: подключение, handshake, отправка ``payload``, ожидание ответа с тем же id.

    Returns:
        ``(полный JSON ответа, None)`` либо ``(None, причина)`` при ошибке соединения / таймаута.
    """
    ws, conn_err = _open_glagol_ws(host, port, logger)
    if not ws:
        return None, (str(conn_err) if conn_err else "не удалось открыть WebSocket")
    try:
        _session_handshake(ws, conversation_token, logger)
        req_id = str(uuid.uuid4())
        _ws_send_json(ws, conversation_token, payload, msg_id=req_id)
        deadline = time.monotonic() + READ_TIMEOUT_SEC
        while time.monotonic() < deadline:
            raw = ws.recv()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            rid = data.get("requestId") or data.get("id")
            if rid == req_id:
                return data, None
        logger.warning("Glagol: таймаут ожидания ответа для id=%s (%s)", req_id, host)
        return None, "timeout waiting for response"
    except Exception as ex:
        logger.warning("Glagol: ошибка запроса к %s:%s — %s", host, port, ex)
        return None, str(ex)
    finally:
        try:
            ws.close()
        except Exception:
            pass


def glagol_send_text(
    host: str,
    port: int,
    conversation_token: str,
    text: str,
    logger: logging.Logger,
) -> bool:
    """Сокращение для ``{"command": "sendText", "text": ...}``; True если ``status == ok``."""
    data, _err = glagol_request(
        host,
        port,
        conversation_token,
        {"command": "sendText", "text": text},
        logger,
    )
    if not data:
        return False
    ok = data.get("status") == "ok"
    if ok:
        logger.info("Glagol: sendText ok (%s)", host)
    else:
        logger.warning("Glagol: sendText status=%s payload=%s", data.get("status"), data)
    return ok


def cover_uri_to_url(cover_uri: str, size: str = "400x400") -> Optional[str]:
    """Превращает ``coverURI`` из ``playerState.extra`` в абсолютный URL обложки."""
    if not cover_uri or not isinstance(cover_uri, str):
        return None
    u = cover_uri.strip()
    if not u:
        return None
    u = u.replace("%%", size)
    if u.startswith("http://") or u.startswith("https://"):
        return u
    return "https://" + u.lstrip("/")


def normalize_player_state(player_state: Dict[str, Any]) -> Dict[str, Any]:
    """Уплощённое описание трека для JSON/UI (поля как у прошивки Колонки)."""
    if not isinstance(player_state, dict):
        return {}
    extra = player_state.get("extra") if isinstance(player_state.get("extra"), dict) else {}
    entity = (
        player_state.get("entityInfo")
        if isinstance(player_state.get("entityInfo"), dict)
        else {}
    )
    cover = None
    if isinstance(extra, dict):
        cover = cover_uri_to_url(str(extra.get("coverURI") or ""))
    return {
        "id": player_state.get("id"),
        "type": player_state.get("type"),
        "title": player_state.get("title"),
        "subtitle": player_state.get("subtitle"),
        "duration_sec": player_state.get("duration") or None,
        "progress_sec": player_state.get("progress"),
        "playlist_type": player_state.get("playlistType"),
        "player_type": player_state.get("playerType"),
        "live_stream_text": player_state.get("liveStreamText"),
        "has_next": bool(player_state.get("hasNext")),
        "has_prev": bool(player_state.get("hasPrev")),
        "cover_url": cover,
        "repeat_mode": entity.get("repeatMode"),
        "shuffled": entity.get("shuffled"),
    }


def glagol_snapshot(
    host: str,
    port: int,
    conversation_token: str,
    logger: logging.Logger,
    *,
    listen_seconds: float = 2.8,
    max_frames: int = 72,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Подключение к Glagol, короткое прослушивание кадров со ``state`` (плеер шлёт их периодически).

    Returns:
        ``(словарь полей снимка, None)`` при успешном TCP/WS;
        ``(None, текст_ошибки)`` если не удалось подключиться или оборвалась сессия до чтения.
    """
    ws, conn_err = _open_glagol_ws(host, port, logger)
    if not ws:
        return None, (str(conn_err) if conn_err else "не удалось открыть WebSocket")
    out: Dict[str, Any] = {
        "playing": None,
        "volume": None,
        "muted": None,
        "alice_state": None,
        "player": None,
        "software_version": None,
        "raw_state": None,
    }
    try:
        deadline = time.monotonic() + float(listen_seconds)
        _ws_send_json(ws, conversation_token, {"command": "softwareVersion"})
        frames = 0
        while time.monotonic() < deadline and frames < max_frames:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ws.settimeout(min(1.0, max(0.25, remaining)))
            try:
                raw = ws.recv()
            except Exception:
                break
            frames += 1
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if isinstance(data.get("softwareVersion"), str):
                out["software_version"] = data["softwareVersion"]
            st = data.get("state")
            if isinstance(st, dict):
                out["raw_state"] = st
                st = dict(st)
                st.pop("timeSinceLastVoiceActivity", None)
                out["playing"] = st.get("playing")
                vol = st.get("volume")
                if isinstance(vol, (int, float)):
                    out["volume"] = float(vol)
                    out["muted"] = vol <= 0
                out["alice_state"] = st.get("aliceState")
                ps = st.get("playerState")
                if isinstance(ps, dict):
                    out["player"] = normalize_player_state(ps)
                else:
                    out["player"] = None
        return out, None
    except Exception as ex:
        logger.warning("Glagol: snapshot %s:%s — %s", host, port, ex)
        return None, str(ex)
    finally:
        try:
            ws.close()
        except Exception:
            pass


def glagol_player_command(
    host: str,
    port: int,
    conversation_token: str,
    logger: logging.Logger,
    action: str,
    *,
    volume: Optional[float] = None,
    position: Optional[float] = None,
    repeat_mode: Optional[str] = None,
    shuffle: Optional[bool] = None,
    music_id: Optional[str] = None,
    music_type: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Управление плеером по Glagol. ``pause`` на колонке соответствует команде ``stop``.

    action: play | pause | stop | next | prev | volume | seek | repeat | shuffle | play_music

    Returns:
        ``(ответ JSON, None)`` или ``(None, причина)``.
    """
    act = (action or "").strip().lower()
    payload: Dict[str, Any]
    if act == "play":
        payload = {"command": "play"}
    elif act in ("pause", "stop"):
        payload = {"command": "stop"}
    elif act == "next":
        payload = {"command": "next"}
    elif act == "prev":
        payload = {"command": "prev"}
    elif act == "volume":
        if volume is None:
            logger.warning("Glagol: volume без значения")
            return None, "volume required"
        payload = {"command": "setVolume", "volume": round(float(volume), 1)}
    elif act == "seek":
        if position is None:
            logger.warning("Glagol: seek без position")
            return None, "position required"
        payload = {"command": "rewind", "position": float(position)}
    elif act == "repeat":
        if not repeat_mode:
            logger.warning("Glagol: repeat без repeat_mode (None|All|One)")
            return None, "repeat_mode required"
        payload = {"command": "repeat", "mode": str(repeat_mode)}
    elif act == "shuffle":
        if shuffle is None:
            logger.warning("Glagol: shuffle без enable")
            return None, "shuffle required"
        payload = {"command": "shuffle", "enable": bool(shuffle)}
    elif act == "play_music":
        if not music_id or not music_type:
            logger.warning("Glagol: play_music нужны music_id и music_type")
            return None, "music_id and music_type required"
        payload = {"command": "playMusic", "id": str(music_id), "type": str(music_type)}
    else:
        logger.warning("Glagol: неизвестное действие %r", action)
        return None, "unknown action"
    return glagol_request(host, port, conversation_token, payload, logger)
