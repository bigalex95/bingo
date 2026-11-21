#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
Мультиплеерное Бинго — сервер.

Только стандартная библиотека Python: обычный HTTP для действий
и Server-Sent Events для живых обновлений.

Запуск:  python server.py      (или  uv run server.py)
Порт:    переменная окружения PORT (по умолчанию 8000)
"""

from __future__ import annotations

import json
import os
import random
import secrets
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")
PUBLIC_DIR = (Path(__file__).resolve().parent / "public").resolve()

PATTERNS = {
    "line": "Линия",
    "two_lines": "Две линии",
    "corners": "Четыре угла",
    "full": "Вся карточка",
}
AUTO_INTERVALS = [4, 6, 8, 12, 20]
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # без I и O, чтобы не путать с 1 и 0
CYR_TO_LAT = str.maketrans("АВСЕНКМОРТХУ", "ABCEHKMOPTXY")  # код набрали в русской раскладке
HOST_TIMEOUT = 20  # сек. ведущий офлайн → права переходят к другому игроку
ROOM_TTL = 3 * 3600  # сек. без игроков онлайн → комната удаляется
PING_EVERY = 10  # сек. между keep-alive пингами SSE
FALSE_CLAIM_COOLDOWN = 3
MAX_PLAYERS = 60
MAX_ROOMS = 500
MAX_NAME = 24
MAX_BODY = 10_000
LOG_SIZE = 40

FREE = 12  # центральная клетка
LINES = (
    [[r * 5 + c for c in range(5)] for r in range(5)]
    + [[r * 5 + c for r in range(5)] for c in range(5)]
    + [[i * 5 + i for i in range(5)], [i * 5 + 4 - i for i in range(5)]]
)
CORNERS = [0, 4, 20, 24]

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

rng = random.SystemRandom()
lock = threading.RLock()
rooms: dict[str, Room] = {}


class GameError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Channel:
    """Одно SSE-соединение. Хранит только последнее состояние, поэтому медленный клиент не копит очередь."""

    def __init__(self):
        self._cond = threading.Condition()
        self._payload: str | None = None
        self.closed = False

    def push(self, payload: str):
        with self._cond:
            if not self.closed:
                self._payload = payload
                self._cond.notify()

    def close(self, payload: str):
        with self._cond:
            self._payload = payload
            self.closed = True
            self._cond.notify()

    def pop(self, timeout: float) -> str | None:
        with self._cond:
            if self._payload is None and not self.closed:
                self._cond.wait(timeout)
            payload, self._payload = self._payload, None
            return payload


def make_card() -> list[int]:
    cols = [sorted(rng.sample(range(c * 15 + 1, c * 15 + 16), 5)) for c in range(5)]
    card = [cols[c][r] for r in range(5) for c in range(5)]
    card[FREE] = 0
    return card


def clean_name(raw) -> str:
    name = " ".join(str(raw or "").split())
    name = "".join(ch for ch in name if ch.isprintable())[:MAX_NAME].strip()
    if not name:
        raise GameError("Введите имя")
    return name


def clean_code(raw) -> str:
    return str(raw or "").strip().upper().translate(CYR_TO_LAT)


class Player:
    def __init__(self, name: str):
        self.id = secrets.token_hex(4)
        self.token = secrets.token_urlsafe(18)
        self.name = name
        self.card = make_card()
        self.marks = {FREE}
        self.wins = 0
        self.auto_mark = False
        self.channels: set[Channel] = set()
        self.offline_since = time.time()
        self.claim_blocked_until = 0.0

    @property
    def online(self) -> bool:
        return bool(self.channels)

    def new_card(self):
        self.card = make_card()
        self.marks = {FREE}

    def private_state(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "card": self.card,
            "marks": sorted(self.marks),
            "autoMark": self.auto_mark,
        }


class Room:
    def __init__(self, code: str):
        self.code = code
        self.players: dict[str, Player] = {}  # порядок вставки = порядок входа
        self.host_id: str | None = None
        self.status = "lobby"  # lobby → playing → finished → lobby ...
        self.pattern = "line"
        self.round = 1
        self.drawn: list[int] = []
        self.pool: list[int] = []
        self.auto = False
        self.interval = 8
        self.next_draw_at = 0.0
        self.winners: list[dict] = []
        self.win_at: int | None = None
        self.log: list[dict] = []
        self.log_seq = 0
        self.empty_since = time.time()

    # ---------- служебное ----------

    def add_log(self, text: str, kind: str = "info"):
        self.log_seq += 1
        self.log.append({"id": self.log_seq, "t": int(time.time() * 1000), "text": text, "kind": kind})
        del self.log[:-LOG_SIZE]

    def by_token(self, token: str) -> Player | None:
        for p in self.players.values():
            if secrets.compare_digest(p.token, token):
                return p
        return None

    def add_player(self, name: str) -> Player:
        if len(self.players) >= MAX_PLAYERS:
            raise GameError("Комната заполнена")
        taken = {p.name.lower() for p in self.players.values()}
        base, n = name, 2
        while name.lower() in taken:
            suffix = f" {n}"
            name = base[: MAX_NAME - len(suffix)] + suffix
            n += 1
        p = Player(name)
        self.players[p.id] = p
        if self.host_id is None:
            self.host_id = p.id
        return p

    def remove_player(self, pid: str, final_payload: str):
        p = self.players.pop(pid, None)
        if not p:
            return
        for ch in list(p.channels):
            ch.close(final_payload)
        p.channels.clear()
        if self.host_id == pid:
            nxt = next((q for q in self.players.values() if q.online), None) or next(iter(self.players.values()), None)
            self.host_id = nxt.id if nxt else None
            if nxt:
                self.add_log(f"{nxt.name} теперь ведёт игру")
        if not self.players:
            rooms.pop(self.code, None)

    # ---------- игра ----------

    def start(self):
        if self.status != "lobby":
            raise GameError("Раунд уже идёт")
        self.status = "playing"
        self.drawn = []
        self.pool = list(range(1, 76))
        rng.shuffle(self.pool)
        self.winners = []
        self.win_at = None
        self.next_draw_at = time.time() + 2
        for p in self.players.values():
            p.marks = {FREE}
        self.add_log(f"Раунд {self.round} начался! Цель: {PATTERNS[self.pattern].lower()}", "start")

    def draw(self):
        if self.status != "playing":
            raise GameError("Раунд сейчас не идёт")
        if not self.pool:
            self.auto = False
            raise GameError("Все шары уже вытянуты")
        n = self.pool.pop()
        self.drawn.append(n)
        for p in self.players.values():
            if p.auto_mark and n in p.card:
                p.marks.add(p.card.index(n))
        if not self.pool:
            self.auto = False
            self.add_log("Все 75 шаров вытянуты")
        self.next_draw_at = time.time() + self.interval

    def new_round(self):
        if self.status == "lobby":
            raise GameError("Новый раунд уже готовится")
        self.status = "lobby"
        self.round += 1
        self.drawn = []
        self.pool = []
        self.auto = False
        self.winners = []
        self.win_at = None
        for p in self.players.values():
            p.new_card()
        self.add_log(f"Готовим раунд {self.round} — у всех новые карточки")

    def mark(self, p: Player, idx):
        if self.status == "lobby":
            raise GameError("Раунд ещё не начался")
        if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < 25 or idx == FREE:
            raise GameError("Неверная клетка")
        if idx in p.marks:
            p.marks.discard(idx)
        elif p.card[idx] in self.drawn:
            p.marks.add(idx)
        else:
            raise GameError("Это число ещё не выпадало")

    def winning_cells(self, p: Player) -> list[int] | None:
        drawn = set(self.drawn)
        ok = {i for i in p.marks if i == FREE or p.card[i] in drawn}
        full = [line for line in LINES if all(i in ok for i in line)]
        if self.pattern == "line" and full:
            return sorted(set().union(*full))
        if self.pattern == "two_lines" and len(full) >= 2:
            return sorted(set().union(*full))
        if self.pattern == "corners" and all(i in ok for i in CORNERS):
            return CORNERS[:]
        if self.pattern == "full" and len(ok) == 25:
            return list(range(25))
        return None

    def claim(self, p: Player) -> bool:
        now = time.time()
        if self.status == "lobby":
            raise GameError("Раунд ещё не начался")
        if any(w["id"] == p.id for w in self.winners):
            raise GameError("Вы уже победили в этом раунде")
        if self.status == "finished" and len(self.drawn) != self.win_at:
            raise GameError("Раунд уже завершён")
        if now < p.claim_blocked_until:
            raise GameError("Не так быстро — подождите пару секунд")
        cells = self.winning_cells(p)
        if cells is None:
            p.claim_blocked_until = now + FALSE_CLAIM_COOLDOWN
            self.add_log(f"{p.name} кричит «Бинго!», но карточка не сходится 🙈", "false")
            return False
        p.wins += 1
        self.winners.append({"id": p.id, "name": p.name, "card": p.card[:], "cells": cells})
        if self.status == "playing":
            self.status = "finished"
            self.win_at = len(self.drawn)
            self.auto = False
        self.add_log(f"🎉 БИНГО! Побеждает {p.name}", "bingo")
        return True

    # ---------- рассылка ----------

    def public_state(self) -> dict:
        playing = self.status == "playing"
        return {
            "room": self.code,
            "round": self.round,
            "status": self.status,
            "pattern": self.pattern,
            "patterns": PATTERNS,
            "intervals": AUTO_INTERVALS,
            "drawn": self.drawn,
            "auto": self.auto,
            "interval": self.interval,
            "nextDrawIn": round(max(0.0, self.next_draw_at - time.time()), 2) if self.auto and playing else None,
            "hostId": self.host_id,
            "players": [
                {"id": p.id, "name": p.name, "online": p.online, "wins": p.wins} for p in self.players.values()
            ],
            "winners": self.winners,
            "log": self.log,
        }

    def broadcast(self):
        base = self.public_state()
        for p in self.players.values():
            if not p.channels:
                continue
            data = json.dumps({**base, "you": p.private_state()}, ensure_ascii=False, separators=(",", ":"))
            for ch in list(p.channels):
                ch.push(data)


# ---------- API ----------

KICKED = json.dumps({"kicked": True})
LEFT = json.dumps({"left": True})


def get_room(code) -> Room:
    room = rooms.get(clean_code(code))
    if not room:
        raise GameError("Комната не найдена", 404)
    return room


def new_code() -> str:
    for _ in range(1000):
        code = "".join(rng.choice(CODE_ALPHABET) for _ in range(4))
        if code not in rooms:
            return code
    raise GameError("Не удалось создать комнату", 503)


def api_create(body: dict) -> dict:
    name = clean_name(body.get("name"))
    if len(rooms) >= MAX_ROOMS:
        raise GameError("Сервер перегружен, попробуйте позже", 503)
    room = Room(new_code())
    rooms[room.code] = room
    p = room.add_player(name)
    room.add_log(f"{p.name} создаёт комнату")
    return {"room": room.code, "token": p.token, "id": p.id}


def api_join(body: dict) -> dict:
    room = get_room(body.get("room"))
    p = room.add_player(clean_name(body.get("name")))
    room.add_log(f"{p.name} присоединяется к игре", "join")
    room.broadcast()
    return {"room": room.code, "token": p.token, "id": p.id}


def act_pattern(room: Room, p: Player, body: dict):
    if room.status != "lobby":
        raise GameError("Цель меняется только до начала раунда")
    value = body.get("value")
    if value not in PATTERNS:
        raise GameError("Неизвестная цель")
    room.pattern = value


def act_auto(room: Room, p: Player, body: dict):
    interval = body.get("interval", room.interval)
    if interval not in AUTO_INTERVALS:
        raise GameError("Неверный интервал")
    room.interval = int(interval)
    on = bool(body.get("on"))
    if on and room.status != "playing":
        raise GameError("Автовытягивание работает только во время раунда")
    now = time.time()
    if on and not room.auto:
        room.next_draw_at = now + 1.5
    elif on:
        room.next_draw_at = min(room.next_draw_at, now + room.interval)
    room.auto = on


def act_kick(room: Room, p: Player, body: dict):
    target = room.players.get(str(body.get("target") or ""))
    if not target or target is p:
        raise GameError("Игрок не найден")
    room.add_log(f"{target.name} покидает комнату по решению ведущего")
    room.remove_player(target.id, KICKED)


def act_make_host(room: Room, p: Player, body: dict):
    target = room.players.get(str(body.get("target") or ""))
    if not target or target is p:
        raise GameError("Игрок не найден")
    room.host_id = target.id
    room.add_log(f"{target.name} теперь ведёт игру")


def act_new_card(room: Room, p: Player, body: dict):
    if room.status != "lobby":
        raise GameError("Карточку можно сменить только до начала раунда")
    p.new_card()


def act_auto_mark(room: Room, p: Player, body: dict):
    p.auto_mark = bool(body.get("on"))
    if p.auto_mark and room.status != "lobby":
        drawn = set(room.drawn)
        p.marks |= {i for i, n in enumerate(p.card) if n in drawn}


def act_leave(room: Room, p: Player, body: dict):
    room.add_log(f"{p.name} покидает комнату")
    room.remove_player(p.id, LEFT)


ACTIONS = {
    "pattern": act_pattern,
    "start": lambda room, p, body: room.start(),
    "draw": lambda room, p, body: room.draw(),
    "auto": act_auto,
    "new_round": lambda room, p, body: room.new_round(),
    "kick": act_kick,
    "make_host": act_make_host,
    "mark": lambda room, p, body: room.mark(p, body.get("cell")),
    "claim": lambda room, p, body: {"valid": room.claim(p)},
    "new_card": act_new_card,
    "auto_mark": act_auto_mark,
    "leave": act_leave,
}
HOST_ACTIONS = {"pattern", "start", "draw", "auto", "new_round", "kick", "make_host"}


def api_action(body: dict) -> dict:
    room = get_room(body.get("room"))
    player = room.by_token(str(body.get("token") or ""))
    if not player:
        raise GameError("Вы не в этой комнате", 403)
    action = body.get("action")
    handler = ACTIONS.get(action)
    if not handler:
        raise GameError("Неизвестное действие")
    if action in HOST_ACTIONS and player.id != room.host_id:
        raise GameError("Это может только ведущий", 403)
    result = handler(room, player, body) or {}
    if rooms.get(room.code) is room:
        room.broadcast()
    return {"ok": True, **result}


POST_ROUTES = {"/api/create": api_create, "/api/join": api_join, "/api/action": api_action}


# ---------- фоновый цикл: автовытягивание, смена ведущего, уборка ----------


def ticker():
    while True:
        time.sleep(0.25)
        try:
            now = time.time()
            with lock:
                for code, room in list(rooms.items()):
                    changed = False
                    if room.status == "playing" and room.auto and now >= room.next_draw_at:
                        try:
                            room.draw()
                        except GameError:
                            pass
                        changed = True

                    host = room.players.get(room.host_id or "")
                    if host is None or (not host.online and now - host.offline_since > HOST_TIMEOUT):
                        candidate = next((p for p in room.players.values() if p.online), None)
                        if candidate and candidate is not host:
                            room.host_id = candidate.id
                            room.add_log(f"{candidate.name} теперь ведёт игру")
                            changed = True

                    if any(p.online for p in room.players.values()):
                        room.empty_since = now
                    elif now - room.empty_since > ROOM_TTL:
                        del rooms[code]
                        continue

                    if changed:
                        room.broadcast()
        except Exception:
            traceback.print_exc()


# ---------- HTTP ----------


class Handler(BaseHTTPRequestHandler):
    server_version = "Bingo/1.0"

    def log_message(self, format, *args):  # не засоряем консоль логами запросов
        pass

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/api/events":
            return self.handle_events(query)
        if url.path == "/api/check":
            return self.handle_check(query)
        if url.path == "/healthz":
            return self.send_json({"ok": True, "rooms": len(rooms)})
        return self.serve_static(url.path)

    def do_POST(self):
        route = POST_ROUTES.get(urlparse(self.path).path)
        if not route:
            return self.send_json({"error": "Не найдено"}, 404)
        try:
            body = self.read_json()
            with lock:
                result = route(body)
        except GameError as e:
            return self.send_json({"error": str(e)}, e.status)
        except Exception:
            traceback.print_exc()
            return self.send_json({"error": "Внутренняя ошибка сервера"}, 500)
        self.send_json(result)

    # --- helpers ---

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise GameError("Слишком большой запрос", 413)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise GameError("Некорректный JSON")
        if not isinstance(data, dict):
            raise GameError("Некорректный запрос")
        return data

    def send_json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def serve_static(self, path: str):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (PUBLIC_DIR / rel).resolve()
        if PUBLIC_DIR not in target.parents or not target.is_file():
            return self.send_json({"error": "Не найдено"}, 404)
        data = target.read_bytes()
        try:
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass

    def find_player(self, query) -> tuple[Room | None, Player | None]:
        room = rooms.get(clean_code(query.get("room", [""])[0]))
        player = room.by_token(query.get("token", [""])[0]) if room else None
        return room, player

    def handle_check(self, query):
        with lock:
            room, player = self.find_player(query)
            if not player:
                return self.send_json({"error": "Комната не найдена"}, 404)
            return self.send_json({"ok": True, "room": room.code, "name": player.name})

    def handle_events(self, query):
        with lock:
            room, player = self.find_player(query)
            if not player:
                return self.send_json({"error": "Комната не найдена"}, 404)
            channel = Channel()
            player.channels.add(channel)
            room.broadcast()  # новый канал сразу получит состояние, остальные увидят «в сети»

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                payload = channel.pop(PING_EVERY)
                if payload is None:
                    self.wfile.write(b": ping\n\n")
                else:
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                if channel.closed:
                    break
        except OSError:
            pass
        finally:
            with lock:
                player.channels.discard(channel)
                if not player.online:
                    player.offline_since = time.time()
                if rooms.get(room.code) is room and player.id in room.players:
                    room.broadcast()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # на Windows SO_REUSEADDR позволяет двум серверам молча занять один порт
    allow_reuse_address = sys.platform != "win32"


class DualStackServer(Server):
    """Слушает IPv6 и IPv4 сразу: иначе на Windows каждый запрос к localhost ждёт ~2 с (сначала пробуется ::1)."""

    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass
        super().server_bind()


def make_server():
    if HOST in ("", "0.0.0.0", "::") and socket.has_ipv6:
        try:
            return DualStackServer(("::", PORT), Handler)
        except OSError:
            pass
    return Server((HOST, PORT), Handler)


def lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # пакеты не отправляются, просто выбираем интерфейс
            return s.getsockname()[0]
    except OSError:
        return None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    threading.Thread(target=ticker, daemon=True).start()
    server = make_server()
    print("Бинго-сервер запущен!")
    print(f"  На этом компьютере:  http://localhost:{PORT}")
    ip = lan_ip()
    if ip and HOST in ("0.0.0.0", "", "::"):
        print(f"  Для коллег в сети:   http://{ip}:{PORT}")
    print("Остановить: Ctrl+C", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен")


if __name__ == "__main__":
    main()
