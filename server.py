#!/usr/bin/env python3
"""Ася — самодельный мессенджер в стиле старых пейджеров (UIN, контакты, статусы, файлы).

Только стандартная библиотека Python 3.8+. Запуск:  python3 server.py
"""
import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

FROZEN = bool(getattr(sys, "frozen", False))   # True, когда запущен как .exe
if FROZEN:
    # ресурсы упакованы внутрь .exe, а данные хранятся рядом с ним
    BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    DATA = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "data")
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
    DATA = os.path.join(BASE, "data")
STATIC = os.path.join(BASE, "static")
UPLOADS = os.path.join(DATA, "uploads")
DB_PATH = os.path.join(DATA, "chat.db")

MAX_FILE = 25 * 1024 * 1024
MAX_TEXT = 4000
MAX_JSON = 64 * 1024
ONLINE_TTL = 45          # секунд без опроса -> считаем «не в сети»
POLL_WAIT = 20           # длительность long-poll
STATUSES = {"online", "away", "dnd", "invisible"}
PBKDF_ITER = 200_000

cond = threading.Condition()
version = 0
fail_lock = threading.Lock()
fails = {}               # uin -> [timestamps неудачных входов]


def bump():
    global version
    with cond:
        version += 1
        cond.notify_all()


def now():
    return time.time()


def db():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    os.makedirs(UPLOADS, exist_ok=True)
    c = db()
    c.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users(
            uin INTEGER PRIMARY KEY,
            nick TEXT NOT NULL,
            salt BLOB NOT NULL,
            pw BLOB NOT NULL,
            status TEXT NOT NULL DEFAULT 'online',
            last_seen REAL NOT NULL DEFAULT 0,
            created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tokens(
            token TEXT PRIMARY KEY,
            uin INTEGER NOT NULL,
            created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS contacts(
            owner INTEGER NOT NULL,
            contact INTEGER NOT NULL,
            PRIMARY KEY(owner, contact)
        );
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender INTEGER NOT NULL,
            recipient INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'text',
            body TEXT NOT NULL DEFAULT '',
            file_name TEXT,
            file_path TEXT,
            file_size INTEGER,
            ts REAL NOT NULL,
            read INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_msg_pair ON messages(sender, recipient, id);
        CREATE INDEX IF NOT EXISTS idx_msg_rcpt ON messages(recipient, read);
        """
    )
    c.commit()
    c.close()


def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF_ITER)


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def shown_status(status, last_seen):
    """Статус, который видят другие пользователи."""
    if status == "invisible" or now() - last_seen > ONLINE_TTL:
        return "offline"
    return status


def msg_json(r):
    d = {
        "id": r["id"],
        "from": r["sender"],
        "to": r["recipient"],
        "kind": r["kind"],
        "text": r["body"],
        "ts": r["ts"],
    }
    if r["kind"] == "file":
        d["file"] = {"name": r["file_name"], "size": r["file_size"]}
    return d


def contacts_snapshot(c, uid):
    rows = c.execute(
        """
        SELECT u.uin, u.nick, u.status, u.last_seen,
               (SELECT COUNT(*) FROM messages m
                 WHERE m.sender = u.uin AND m.recipient = ? AND m.read = 0) AS unread
          FROM contacts k JOIN users u ON u.uin = k.contact
         WHERE k.owner = ?
         ORDER BY u.nick COLLATE NOCASE
        """,
        (uid, uid),
    ).fetchall()
    return [
        {
            "uin": r["uin"],
            "nick": r["nick"],
            "status": shown_status(r["status"], r["last_seen"]),
            "unread": r["unread"],
        }
        for r in rows
    ]


def clean_nick(s):
    s = re.sub(r"[\x00-\x1f\x7f]", "", str(s or "")).strip()
    if not 1 <= len(s) <= 32:
        raise ApiError(400, "Ник: от 1 до 32 символов")
    return s


def parse_uin(v):
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise ApiError(400, "Неверный UIN")
    if not 10_000 <= n <= 9_999_999_999:
        raise ApiError(400, "Неверный UIN")
    return n


def new_token(c, uin):
    t = secrets.token_urlsafe(32)
    c.execute("INSERT INTO tokens(token, uin, created) VALUES(?,?,?)", (t, uin, now()))
    return t


class Handler(BaseHTTPRequestHandler):
    server_version = "Asya/1.0"
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        pass

    # ---------- утилиты ----------
    def send_json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_JSON:
            raise ApiError(413, "Слишком большой запрос")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            d = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "Некорректный JSON")
        if not isinstance(d, dict):
            raise ApiError(400, "Некорректный JSON")
        return d

    def auth(self):
        h = self.headers.get("Authorization", "")
        if not h.startswith("Bearer "):
            raise ApiError(401, "Нужен вход")
        c = db()
        try:
            r = c.execute("SELECT uin FROM tokens WHERE token=?", (h[7:],)).fetchone()
        finally:
            c.close()
        if not r:
            raise ApiError(401, "Сессия истекла")
        return r["uin"]

    # ---------- маршрутизация ----------
    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        try:
            u = urlparse(self.path)
            path, q = u.path, parse_qs(u.query)
            if method == "GET" and not path.startswith("/api/"):
                return self.serve_static(path)
            fn = ROUTES.get((method, path))
            if fn is None and method == "GET" and path.startswith("/api/file/"):
                fn = Handler.api_file
            if fn is None:
                raise ApiError(404, "Не найдено")
            fn(self, q, path)
        except ApiError as e:
            self.send_json(e.code, {"error": e.msg})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            try:
                self.send_json(500, {"error": "Внутренняя ошибка сервера"})
            except Exception:
                pass

    def serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        name = path.lstrip("/")
        if name.startswith("static/"):
            name = name[len("static/"):]
        full = os.path.normpath(os.path.join(STATIC, name))
        if not full.startswith(STATIC + os.sep) or not os.path.isfile(full):
            raise ApiError(404, "Не найдено")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            raw = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or "javascript" in ctype else ""))
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    # ---------- API: аккаунт ----------
    def api_register(self, q, path):
        d = self.body_json()
        nick = clean_nick(d.get("nick"))
        pw = str(d.get("password") or "")
        if not 6 <= len(pw) <= 128:
            raise ApiError(400, "Пароль: от 6 до 128 символов")
        salt = secrets.token_bytes(16)
        pwh = hash_pw(pw, salt)
        c = db()
        try:
            for _ in range(50):
                uin = secrets.randbelow(900_000_000) + 100_000_000
                try:
                    c.execute(
                        "INSERT INTO users(uin,nick,salt,pw,status,last_seen,created) VALUES(?,?,?,?,?,?,?)",
                        (uin, nick, salt, pwh, "online", now(), now()),
                    )
                    break
                except sqlite3.IntegrityError:
                    continue
            else:
                raise ApiError(500, "Не удалось выдать UIN")
            token = new_token(c, uin)
            c.commit()
        finally:
            c.close()
        bump()
        self.send_json(200, {"uin": uin, "nick": nick, "token": token})

    def api_login(self, q, path):
        d = self.body_json()
        uin = parse_uin(d.get("uin"))
        pw = str(d.get("password") or "")
        with fail_lock:
            lst = [t for t in fails.get(uin, []) if now() - t < 300]
            fails[uin] = lst
            if len(lst) >= 10:
                raise ApiError(429, "Слишком много попыток, подождите 5 минут")
        c = db()
        try:
            r = c.execute("SELECT * FROM users WHERE uin=?", (uin,)).fetchone()
            ok = bool(r) and hmac.compare_digest(hash_pw(pw, r["salt"]), r["pw"])
            if not ok:
                with fail_lock:
                    fails[uin].append(now())
                raise ApiError(403, "Неверный UIN или пароль")
            token = new_token(c, uin)
            c.execute("UPDATE users SET last_seen=? WHERE uin=?", (now(), uin))
            c.commit()
            nick = r["nick"]
        finally:
            c.close()
        bump()
        self.send_json(200, {"uin": uin, "nick": nick, "token": token})

    def api_logout(self, q, path):
        uid = self.auth()
        tok = self.headers.get("Authorization")[7:]
        c = db()
        try:
            c.execute("DELETE FROM tokens WHERE token=?", (tok,))
            left = c.execute("SELECT COUNT(*) FROM tokens WHERE uin=?", (uid,)).fetchone()[0]
            if left == 0:
                c.execute("UPDATE users SET last_seen=0 WHERE uin=?", (uid,))
            c.commit()
        finally:
            c.close()
        bump()
        self.send_json(200, {"ok": True})

    def api_me(self, q, path):
        uid = self.auth()
        c = db()
        try:
            u = c.execute("SELECT uin,nick,status FROM users WHERE uin=?", (uid,)).fetchone()
            last = c.execute(
                "SELECT COALESCE(MAX(id),0) FROM messages WHERE sender=? OR recipient=?", (uid, uid)
            ).fetchone()[0]
            c.execute("UPDATE users SET last_seen=? WHERE uin=?", (now(), uid))
            c.commit()
            snap = contacts_snapshot(c, uid)
        finally:
            c.close()
        bump()
        self.send_json(
            200,
            {"uin": u["uin"], "nick": u["nick"], "status": u["status"], "last": last, "contacts": snap},
        )

    def api_status(self, q, path):
        uid = self.auth()
        s = str(self.body_json().get("status"))
        if s not in STATUSES:
            raise ApiError(400, "Неверный статус")
        c = db()
        try:
            c.execute("UPDATE users SET status=?, last_seen=? WHERE uin=?", (s, now(), uid))
            c.commit()
        finally:
            c.close()
        bump()
        self.send_json(200, {"ok": True})

    # ---------- API: контакты ----------
    def api_contact_add(self, q, path):
        uid = self.auth()
        target = parse_uin(self.body_json().get("uin"))
        if target == uid:
            raise ApiError(400, "Нельзя добавить самого себя")
        c = db()
        try:
            if not c.execute("SELECT 1 FROM users WHERE uin=?", (target,)).fetchone():
                raise ApiError(404, "Пользователь с таким UIN не найден")
            c.execute("INSERT OR IGNORE INTO contacts(owner,contact) VALUES(?,?)", (uid, target))
            c.commit()
        finally:
            c.close()
        bump()
        self.send_json(200, {"ok": True})

    def api_contact_remove(self, q, path):
        uid = self.auth()
        target = parse_uin(self.body_json().get("uin"))
        c = db()
        try:
            c.execute("DELETE FROM contacts WHERE owner=? AND contact=?", (uid, target))
            c.commit()
        finally:
            c.close()
        bump()
        self.send_json(200, {"ok": True})

    # ---------- API: сообщения ----------
    def _add_message(self, c, uid, to, kind, body, fname=None, fpath=None, fsize=None):
        if not c.execute("SELECT 1 FROM users WHERE uin=?", (to,)).fetchone():
            raise ApiError(404, "Получатель не найден")
        cur = c.execute(
            "INSERT INTO messages(sender,recipient,kind,body,file_name,file_path,file_size,ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (uid, to, kind, body, fname, fpath, fsize, now()),
        )
        c.execute("INSERT OR IGNORE INTO contacts(owner,contact) VALUES(?,?)", (uid, to))
        c.execute("INSERT OR IGNORE INTO contacts(owner,contact) VALUES(?,?)", (to, uid))
        r = c.execute("SELECT * FROM messages WHERE id=?", (cur.lastrowid,)).fetchone()
        c.commit()
        return msg_json(r)

    def api_send(self, q, path):
        uid = self.auth()
        d = self.body_json()
        to = parse_uin(d.get("to"))
        text = str(d.get("text") or "").replace("\r\n", "\n").strip("\n")
        if not text.strip():
            raise ApiError(400, "Пустое сообщение")
        if len(text) > MAX_TEXT:
            raise ApiError(400, "Сообщение длиннее %d символов" % MAX_TEXT)
        c = db()
        try:
            m = self._add_message(c, uid, to, "text", text)
        finally:
            c.close()
        bump()
        self.send_json(200, m)

    def api_upload(self, q, path):
        uid = self.auth()
        to = parse_uin((q.get("to") or [""])[0])
        name = os.path.basename(unquote((q.get("name") or ["file"])[0]).replace("\\", "/"))
        name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()[:120] or "file"
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise ApiError(400, "Пустой файл")
        if n > MAX_FILE:
            raise ApiError(413, "Файл больше %d МБ" % (MAX_FILE // 1048576))
        c = db()
        try:
            if not c.execute("SELECT 1 FROM users WHERE uin=?", (to,)).fetchone():
                raise ApiError(404, "Получатель не найден")
        finally:
            c.close()
        stored = secrets.token_hex(16)
        full = os.path.join(UPLOADS, stored)
        left = n
        try:
            with open(full, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(65536, left))
                    if not chunk:
                        raise ApiError(400, "Загрузка прервана")
                    f.write(chunk)
                    left -= len(chunk)
            c = db()
            try:
                m = self._add_message(c, uid, to, "file", "", name, stored, n)
            finally:
                c.close()
        except Exception:
            if os.path.exists(full):
                os.remove(full)
            raise
        bump()
        self.send_json(200, m)

    def api_file(self, q, path):
        uid = self.auth()
        try:
            mid = int(path.rsplit("/", 1)[1])
        except ValueError:
            raise ApiError(404, "Не найдено")
        c = db()
        try:
            r = c.execute(
                "SELECT * FROM messages WHERE id=? AND kind='file' AND (sender=? OR recipient=?)",
                (mid, uid, uid),
            ).fetchone()
        finally:
            c.close()
        if not r:
            raise ApiError(404, "Файл не найден")
        full = os.path.join(UPLOADS, r["file_path"])
        if not os.path.isfile(full):
            raise ApiError(404, "Файл удалён")
        size = os.path.getsize(full)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition", "attachment; filename*=UTF-8''" + quote(r["file_name"], safe="")
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with open(full, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def api_history(self, q, path):
        uid = self.auth()
        other = parse_uin((q.get("with") or [""])[0])
        c = db()
        try:
            rows = c.execute(
                """SELECT * FROM (
                     SELECT * FROM messages
                      WHERE (sender=? AND recipient=?) OR (sender=? AND recipient=?)
                      ORDER BY id DESC LIMIT 300
                   ) ORDER BY id""",
                (uid, other, other, uid),
            ).fetchall()
        finally:
            c.close()
        self.send_json(200, {"messages": [msg_json(r) for r in rows]})

    def api_read(self, q, path):
        uid = self.auth()
        other = parse_uin(self.body_json().get("with"))
        c = db()
        try:
            cur = c.execute(
                "UPDATE messages SET read=1 WHERE sender=? AND recipient=? AND read=0", (other, uid)
            )
            c.commit()
            changed = cur.rowcount
        finally:
            c.close()
        if changed:
            bump()
        self.send_json(200, {"ok": True})

    # ---------- long-poll ----------
    def api_poll(self, q, path):
        uid = self.auth()
        try:
            since = int((q.get("since") or ["0"])[0])
            client_v = int((q.get("v") or ["-1"])[0])
        except ValueError:
            raise ApiError(400, "Неверные параметры")
        c = db()
        try:
            prev = c.execute("SELECT last_seen FROM users WHERE uin=?", (uid,)).fetchone()
            c.execute("UPDATE users SET last_seen=? WHERE uin=?", (now(), uid))
            c.commit()
        finally:
            c.close()
        if prev and now() - prev["last_seen"] > ONLINE_TTL:
            bump()
        deadline = now() + POLL_WAIT
        while True:
            with cond:
                cur_v = version
            c = db()
            try:
                rows = c.execute(
                    "SELECT * FROM messages WHERE id>? AND (sender=? OR recipient=?) ORDER BY id LIMIT 200",
                    (since, uid, uid),
                ).fetchall()
                if rows or cur_v != client_v or now() >= deadline:
                    snap = contacts_snapshot(c, uid)
                    me = c.execute("SELECT status FROM users WHERE uin=?", (uid,)).fetchone()
                    break
            finally:
                c.close()
            with cond:
                cond.wait(min(5, max(0.1, deadline - now())))
            c = db()
            try:
                c.execute("UPDATE users SET last_seen=? WHERE uin=?", (now(), uid))
                c.commit()
            finally:
                c.close()
        self.send_json(
            200,
            {"messages": [msg_json(r) for r in rows], "contacts": snap, "v": cur_v, "status": me["status"]},
        )


ROUTES = {
    ("POST", "/api/register"): Handler.api_register,
    ("POST", "/api/login"): Handler.api_login,
    ("POST", "/api/logout"): Handler.api_logout,
    ("GET", "/api/me"): Handler.api_me,
    ("POST", "/api/status"): Handler.api_status,
    ("POST", "/api/contacts/add"): Handler.api_contact_add,
    ("POST", "/api/contacts/remove"): Handler.api_contact_remove,
    ("POST", "/api/send"): Handler.api_send,
    ("POST", "/api/upload"): Handler.api_upload,
    ("GET", "/api/history"): Handler.api_history,
    ("POST", "/api/read"): Handler.api_read,
    ("GET", "/api/poll"): Handler.api_poll,
}


def main():
    ap = argparse.ArgumentParser(description="Ася — сервер мессенджера")
    ap.add_argument("--host", default="127.0.0.1", help="адрес (0.0.0.0 — для доступа из сети)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер при запуске")
    a = ap.parse_args()
    try:
        init_db()
        srv = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError as e:
        print("Не удалось запустить сервер на порту %d: %s" % (a.port, e))
        print("Возможно, Ася уже запущена, либо порт занят. Попробуйте другой: --port 8766")
        pause_if_exe()
        sys.exit(1)
    srv.daemon_threads = True
    shown = "127.0.0.1" if a.host in ("0.0.0.0", "") else a.host
    url = "http://%s:%d" % (shown, a.port)
    print("Ася запущена: %s" % url)
    print("Окно не закрывайте: пока оно открыто, мессенджер работает.")
    print("Данные хранятся в папке: %s" % DATA)
    if FROZEN and not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")


def pause_if_exe():
    """В .exe окно закрывается сразу, поэтому ждём, чтобы пользователь успел прочитать сообщение."""
    if FROZEN:
        try:
            input("Нажмите Enter, чтобы закрыть…")
        except EOFError:
            pass


if __name__ == "__main__":
    main()
