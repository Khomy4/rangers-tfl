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
APP_TZ = os.getenv("APP_TZ", "Europe/Moscow")


def now_local() -> datetime:
    """Current time in the team's timezone (match dates and deadlines are entered in it)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(APP_TZ)).replace(tzinfo=None)
    except Exception:
        from datetime import timedelta
        return datetime.utcnow() + timedelta(hours=3)  # Moscow is UTC+3 all year

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
        # Add discipline_ok column if not exists
        c.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS discipline_ok INTEGER DEFAULT NULL")
        c.execute("ALTER TABLE stats ADD COLUMN IF NOT EXISTS yellow_card INTEGER DEFAULT 0")
        c.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS voting_opened_at TEXT DEFAULT NULL")
        c.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS phase TEXT DEFAULT 'season'")
        c.execute("ALTER TABLE seasons ADD COLUMN IF NOT EXISTS total_games INTEGER DEFAULT 5")
        # Scoring rules version: 1 = old (seasons before Nov 2026), 2 = new rules
        c.execute("ALTER TABLE seasons ADD COLUMN IF NOT EXISTS rules INTEGER DEFAULT 1")
        c.execute("ALTER TABLE seasons ALTER COLUMN rules SET DEFAULT 2")
        c.execute("ALTER TABLE seasons ADD COLUMN IF NOT EXISTS r1_done INTEGER DEFAULT 0")
        c.execute("ALTER TABLE seasons ADD COLUMN IF NOT EXISTS r1_top3 INTEGER DEFAULT NULL")
        c.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS is_top INTEGER DEFAULT 0")
        c.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS round INTEGER DEFAULT 1")
        c.execute("ALTER TABLE stats ADD COLUMN IF NOT EXISTS match_pos TEXT DEFAULT NULL")
        # Backfill: start 24h clock now for any active voting that predates this feature
        c.execute(
            """
            UPDATE matches SET voting_opened_at=%s
            WHERE voting_opened_at IS NULL AND voting_closed=0
            AND id IN (SELECT DISTINCT match_id FROM votes)
            """,
            (datetime.utcnow().isoformat(),)
        )
        c.execute("ALTER TABLE stats ADD COLUMN IF NOT EXISTS red_card INTEGER DEFAULT 0")
        c.execute("""
        CREATE TABLE IF NOT EXISTS vote_override(
            match_id INTEGER,
            player_id INTEGER,
            vtype TEXT,
            count INTEGER DEFAULT 0,
            PRIMARY KEY(match_id, player_id, vtype)
        )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS match_formation(
            match_id INTEGER,
            team_num INTEGER NOT NULL DEFAULT 1,
            formation TEXT NOT NULL DEFAULT '2-1-2',
            PRIMARY KEY(match_id, team_num)
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

        if CAPTAIN_ID:
            c.execute("UPDATE players SET approved=1 WHERE telegram_id=?", (CAPTAIN_ID,))
        c.execute("SELECT COUNT(*) AS n FROM seasons")
        if c.fetchone()["n"] == 0:
            c.execute("INSERT INTO seasons(name,active,rules) VALUES('Сезон 1',1,2)")


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


def get_member(request: Request):
    """Logged-in player who has been approved by the captain."""
    p = get_player(request)
    if not p.get("approved") and not is_captain(p):
        raise HTTPException(403, "Капитан ещё не подтвердил твой аккаунт")
    return p


def is_captain(p: dict) -> bool:
    return (CAPTAIN_ID != 0 and p.get("telegram_id") == CAPTAIN_ID) or (DEV and p["id"] == 1)


# ── Finance ───────────────────────────────────────────────────────────────────

def po_round(x) -> int:
    """Round half up (4.5 -> 5), not Python's banker's rounding."""
    return int(x + 0.5)


def result_bonus(gf, ga) -> int:
    if gf is None or ga is None:
        return 0
    if gf < ga: return 0
    if gf == ga: return 200
    return {1: 300, 2: 400, 3: 500}.get(gf - ga, 600)


def season_rules(c: Cur, season_id) -> int:
    c.execute("SELECT rules FROM seasons WHERE id=%s", (season_id,))
    row = c.fetchone()
    return (row or {}).get("rules") or 1


def match_mult(m: dict, rules: int) -> float:
    """Multiplier for a match. Rules 2: win over a top-3 opponent = x2 (not stacked with playoff)."""
    is_playoff = (m.get("phase") or "season") == "playoff"
    if rules >= 2 and m.get("is_top") and m.get("gf") is not None and m["gf"] > m["ga"]:
        return 2.0
    return 1.5 if is_playoff else 1.0


def result_bonus_v2(gf, ga) -> int:
    if gf is None or ga is None or gf < ga:
        return 0
    if gf == ga:
        return 200
    return 600 if gf - ga >= 3 else 500


POS_FROM_PROFILE = {"Вратарь": "GK", "Защитник": "DEF", "Универсал": "MID", "Нападающий": "FWD"}


def player_points_v2(m: dict, st: dict, profile_pos: str, is_mvp: bool) -> dict:
    """New (rules 2) points for one player in one match."""
    pos = st.get("match_pos") or POS_FROM_PROFILE.get(profile_pos or "", "")
    gf, ga = m.get("gf"), m.get("ga")
    card_pen = (st.get("yellow_card") or 0) * 1 + (st.get("red_card") or 0) * 3
    if not st.get("played"):
        return {"points": 0, "pos": pos, "result_pts": 0, "pos_pts": 0, "mult": 1.0}
    result_pts = pos_pts = 0
    if gf is not None and ga is not None:
        result_pts = 2 if gf > ga else 1 if gf == ga else 0
        if pos == "GK":
            pos_pts = 3 if ga == 0 else 1 if ga <= 2 else 0
        elif pos == "DEF":
            pos_pts = (2 if ga == 0 else 1 if ga == 1 else 0) + min(max(gf - ga, 0), 3)
    base = (st.get("goals") or 0) + (st.get("assists") or 0) + (3 if is_mvp else 0) + result_pts + pos_pts
    mult = match_mult(m, 2)
    return {"points": po_round(base * mult) - card_pen, "pos": pos,
            "result_pts": result_pts, "pos_pts": pos_pts, "mult": mult}


def vote_winners(c: Cur, mid: int):
    """MVP / defense winner sets for a match (captain overrides win if present)."""
    c.execute("SELECT vtype, target_id FROM votes WHERE match_id=%s", (mid,))
    mvp_votes, def_votes = {}, {}
    for v in c.fetchall():
        b = mvp_votes if v["vtype"] == "mvp" else def_votes
        b[v["target_id"]] = b.get(v["target_id"], 0) + 1
    c.execute("SELECT player_id, vtype, count FROM vote_override WHERE match_id=%s", (mid,))
    ov = c.fetchall()
    if ov:
        mvp_votes, def_votes = {}, {}
        for o in ov:
            if o["count"] > 0:
                (mvp_votes if o["vtype"] == "mvp" else def_votes)[o["player_id"]] = o["count"]
    def top(d):
        if not d:
            return set()
        mx = max(d.values())
        return {k for k, v in d.items() if v == mx}
    return top(mvp_votes), top(def_votes)


def calc_finance(c: Cur, mid: int) -> dict:
    c.execute("SELECT * FROM matches WHERE id=%s", (mid,))
    m = c.fetchone()
    if not m:
        return {}
    rules = season_rules(c, m.get("season_id") or 1)
    if rules >= 2:
        mult = match_mult(m, rules)
        disc_ok = m.get("discipline_ok")
        disc = int(300 * mult) if disc_ok else 0
        rb = int(result_bonus_v2(m["gf"], m["ga"]) * mult)
        return {
            "discipline": disc,
            "discipline_ok": disc_ok,
            "discipline_pending": disc_ok is None,
            "result": rb,
            "team_bank": disc + rb,
            "personal_fund": int(600 * mult) if m["gf"] is not None else 0,
            "is_playoff": (m.get("phase") or "season") == "playoff",
            "is_top": bool(m.get("is_top")),
            "mult": mult,
            "rules": rules,
        }
    is_playoff = (m.get("phase") or "season") == "playoff"
    mult = 1.5 if is_playoff else 1.0
    disc_ok = m.get("discipline_ok")
    if disc_ok is None:
        disc = 0
        disc_pending = True
    else:
        disc = int(300 * mult) if disc_ok else 0
        disc_pending = False
    rb = int(result_bonus(m["gf"], m["ga"]) * mult)
    personal = int(600 * mult) if m["gf"] is not None else 0
    return {
        "discipline": disc,
        "discipline_ok": disc_ok,
        "discipline_pending": disc_pending,
        "result": rb,
        "team_bank": disc + rb,
        "personal_fund": personal,
        "is_playoff": is_playoff,
        "is_top": bool(m.get("is_top")),
        "mult": mult,
        "rules": rules,
    }


def deadline_passed(m) -> bool:
    dl = m.get("rsvp_deadline") if isinstance(m, dict) else m["rsvp_deadline"]
    if not dl:
        return False
    try:
        return now_local() > datetime.fromisoformat(str(dl))
    except Exception:
        return False


# ── Pydantic models ───────────────────────────────────────────────────────────

class MatchIn(BaseModel):
    opponent: str
    match_date: str
    rsvp_deadline: Optional[str] = None
    season_id: Optional[int] = None  # None = active season
    phase: Optional[str] = None  # "season" or "playoff"; None on edit = keep
    is_top: Optional[bool] = None  # top-3 opponent; None on edit = keep

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
    yellow_card: int = 0
    red_card: int = 0
    match_pos: Optional[str] = None  # GK / DEF / MID / FWD

class VoteIn(BaseModel):
    vtype: str
    target_id: int

class VoteAsIn(BaseModel):
    voter_id: int      # игрок вместо которого голосуем
    mvp_target: int    # за кого MVP
    def_target: Optional[int] = None  # за кого защита (нет в новых правилах)

class LineupIn(BaseModel):
    lineup: dict  # {slot: player_id or None}
    subs: Optional[dict] = None  # {slot: [pid, ...]}
    team_num: Optional[int] = 1  # 1 or 2

class NotesIn(BaseModel):
    notes: str

class FormationIn(BaseModel):
    formation: str  # '2-1-2' or '2-2-1'
    team_num: Optional[int] = 1

class PositionIn(BaseModel):
    position: str

class PlayerNameIn(BaseModel):
    name: str

class RemainingGamesIn(BaseModel):
    remaining: int

class SeasonIn(BaseModel):
    name: str

class RulesIn(BaseModel):
    rules: int

class Round1In(BaseModel):
    top3: Optional[bool] = None
    undo: bool = False


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
    get_member(request)
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
        c.execute("DELETE FROM vote_override WHERE player_id=?", (pid,))
        for tbl in ("lineup", "lineup2", "lineup_subs", "lineup_subs2"):
            c.execute(f"DELETE FROM {tbl} WHERE player_id=?", (pid,))
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
    get_member(request)
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
        c.execute("INSERT INTO seasons(name,active,rules) VALUES(?,1,2) RETURNING id", (x.name,))
        row = c.fetchone()
        return {"id": row["id"]}


@app.put("/api/seasons/{sid}/remaining")
def set_remaining_games(sid: int, x: RemainingGamesIn, request: Request):
    """Captain sets how many games are left; stored as total = played + remaining."""
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    if x.remaining < 0 or x.remaining > 200:
        raise HTTPException(400, "Некорректное число игр")
    with db() as c:
        c.execute("SELECT COUNT(*) AS n FROM matches WHERE gf IS NOT NULL AND season_id=?", (sid,))
        played = c.fetchone()["n"]
        c.execute("UPDATE seasons SET total_games=? WHERE id=?", (played + x.remaining, sid))
    return {"ok": True, "total_games": played + x.remaining}


@app.put("/api/seasons/{sid}/rules")
def set_season_rules(sid: int, x: RulesIn, request: Request):
    """Captain switches a season between old (1) and new (2) scoring rules."""
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    if x.rules not in (1, 2):
        raise HTTPException(400, "Неизвестные правила")
    with db() as c:
        c.execute("UPDATE seasons SET rules=? WHERE id=?", (x.rules, sid))
    return {"ok": True}


@app.post("/api/seasons/{sid}/round1")
def finish_round1(sid: int, x: Round1In, request: Request):
    """Captain closes the first round (top3 = whether we are in the league top-3), or undoes it."""
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute("SELECT r1_done FROM seasons WHERE id=?", (sid,))
        srow = c.fetchone()
        if not srow:
            raise HTTPException(404)
        if x.undo:
            c.execute("UPDATE seasons SET r1_done=0, r1_top3=NULL WHERE id=?", (sid,))
            # Not-yet-played matches go back to round 1; played round-2 matches stay in round 2
            c.execute("UPDATE matches SET round=1 WHERE season_id=? AND gf IS NULL", (sid,))
        else:
            if x.top3 is None:
                raise HTTPException(400, "Укажи, в топ-3 команда или нет")
            c.execute("UPDATE seasons SET r1_done=1, r1_top3=? WHERE id=?", (1 if x.top3 else 0, sid))
            if not srow.get("r1_done"):
                # Closing the round: matches not played yet move to round 2
                c.execute("UPDATE matches SET round=2 WHERE season_id=? AND gf IS NULL AND round=1", (sid,))
    return {"ok": True}


# ── Matches ───────────────────────────────────────────────────────────────────

@app.get("/api/matches")
def matches(request: Request, season_id: Optional[int] = None):
    get_member(request)
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
    p = get_member(request)
    # Auto-close voting if 24h from match_date have passed
    with db() as c:
        maybe_close_voting(c, mid)
    with db() as c:
        c.execute("SELECT * FROM matches WHERE id=?", (mid,))
        m = c.fetchone()
        if not m:
            raise HTTPException(404)
        d = dict(m)
        d["finance"] = calc_finance(c, mid)
        d["deadline_passed"] = deadline_passed(d)
        rules = season_rules(c, m.get("season_id") or 1)
        d["rules"] = rules

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
        # Apply captain overrides if any
        c.execute("SELECT player_id, vtype, count FROM vote_override WHERE match_id=?", (mid,))
        ov_rows = c.fetchall()
        if ov_rows:
            mvp_votes, def_votes = {}, {}
            for ov in ov_rows:
                if ov["count"] > 0:
                    (mvp_votes if ov["vtype"] == "mvp" else def_votes)[ov["player_id"]] = ov["count"]

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
            if rules >= 2:
                is_mvp = m["voting_closed"] == 1 and pl["id"] in mvp_winners
                pp = player_points_v2(m, st, pl["position"], is_mvp)
                d["stats"].append({
                    "player_id": pl["id"],
                    "name": pl["name"],
                    "position": pl["position"] or "",
                    "match_pos": pp["pos"],
                    "played": st["played"],
                    "goals": st["goals"],
                    "assists": st["assists"],
                    "keeper_points": st["keeper_points"],
                    "yellow_card": st.get("yellow_card", 0),
                    "red_card": st.get("red_card", 0),
                    "is_mvp": is_mvp,
                    "is_best_defense": False,
                    "result_pts": pp["result_pts"],
                    "pos_pts": pp["pos_pts"],
                    "points": pp["points"],
                })
                continue
            # MVP/defense points only count if voting is closed; all tied winners get points
            mvp_pts = 3 if (m["voting_closed"] == 1 and pl["id"] in mvp_winners) else 0
            def_pts = 2 if (m["voting_closed"] == 1 and pl["id"] in def_winners) else 0
            card_pen = st.get("yellow_card",0) * 1 + st.get("red_card",0) * 3
            _mult = 1.5 if (m.get("phase") or "season") == "playoff" else 1.0
            pts = po_round((st["goals"] * 2 + st["assists"] + st["keeper_points"] + mvp_pts + def_pts) * _mult) - card_pen
            d["stats"].append({
                "player_id": pl["id"],
                "name": pl["name"],
                "position": pl["position"] or "",
                "played": st["played"],
                "goals": st["goals"],
                "assists": st["assists"],
                "keeper_points": st["keeper_points"],
                "yellow_card": st.get("yellow_card", 0),
                "red_card": st.get("red_card", 0),
                "is_mvp": m["voting_closed"] == 1 and pl["id"] in mvp_winners,
                "is_best_defense": m["voting_closed"] == 1 and pl["id"] in def_winners,
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
        # Compute whether 48h deadline has passed (captain decides to close or annul)
        voting_expired = False
        if m.get("match_date") and m["voting_closed"] == 0:
            from datetime import datetime
            try:
                match_dt = datetime.fromisoformat(m["match_date"])
                voting_expired = (now_local() - match_dt).total_seconds() >= 172800
            except Exception:
                pass
        d["voting_expired"] = voting_expired
        d["voting_opened_at"] = m.get("match_date")
        d["voting_annulled"] = m["voting_closed"] == 2
        d["phase"] = m.get("phase") or "season"

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
        # Formation
        c.execute("SELECT team_num, formation FROM match_formation WHERE match_id=%s", (mid,))
        fm_rows = c.fetchall()
        fm_map = {r["team_num"]: r["formation"] for r in fm_rows}
        d["formation"]  = fm_map.get(1, "2-1-2")
        d["formation2"] = fm_map.get(2, "2-1-2")
        # Notes
        c.execute("SELECT notes FROM match_notes WHERE match_id=%s", (mid,))
        nrow = c.fetchone()
        d["notes"] = nrow["notes"] if nrow else ""

        # Captain sees live vote breakdown
        if is_captain(p):
            d["raw_stats"] = list(stats_map.values())
            name_map = {pl["id"]: pl["name"] for pl in all_players}
            def breakdown(vdict):
                items = [{"id": k, "name": name_map.get(k, "?"), "count": v} for k, v in vdict.items()]
                return sorted(items, key=lambda x: -x["count"])
            d["vote_counts"] = {"mvp": breakdown(mvp_votes), "defense": breakdown(def_votes)}
            ov_map = {}
            for ov in ov_rows:
                ov_map.setdefault(ov["player_id"], {"mvp": 0, "defense": 0})[ov["vtype"]] = ov["count"]
            d["vote_overrides"] = ov_map
            # Детальная таблица: кто за кого проголосовал
            mvp_by_voter = {v["voter_id"]: v["target_id"] for v in all_votes if v["vtype"] == "mvp"}
            def_by_voter  = {v["voter_id"]: v["target_id"] for v in all_votes if v["vtype"] == "defense"}
            played_ids = [s["player_id"] for s in stats_map.values() if s.get("played")]
            d["vote_details"] = [
                {
                    "voter_id": pid,
                    "voter_name": name_map.get(pid, "?"),
                    "mvp_target": name_map.get(mvp_by_voter.get(pid), "—") if pid in mvp_by_voter else "—",
                    "def_target": name_map.get(def_by_voter.get(pid), "—") if pid in def_by_voter else "—",
                    "voted": pid in mvp_by_voter or pid in def_by_voter,
                }
                for pid in played_ids
            ]

        return d


@app.post("/api/matches")
def add_match(x: MatchIn, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        sid = x.season_id
        if not sid:
            c.execute("SELECT id FROM seasons WHERE active=1 ORDER BY id DESC LIMIT 1")
            srow = c.fetchone()
            sid = srow["id"] if srow else 1
        c.execute("SELECT r1_done FROM seasons WHERE id=?", (sid,))
        srow = c.fetchone() or {}
        rnd = 2 if srow.get("r1_done") else 1
        c.execute(
            "INSERT INTO matches(season_id,opponent,match_date,rsvp_deadline,phase,is_top,round) VALUES(?,?,?,?,?,?,?) RETURNING id",
            (sid, x.opponent, x.match_date, x.rsvp_deadline, x.phase or "season", 1 if x.is_top else 0, rnd),
        )
        row = c.fetchone()
        return {"id": row["id"]}


@app.put("/api/matches/{mid}")
def update_match(mid: int, x: MatchIn, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute(
            "UPDATE matches SET opponent=?,match_date=?,rsvp_deadline=?,"
            "phase=COALESCE(?,phase), is_top=COALESCE(?,is_top) WHERE id=?",
            (x.opponent, x.match_date, x.rsvp_deadline, x.phase,
             None if x.is_top is None else (1 if x.is_top else 0), mid),
        )
    return {"ok": True}


@app.delete("/api/matches/{mid}")
def delete_match(mid: int, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        for tbl in ("rsvp", "stats", "votes", "vote_override", "lineup", "lineup2",
                    "lineup_subs", "lineup_subs2", "match_notes", "match_formation"):
            c.execute(f"DELETE FROM {tbl} WHERE match_id=?", (mid,))
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


@app.post("/api/matches/{mid}/annul_voting")
def annul_voting(mid: int, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute("UPDATE matches SET voting_closed=2 WHERE id=?", (mid,))
    return {"ok": True}


@app.post("/api/matches/{mid}/vote_as")
def vote_as(mid: int, x: VoteAsIn, request: Request):
    """Капитан голосует вместо другого игрока (удаляет старый голос и пишет новый)."""
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute("SELECT * FROM matches WHERE id=?", (mid,))
        m = c.fetchone()
        if not m:
            raise HTTPException(404)
        if m["voting_closed"]:
            raise HTTPException(403, "Голосование закрыто")
        if m["gf"] is None:
            raise HTTPException(403, "Матч ещё не сыгран")
        voter = x.voter_id
        mvp_only = season_rules(c, m.get("season_id") or 1) >= 2
        if not mvp_only and x.def_target is None:
            raise HTTPException(400, "Выбери игрока для защиты")
        if x.mvp_target == voter or (not mvp_only and x.def_target == voter):
            raise HTTPException(400, "Нельзя голосовать за себя")
        targets = [x.mvp_target] + ([] if mvp_only else [x.def_target])
        for t in targets:
            c.execute("SELECT 1 AS ok FROM stats WHERE match_id=? AND player_id=? AND played=1", (mid, t))
            if not c.fetchone():
                raise HTTPException(400, "Голосовать можно только за игроков матча")
        # Удаляем старые голоса этого игрока, пишем новые
        c.execute("DELETE FROM votes WHERE match_id=? AND voter_id=?", (mid, voter))
        c.execute("INSERT INTO votes VALUES(?,?,?,?)", (mid, voter, "mvp", x.mvp_target))
        if not mvp_only:
            c.execute("INSERT INTO votes VALUES(?,?,?,?)", (mid, voter, "defense", x.def_target))
        c.execute(
            "UPDATE matches SET voting_opened_at=? WHERE id=? AND voting_opened_at IS NULL",
            (datetime.utcnow().isoformat(), mid)
        )
        maybe_close_voting(c, mid)
    return {"ok": True}


# ── RSVP ─────────────────────────────────────────────────────────────────────

@app.post("/api/matches/{mid}/rsvp")
def rsvp(mid: int, x: RsvpIn, request: Request):
    p = get_member(request)
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
    match_pos = x.match_pos if x.match_pos in ("GK", "DEF", "MID", "FWD") else None
    with db() as c:
        if match_pos is None:
            c.execute("SELECT position FROM players WHERE id=?", (x.player_id,))
            prow = c.fetchone() or {}
            match_pos = POS_FROM_PROFILE.get(prow.get("position") or "")
        c.execute(
            """INSERT INTO stats(match_id,player_id,played,goals,assists,keeper_points,yellow_card,red_card,match_pos)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(match_id,player_id) DO UPDATE
               SET played=EXCLUDED.played, goals=EXCLUDED.goals,
                   assists=EXCLUDED.assists, keeper_points=EXCLUDED.keeper_points,
                   yellow_card=EXCLUDED.yellow_card, red_card=EXCLUDED.red_card,
                   match_pos=EXCLUDED.match_pos""",
            (mid, x.player_id, x.played, x.goals, x.assists, x.keeper_points,
             x.yellow_card, x.red_card, match_pos),
        )
    return {"ok": True}


# ── Votes ─────────────────────────────────────────────────────────────────────

def maybe_close_voting(c, mid: int):
    """Auto-close voting only when 100% of players voted for both categories."""
    c.execute("SELECT voting_closed, season_id FROM matches WHERE id=?", (mid,))
    m = c.fetchone()
    if not m or m["voting_closed"] != 0:
        return
    mvp_only = season_rules(c, m.get("season_id") or 1) >= 2
    c.execute("SELECT COUNT(*) AS n FROM stats WHERE match_id=? AND played=1", (mid,))
    eligible = c.fetchone()["n"]
    if eligible == 0:
        return
    played_voters = ("SELECT COUNT(DISTINCT v.voter_id) AS n FROM votes v "
                     "JOIN stats s ON s.match_id=v.match_id AND s.player_id=v.voter_id AND s.played=1 "
                     "WHERE v.match_id=? AND v.vtype=?")
    c.execute(played_voters, (mid, "mvp"))
    mvp_n = c.fetchone()["n"]
    c.execute(played_voters, (mid, "defense"))
    def_n = c.fetchone()["n"]
    if (mvp_n if mvp_only else min(mvp_n, def_n)) >= eligible:
        c.execute("UPDATE matches SET voting_closed=1 WHERE id=?", (mid,))


@app.post("/api/matches/{mid}/vote")
def vote(mid: int, x: VoteIn, request: Request):
    p = get_member(request)
    with db() as c:
        c.execute("SELECT * FROM matches WHERE id=?", (mid,))
        m = c.fetchone()
        if not m:
            raise HTTPException(404)
        if m["voting_closed"]:
            raise HTTPException(403, "Голосование закрыто")
        # Block voting after 48h deadline (captain decides manually)
        if m.get("match_date"):
            from datetime import datetime
            try:
                match_dt = datetime.fromisoformat(m["match_date"])
                if (now_local() - match_dt).total_seconds() >= 172800:
                    raise HTTPException(403, "Время голосования истекло")
            except HTTPException:
                raise
            except Exception:
                pass
        if m["gf"] is None:
            raise HTTPException(403, "Матч ещё не сыгран")
        if x.vtype not in ("mvp", "defense"):
            raise HTTPException(400)
        if x.vtype == "defense" and season_rules(c, m.get("season_id") or 1) >= 2:
            raise HTTPException(400, "Голосования за защиту больше нет")
        if x.target_id == p["id"]:
            raise HTTPException(400, "Нельзя голосовать за себя")
        c.execute("SELECT 1 AS ok FROM stats WHERE match_id=? AND player_id=? AND played=1", (mid, x.target_id))
        if not c.fetchone():
            raise HTTPException(400, "Этот игрок не играл в матче")
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
        # Record when voting started
        c.execute(
            "UPDATE matches SET voting_opened_at=? WHERE id=? AND voting_opened_at IS NULL",
            (datetime.utcnow().isoformat(), mid)
        )
        maybe_close_voting(c, mid)
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


@app.post("/api/matches/{mid}/formation")
def save_formation(mid: int, x: FormationIn, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    if x.formation not in ("2-1-2", "2-2-1"):
        raise HTTPException(400, "invalid formation")
    t = x.team_num or 1
    with db() as c:
        c.execute(
            """INSERT INTO match_formation(match_id,team_num,formation) VALUES(%s,%s,%s)
               ON CONFLICT(match_id,team_num) DO UPDATE SET formation=EXCLUDED.formation""",
            (mid, t, x.formation)
        )
    return {"ok": True}


class DisciplineIn(BaseModel):
    ok: bool

@app.post("/api/matches/{mid}/discipline")
def set_discipline(mid: int, x: DisciplineIn, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute(
            "UPDATE matches SET discipline_ok=%s WHERE id=%s",
            (1 if x.ok else 0, mid)
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


class VoteAdminIn(BaseModel):
    overrides: list[dict]

@app.post("/api/matches/{mid}/votes/admin")
def set_vote_admin(mid: int, x: VoteAdminIn, request: Request):
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute("DELETE FROM vote_override WHERE match_id=?", (mid,))
        for item in x.overrides:
            pid = item.get("player_id")
            for vtype in ("mvp", "defense"):
                cnt = int(item.get(vtype, 0) or 0)
                if cnt > 0:
                    c.execute(
                        "INSERT INTO vote_override(match_id,player_id,vtype,count) VALUES(?,?,?,?)",
                        (mid, pid, vtype, cnt)
                    )
    return {"ok": True}

@app.delete("/api/matches/{mid}/vote")
def reset_vote(mid: int, request: Request):
    """Captain-only: delete their own votes for a match (to re-vote or for testing)."""
    p = get_player(request)
    if not is_captain(p):
        raise HTTPException(403)
    with db() as c:
        c.execute("SELECT voting_closed FROM matches WHERE id=?", (mid,))
        m = c.fetchone()
        if not m:
            raise HTTPException(404)
        if m["voting_closed"]:
            raise HTTPException(403, "Голосование уже закрыто")
        c.execute("DELETE FROM votes WHERE match_id=? AND voter_id=?", (mid, p["id"]))
    return {"ok": True}


# ── Leaderboard ───────────────────────────────────────────────────────────────

def leaderboard_v2(c: Cur, season_id: int, rnd: Optional[int] = None) -> list:
    """Rules 2: points are the sum of each match's points (same numbers as the match card)."""
    q = "SELECT * FROM matches WHERE season_id=%s AND gf IS NOT NULL"
    args = [season_id]
    if rnd in (1, 2):
        q += " AND round=%s"
        args.append(rnd)
    c.execute(q, args)
    ms = c.fetchall()
    c.execute("SELECT * FROM players WHERE approved=1 AND COALESCE(position,'') != 'Босс'")
    players = c.fetchall()
    rows = {pl["id"]: dict(id=pl["id"], name=pl["name"], position=pl["position"] or "",
                           games=0, total=len(ms), goals=0, assists=0, mvp=0, defense=0,
                           clean_sheets=0, wins=0, yellow_cards=0, red_cards=0, points=0)
            for pl in players}
    prof = {pl["id"]: pl["position"] for pl in players}
    for m in ms:
        mvp_w, _ = vote_winners(c, m["id"])
        c.execute("SELECT * FROM stats WHERE match_id=%s", (m["id"],))
        for st in c.fetchall():
            r = rows.get(st["player_id"])
            if not r:
                continue
            is_mvp = m["voting_closed"] == 1 and st["player_id"] in mvp_w
            pp = player_points_v2(m, st, prof.get(st["player_id"]), is_mvp)
            r["points"] += pp["points"]
            r["yellow_cards"] += st.get("yellow_card") or 0
            r["red_cards"] += st.get("red_card") or 0
            if st.get("played"):
                r["games"] += 1
                r["goals"] += st["goals"] or 0
                r["assists"] += st["assists"] or 0
                r["mvp"] += 1 if is_mvp else 0
                r["wins"] += 1 if m["gf"] > m["ga"] else 0
                if m["ga"] == 0 and pp["pos"] in ("GK", "DEF"):
                    r["clean_sheets"] += 1
    out = sorted(rows.values(), key=lambda r: (-r["points"], -r["goals"], -r["assists"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out


@app.get("/api/leaderboard")
def leaderboard(request: Request, season_id: Optional[int] = None, round: Optional[int] = None):
    get_member(request)
    with db() as c:
        if season_id is None:
            c.execute("SELECT id FROM seasons WHERE active=1")
            row = c.fetchone()
            season_id = row["id"] if row else 1
        if season_rules(c, season_id) >= 2:
            return leaderboard_v2(c, season_id, round)
        c.execute("SELECT id, phase FROM matches WHERE season_id=?", (season_id,))
        match_rows = c.fetchall()
        match_ids = [r["id"] for r in match_rows]
        playoff_ids = {r["id"] for r in match_rows if (r.get("phase") or "season") == "playoff"}

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

            # Card penalties per season
            if match_ids:
                ph = ",".join(["%s"] * len(match_ids))
                c.execute(
                    f"SELECT COALESCE(SUM(yellow_card),0) AS yc, COALESCE(SUM(red_card),0) AS rc"
                    f" FROM stats WHERE player_id=%s AND match_id IN ({ph})",
                    [pl["id"]] + match_ids,
                )
                cards = c.fetchone()
                yc, rc = cards["yc"], cards["rc"]
            else:
                yc = rc = 0
            card_pen = yc * 1 + rc * 3
            # Compute pts with playoff multiplier per match
            if match_ids and playoff_ids:
                non_po = [i for i in match_ids if i not in playoff_ids]
                po = [i for i in match_ids if i in playoff_ids]
                def _stat(ids, col):
                    if not ids:
                        return 0
                    ph2 = ",".join(["%s"] * len(ids))
                    c.execute(
                        f"SELECT COALESCE(SUM({col}),0) AS v FROM stats WHERE player_id=%s AND played=1 AND match_id IN ({ph2})",
                        [pl["id"]] + ids,
                    )
                    return c.fetchone()["v"]
                def _mvp(ids, vtype):
                    if not ids:
                        return 0
                    ph2 = ",".join(["%s"] * len(ids))
                    c.execute(
                        f"""SELECT COUNT(*) AS n FROM (
                            SELECT v.match_id, v.target_id,
                                   RANK() OVER(PARTITION BY v.match_id ORDER BY COUNT(*) DESC) AS rk
                            FROM votes v JOIN matches mx ON mx.id=v.match_id
                            WHERE v.vtype=%s AND v.match_id IN ({ph2}) AND mx.voting_closed=1
                            GROUP BY v.match_id, v.target_id
                        ) q WHERE target_id=%s AND rk=1""",
                        [vtype] + ids + [pl["id"]],
                    )
                    return c.fetchone()["n"]
                pts_base = (
                    _stat(non_po,"goals")*2 + _stat(non_po,"assists") +
                    _stat(non_po,"keeper_points") +
                    _mvp(non_po,"mvp")*3 + _mvp(non_po,"defense")*2
                )
                pts_po = sum(
                    po_round((
                        _stat([pm],"goals")*2 + _stat([pm],"assists") +
                        _stat([pm],"keeper_points") +
                        _mvp([pm],"mvp")*3 + _mvp([pm],"defense")*2
                    ) * 1.5)
                    for pm in po
                )
                pts = pts_base + pts_po - card_pen
            else:
                pts = goals * 2 + assists + mvp * 3 + dfn * 2 + kp - card_pen
            rows.append(dict(
                id=pl["id"], name=pl["name"], position=pl["position"] or "",
                games=games, total=len(match_ids),
                goals=goals, assists=assists, mvp=mvp, defense=dfn,
                yellow_cards=yc, red_cards=rc,
                points=pts,
            ))
        rows.sort(key=lambda r: (-r["points"], -r["goals"], -r["assists"]))
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        return rows


# ── Summary ───────────────────────────────────────────────────────────────────

@app.get("/api/summary")
def summary(request: Request, season_id: Optional[int] = None):
    get_member(request)
    with db() as c:
        if season_id is None:
            c.execute("SELECT id FROM seasons WHERE active=1")
            row = c.fetchone()
            season_id = row["id"] if row else 1
        c.execute(
            "SELECT id FROM matches WHERE gf IS NOT NULL AND season_id=?", (season_id,)
        )
        ids = [r["id"] for r in c.fetchall()]
        fin = [calc_finance(c, i) for i in ids]
        c.execute("SELECT total_games FROM seasons WHERE id=?", (season_id,))
        srow = c.fetchone()
        total = (srow or {}).get("total_games") or 5
        total = max(total, len(ids))
        c.execute("SELECT * FROM seasons WHERE id=?", (season_id,))
        season = c.fetchone() or {}
        rules = season.get("rules") or 1
        extra = {"rules": rules}
        if rules >= 2:
            c.execute("SELECT id, round FROM matches WHERE gf IS NOT NULL AND season_id=?", (season_id,))
            rmap = {r["id"]: r["round"] or 1 for r in c.fetchall()}
            fund_r1 = sum(f["personal_fund"] for i, f in zip(ids, fin) if rmap.get(i, 1) == 1)
            extra.update(r1_done=bool(season.get("r1_done")), r1_top3=season.get("r1_top3"),
                         fund_r1=fund_r1, payout=[], paid_out=0)
            if season.get("r1_done") and season.get("r1_top3"):
                # Paid by round-1 points, among players who played at least half of round-1 games
                lb = leaderboard_v2(c, season_id, 1)
                n1 = sum(1 for v in rmap.values() if v == 1)
                elig = [r for r in lb if r["points"] > 0 and r["games"] * 2 >= n1]
                tot = sum(r["points"] for r in elig)
                if tot > 0 and fund_r1 > 0:
                    pay = [{"id": r["id"], "name": r["name"], "points": r["points"],
                            "amount": int(fund_r1 * r["points"] / tot)} for r in elig]
                    # give rounding remainder to the leader so the sum matches the fund
                    pay[0]["amount"] += fund_r1 - sum(x["amount"] for x in pay)
                    extra["payout"] = pay
                    extra["paid_out"] = fund_r1
        return {
            **extra,
            "season_id": season_id,
            "played": len(ids),
            "total_games": total,
            "remaining": total - len(ids),
            "team_bank": sum(f["team_bank"] for f in fin),
            "personal_fund": sum(f["personal_fund"] for f in fin),
        }
