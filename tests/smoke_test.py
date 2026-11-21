"""Интеграционный тест сервера бинго (нужен запущенный сервер): python tests/smoke_test.py [base_url]"""
import json
import sys
import threading
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
LINES = (
    [[r * 5 + c for c in range(5)] for r in range(5)]
    + [[r * 5 + c for r in range(5)] for c in range(5)]
    + [[i * 5 + i for i in range(5)], [i * 5 + 4 - i for i in range(5)]]
)


def post(path, body):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def snapshot(room, token):
    with urllib.request.urlopen(f"{BASE}/api/events?room={room}&token={token}", timeout=5) as r:
        for raw in r:
            line = raw.decode().strip()
            if line.startswith("data: "):
                return json.loads(line[6:])


def act(room, who, action, **extra):
    return post("/api/action", {"room": room, "token": who["token"], "action": action, **extra})


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        global failed
        failed = True


failed = False
s, host = post("/api/create", {"name": "Хост"})
room = host["room"]
check(s == 200 and len(room) == 4, f"create room {room}")
_, p1 = post("/api/join", {"room": room.lower(), "name": "Игрок"})
_, p2 = post("/api/join", {"room": room, "name": "игрок"})
st = snapshot(room, host["token"])
names = [p["name"] for p in st["players"]]
check(names == ["Хост", "Игрок", "игрок 2"], f"dedupe names {names}")

s, r = act(room, p1, "start")
check(s == 403, f"non-host cannot start: {r}")
s, r = act(room, host, "pattern", value="line")
check(s == 200, "set pattern")
s, r = act(room, p1, "mark", cell=0)
check(s == 400, f"mark in lobby rejected: {r.get('error')}")
s, r = act(room, host, "start")
check(s == 200, "start")
s, r = act(room, host, "pattern", value="full")
check(s == 400, f"pattern locked while playing: {r.get('error')}")

act(room, p1, "auto_mark", on=True)
s, r = act(room, p2, "claim")
check(s == 200 and r["valid"] is False, "false claim detected")
s, r = act(room, p2, "claim")
check(s == 400, f"claim cooldown: {r.get('error')}")

# p2 пытается отметить невыпавшее число
st2 = snapshot(room, p2["token"])
card2 = st2["you"]["card"]
idx = 0
s, r = act(room, p2, "mark", cell=idx)
check(s == 400, f"mark undrawn rejected: {r.get('error')}")
s, r = act(room, p2, "mark", cell=12)
check(s == 400, "free cell not toggleable")

# тянем, пока у p1 (автоотметка) не соберётся линия
for _ in range(75):
    act(room, host, "draw")
    st1 = snapshot(room, p1["token"])
    marks = set(st1["you"]["marks"])
    if any(all(i in marks for i in line) for line in LINES):
        break
drawn = st1["drawn"]
check(all(st1["you"]["card"][i] in drawn for i in marks if i != 12), f"automark marks only drawn ({len(drawn)} balls)")
s, r = act(room, p1, "claim")
check(s == 200 and r["valid"] is True, "valid bingo accepted")
st = snapshot(room, host["token"])
check(st["status"] == "finished" and st["winners"][0]["name"] == "Игрок", "round finished with winner")
check([p["wins"] for p in st["players"] if p["name"] == "Игрок"] == [1], "win counted")
s, r = act(room, host, "draw")
check(s == 400, f"no draw after finish: {r.get('error')}")
s, r = act(room, p1, "claim")
check(s == 400, f"winner cannot claim twice: {r.get('error')}")

old_card = st1["you"]["card"]
s, r = act(room, host, "new_round")
st1 = snapshot(room, p1["token"])
check(st1["status"] == "lobby" and st1["round"] == 2 and st1["you"]["card"] != old_card and st1["you"]["marks"] == [12], "new round resets")
s, r = act(room, p1, "new_card")
check(s == 200, "new card in lobby")

# kick p2 — его SSE должен получить kicked
got = {}
def listen():
    with urllib.request.urlopen(f"{BASE}/api/events?room={room}&token={p2['token']}", timeout=5) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if line.startswith("data: "):
                msg = json.loads(line[6:])
                if msg.get("kicked"):
                    got["kicked"] = True
t = threading.Thread(target=listen)
t.start()
import time; time.sleep(0.5)
s, r = act(room, host, "kick", target=p2["id"])
t.join(3)
check(got.get("kicked") is True, "kicked player notified")
s, r = act(room, p2, "claim")
check(s == 403, "kicked player has no access")

s, r = act(room, host, "leave")
st1 = snapshot(room, p1["token"])
check(st1["hostId"] == p1["id"], "host transferred on leave")
act(room, p1, "leave")
s, r = post("/api/join", {"room": room, "name": "x"})
check(s == 404, "empty room deleted")

s, r = post("/api/join", {"room": "ZZZZ", "name": "x"})
check(s == 404, "unknown room 404")
s, r = post("/api/create", {"name": "   "})
check(s == 400, "empty name rejected")
for path in ["/../server.py", "/%2e%2e/server.py", "/..%5cserver.py"]:
    try:
        with urllib.request.urlopen(BASE + path) as resp:
            body = resp.read()
        check(b"import" not in body, f"no traversal {path}")
    except urllib.error.HTTPError as e:
        check(e.code == 404, f"no traversal {path}")

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
