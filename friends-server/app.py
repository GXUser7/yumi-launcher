"""Yumi Friends: the friends list and online status behind Lumi Launcher.

A small JSON API on Python's standard library alone, so a host has nothing to install: SQLite
keeps the data in /data, the folder Amvera keeps between deploys. Launchers sign up with a unique
name and get a token back, add each other by name, and every half a minute tell the server what
they are doing — the answer is their friends and what those are doing.

Only friends see anything of each other: whether someone is online, the pack they play and the
server they are on. The launcher decides what to send; the server keeps the last of it and when it
came, and no history.
"""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = os.environ.get("YUMI_DATA", "/data")
PORT = int(os.environ.get("PORT", "8080"))

# A launcher reports every 30 seconds; missing two of those in a row means it is gone.
ONLINE_FOR = 75
NAME = re.compile(r"^[A-Za-z0-9_]{3,16}$")
STATES = {"launcher", "game"}
MAX_BODY = 4096
MAX_FRIENDS = 200

# Sign-ups per address per hour, so nobody can take every name in an afternoon.
SIGNUPS_PER_HOUR = 5

os.makedirs(DATA, exist_ok=True)
db = sqlite3.connect(os.path.join(DATA, "friends.db"), check_same_thread=False)
lock = threading.Lock()
signups = {}

with lock:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            token TEXT NOT NULL UNIQUE,
            created INTEGER NOT NULL,
            seen INTEGER NOT NULL DEFAULT 0,
            state TEXT,
            pack TEXT,
            server TEXT
        );
        CREATE TABLE IF NOT EXISTS requests (
            from_id INTEGER NOT NULL,
            to_id INTEGER NOT NULL,
            created INTEGER NOT NULL,
            PRIMARY KEY (from_id, to_id)
        );
        CREATE TABLE IF NOT EXISTS friends (
            a INTEGER NOT NULL,
            b INTEGER NOT NULL,
            created INTEGER NOT NULL,
            PRIMARY KEY (a, b)
        );
        """
    )
    db.commit()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def now():
    return int(time.time())


def hashed(token):
    return hashlib.sha256(token.encode()).hexdigest()


def pair(x, y):
    return (x, y) if x < y else (y, x)


def user_by_name(name):
    row = db.execute("SELECT id, name FROM users WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise ApiError(404, f"Игрока «{name}» нет — проверьте имя")
    return row


def are_friends(x, y):
    return db.execute("SELECT 1 FROM friends WHERE a = ? AND b = ?", pair(x, y)).fetchone() is not None


def befriend(x, y):
    db.execute("INSERT OR IGNORE INTO friends (a, b, created) VALUES (?, ?, ?)", (*pair(x, y), now()))
    db.execute("DELETE FROM requests WHERE (from_id = ? AND to_id = ?) OR (from_id = ? AND to_id = ?)", (x, y, y, x))


def view(me):
    """The signed-in user's side of things: their friends, online first, and pending requests."""
    user = db.execute("SELECT name FROM users WHERE id = ?", (me,)).fetchone()
    rows = db.execute(
        """
        SELECT u.name, u.seen, u.state, u.pack, u.server FROM friends f
        JOIN users u ON u.id = CASE WHEN f.a = ? THEN f.b ELSE f.a END
        WHERE f.a = ? OR f.b = ?
        """,
        (me, me, me),
    ).fetchall()
    cutoff = now() - ONLINE_FOR
    friends = []
    for name, seen, state, pack, server in rows:
        online = seen >= cutoff
        friends.append(
            {
                "name": name,
                "online": online,
                "state": state if online else None,
                "pack": json.loads(pack) if online and pack else None,
                "server": server if online else None,
                "seen": seen,
            }
        )
    order = {"game": 0, "launcher": 1, None: 2}
    friends.sort(key=lambda f: (order.get(f["state"], 2), f["name"].lower()))
    incoming = [r[0] for r in db.execute(
        "SELECT u.name FROM requests r JOIN users u ON u.id = r.from_id WHERE r.to_id = ? ORDER BY r.created", (me,))]
    outgoing = [r[0] for r in db.execute(
        "SELECT u.name FROM requests r JOIN users u ON u.id = r.to_id WHERE r.from_id = ? ORDER BY r.created", (me,))]
    return {"me": {"name": user[0]}, "friends": friends, "incoming": incoming, "outgoing": outgoing}


def text(value, limit):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(400, "Неверный запрос")
    value = value.strip()
    return value[:limit] or None


def register(body, address):
    name = body.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        raise ApiError(400, "Имя — от 3 до 16 символов: латиница, цифры и _")
    hour = now() // 3600
    key = (address, hour)
    if signups.get(key, 0) >= SIGNUPS_PER_HOUR:
        raise ApiError(429, "Слишком много новых профилей с этого адреса — попробуйте через час")
    if db.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
        raise ApiError(409, f"Имя «{name}» уже занято — выберите другое")
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO users (name, token, created) VALUES (?, ?, ?)", (name, hashed(token), now()))
    signups[key] = signups.get(key, 0) + 1
    for old in [k for k in signups if k[1] < hour]:
        del signups[old]
    return 201, {"name": name, "token": token}


def presence(me, body):
    state = body.get("state")
    if state not in STATES:
        raise ApiError(400, "Неверный запрос")
    pack = body.get("pack")
    if pack is not None:
        if not isinstance(pack, dict):
            raise ApiError(400, "Неверный запрос")
        pack = json.dumps(
            {"name": text(pack.get("name"), 64), "loader": text(pack.get("loader"), 16), "version": text(pack.get("version"), 32)},
            ensure_ascii=False,
        )
    server = text(body.get("server"), 100)
    db.execute("UPDATE users SET seen = ?, state = ?, pack = ?, server = ? WHERE id = ?", (now(), state, pack, server, me))
    return 200, view(me)


def add(me, body):
    other, name = user_by_name(text(body.get("name"), 16) or "")
    if other == me:
        raise ApiError(400, "Это вы :)")
    if not are_friends(me, other):
        count = db.execute("SELECT COUNT(*) FROM friends WHERE a = ? OR b = ?", (me, me)).fetchone()[0]
        if count >= MAX_FRIENDS:
            raise ApiError(400, f"Друзей может быть не больше {MAX_FRIENDS}")
        # They asked first: adding them back is saying yes.
        if db.execute("SELECT 1 FROM requests WHERE from_id = ? AND to_id = ?", (other, me)).fetchone():
            befriend(me, other)
        else:
            db.execute("INSERT OR IGNORE INTO requests (from_id, to_id, created) VALUES (?, ?, ?)", (me, other, now()))
    return 200, view(me)


def accept(me, body):
    other, name = user_by_name(text(body.get("name"), 16) or "")
    if not db.execute("SELECT 1 FROM requests WHERE from_id = ? AND to_id = ?", (other, me)).fetchone():
        raise ApiError(404, f"Заявки от «{name}» нет")
    befriend(me, other)
    return 200, view(me)


def decline(me, body):
    other, _ = user_by_name(text(body.get("name"), 16) or "")
    db.execute("DELETE FROM requests WHERE from_id = ? AND to_id = ?", (other, me))
    return 200, view(me)


def remove(me, body):
    """Unfriends, or takes back a request not yet answered."""
    other, _ = user_by_name(text(body.get("name"), 16) or "")
    db.execute("DELETE FROM friends WHERE a = ? AND b = ?", pair(me, other))
    db.execute("DELETE FROM requests WHERE from_id = ? AND to_id = ?", (me, other))
    return 200, view(me)


def offline(me, _):
    db.execute("UPDATE users SET seen = 0, state = NULL, pack = NULL, server = NULL WHERE id = ?", (me,))
    return 200, {"ok": True}


def delete(me, _):
    """The profile and everything about it; the name is free again."""
    db.execute("DELETE FROM friends WHERE a = ? OR b = ?", (me, me))
    db.execute("DELETE FROM requests WHERE from_id = ? OR to_id = ?", (me, me))
    db.execute("DELETE FROM users WHERE id = ?", (me,))
    return 200, {"ok": True}


AUTHED = {
    "/api/presence": presence,
    "/api/friends": lambda me, body: (200, view(me)),
    "/api/friends/add": add,
    "/api/friends/accept": accept,
    "/api/friends/decline": decline,
    "/api/friends/remove": remove,
    "/api/offline": offline,
    "/api/delete": delete,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "YumiFriends/1"

    def do_GET(self):
        if self.path in ("/", "/api/health"):
            self.reply(200, {"ok": True, "service": "yumi-friends"})
        else:
            self.reply(404, {"error": "Нет такого адреса"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(413, "Слишком большой запрос")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                raise ApiError(400, "Неверный запрос")
            if not isinstance(body, dict):
                raise ApiError(400, "Неверный запрос")
            with lock:
                if self.path == "/api/register":
                    status, answer = register(body, self.address())
                else:
                    route = AUTHED.get(self.path)
                    if route is None:
                        raise ApiError(404, "Нет такого адреса")
                    status, answer = route(self.user(), body)
                db.commit()
            self.reply(status, answer)
        except ApiError as e:
            with lock:
                db.rollback()
            self.reply(e.status, {"error": e.message})
        except Exception:
            with lock:
                db.rollback()
            self.reply(500, {"error": "Сервер друзей споткнулся — попробуйте ещё раз"})

    def user(self):
        header = self.headers.get("Authorization") or ""
        token = header[7:] if header.startswith("Bearer ") else ""
        row = db.execute("SELECT id FROM users WHERE token = ?", (hashed(token),)).fetchone() if token else None
        if row is None:
            raise ApiError(401, "Профиль друзей не найден — создайте его заново")
        return row[0]

    def address(self):
        # Behind Amvera's proxy the caller is the first address it forwards.
        forwarded = self.headers.get("X-Forwarded-For")
        return forwarded.split(",")[0].strip() if forwarded else self.client_address[0]

    def reply(self, status, answer):
        data = json.dumps(answer, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        # One line per request, without the addresses and tokens a full log would keep.
        print(f"{self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}", flush=True)


if __name__ == "__main__":
    print(f"Yumi Friends on port {PORT}, data in {DATA}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
