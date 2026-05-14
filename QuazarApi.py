import json
import logging
import os
import re
from typing import Any, Callable, Optional

import certifi
import requests

# Таймауты (connect, read) в секундах — без них запрос может висеть бесконечно при проблемах с сетью/сервером
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_READ_TIMEOUT = 30
DEFAULT_TIMEOUT = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT)

# Заголовки как у мобильного Chrome — без них passport может отдавать другую разметку.
_PASSPORT_PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

_PASSPORT_FORM_HEADERS = {
    **_PASSPORT_PAGE_HEADERS,
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

# Колбэк: (title, description, level) level — "warning" | "error"
UserNotifyFn = Optional[Callable[[str, str, str], None]]


class ExpiredXTokenError(ValueError):
    """Яндекс отклонил OAuth x-token (из token_by_sessionid) при обмене на music token."""


def _extract_passport_csrf(html: str) -> Optional[str]:
    """Достаёт csrf для passport: раньше был hidden input, сейчас часто CSRF__ в скрипте страницы."""
    patterns = [
        r'"csrf_token"\s+value="(.+?)"',
        r'name="csrf_token"[^>]*value="(.+?)"',
        r'value="(.+?)"[^>]*name="csrf_token"',
        r'CSRF__\s*=\s*"([^"]+)"',
        r"CSRF__\s*=\s*'([^']+)'",
        r'"csrf_token"\s*:\s*"(.+?)"',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            token = (m.group(1) or "").strip()
            if len(token) > 5:
                return token
    return None


def _extract_pwl_xsrf(html: str) -> Optional[str]:
    """Заголовок X-CSRF-Token для API pwl-yandex (страница отдаёт __CSRF__ = \"...\")."""
    m = re.search(r"__CSRF__\s*=\s*\"([^\"]+)\"", html or "")
    if not m:
        return None
    token = (m.group(1) or "").strip()
    return token if len(token) > 5 else None


def _pwl_api_headers(x_csrf: str, *, json_body: bool = False) -> dict[str, str]:
    h: dict[str, str] = {
        **_PASSPORT_PAGE_HEADERS,
        "X-CSRF-Token": x_csrf,
        "Origin": "https://passport.yandex.ru",
        "Referer": "https://passport.yandex.ru/pwl-yandex",
    }
    if json_body:
        h["Content-Type"] = "application/json; charset=UTF-8"
    return h


class QuazarApi:
    """HTTP-клиент к Quasar / IoT Яндекса. Логгер — тот же, что у плагина (handlers в getLogger)."""

    def __init__(self, cache_dir: str, logger: logging.Logger, user_notify: UserNotifyFn = None):
        if logger is None:
            logger = logging.getLogger("QuazarApi")
            if not logger.handlers:
                logging.basicConfig(level=logging.INFO)
        self.logger = logger
        self._user_notify = user_notify
        self.cache_dir = cache_dir
        self.cookie_path = os.path.join(self.cache_dir, "cookie")
        self.csrf_token = self.get_token()
        self.music_token = None
        self._iot_unauthorized_blocked = False
        self._iot_unauthorized_cookie_mtime: Optional[float] = None

    def _qr_pending_path(self) -> str:
        return os.path.join(self.cache_dir, "qr_pending.json")

    def _dump_session_cookies(self, session: requests.Session, path: str) -> None:
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(requests.utils.dict_from_cookiejar(session.cookies), f)

    def _load_session_cookies(self, session: requests.Session, path: str) -> None:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                session.cookies.update(requests.utils.cookiejar_from_dict(json.load(f)))

    def _sync_iot_auth_state_with_cookie(self) -> None:
        """Снять блокировку после 401, если файл cookie заменили (новый вход)."""
        if not self.cookie_path:
            return
        if not os.path.exists(self.cookie_path):
            self._iot_unauthorized_blocked = False
            self._iot_unauthorized_cookie_mtime = None
            return
        if not self._iot_unauthorized_blocked:
            return
        cur = os.path.getmtime(self.cookie_path)
        if self._iot_unauthorized_cookie_mtime is None or cur != self._iot_unauthorized_cookie_mtime:
            self._iot_unauthorized_blocked = False
            self._iot_unauthorized_cookie_mtime = None
            self._reset_user_notify_flag("iot_401")
            self.logger.info("Quazar API: обновлён файл cookie — повторные запросы к IoT разрешены.")

    def _reset_user_notify_flag(self, dedupe_key: str) -> None:
        setattr(self, f"_user_notified_{dedupe_key}", False)

    def _push_user_auth_issue(
        self,
        title: str,
        description: str = "",
        *,
        level: str = "warning",
        dedupe_key: str,
    ) -> None:
        """Одно уведомление пользователю на эпизод (пока не снята причина)."""
        fk = f"_user_notified_{dedupe_key}"
        if getattr(self, fk, False):
            return
        setattr(self, fk, True)
        text = f"{title}" if not description else f"{title} — {description}"
        if level == "error":
            self.logger.error(text)
        else:
            self.logger.warning(text)
        if self._user_notify:
            try:
                self._user_notify(title, description, level)
            except Exception as exc:
                self.logger.debug("Quazar user_notify failed: %s", exc)

    def _clear_iot_auth_failure(self) -> None:
        """Успешное обращение к API — снимаем блок 401 и флаги уведомлений."""
        if self._iot_unauthorized_blocked:
            self._iot_unauthorized_blocked = False
            self._iot_unauthorized_cookie_mtime = None
        self._reset_user_notify_flag("iot_401")

    def _iot_backend_allowed(self) -> bool:
        """Можно ли слать запросы к IoT/Quasar с основным cookie (есть файл и нет активной блокировки 401)."""
        self._sync_iot_auth_state_with_cookie()
        if not self.cookie_path or not os.path.exists(self.cookie_path):
            return False
        if self._iot_unauthorized_blocked:
            return False
        return True

    def api_request(self, url, method="GET", params=None, repeating=0, csrf_token=None, debug=0):
        self._sync_iot_auth_state_with_cookie()

        if self.cookie_path and os.path.exists(self.cookie_path):
            self._warned_missing_cookie = False
            self._reset_user_notify_flag("missing_cookie")
        elif self.cookie_path:
            self._push_user_auth_issue(
                "Yandex Devices: нет авторизации",
                "Файл сессии (cookie) не найден. Откройте настройки плагина и выполните вход.",
                level="warning",
                dedupe_key="missing_cookie",
            )
            if not getattr(self, "_warned_missing_cookie", False):
                self.logger.warning(
                    "Quazar API: запросы к IoT отключены — нет cookie. Авторизуйтесь в настройках плагина."
                )
                self._warned_missing_cookie = True
            return None

        if self._iot_unauthorized_blocked:
            self.logger.debug("Quazar API: запрос пропущен — сессия IoT заблокирована после 401 до нового cookie.")
            return None

        headers = {}
        if method != "GET" and not csrf_token:
            csrf_token = self.get_token()

        if method != "GET":
            headers = {
                "Content-type": "application/json",
                "x-csrf-token": csrf_token,
            }

        session = requests.Session()
        if self.cookie_path is not None and os.path.exists(self.cookie_path):
            with open(self.cookie_path, "r", encoding="utf-8") as f:
                cookies = requests.utils.cookiejar_from_dict(json.load(f))
                session.cookies.update(cookies)

        response = None
        try:
            if method == "GET":
                response = session.get(
                    url, headers=headers, cookies=session.cookies, timeout=DEFAULT_TIMEOUT, verify=certifi.where()
                )
            else:
                if isinstance(params, dict):
                    params = json.dumps(params)
                if method == "POST":
                    response = session.post(
                        url, data=params, headers=headers, cookies=session.cookies, timeout=DEFAULT_TIMEOUT, verify=certifi.where()
                    )
                else:
                    response = session.request(
                        method, url, data=params, headers=headers, cookies=session.cookies, timeout=DEFAULT_TIMEOUT, verify=certifi.where()
                    )
        except requests.RequestException as e:
            self.logger.error("Quazar API: ошибка сети %s %s — %s", method, url, e)
            return None

        result_code = response.status_code
        if result_code == 401:
            if self.cookie_path and os.path.exists(self.cookie_path):
                self._iot_unauthorized_blocked = True
                self._iot_unauthorized_cookie_mtime = os.path.getmtime(self.cookie_path)
            self._push_user_auth_issue(
                "Yandex Devices: сессия недействительна",
                "Яндекс вернул 401. Выполните вход в настройках плагина заново.",
                level="error",
                dedupe_key="iot_401",
            )
            return None

        if 200 <= result_code < 300:
            self._clear_iot_auth_failure()

        if result_code == 403:
            self.logger.warning("Quazar API: HTTP 403 для %s %s", method, url)

        try:
            data = response.json()
        except ValueError:
            data = None
            if result_code >= 400:
                self.logger.warning(
                    "Quazar API: не-JSON ответ %s для %s %s, первые 200 символов: %.200s",
                    result_code,
                    method,
                    url,
                    response.text,
                )

        if not repeating and (data is None or data.get("code") != "BAD_REQUEST") and (
            data is None or result_code == 403 or data.get("status") == "error"
        ):
            if debug:
                self.logger.debug("Quazar API: повтор запроса (csrf) %s %s", method, url)
            csrf_token = ""
            return self.api_request(url, method, params, repeating=1, csrf_token=csrf_token, debug=debug)

        return data

    def _get_session(self):
        session = requests.Session()
        if self.cookie_path is not None and os.path.exists(self.cookie_path):
            with open(self.cookie_path, "r", encoding="utf-8") as f:
                cookies = requests.utils.cookiejar_from_dict(json.load(f))
                session.cookies.update(cookies)
        return session

    def _cookie_header_for_mobileproxy(self, session: requests.Session) -> Optional[str]:
        """Строка Cookie для mobileproxy (все пары из jar, как в типичном клиенте)."""
        d = session.cookies.get_dict()
        if not d:
            return None
        return "; ".join(f"{k}={v}" for k, v in d.items())

    def get_oauth_access_token_by_sessionid(self) -> Optional[str]:
        """
        OAuth ``access_token`` по сохранённой сессии (``token_by_sessionid``).

        Именно его ожидает ``grant_type=x-token`` на ``oauth.mobile.yandex.net`` для music /
        Glagol — **не** ``csrfToken2`` из HTML Quasar (тот идёт только в заголовок ``x-csrf-token`` к IoT).
        """
        session = self._get_session()
        cookie_hdr = self._cookie_header_for_mobileproxy(session)
        if not cookie_hdr:
            self.logger.error("Quazar API: пустые cookies — нельзя вызвать token_by_sessionid")
            return None
        headers = {
            **_PASSPORT_PAGE_HEADERS,
            "Ya-Client-Host": "passport.yandex.ru",
            "Ya-Client-Cookie": cookie_hdr,
        }
        data = {
            "client_id": "c0ebe342af7d48fbbbfcf2d2eedb8f9e",
            "client_secret": "ad0a908f0aa341a182a37ecd75bc319e",
        }
        url = "https://mobileproxy.passport.yandex.net/1/bundle/oauth/token_by_sessionid"
        try:
            r = session.post(
                url,
                data=data,
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
                verify=certifi.where(),
            )
        except requests.RequestException as e:
            self.logger.error("Quazar API: token_by_sessionid — %s", e)
            return None
        try:
            resp = r.json()
        except ValueError:
            self.logger.error(
                "Quazar API: token_by_sessionid — не JSON, HTTP %s: %.400s",
                r.status_code,
                r.text or "",
            )
            return None
        tok = resp.get("access_token")
        if not tok:
            self.logger.error(
                "Quazar API: token_by_sessionid — нет access_token (HTTP %s): %s",
                r.status_code,
                resp,
            )
            return None
        self.logger.debug("Quazar API: OAuth access_token по sessionid получен")
        return str(tok)

    def resolve_glagol_token_params(self, iot_device_id: str, fallback_platform: Optional[str]) -> tuple[str, Optional[str]]:
        """
        Для ``https://quasar.yandex.net/glagol/token`` нужны ``device_id`` и ``platform`` из блока
        ``quasar_info`` устройства в ответе ``/m/user/devices``. Сырой IoT ``id`` без связки с
        пользователем Glagol даёт ``Device has no associated user``.
        """
        out_id = str(iot_device_id)
        out_plat: Optional[str] = fallback_platform if fallback_platform else None
        data = self.api_request("https://iot.quasar.yandex.ru/m/user/devices")
        if not isinstance(data, dict):
            return out_id, out_plat
        for room in data.get("rooms") or []:
            for dev in room.get("devices") or []:
                if str(dev.get("id", "")) != str(iot_device_id):
                    continue
                qi = dev.get("quasar_info") or {}
                if qi.get("device_id"):
                    out_id = str(qi["device_id"])
                plat = qi.get("platform") or dev.get("platform")
                if plat:
                    out_plat = str(plat)
                self.logger.debug(
                    "Quazar API: glagol/token: iot_id=%s → device_id=%s platform=%s (есть quasar_info=%s)",
                    iot_device_id,
                    out_id,
                    out_plat,
                    bool(qi.get("device_id")),
                )
                return out_id, out_plat
        self.logger.warning(
            "Quazar API: устройство iot_id=%s не найдено в /m/user/devices — для glagol/token используем его как есть",
            iot_device_id,
        )
        return out_id, out_plat

    def get_device_token(self, device_id, platform):
        self._sync_iot_auth_state_with_cookie()
        if not self._iot_backend_allowed():
            self.logger.warning(
                "Quazar API: get_device_token пропущен — нет действующей авторизации IoT."
            )
            return None

        self.logger.debug("Quazar API: обновление токена устройства %s", device_id)

        session = self._get_session()

        glagol_device_id, glagol_platform = self.resolve_glagol_token_params(device_id, platform)
        if not glagol_platform:
            self.logger.warning(
                "Quazar API: для glagol/token неизвестен platform (iot_id=%s) — запрос может завершиться ошибкой",
                device_id,
            )

        if not self.music_token:
            for attempt in range(2):
                oauth_x = self.get_oauth_access_token_by_sessionid()
                if not oauth_x:
                    self.logger.error(
                        "Quazar API: не удалось получить OAuth x-token по cookie (token_by_sessionid)"
                    )
                    return None
                try:
                    self.music_token = self.get_music_token(oauth_x)
                    break
                except ExpiredXTokenError:
                    if attempt == 0:
                        self.logger.warning(
                            "Quazar API: повтор цепочки token_by_sessionid → music OAuth"
                        )
                        self.music_token = None
                        continue
                    self.logger.error(
                        "Quazar API: music OAuth не прошёл после повтора — выполните вход в плагине заново"
                    )
                    return None
                except (ValueError, KeyError) as e:
                    self.logger.error("Quazar API: не удалось получить music token — %s", e)
                    return None

        headers = {"Authorization": f"OAuth {self.music_token}"}
        payload = {"device_id": glagol_device_id, "platform": glagol_platform or platform or ""}
        try:
            r = session.get(
                "https://quasar.yandex.net/glagol/token",
                headers=headers,
                params=payload,
                cookies=session.cookies,
                timeout=DEFAULT_TIMEOUT,
            )
        except requests.RequestException as e:
            self.logger.error("Quazar API: glagol/token — %s", e)
            return None

        try:
            resp = json.loads(r.text)
        except json.JSONDecodeError:
            self.logger.error("Quazar API: glagol/token — не JSON, HTTP %s", r.status_code)
            return None

        if resp.get("status") == "ok":
            self.logger.info("Quazar API: токен устройства получен (glagol device_id=%s)", glagol_device_id)
            return resp.get("token")

        self.logger.warning("Quazar API: glagol/token — status=%s, тело=%s", resp.get("status"), resp)
        return None

    def get_music_token(self, x_token: str):
        """Токен музыки / Glagol: ``grant_type=x-token``, в теле ``access_token`` = OAuth токен из ``token_by_sessionid``."""
        self.logger.debug("Quazar API: запрос music OAuth по OAuth x-token (sessionid)")

        payload = {
            "client_secret": "53bc75238f0c4d08a118e51fe9203300",
            "client_id": "23cabbbdc6cd418abb4b39c32c41195d",
            "grant_type": "x-token",
            "access_token": x_token,
        }
        session = self._get_session()
        try:
            r = session.post("https://oauth.mobile.yandex.net/1/token", data=payload, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as e:
            self.logger.error("Quazar API: music OAuth — %s", e)
            raise

        try:
            resp = r.json()
        except ValueError as e:
            self.logger.error("Quazar API: music OAuth — не JSON, HTTP %s", r.status_code)
            raise ValueError("invalid oauth response") from e

        if "access_token" not in resp:
            err = resp.get("error")
            desc = (resp.get("error_description") or "").lower()
            if err == "invalid_grant" and ("expired" in desc or "expired_token" in desc):
                self.logger.warning(
                    "Quazar API: music OAuth — устарел OAuth x-token: %s",
                    resp.get("error_description", err),
                )
                raise ExpiredXTokenError("music OAuth: expired OAuth x-token")
            self.logger.error("Quazar API: music OAuth — нет access_token в ответе: %s", resp)
            raise ValueError("music OAuth: no access_token in response")
        return resp["access_token"]

    def get_token(self, url="https://yandex.ru/quasar/iot", error_monitor=False, error_monitor_type=1):
        session = requests.Session()
        if self.cookie_path is not None and os.path.exists(self.cookie_path):
            with open(self.cookie_path, "r", encoding="utf-8") as f:
                cookies = requests.utils.cookiejar_from_dict(json.load(f))
                session.cookies.update(cookies)

        headers = {"Accept-Encoding": "gzip"}

        try:
            response = session.get(url, headers=headers, cookies=session.cookies, timeout=DEFAULT_TIMEOUT, verify=certifi.where())
        except requests.RequestException as e:
            self.logger.error("Quazar API: get_token (%s) — %s", url, e)
            return False

        match = re.search(r'"csrfToken2":"(.+?)"', response.text)
        if match:
            token = match.group(1)
            self.logger.debug("Quazar API: csrfToken2 получен с %s", url)
            return token

        if error_monitor:
            if error_monitor_type == 1:
                self.logger.error("Quazar API: не найден csrfToken2 на странице (короткий режим)")
            elif error_monitor_type == 2:
                self.logger.error(
                    "Quazar API: не найден csrfToken2 (verbose), HTTP %s, фрагмент: %.400s",
                    response.status_code,
                    response.text,
                )
        else:
            self.logger.debug(
                "Quazar API: csrfToken2 не найден, HTTP %s (это нормально без сессии)",
                response.status_code,
            )
        return False

    def get_csrf_token(self, cookie_path):
        url = "https://passport.yandex.ru/am?app_platform=android"

        session = requests.Session()
        if cookie_path is not None and os.path.exists(cookie_path):
            with open(cookie_path, "r", encoding="utf-8") as f:
                cookies = requests.utils.cookiejar_from_dict(json.load(f))
                session.cookies.update(cookies)

        try:
            response = session.get(
                url,
                headers=_PASSPORT_PAGE_HEADERS,
                allow_redirects=True,
                timeout=DEFAULT_TIMEOUT,
                verify=certifi.where(),
            )
        except requests.RequestException as e:
            self.logger.error("Quazar API: get_csrf_token — %s", e)
            return False

        html = response.text or ""
        match_tok = _extract_passport_csrf(html)

        cookie_dir = os.path.dirname(os.path.abspath(cookie_path))
        if cookie_dir:
            os.makedirs(cookie_dir, exist_ok=True)
        try:
            with open(cookie_path, "w", encoding="utf-8") as f:
                json.dump(requests.utils.dict_from_cookiejar(session.cookies), f)
        except OSError as e:
            self.logger.error("Quazar API: не удалось сохранить cookie_qr: %s", e)
            return False

        if match_tok:
            self.logger.info("Quazar API: CSRF для passport получен")
            return match_tok

        self.logger.error(
            "Quazar API: CSRF passport не найден в разметке, HTTP %s, фрагмент: %.400s",
            response.status_code,
            html,
        )
        return False

    def getQrCode(self):
        """QR-вход через актуальный поток pwl-yandex (старый registration-validations отвечает 403)."""
        use_cookie_file = os.path.join(self.cache_dir, "cookie_qr")
        pending_path = self._qr_pending_path()
        out: dict[str, Any] = {"AUTHORIZED": None}
        self.logger.info("Quazar API: запрос QR (pwl-yandex)")

        for fp in (pending_path,):
            try:
                if os.path.isfile(fp):
                    os.remove(fp)
            except OSError:
                pass

        session = requests.Session()
        try:
            r0 = session.get(
                "https://passport.yandex.ru/pwl-yandex",
                headers=_PASSPORT_PAGE_HEADERS,
                timeout=DEFAULT_TIMEOUT,
                verify=certifi.where(),
            )
        except requests.RequestException as e:
            self.logger.error("Quazar API: GET pwl-yandex — %s", e)
            out["ERR_MSG"] = "Ошибка сети при получении QR"
            return out

        if r0.status_code != 200:
            self.logger.error("Quazar API: pwl-yandex HTTP %s", r0.status_code)
            out["ERR_MSG"] = "Ошибка получения QR-кода"
            return out

        x_csrf = _extract_pwl_xsrf(r0.text or "")
        if not x_csrf:
            self.logger.error(
                "Quazar API: на pwl-yandex не найден __CSRF__, фрагмент: %.500s",
                r0.text or "",
            )
            out["ERR_MSG"] = "Ошибка получения CSRF-токена"
            return out

        self._dump_session_cookies(session, use_cookie_file)

        try:
            r1 = session.post(
                "https://passport.yandex.ru/pwl-yandex/api/passport/auth/password/submit",
                json={"retpath": "https://passport.yandex.ru/"},
                headers=_pwl_api_headers(x_csrf, json_body=True),
                timeout=DEFAULT_TIMEOUT,
                verify=certifi.where(),
            )
        except requests.RequestException as e:
            self.logger.error("Quazar API: pwl password/submit — %s", e)
            out["ERR_MSG"] = "Ошибка сети при получении QR"
            return out

        self._dump_session_cookies(session, use_cookie_file)

        try:
            auth_submit = r1.json()
        except ValueError:
            self.logger.error(
                "Quazar API: submit не JSON, HTTP %s: %.500s",
                r1.status_code,
                r1.text,
            )
            out["ERR_MSG"] = "Ошибка получения QR-кода"
            return out

        if r1.status_code != 200 or not isinstance(auth_submit, dict) or "track_id" not in auth_submit:
            self.logger.warning(
                "Quazar API: неожиданный ответ password/submit: HTTP %s %s",
                r1.status_code,
                auth_submit,
            )
            out["ERR_MSG"] = "Ошибка получения QR-кода"
            return out

        try:
            r2 = session.post(
                "https://passport.yandex.ru/pwl-yandex/api/passport/auth/magic/code",
                data={"location_id": "0", "magic_track_id": auth_submit["track_id"], "track_id": ""},
                headers=_pwl_api_headers(x_csrf, json_body=False),
                timeout=DEFAULT_TIMEOUT,
                verify=certifi.where(),
            )
        except requests.RequestException as e:
            self.logger.error("Quazar API: pwl magic/code — %s", e)
            out["ERR_MSG"] = "Ошибка сети при получении QR"
            return out

        self._dump_session_cookies(session, use_cookie_file)

        try:
            magic_data = r2.json()
        except ValueError:
            self.logger.error(
                "Quazar API: magic/code не JSON, HTTP %s: %.500s",
                r2.status_code,
                r2.text,
            )
            out["ERR_MSG"] = "Ошибка получения QR-кода"
            return out

        qr_link = magic_data.get("link") if isinstance(magic_data, dict) else None
        if r2.status_code != 200 or not qr_link:
            self.logger.warning("Quazar API: нет link в ответе magic/code: %s", magic_data)
            out["ERR_MSG"] = "Ошибка получения QR-кода"
            return out

        pending = {"x_csrf": x_csrf, "auth_submit": auth_submit, "qr_link": qr_link}
        try:
            with open(pending_path, "w", encoding="utf-8") as f:
                json.dump(pending, f)
        except OSError as e:
            self.logger.error("Quazar API: не удалось сохранить qr_pending.json: %s", e)

        out["TRACK_ID"] = auth_submit["track_id"]
        out["CSRF_TOKEN"] = auth_submit.get("csrf_token", "")
        out["QR_URL"] = qr_link
        self.logger.info("Quazar API: QR-ссылка получена (pwl-yandex)")
        return out

    def confirmQrCode(self, track_id, csrf_token):
        """Проверка сканирования QR и получение сессии (pwl-yandex)."""
        out: dict[str, Any] = {}
        use_cookie_file = os.path.join(self.cache_dir, "cookie_qr")
        pending_path = self._qr_pending_path()

        def _fill_from_pending() -> None:
            if not os.path.isfile(pending_path):
                return
            try:
                with open(pending_path, "r", encoding="utf-8") as f:
                    pend = json.load(f)
                auth = pend.get("auth_submit") or {}
                out["TRACK_ID"] = auth.get("track_id") or track_id
                out["CSRF_TOKEN"] = auth.get("csrf_token") or csrf_token
                out["QR_URL"] = pend.get("qr_link") or ""
            except (OSError, json.JSONDecodeError):
                out["TRACK_ID"] = track_id
                out["CSRF_TOKEN"] = csrf_token
                out["QR_URL"] = ""

        if not os.path.isfile(pending_path):
            self.logger.error("Quazar API: нет qr_pending.json — запросите QR заново")
            out["ERR_MSG"] = "Сессия QR устарела. Обновите страницу и снова откройте вход по QR."
            out["AUTHORIZED"] = None
            _fill_from_pending()
            return out

        try:
            with open(pending_path, "r", encoding="utf-8") as f:
                pending = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            self.logger.error("Quazar API: qr_pending.json повреждён: %s", e)
            out["ERR_MSG"] = "Сессия QR устарела. Запросите QR снова."
            out["AUTHORIZED"] = None
            _fill_from_pending()
            return out

        x_csrf = pending.get("x_csrf")
        auth_submit = pending.get("auth_submit")
        qr_link = pending.get("qr_link", "")
        if not x_csrf or not isinstance(auth_submit, dict) or "track_id" not in auth_submit:
            self.logger.error("Quazar API: неполные данные в qr_pending.json")
            out["ERR_MSG"] = "Сессия QR устарела. Запросите QR снова."
            out["AUTHORIZED"] = None
            _fill_from_pending()
            return out

        if track_id and track_id != auth_submit.get("track_id"):
            self.logger.warning(
                "Quazar API: track_id из URL не совпадает с сессией — используем сохранённый track_id"
            )

        session = requests.Session()
        self._load_session_cookies(session, use_cookie_file)

        self.logger.info("Quazar API: проверка статуса QR (track_id=%s)", auth_submit.get("track_id"))
        try:
            response = session.post(
                "https://passport.yandex.ru/pwl-yandex/api/passport/auth/magic/code/status",
                json=auth_submit,
                headers=_pwl_api_headers(x_csrf, json_body=True),
                timeout=DEFAULT_TIMEOUT,
                verify=certifi.where(),
            )
        except requests.RequestException as e:
            self.logger.error("Quazar API: magic/code/status — %s", e)
            out["ERR_MSG"] = "Ошибка сети при подтверждении входа"
            out["AUTHORIZED"] = None
            out["TRACK_ID"] = auth_submit["track_id"]
            out["CSRF_TOKEN"] = auth_submit.get("csrf_token", "")
            out["QR_URL"] = qr_link
            return out

        try:
            data = response.json()
        except ValueError:
            self.logger.error("Quazar API: status не JSON, HTTP %s", response.status_code)
            out["ERR_MSG"] = "Авторизация не пройдена. Попробуйте ещё раз."
            out["AUTHORIZED"] = None
            out["TRACK_ID"] = auth_submit["track_id"]
            out["CSRF_TOKEN"] = auth_submit.get("csrf_token", "")
            out["QR_URL"] = qr_link
            return out

        state = data.get("state") if isinstance(data, dict) else None
        if state != "otp_auth_finished":
            self.logger.debug("Quazar API: ожидание сканирования QR, state=%s", state or data)
            out["AUTHORIZED"] = None
            out["TRACK_ID"] = auth_submit["track_id"]
            out["CSRF_TOKEN"] = auth_submit.get("csrf_token", "")
            out["QR_URL"] = qr_link
            return out

        track_for_session = data.get("trackId") or data.get("track_id")
        if not track_for_session:
            self.logger.error("Quazar API: в ответе status нет trackId: %s", data)
            out["ERR_MSG"] = "Авторизация не пройдена. Попробуйте ещё раз."
            out["AUTHORIZED"] = None
            out["TRACK_ID"] = auth_submit["track_id"]
            out["CSRF_TOKEN"] = auth_submit.get("csrf_token", "")
            out["QR_URL"] = qr_link
            return out

        try:
            r_gs = session.post(
                "https://passport.yandex.ru/pwl-yandex/api/passport/sessions/get_session",
                data={"track_id": track_for_session},
                headers=_pwl_api_headers(x_csrf, json_body=False),
                timeout=DEFAULT_TIMEOUT,
                verify=certifi.where(),
            )
        except requests.RequestException as e:
            self.logger.error("Quazar API: sessions/get_session — %s", e)
            out["ERR_MSG"] = "Ошибка сети при получении сессии"
            out["AUTHORIZED"] = None
            out["TRACK_ID"] = auth_submit["track_id"]
            out["CSRF_TOKEN"] = auth_submit.get("csrf_token", "")
            out["QR_URL"] = qr_link
            return out

        if r_gs.status_code != 200:
            self.logger.error(
                "Quazar API: get_session HTTP %s: %.400s",
                r_gs.status_code,
                r_gs.text,
            )
            out["ERR_MSG"] = "Авторизация не пройдена. Попробуйте ещё раз."
            out["AUTHORIZED"] = None
            out["TRACK_ID"] = auth_submit["track_id"]
            out["CSRF_TOKEN"] = auth_submit.get("csrf_token", "")
            out["QR_URL"] = qr_link
            return out

        self._dump_session_cookies(session, use_cookie_file)

        with open(self.cookie_path, "w", encoding="utf-8") as f:
            json.dump(requests.utils.dict_from_cookiejar(session.cookies), f)

        self._clear_iot_auth_failure()
        self._iot_unauthorized_blocked = False
        self._iot_unauthorized_cookie_mtime = None
        self.csrf_token = self.get_token()
        self.music_token = None

        try:
            os.remove(pending_path)
        except OSError:
            pass

        check_cookie = self.api_request("https://iot.quasar.yandex.ru/m/user/scenarios")
        if isinstance(check_cookie, dict) and check_cookie.get("status") == "ok":
            out["AUTHORIZED"] = True
            self.logger.info("Quazar API: вход по QR успешен, IoT отвечает")
            return out

        self.logger.error("Quazar API: после QR IoT не подтвердил сессию: %s", check_cookie)
        if self.cookie_path and os.path.exists(self.cookie_path):
            try:
                os.remove(self.cookie_path)
            except OSError as oe:
                self.logger.warning("Quazar API: не удалось удалить cookie после неуспешной проверки: %s", oe)
        out["AUTHORIZED"] = False
        return out
