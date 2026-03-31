import json
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

BASE_URL = "https://lasplatas.com"
TOP_N = 10
BALANCE_WORKERS = 20
ECUADOR_TZ = timezone(timedelta(hours=-5))
SESSION_COOKIE_NAME = "lasplatas_sid"
SESSION_MAX_AGE = 60 * 60 * 24 * 365

PANEL_HTML_FILE = Path(__file__).with_name("lasplatas_panel.html")
SALDOS_HTML_FILE = Path(__file__).with_name("lasplatas_saldos.html")
TRANSFERIR_HTML_FILE = Path(__file__).with_name("lasplatas_transferir.html")
LOGIN_HTML_FILE = Path(__file__).with_name("lasplatas_login.html")
SESSIONS_DIR = Path(__file__).with_name("lasplatas_sessions")

SESSIONS_DIR.mkdir(exist_ok=True)
SESSION_STATES = {}


def empty_session_state():
    return {
        "username": None,
        "password": None,
        "jwt": None,
        "agent_id": None,
        "users": [],
        "top_rows": None,
        "recent_recharges": None,
        "player_balance": None,
    }


def session_file(sid):
    return SESSIONS_DIR / f"{sid}.json"


def ensure_session_state(sid):
    if sid not in SESSION_STATES:
        SESSION_STATES[sid] = empty_session_state()
        path = session_file(sid)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                SESSION_STATES[sid]["username"] = data.get("username")
                SESSION_STATES[sid]["password"] = data.get("password")
            except Exception:
                pass
    return SESSION_STATES[sid]


def save_session_credentials(sid, username, password):
    session_file(sid).write_text(
        json.dumps({"username": username, "password": password}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def delete_session_credentials(sid):
    path = session_file(sid)
    if path.exists():
        path.unlink()


def has_credentials(session):
    return bool(session["username"] and session["password"])


def invalidate_cache(session, *, keep_credentials=True):
    if not keep_credentials:
        session["username"] = None
        session["password"] = None
    session["jwt"] = None
    session["agent_id"] = None
    session["users"] = []
    session["top_rows"] = None
    session["recent_recharges"] = None
    session["player_balance"] = None


def configure_credentials(session, sid, username, password, *, persist=True):
    session["username"] = (username or "").strip()
    session["password"] = password or ""
    invalidate_cache(session, keep_credentials=True)
    if persist:
        save_session_credentials(sid, session["username"], session["password"])


def find_first_value(data, keys):
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if value is not None:
                return value
        for value in data.values():
            found = find_first_value(value, keys)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = find_first_value(item, keys)
            if found is not None:
                return found
    return None


def get_agent_id_from_jwt(jwt):
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(__import__("base64").urlsafe_b64decode(payload).decode("utf-8"))
        return find_first_value(data, ("userId", "id", "playerId", "sub"))
    except Exception:
        return None


def login(session):
    if not has_credentials(session):
        raise PermissionError("No hay credenciales guardadas.")

    resp = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"login": session["username"], "password": session["password"]},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success") or not data.get("jwt"):
        raise PermissionError("Usuario o contraseña inválidos.")
    session["jwt"] = data["jwt"]
    session["agent_id"] = None
    return session["jwt"]


def api_request(session, method, path, *, json_body=None, retry=True, timeout=20):
    if not session["jwt"]:
        login(session)

    resp = requests.request(
        method,
        f"{BASE_URL}{path}",
        headers={"Authorization": f"Bearer {session['jwt']}"},
        json=json_body,
        timeout=timeout,
    )

    if resp.status_code == 401 and retry:
        session["jwt"] = None
        login(session)
        return api_request(session, method, path, json_body=json_body, retry=False, timeout=timeout)

    resp.raise_for_status()
    return resp


def ensure_agent_id(session):
    if session["agent_id"] is not None:
        return int(session["agent_id"])

    try:
        resp = api_request(session, "GET", "/api/player/details")
        data = resp.json()
        session["agent_id"] = find_first_value(data, ("userId", "id", "playerId"))
    except Exception:
        session["agent_id"] = None

    if session["agent_id"] is None and session["jwt"]:
        session["agent_id"] = get_agent_id_from_jwt(session["jwt"])

    if session["agent_id"] is None:
        raise RuntimeError("No se pudo obtener el userId de la cuenta origen.")

    return int(session["agent_id"])


def load_users(session):
    resp = api_request(session, "GET", "/api/v2/hierarchy/get-direct-children-with-balance")
    data = resp.json()
    session["users"] = data if isinstance(data, list) else []
    session["top_rows"] = None
    return session["users"]


def ensure_users(session):
    if session["users"]:
        return session["users"]
    return load_users(session)


def get_balance(session, user_id):
    try:
        resp = api_request(session, "GET", f"/api/v2/balance?userId={user_id}", timeout=15)
        data = resp.json()
        cents = (data.get("balance") or 0) + (data.get("cashBalance") or 0)
        return round(cents / 100, 2)
    except Exception:
        return 0.0


def get_top_balances(session):
    if session["top_rows"] is not None:
        return session["top_rows"]

    users = ensure_users(session)
    balances = []
    with ThreadPoolExecutor(max_workers=BALANCE_WORKERS) as executor:
        futures = {
            executor.submit(get_balance, session, user["userId"]): user
            for user in users
        }
        for future in as_completed(futures):
            user = futures[future]
            balances.append(
                {
                    "username": user["username"],
                    "userId": user["userId"],
                    "balance": future.result(),
                }
            )

    balances.sort(key=lambda item: item["balance"], reverse=True)
    session["top_rows"] = balances[:TOP_N]
    return session["top_rows"]


def get_player_balance(session):
    if session["player_balance"] is not None:
        return session["player_balance"]

    resp = api_request(session, "GET", "/api/player/balance", timeout=20)
    data = resp.json()
    cents = data.get("cash")
    if cents is None:
        cents = data.get("balance")
    if cents is None:
        cents = 0

    session["player_balance"] = {
        "amount": round(float(cents) / 100, 2),
        "currency": data.get("currency") or "USD",
    }
    return session["player_balance"]


def normalize_financial_rows(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("rows", "items", "data", "results", "transactions"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def to_ecuador_datetime(value):
    if not value:
        return ""

    parsed = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue

    if parsed is None:
        return value

    parsed = parsed.replace(tzinfo=timezone.utc).astimezone(ECUADOR_TZ)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def get_recent_recharges(session):
    if session["recent_recharges"] is not None:
        return session["recent_recharges"]

    source_user_id = int(ensure_agent_id(session))
    now_ec = datetime.now(ECUADOR_TZ)
    today = now_ec.strftime("%Y-%m-%d")
    tomorrow = (now_ec + timedelta(days=1)).strftime("%Y-%m-%d")
    body = {"userId": source_user_id, "dateFrom": today, "dateTo": tomorrow}

    resp = api_request(session, "POST", "/agent-api/transactions/financial", json_body=body, timeout=20)
    rows = normalize_financial_rows(resp.json())

    filtered = []
    for item in rows:
        if str(item.get("type")) != "transferBalance":
            continue
        if str(item.get("fromUserId")) != str(source_user_id):
            continue
        filtered.append(
            {
                "fromUsername": item.get("fromUsername") or "",
                "toUsername": item.get("toUsername") or "",
                "amount": round((float(item.get("amount") or 0) / 100), 2),
                "dateTime": to_ecuador_datetime(item.get("dateTime") or ""),
                "referenceId": item.get("referenceId") or "",
            }
        )

    filtered.sort(key=lambda item: item["dateTime"], reverse=True)
    session["recent_recharges"] = filtered[:5]
    return session["recent_recharges"]


def transfer(session, target_user_id, amount_usd):
    source_user_id = ensure_agent_id(session)
    amount_cents = int(round(float(amount_usd) * 100))
    body = {
        "sourceUserId": int(source_user_id),
        "targetUserId": int(target_user_id),
        "amount": amount_cents,
    }
    resp = api_request(session, "POST", "/api/v2/balance/transfer", json_body=body)
    session["top_rows"] = None
    session["recent_recharges"] = None
    session["player_balance"] = None
    return {"ok": True, "status": resp.status_code}


def get_init_payload(session):
    if not has_credentials(session):
        raise PermissionError("No hay sesión configurada.")
    return {
        "username": session["username"],
        "agentId": ensure_agent_id(session),
        "users": ensure_users(session),
        "playerBalance": get_player_balance(session),
    }


def refresh_all(session):
    if not has_credentials(session):
        raise PermissionError("No hay sesión configurada.")
    invalidate_cache(session, keep_credentials=True)
    return get_init_payload(session)


def logout_session(sid, session):
    invalidate_cache(session, keep_credentials=False)
    delete_session_credentials(sid)
    SESSION_STATES.pop(sid, None)


class Handler(BaseHTTPRequestHandler):
    def get_session_id(self):
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        cookie = SimpleCookie()
        cookie.load(raw)
        morsel = cookie.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel else None

    def get_session(self):
        sid = self.get_session_id()
        if not sid:
            return None, None
        return sid, ensure_session_state(sid)

    def new_session(self):
        sid = secrets.token_urlsafe(24)
        return sid, ensure_session_state(sid)

    def _send_json(self, payload, status=200, extra_headers=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for key, value in extra_headers or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _send_html_file(self, path, status=200, extra_headers=None):
        data = path.read_text(encoding="utf-8").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for key, value in extra_headers or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def _session_cookie_header(self, sid):
        return ("Set-Cookie", f"{SESSION_COOKIE_NAME}={sid}; Path=/; Max-Age={SESSION_MAX_AGE}; SameSite=Lax")

    def _clear_session_cookie_header(self):
        return ("Set-Cookie", f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; SameSite=Lax")

    def log_message(self, format, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        sid, session = self.get_session()

        try:
            if parsed.path == "/login":
                self._send_html_file(LOGIN_HTML_FILE)
                return

            if parsed.path == "/":
                self._send_html_file(PANEL_HTML_FILE if session and has_credentials(session) else LOGIN_HTML_FILE)
                return

            if parsed.path == "/saldos":
                self._send_html_file(SALDOS_HTML_FILE if session and has_credentials(session) else LOGIN_HTML_FILE)
                return

            if parsed.path == "/transferir":
                self._send_html_file(TRANSFERIR_HTML_FILE if session and has_credentials(session) else LOGIN_HTML_FILE)
                return

            if parsed.path == "/api/init":
                if not session:
                    raise PermissionError("No hay sesión configurada.")
                self._send_json(get_init_payload(session))
                return

            if parsed.path == "/api/session":
                self._send_json({
                    "configured": bool(session and has_credentials(session)),
                    "username": session["username"] if session else None,
                })
                return

            if parsed.path == "/api/top":
                if not session:
                    raise PermissionError("No hay sesión configurada.")
                self._send_json({"rows": get_top_balances(session)})
                return

            if parsed.path == "/api/recent-recharges":
                if not session:
                    raise PermissionError("No hay sesión configurada.")
                self._send_json({"rows": get_recent_recharges(session)})
                return

            if parsed.path == "/api/users":
                if not session:
                    raise PermissionError("No hay sesión configurada.")
                users = ensure_users(session)
                query = parse_qs(parsed.query).get("q", [""])[0].strip().upper()
                if query:
                    users = [user for user in users if query in str(user.get("username", "")).upper()]
                self._send_json({"users": users[:20]})
                return

            self._send_json({"error": "Ruta no encontrada."}, status=404)
        except PermissionError as exc:
            self._send_json({"error": str(exc)}, status=401)
        except requests.HTTPError as exc:
            response = exc.response
            detail = response.text if response is not None else str(exc)
            status = response.status_code if response is not None else 500
            self._send_json({"error": detail}, status=status)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    def do_POST(self):
        parsed = urlparse(self.path)
        sid, session = self.get_session()

        try:
            if parsed.path == "/api/login":
                payload = self._read_json()
                username = payload.get("username")
                password = payload.get("password")
                if not username or not password:
                    self._send_json({"error": "Faltan credenciales."}, status=400)
                    return

                if not session:
                    sid, session = self.new_session()

                configure_credentials(session, sid, username, password, persist=True)
                login(session)
                self._send_json(get_init_payload(session), extra_headers=[self._session_cookie_header(sid)])
                return

            if parsed.path == "/api/logout":
                if sid and session:
                    logout_session(sid, session)
                self._send_json({"ok": True}, extra_headers=[self._clear_session_cookie_header()])
                return

            if parsed.path == "/api/refresh":
                if not session:
                    raise PermissionError("No hay sesión configurada.")
                self._send_json(refresh_all(session))
                return

            if parsed.path == "/api/transfer":
                if not session:
                    raise PermissionError("No hay sesión configurada.")
                payload = self._read_json()
                target_user_id = payload.get("targetUserId")
                amount = payload.get("amount")
                if not target_user_id:
                    self._send_json({"error": "Falta targetUserId."}, status=400)
                    return
                if amount is None or float(amount) <= 0:
                    self._send_json({"error": "Monto inválido."}, status=400)
                    return
                self._send_json(transfer(session, target_user_id, amount))
                return

            self._send_json({"error": "Ruta no encontrada."}, status=404)
        except PermissionError as exc:
            self._send_json({"error": str(exc)}, status=401)
        except requests.HTTPError as exc:
            response = exc.response
            detail = response.text if response is not None else str(exc)
            status = response.status_code if response is not None else 500
            self._send_json({"error": detail}, status=status)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    print("Panel disponible en:")
    print("  http://127.0.0.1:8000")
    print("  http://192.168.1.69:8000")
    server.serve_forever()


if __name__ == "__main__":
    main()
