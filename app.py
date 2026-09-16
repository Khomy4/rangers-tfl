import os, json, hmac, hashlib
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl
from datetime import datetime
from typing import Optional
import psycopg2, psycopg2.extras
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()
BASE = Path(__file__).resolve().parent
DATABASE_URL = os.getenv("DATABASE_URL", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CAPTAIN_ID = int(os.getenv("CAPTAIN_TELEGRAM_ID", "0") or 0)
DEV = os.getenv("DEV_MODE", "false").lower() == "true"

app = FastAPI()
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

# ── Database ──────────────────────────────────────────────────────────────────

class Cur:
    """Thin psycopg2 cursor wrapper with SQLite-compatible chaining API."""
    def __init__(self, pg_cur):
        self._c = pg_cur

    def execute(self, sql, params=None):
        sql = sql.replace("?", "%s")
        self._c.execute(sql, list(params) if params else None)
        return self

    def fetchone(self):
        row = self._c.fetchone()
        return dict(row) if row else None

    def fetchall(self):
        rows = self._c.fetchall()
        return [dict(r) for r in rows] if rows else []

    def __iter__(self):
        for row in (self._c.fetchall() or []):
            yield dict(row)


@contextmanager
def db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = Cur(conn.cursor())
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init():
    with db() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS players(
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT UNIQUE,
            name TEXT,
            position TEXT DEFAULT '',
            approved INTEGER DEFAULT 0
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS seasons(
            id SERIAL PRIMARY KEY,
            name TEXT,
            active INTEGER DEFAULT 1
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS matches(
            id SERIAL PRIMARY KEY,
            season_id INTEGER DEFAULT 1,
            opponent TEXT,
            match_date TEXT,
            rsvp_deadline TEXT,
            gf INTEGER,
            ga INTEGER,
            voting_closed INTEGER DEFAULT 0
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS rsvp(
            match_id INTEGER,
            player_id INTEGER,
            status TEXT,
            on_time INTEGER DEFAULT 1,
            PRIMARY KEY(match_id, player_id)
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS stats(
            match_id INTEGER,
            player_id INTEGER,
            played INTEGER DEFAULT 1,
            goals INTEGER DEFAULT 0,
            assists INTEGER DEFAULT 0,
            keeper_points INTEGER DEFAULT 0,
            PRIMARY KEY(match_id, player_id)
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS votes(
            match_id INTEGER,
            voter_id INTEGER,
            vtype TEXT,
            target_id INTEGER,
            PRIMARY KEY(match_id, voter_id, vtype)
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS lineup(
            match_id INTEGER,
            slot TEXT,
            player_id INTEGER,
            PRIMARY KEY(match_id, slot)
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS lineup_subs(
            match_id INTEGER,
            slot TEXT,
            sub_index INTEGER,
            player_id INTEGER,
            PRIMARY KEY(match_id, slot, sub_index)
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS match_notes(
            match_id INTEGER PRIMARY KEY,
            notes TEXT
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS lineup2(
            match_id INTEGER,
            slot TEXT,
            player_id INTEGER,
            PRIMARY KEY(match_id, slot)
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS lineup_subs2(
            match_id INTEGER,
            slot TEXT,
            sub_index INTEGER,
            player_id INTEGER,
            PRIMARY KEY(match_id, slot, sub_index)
        )""")
        # Safe migrations — run every startup, idempotent
        c.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS approved INTEGER DEFAULT 0")
        c.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS position TEXT DEFAULT ''")
        # Close voting for any scored match that was left open (e.g. after a bug)
        c.execute("UPDATE matches SET voting_closed=1 WHERE gf IS NOT NULL AND voting_closed=0")

        if CAPTAIN_ID:
            c.execute("UPDATE players SET approved=1 WHERE telegram_id=?", (CAPTAIN_ID,))
        c.execute("SELECT COUNT(*) AS n FROM seasons")
        if c.fetchone()["n"] == 0:
            c.execute("INSERT INTO seasons(name,active) VALUES('Сезон 1',1)")


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
        c.execute("SELECT * FROM players WHERE telegram_id=?", (uid,))
        p = c.fetchone()
        if not p and DEV:
            c.execute("SELECT * FROM players WHERE id=1")
            p = c.fetchone()
        if not p:
            name = (u.get("first_name", "") + " " + u.get("last_name", "")).strip() or "Игрок"
            approved = 1 if uid == CAPTAIN_ID else 0
            c.execute(
                "INSERT INTO players(telegram_id,name,position,approved) VALUES(?,?,?,?)",
                (uid, name, "", approved),
            )
            c.execute("SELECT * FROM players WHERE telegram_id=?", (uid,))
            p = c.fetchone()
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


def calc_finance(c: Cur, mid: int) -> dict:
    c.execute("SELECT * FROM matches WHERE id=?", (mid,))
    m = c.fetchone()
    if not m:
        return {}
    # Exclude Босс from discipline calculations
    c.execute(
        "SELECT COUNT(*) AS n FROM players WHERE approved=1 AND COALESCE(position,'') != 'Босс'"
    )
    total_players = c.fetchone()["n"]
    c.execute("SELECT COUNT(*) AS n FROM rsvp WHERE match_id=? AND on_time=1", (mid,))
    on_time_replies = c.fetchone()["n"]
    c.execute("""
        SELECT COUNT(*) AS n FROM rsvp r
        LEFT JOIN stats s ON s.match_id=r.match_id AND s.player_id=r.player_id
        WHERE r.match_id=? AND r.status='yes' AND COALESCE(s.played,0)=0
    """, (mid,))
    no_show = c.fetchone()["n"]
    disc = 300 if total_players > 0 and on_time_replies == total_players and no_show == 0 else 0
    rb = result_bonus(m["gf"], m["ga"])
    return {
        "discipline": disc,
        "result": rb,
        "team_bank": disc + rb,
        "personal_fund": 600 if m["gf"] is not None else 0,
    }


def deadline_passed(m) -> bool:
    dl = m.get("rsvp_deadline") if isinstance(m, dict) else m["rsvp_deadline"]
    if not dl:
        return False
    try:
        return datetime.utcnow() > datetime.fromisoformat(str(dl))
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
    player_id: Optional[int] = None

class StatIn(BaseModel):
    player_id: int
    played: int = 1
    goals: int = 0
    assists: int = 0
    keeper_points: int = 0

class VoteIn(BaseModel):
    vtype: str
    target_id: int

class LineupIn(BaseModel):
    lineup: dict  # {slot: player_id or None}
    subs: Optional[dict] = None  # {slot: [pid, ...]}
    team_num: Optional[int] = 1  # 1 or 2

class NotesIn(BaseModel):
    notes: str

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
    """Public list: approved, non-Босс players only."""
    get_player(request)
    with db() as c:
        c.execute(
            "SELECT * FROM players WHERE approved=1 AND COALESCE(position,'') != 'Босс' ORDER BY name"
        )
        return c.fetchall()


@app.get("/api/captain/all_players")
def all_players_cap(request: Request):
    """Captain view: ALL approved players including Босс."""
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute("SELECT * FROM players WHERE approved=1 ORDER BY name")
        return c.fetchall()


@app.get("/api/captain/pending")
def pending_players(request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute("SELECT * FROM players WHERE approved=0 ORDER BY id")
        return c.fetchall()


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


@app.delete("/api/players/{pid}")
def delete_player(pid: int, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    if p["id"] == pid:
        raise HTTPException(400, "Нельзя удалить себя")
    with db() as c:
        c.execute("DELETE FROM rsvp WHERE player_id=?", (pid,))
        c.execute("DELETE FROM stats WHERE player_id=?", (pid,))
        c.execute("DELETE FROM votes WHERE voter_id=? OR target_id=?", (pid, pid))
        c.execute("DELETE FROM players WHERE id=?", (pid,))
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
        c.execute("SELECT * FROM seasons ORDER BY id")
        return c.fetchall()


@app.post("/api/seasons")
def new_season(x: SeasonIn, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute("UPDATE seasons SET active=0")
        c.execute("INSERT INTO seasons(name,active) VALUES(?,1) RETURNING id", (x.name,))
        row = c.fetchone()
        return {"id": row["id"]}


# ── Matches ───────────────────────────────────────────────────────────────────

@app.get("/api/matches")
def matches(request: Request, season_id: Optional[int] = None):
    get_player(request)
    with db() as c:
        if season_id is None:
            c.execute("SELECT id FROM seasons WHERE active=1")
            row = c.fetchone()
            season_id = row["id"] if row else 1
        c.execute(
            "SELECT * FROM matches WHERE season_id=? ORDER BY match_date DESC, id DESC",
            (season_id,),
        )
        rows = c.fetchall()
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
        c.execute("SELECT * FROM matches WHERE id=?", (mid,))
        m = c.fetchone()
        if not m:
            raise HTTPException(404)
        d = dict(m)
        d["finance"] = calc_finance(c, mid)
        d["deadline_passed"] = deadline_passed(d)

        # Exclude Босс from RSVP and stats lists
        c.execute(
            "SELECT * FROM players WHERE approved=1 AND COALESCE(position,'') != 'Босс' ORDER BY name"
        )
        all_players = c.fetchall()

        c.execute("SELECT * FROM rsvp WHERE match_id=?", (mid,))
        rsvp_map = {r["player_id"]: dict(r) for r in c.fetchall()}
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

        c.execute("SELECT * FROM stats WHERE match_id=?", (mid,))
        stats_map = {s["player_id"]: dict(s) for s in c.fetchall()}

        c.execute("SELECT * FROM votes WHERE match_id=?", (mid,))
        all_votes = c.fetchall()
        mvp_votes, def_votes = {}, {}
        for v in all_votes:
            bucket = mvp_votes if v["vtype"] == "mvp" else def_votes
            bucket[v["target_id"]] = bucket.get(v["target_id"], 0) + 1
        def top_ids(votes_dict):
            """Return set of all ids tied at the highest vote count."""
            if not votes_dict:
                return set()
            top = max(votes_dict.values())
            return {pid for pid, cnt in votes_dict.items() if cnt == top}

        mvp_winners = top_ids(mvp_votes)
        def_winners = top_ids(def_votes)

        d["stats"] = []
        for pl in all_players:
            st = stats_map.get(pl["id"])
            if not st:
                continue
            # MVP/defense points only count if voting is closed; all tied winners get points
            mvp_pts = 3 if (m["voting_closed"] and pl["id"] in mvp_winners) else 0
            def_pts = 2 if (m["voting_closed"] and pl["id"] in def_winners) else 0
            pts = st["goals"] * 2 + st["assists"] + st["keeper_points"] + mvp_pts + def_pts
            d["stats"].append({
                "player_id": pl["id"],
                "name": pl["name"],
                "position": pl["position"] or "",
                "played": st["played"],
                "goals": st["goals"],
                "assists": st["assists"],
                "keeper_points": st["keeper_points"],
                "is_mvp": m["voting_closed"] and pl["id"] in mvp_winners,
                "is_best_defense": m["voting_closed"] and pl["id"] in def_winners,
                "points": pts,
            })

        c.execute("SELECT * FROM votes WHERE match_id=? AND voter_id=?", (mid, p["id"]))
        d["my_votes"] = {v["vtype"]: v["target_id"] for v in c.fetchall()}

        # Voting progress
        mvp_voted_n = len(set(v["voter_id"] for v in all_votes if v["vtype"] == "mvp"))
        def_voted_n = len(set(v["voter_id"] for v in all_votes if v["vtype"] == "defense"))
        c.execute("SELECT COUNT(*) AS n FROM stats WHERE match_id=? AND played=1", (mid,))
        eligible_n = c.fetchone()["n"]
        d["vote_progress"] = {"mvp_voted": mvp_voted_n, "def_voted": def_voted_n, "eligible": eligible_n}

        # Lineup
        c.execute("SELECT slot, player_id FROM lineup WHERE match_id=?", (mid,))
        d["lineup"] = {r["slot"]: r["player_id"] for r in c.fetchall()}

        # Subs team 1
        c.execute("SELECT slot, sub_index, player_id FROM lineup_subs WHERE match_id=%s ORDER BY slot, sub_index", (mid,))
        subs_raw = c.fetchall()
        d["subs"] = {}
        for r in subs_raw:
            sl = r["slot"]
            if sl not in d["subs"]:
                d["subs"][sl] = []
            d["subs"][sl].append(r["player_id"])
        # Lineup team 2
        c.execute("SELECT slot, player_id FROM lineup2 WHERE match_id=%s", (mid,))
        d["lineup2"] = {r["slot"]: r["player_id"] for r in c.fetchall()}
        # Subs team 2
        c.execute("SELECT slot, sub_index, player_id FROM lineup_subs2 WHERE match_id=%s ORDER BY slot, sub_index", (mid,))
        subs2_raw = c.fetchall()
        d["subs2"] = {}
        for r in subs2_raw:
            sl = r["slot"]
            if sl not in d["subs2"]:
                d["subs2"][sl] = []
            d["subs2"][sl].append(r["player_id"])
        # Notes
        c.execute("SELECT notes FROM match_notes WHERE match_id=%s", (mid,))
        nrow = c.fetchone()
        d["notes"] = nrow["notes"] if nrow else ""

        # Captain sees live vote breakdown
        if is_captain(p) and not m["voting_closed"]:
            name_map = {pl["id"]: pl["name"] for pl in all_players}
            def breakdown(vdict):
                items = [{"id": k, "name": name_map.get(k, "?"), "count": v} for k, v in vdict.items()]
                return sorted(items, key=lambda x: -x["count"])
            d["vote_counts"] = {"mvp": breakdown(mvp_votes), "defense": breakdown(def_votes)}

        return d


@app.post("/api/matches")
def add_match(x: MatchIn, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute(
            "INSERT INTO matches(season_id,opponent,match_date,rsvp_deadline) VALUES(?,?,?,?) RETURNING id",
            (x.season_id or 1, x.opponent, x.match_date, x.rsvp_deadline),
        )
        row = c.fetchone()
        # Auto-close voting on all previously scored matches
        c.execute("UPDATE matches SET voting_closed=1 WHERE gf IS NOT NULL AND voting_closed=0")
        return {"id": row["id"]}


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


@app.delete("/api/matches/{mid}")
def delete_match(mid: int, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute("DELETE FROM rsvp WHERE match_id=?", (mid,))
        c.execute("DELETE FROM stats WHERE match_id=?", (mid,))
        c.execute("DELETE FROM votes WHERE match_id=?", (mid,))
        c.execute("DELETE FROM matches WHERE id=?", (mid,))
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
        c.execute("SELECT * FROM matches WHERE id=?", (mid,))
        m = c.fetchone()
        if not m:
            raise HTTPException(404)
        cap = is_captain(p)
        passed = deadline_passed(dict(m))
        if cap:
            target_id = x.player_id or p["id"]
            on_time = 1
        else:
            if passed:
                raise HTTPException(403, "Дедлайн уже прошёл. Обратись к капитану.")
            target_id = p["id"]
            c.execute(
                "SELECT on_time FROM rsvp WHERE match_id=? AND player_id=?", (mid, target_id)
            )
            existing = c.fetchone()
            on_time = existing["on_time"] if existing else 1
        c.execute(
            """INSERT INTO rsvp VALUES(?,?,?,?)
               ON CONFLICT(match_id,player_id) DO UPDATE
               SET status=EXCLUDED.status, on_time=EXCLUDED.on_time""",
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
               SET played=EXCLUDED.played, goals=EXCLUDED.goals,
                   assists=EXCLUDED.assists, keeper_points=EXCLUDED.keeper_points""",
            (mid, x.player_id, x.played, x.goals, x.assists, x.keeper_points),
        )
    return {"ok": True}


# ── Votes ─────────────────────────────────────────────────────────────────────

@app.post("/api/matches/{mid}/vote")
def vote(mid: int, x: VoteIn, request: Request):
    p = get_player(request)
    with db() as c:
        c.execute("SELECT * FROM matches WHERE id=?", (mid,))
        m = c.fetchone()
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
        is_boss = (p.get("position") or "") == "Босс"
        if not is_captain(p) and not is_boss:
            c.execute(
                "SELECT 1 AS ok FROM stats WHERE match_id=? AND player_id=? AND played=1",
                (mid, p["id"]),
            )
            if not c.fetchone():
                raise HTTPException(403, "Ты не участвовал в этом матче")
        # One vote only — no changes allowed
        c.execute("SELECT 1 FROM votes WHERE match_id=? AND voter_id=? AND vtype=?",
                  (mid, p["id"], x.vtype))
        if c.fetchone():
            raise HTTPException(400, "Ты уже проголосовал")
        c.execute("INSERT INTO votes VALUES(?,?,?,?)",
                  (mid, p["id"], x.vtype, x.target_id))
        # Auto-close if all played players voted for both mvp and defense
        c.execute("SELECT COUNT(*) AS n FROM stats WHERE match_id=? AND played=1", (mid,))
        played_n = c.fetchone()["n"]
        c.execute("SELECT COUNT(DISTINCT voter_id) AS n FROM votes WHERE match_id=? AND vtype='mvp'", (mid,))
        mvp_n = c.fetchone()["n"]
        c.execute("SELECT COUNT(DISTINCT voter_id) AS n FROM votes WHERE match_id=? AND vtype='defense'", (mid,))
        def_n = c.fetchone()["n"]
        if played_n > 0 and mvp_n >= played_n and def_n >= played_n:
            c.execute("UPDATE matches SET voting_closed=1 WHERE id=?", (mid,))
    return {"ok": True}


@app.post("/api/matches/{mid}/lineup")
def save_lineup(mid: int, x: LineupIn, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    t = x.team_num or 1
    tbl = "lineup2" if t == 2 else "lineup"
    subs_tbl = "lineup_subs2" if t == 2 else "lineup_subs"
    with db() as c:
        for slot, pid in x.lineup.items():
            if pid:
                c.execute(
                    f"""INSERT INTO {tbl}(match_id,slot,player_id) VALUES(%s,%s,%s)
                       ON CONFLICT(match_id,slot) DO UPDATE SET player_id=EXCLUDED.player_id""",
                    (mid, slot, int(pid))
                )
            else:
                c.execute(f"DELETE FROM {tbl} WHERE match_id=%s AND slot=%s", (mid, slot))
        if x.subs is not None:
            # Delete all existing subs for this match/team, then reinsert
            c.execute(f"DELETE FROM {subs_tbl} WHERE match_id=%s", (mid,))
            for slot, sub_list in x.subs.items():
                for idx, pid in enumerate(sub_list or [], 1):
                    if pid:
                        c.execute(
                            f"INSERT INTO {subs_tbl}(match_id,slot,sub_index,player_id) VALUES(%s,%s,%s,%s)",
                            (mid, slot, idx, int(pid))
                        )
    return {"ok": True}


@app.post("/api/matches/{mid}/notes")
def save_notes(mid: int, x: NotesIn, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute(
            """INSERT INTO match_notes(match_id,notes) VALUES(?,?)
               ON CONFLICT(match_id) DO UPDATE SET notes=EXCLUDED.notes""",
            (mid, x.notes)
        )
    return {"ok": True}


@app.delete("/api/matches/{mid}/vote")
def reset_vote(mid: int, request: Request):
    """Captain-only: delete their own votes for a match (to re-vote or for testing)."""
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute("DELETE FROM votes WHERE match_id=? AND voter_id=?", (mid, p["id"]))
    return {"ok": True}


# ── Leaderboard ───────────────────────────────────────────────────────────────

@app.get("/api/leaderboard")
def leaderboard(request: Request, season_id: Optional[int] = None):
    get_player(request)
    with db() as c:
        if season_id is None:
            c.execute("SELECT id FROM seasons WHERE active=1")
            row = c.fetchone()
            season_id = row["id"] if row else 1
        c.execute("SELECT id FROM matches WHERE season_id=?", (season_id,))
        match_ids = [r["id"] for r in c.fetchall()]

        # Exclude Босс from leaderboard
        c.execute(
            "SELECT * FROM players WHERE approved=1 AND COALESCE(position,'') != 'Босс'"
        )
        all_players = c.fetchall()

        rows = []
        for pl in all_players:
            if match_ids:
                ph = ",".join(["%s"] * len(match_ids))
                c.execute(
                    f"SELECT COUNT(*) AS g, COALESCE(SUM(goals),0) AS goals,"
                    f" COALESCE(SUM(assists),0) AS assists, COALESCE(SUM(keeper_points),0) AS kp"
                    f" FROM stats WHERE player_id=%s AND played=1 AND match_id IN ({ph})",
                    [pl["id"]] + match_ids,
                )
                st = c.fetchone()
                c.execute(
                    f"""SELECT COUNT(*) AS n FROM (
                        SELECT v.match_id, v.target_id,
                               RANK() OVER(PARTITION BY v.match_id ORDER BY COUNT(*) DESC) AS rk
                        FROM votes v JOIN matches mx ON mx.id=v.match_id
                        WHERE v.vtype='mvp' AND v.match_id IN ({ph}) AND mx.voting_closed=1
                        GROUP BY v.match_id, v.target_id
                    ) q WHERE target_id=%s AND rk=1""",
                    match_ids + [pl["id"]],
                )
                mvp = c.fetchone()["n"]
                c.execute(
                    f"""SELECT COUNT(*) AS n FROM (
                        SELECT v.match_id, v.target_id,
                               RANK() OVER(PARTITION BY v.match_id ORDER BY COUNT(*) DESC) AS rk
                        FROM votes v JOIN matches mx ON mx.id=v.match_id
                        WHERE v.vtype='defense' AND v.match_id IN ({ph}) AND mx.voting_closed=1
                        GROUP BY v.match_id, v.target_id
                    ) q WHERE target_id=%s AND rk=1""",
                    match_ids + [pl["id"]],
                )
                dfn = c.fetchone()["n"]
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
            c.execute("SELECT id FROM seasons WHERE active=1")
            row = c.fetchone()
            season_id = row["id"] if row else 1
        c.execute(
            "SELECT id FROM matches WHERE gf IS NOT NULL AND season_id=?", (season_id,)
        )
        ids = [r["id"] for r in c.fetchall()]
        team_bank = sum(calc_finance(c, i)["team_bank"] for i in ids)
        return {
            "played": len(ids),
            "team_bank": team_bank,
            "personal_fund": len(ids) * 600,
        }
