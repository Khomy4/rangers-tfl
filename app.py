import os, sqlite3, json, hmac, hashlib
from pathlib import Path
from urllib.parse import parse_qsl
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()
BASE = Path(__file__).resolve().parent
DB = BASE / "tfl.db"
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CAPTAIN_ID = int(os.getenv("CAPTAIN_TELEGRAM_ID", "0") or 0)
DEV = os.getenv("DEV_MODE", "true").lower() == "true"

app = FastAPI()
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

# ── Database ──────────────────────────────────────────────────────────────────

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS players(
            id INTEGER PRIMARY KEY,
            telegram_id INTEGER UNIQUE,
            name TEXT,
            position TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS seasons(
            id INTEGER PRIMARY KEY,
            name TEXT,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS matches(
            id INTEGER PRIMARY KEY,
            season_id INTEGER DEFAULT 1,
            opponent TEXT,
            match_date TEXT,
            rsvp_deadline TEXT,
            gf INTEGER,
            ga INTEGER,
            voting_closed INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS rsvp(
            match_id INTEGER,
            player_id INTEGER,
            status TEXT,
            on_time INTEGER DEFAULT 1,
            PRIMARY KEY(match_id, player_id)
        );
        CREATE TABLE IF NOT EXISTS stats(
            match_id INTEGER,
            player_id INTEGER,
            played INTEGER DEFAULT 1,
            goals INTEGER DEFAULT 0,
            assists INTEGER DEFAULT 0,
            keeper_points INTEGER DEFAULT 0,
            PRIMARY KEY(match_id, player_id)
        );
        CREATE TABLE IF NOT EXISTS votes(
            match_id INTEGER,
            voter_id INTEGER,
            vtype TEXT,
            target_id INTEGER,
            PRIMARY KEY(match_id, voter_id, vtype)
        );
        """)
        # Migration: add approved column (safe if already exists)
        try:
            c.execute("ALTER TABLE players ADD COLUMN approved INTEGER DEFAULT 0")
        except Exception:
            pass
        # Captain is always approved
        if CAPTAIN_ID:
            c.execute("UPDATE players SET approved=1 WHERE telegram_id=?", (CAPTAIN_ID,))
        if c.execute("SELECT COUNT(*) n FROM seasons").fetchone()["n"] == 0:
            c.execute("INSERT INTO seasons(id,name,active) VALUES(1,'Сезон 1',1)")

init()

# ── Auth ──────────────────────────────────────────────────────────────────────

def tg_user(request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if DEV and not init_data:
        return {"id": CAPTAIN_ID or 1, "first_name": "Артём"}
    parts = dict(parse_qsl(init_data))
    recv = parts.pop("hash", None)
    if not recv or not BOT_TOKEN:
        raise HTTPException(401, "Telegram auth failed")
    check = "\n".join(f"{k}={parts[k]}" for k in sorted(parts))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv):
        raise HTTPException(401, "Bad signature")
    return json.loads(parts["user"])

def get_player(request: Request):
    """Auto-register player on first login."""
    u = tg_user(request)
    uid = int(u["id"])
    with db() as c:
        p = c.execute("SELECT * FROM players WHERE telegram_id=?", (uid,)).fetchone()
        if not p and DEV:
            p = c.execute("SELECT * FROM players WHERE id=1").fetchone()
        if not p:
            name = (u.get("first_name","") + " " + u.get("last_name","")).strip() or "Игрок"
            approved = 1 if uid == CAPTAIN_ID else 0
            c.execute("INSERT INTO players(telegram_id,name,position,approved) VALUES(?,?,?,?)", (uid, name, "", approved))
            p = c.execute("SELECT * FROM players WHERE telegram_id=?", (uid,)).fetchone()
        return dict(p)

def is_captain(p: dict) -> bool:
    return (CAPTAIN_ID != 0 and p.get("telegram_id") == CAPTAIN_ID) or (DEV and p["id"] == 1)

# ── Finance ───────────────────────────────────────────────────────────────────

def result_bonus(gf, ga) -> int:
    if gf is None or ga is None:
        return 0
    if gf < ga: return 0
    if gf == ga: return 200
    return {1: 300, 2: 400, 3: 500}.get(gf - ga, 600)

def calc_finance(c, mid: int) -> dict:
    m = c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
    if not m:
        return {}
    total_players = c.execute("SELECT COUNT(*) n FROM players").fetchone()["n"]
    on_time_replies = c.execute(
        "SELECT COUNT(*) n FROM rsvp WHERE match_id=? AND on_time=1", (mid,)
    ).fetchone()["n"]
    no_show = c.execute("""
        SELECT COUNT(*) n FROM rsvp r
        LEFT JOIN stats s ON s.match_id=r.match_id AND s.player_id=r.player_id
        WHERE r.match_id=? AND r.status='yes' AND COALESCE(s.played,0)=0
    """, (mid,)).fetchone()["n"]
    disc = 300 if total_players > 0 and on_time_replies == total_players and no_show == 0 else 0
    rb = result_bonus(m["gf"], m["ga"])
    return {
        "discipline": disc,
        "result": rb,
        "team_bank": disc + rb,
        "personal_fund": 600 if m["gf"] is not None else 0,
    }

def deadline_passed(m) -> bool:
    dl = m["rsvp_deadline"] if isinstance(m, dict) else m["rsvp_deadline"]
    if not dl:
        return False
    try:
        return datetime.utcnow() > datetime.fromisoformat(dl)
    except Exception:
        return False

# ── Pydantic models ───────────────────────────────────────────────────────────

class MatchIn(BaseModel):
    opponent: str
    match_date: str
    rsvp_deadline: Optional[str] = None
    season_id: Optional[int] = 1

class ScoreIn(BaseModel):
    gf: int
    ga: int

class RsvpIn(BaseModel):
    status: str
    player_id: Optional[int] = None   # captain can set for any player

class StatIn(BaseModel):
    player_id: int
    played: int = 1
    goals: int = 0
    assists: int = 0
    keeper_points: int = 0

class VoteIn(BaseModel):
    vtype: str
    target_id: int

class PositionIn(BaseModel):
    position: str

class PlayerNameIn(BaseModel):
    name: str

class SeasonIn(BaseModel):
    name: str

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def home():
    return FileResponse(BASE / "static/index.html")

@app.get("/api/me")
def me(request: Request):
    p = get_player(request)
    return {"player": p, "captain": is_captain(p)}

@app.get("/api/players")
def players(request: Request):
    get_player(request)
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM players WHERE approved=1 ORDER BY name")]

@app.get("/api/captain/pending")
def pending_players(request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM players WHERE approved=0 ORDER BY id")]

@app.put("/api/players/{pid}/approve")
def approve_player(pid: int, x: PlayerNameIn, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        if x.name.strip():
            c.execute("UPDATE players SET approved=1, name=? WHERE id=?", (x.name.strip(), pid))
        else:
            c.execute("UPDATE players SET approved=1 WHERE id=?", (pid,))
    return {"ok": True}

@app.put("/api/players/{pid}/name")
def rename_player(pid: int, x: PlayerNameIn, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    if not x.name.strip():
        raise HTTPException(400, "Пустое имя")
    with db() as c:
        c.execute("UPDATE players SET name=? WHERE id=?", (x.name.strip(), pid))
    return {"ok": True}

@app.put("/api/players/{pid}/position")
def set_position(pid: int, x: PositionIn, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute("UPDATE players SET position=? WHERE id=?", (x.position, pid))
    return {"ok": True}

# ── Seasons ───────────────────────────────────────────────────────────────────

@app.get("/api/seasons")
def seasons(request: Request):
    get_player(request)
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM seasons ORDER BY id")]

@app.post("/api/seasons")
def new_season(x: SeasonIn, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute("UPDATE seasons SET active=0")
        cur = c.execute("INSERT INTO seasons(name,active) VALUES(?,1)", (x.name,))
        return {"id": cur.lastrowid}

# ── Matches ───────────────────────────────────────────────────────────────────

@app.get("/api/matches")
def matches(request: Request, season_id: Optional[int] = None):
    get_player(request)
    with db() as c:
        if season_id is None:
            row = c.execute("SELECT id FROM seasons WHERE active=1").fetchone()
            season_id = row["id"] if row else 1
        rows = c.execute(
            "SELECT * FROM matches WHERE season_id=? ORDER BY match_date DESC, id DESC",
            (season_id,)
        )
        out = []
        for m in rows:
            d = dict(m)
            d["finance"] = calc_finance(c, m["id"])
            d["deadline_passed"] = deadline_passed(d)
            out.append(d)
        return out

@app.get("/api/matches/{mid}")
def match_detail(mid: int, request: Request):
    p = get_player(request)
    with db() as c:
        m = c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
        if not m:
            raise HTTPException(404)
        d = dict(m)
        d["finance"] = calc_finance(c, mid)
        d["deadline_passed"] = deadline_passed(d)

        # RSVP list: all players with their status
        all_players = [dict(r) for r in c.execute("SELECT * FROM players ORDER BY name")]
        rsvp_map = {
            r["player_id"]: dict(r)
            for r in c.execute("SELECT * FROM rsvp WHERE match_id=?", (mid,))
        }
        d["rsvp_list"] = [
            {
                "player_id": pl["id"],
                "name": pl["name"],
                "position": pl["position"] or "",
                "status": rsvp_map.get(pl["id"], {}).get("status"),
                "on_time": rsvp_map.get(pl["id"], {}).get("on_time"),
            }
            for pl in all_players
        ]

        # Stats with vote winners
        stats_map = {
            s["player_id"]: dict(s)
            for s in c.execute("SELECT * FROM stats WHERE match_id=?", (mid,))
        }
        mvp_votes, def_votes = {}, {}
        for v in c.execute("SELECT * FROM votes WHERE match_id=?", (mid,)):
            bucket = mvp_votes if v["vtype"] == "mvp" else def_votes
            bucket[v["target_id"]] = bucket.get(v["target_id"], 0) + 1
        mvp_winner = max(mvp_votes, key=mvp_votes.get) if mvp_votes else None
        def_winner = max(def_votes, key=def_votes.get) if def_votes else None

        d["stats"] = []
        for pl in all_players:
            st = stats_map.get(pl["id"])
            if not st:
                continue
            pts = (
                st["goals"] * 2 + st["assists"] + st["keeper_points"]
                + (3 if pl["id"] == mvp_winner else 0)
                + (2 if pl["id"] == def_winner else 0)
            )
            d["stats"].append({
                "player_id": pl["id"],
                "name": pl["name"],
                "position": pl["position"] or "",
                "played": st["played"],
                "goals": st["goals"],
                "assists": st["assists"],
                "keeper_points": st["keeper_points"],
                "is_mvp": pl["id"] == mvp_winner,
                "is_best_defense": pl["id"] == def_winner,
                "points": pts,
            })

        # My own votes
        d["my_votes"] = {
            v["vtype"]: v["target_id"]
            for v in c.execute(
                "SELECT * FROM votes WHERE match_id=? AND voter_id=?", (mid, p["id"])
            )
        }
        return d

@app.post("/api/matches")
def add_match(x: MatchIn, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        cur = c.execute(
            "INSERT INTO matches(season_id,opponent,match_date,rsvp_deadline) VALUES(?,?,?,?)",
            (x.season_id or 1, x.opponent, x.match_date, x.rsvp_deadline),
        )
        return {"id": cur.lastrowid}

@app.put("/api/matches/{mid}")
def update_match(mid: int, x: MatchIn, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute(
            "UPDATE matches SET opponent=?,match_date=?,rsvp_deadline=? WHERE id=?",
            (x.opponent, x.match_date, x.rsvp_deadline, mid),
        )
    return {"ok": True}

@app.post("/api/matches/{mid}/score")
def set_score(mid: int, x: ScoreIn, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute("UPDATE matches SET gf=?,ga=? WHERE id=?", (x.gf, x.ga, mid))
        return calc_finance(c, mid)

@app.post("/api/matches/{mid}/close_voting")
def close_voting(mid: int, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute("UPDATE matches SET voting_closed=1 WHERE id=?", (mid,))
    return {"ok": True}

# ── RSVP ─────────────────────────────────────────────────────────────────────

@app.post("/api/matches/{mid}/rsvp")
def rsvp(mid: int, x: RsvpIn, request: Request):
    p = get_player(request)
    if x.status not in ("yes", "no"):
        raise HTTPException(400, "Invalid status")

    with db() as c:
        m = c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
        if not m:
            raise HTTPException(404)

        cap = is_captain(p)
        passed = deadline_passed(dict(m))

        if cap:
            # Captain can always set RSVP for any player
            target_id = x.player_id or p["id"]
            on_time = 1
        else:
            if passed:
                raise HTTPException(403, "Дедлайн уже прошёл. Обратись к капитану.")
            target_id = p["id"]
            existing = c.execute(
                "SELECT on_time FROM rsvp WHERE match_id=? AND player_id=?", (mid, target_id)
            ).fetchone()
            on_time = existing["on_time"] if existing else 1

        c.execute(
            """INSERT INTO rsvp VALUES(?,?,?,?)
               ON CONFLICT(match_id,player_id) DO UPDATE
               SET status=excluded.status, on_time=excluded.on_time""",
            (mid, target_id, x.status, on_time),
        )
    return {"ok": True}

# ── Stats ─────────────────────────────────────────────────────────────────────

@app.post("/api/matches/{mid}/stats")
def save_stats(mid: int, x: StatIn, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute(
            """INSERT INTO stats VALUES(?,?,?,?,?,?)
               ON CONFLICT(match_id,player_id) DO UPDATE
               SET played=excluded.played, goals=excluded.goals,
                   assists=excluded.assists, keeper_points=excluded.keeper_points""",
            (mid, x.player_id, x.played, x.goals, x.assists, x.keeper_points),
        )
    return {"ok": True}

# ── Votes ─────────────────────────────────────────────────────────────────────

@app.post("/api/matches/{mid}/vote")
def vote(mid: int, x: VoteIn, request: Request):
    p = get_player(request)
    with db() as c:
        m = c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
        if not m:
            raise HTTPException(404)
        if m["voting_closed"]:
            raise HTTPException(403, "Голосование закрыто")
        if m["gf"] is None:
            raise HTTPException(403, "Матч ещё не сыгран")
        if x.vtype not in ("mvp", "defense"):
            raise HTTPException(400)
        if x.target_id == p["id"]:
            raise HTTPException(400, "Нельзя голосовать за себя")
        # Only played players can vote (captain is exempt)
        if not is_captain(p):
            played = c.execute(
                "SELECT 1 FROM stats WHERE match_id=? AND player_id=? AND played=1",
                (mid, p["id"]),
            ).fetchone()
            if not played:
                raise HTTPException(403, "Ты не участвовал в этом матче")
        c.execute(
            """INSERT INTO votes VALUES(?,?,?,?)
               ON CONFLICT(match_id,voter_id,vtype) DO UPDATE SET target_id=excluded.target_id""",
            (mid, p["id"], x.vtype, x.target_id),
        )
    return {"ok": True}

# ── Leaderboard ───────────────────────────────────────────────────────────────

@app.get("/api/leaderboard")
def leaderboard(request: Request, season_id: Optional[int] = None):
    get_player(request)
    with db() as c:
        if season_id is None:
            row = c.execute("SELECT id FROM seasons WHERE active=1").fetchone()
            season_id = row["id"] if row else 1
        match_ids = [
            r["id"] for r in c.execute(
                "SELECT id FROM matches WHERE season_id=?", (season_id,)
            )
        ]
        rows = []
        for pl in c.execute("SELECT * FROM players"):
            if match_ids:
                ph = ",".join("?" * len(match_ids))
                st = c.execute(
                    f"SELECT COUNT(*) g, COALESCE(SUM(goals),0) goals,"
                    f" COALESCE(SUM(assists),0) assists, COALESCE(SUM(keeper_points),0) kp"
                    f" FROM stats WHERE player_id=? AND played=1 AND match_id IN ({ph})",
                    [pl["id"]] + match_ids,
                ).fetchone()
                mvp = c.execute(
                    f"""SELECT COUNT(*) n FROM (
                        SELECT match_id,target_id,RANK() OVER(PARTITION BY match_id ORDER BY COUNT(*) DESC) rk
                        FROM votes WHERE vtype='mvp' AND match_id IN ({ph}) GROUP BY match_id,target_id
                    ) q WHERE target_id=? AND rk=1""",
                    match_ids + [pl["id"]],
                ).fetchone()["n"]
                dfn = c.execute(
                    f"""SELECT COUNT(*) n FROM (
                        SELECT match_id,target_id,RANK() OVER(PARTITION BY match_id ORDER BY COUNT(*) DESC) rk
                        FROM votes WHERE vtype='defense' AND match_id IN ({ph}) GROUP BY match_id,target_id
                    ) q WHERE target_id=? AND rk=1""",
                    match_ids + [pl["id"]],
                ).fetchone()["n"]
                games, goals, assists, kp = st["g"], st["goals"], st["assists"], st["kp"]
            else:
                games = goals = assists = kp = mvp = dfn = 0

            pts = goals * 2 + assists + mvp * 3 + dfn * 2 + kp
            rows.append(dict(
                id=pl["id"], name=pl["name"], position=pl["position"] or "",
                games=games, total=len(match_ids),
                goals=goals, assists=assists, mvp=mvp, defense=dfn,
                points=pts,
            ))
        rows.sort(key=lambda r: (-r["points"], -r["goals"], -r["assists"]))
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        return rows

# ── Summary ───────────────────────────────────────────────────────────────────

@app.get("/api/summary")
def summary(request: Request, season_id: Optional[int] = None):
    get_player(request)
    with db() as c:
        if season_id is None:
            row = c.execute("SELECT id FROM seasons WHERE active=1").fetchone()
            season_id = row["id"] if row else 1
        ids = [
            r["id"] for r in c.execute(
                "SELECT id FROM matches WHERE gf IS NOT NULL AND season_id=?", (season_id,)
            )
        ]
        team_bank = sum(calc_finance(c, i)["team_bank"] for i in ids)
        return {
            "played": len(ids),
            "team_bank": team_bank,
            "personal_fund": len(ids) * 600,
        }
