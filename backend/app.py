
import hashlib, secrets, sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

PRODUCT = "源到达SLA"
SKELETON = "多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账"

# 滞后切断口径：本仓只走甲路径。
# 甲：策略改口与未结到达重评分在同一锁事务内完成，遇错整体回退。
# 乙路径不实现，请求乙一律拦下并记「滞后偏了」。
CUTOVER_PATH = "甲"
SEATS = {"配置席", "补采席"}


class Contention(Exception):
    pass


class UnknownSource(Exception):
    pass


class CutoverFailed(Exception):
    pass


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def tx():
    """短事务 + 滞后锁（BEGIN IMMEDIATE 串行化同一库的写入）。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))


class LoginIn(BaseModel):
    username: str
    password: str


def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u


app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


def _columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_col(conn, table: str, name: str, ddl: str):
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE NOT NULL,
          sla_minutes INTEGER NOT NULL,
          output_mode TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS arrivals(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          source_clock TEXT NOT NULL,
          ingest_clock TEXT NOT NULL,
          lag_minutes INTEGER NOT NULL,
          level TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS catchups(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          status TEXT NOT NULL,
          restored_mode TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        # 旧库平滑加列；新库同样走这里补齐。
        _add_col(conn, "sources", "policy_version", "INTEGER NOT NULL DEFAULT 1")
        _add_col(conn, "sources", "config_version", "INTEGER NOT NULL DEFAULT 1")
        _add_col(conn, "sources", "collect_window_minutes", "INTEGER NOT NULL DEFAULT 60")
        _add_col(conn, "arrivals", "settled", "INTEGER NOT NULL DEFAULT 0")
        _add_col(conn, "arrivals", "policy_version", "INTEGER NOT NULL DEFAULT 1")
        _add_col(conn, "arrivals", "scored_sla", "INTEGER")
        _add_col(conn, "arrivals", "seat", "TEXT NOT NULL DEFAULT '补采席'")
        _add_col(conn, "arrivals", "created_at", "TEXT")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS policies(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          version INTEGER NOT NULL,
          sla_minutes INTEGER NOT NULL,
          path TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cutover_cards(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          path TEXT NOT NULL,
          from_version INTEGER NOT NULL,
          to_version INTEGER NOT NULL,
          from_sla INTEGER NOT NULL,
          to_sla INTEGER NOT NULL,
          resecored INTEGER NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          sealed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lag_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT NOT NULL,
          source TEXT NOT NULL,
          seat TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        if conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0:
            conn.executemany("INSERT INTO sources(name,sla_minutes,output_mode) VALUES (?,?,?)", [
                ("订单源", 15, "可写"), ("支付源", 20, "降级只读"), ("物流源", 30, "冻结"),
            ])
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("支付源", "2026-11-16T08:00:00", "2026-11-16T08:35:00", 35, "告警"))
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("物流源", "2026-11-16T07:00:00", "2026-11-16T09:10:00", 130, "违约"))
        conn.execute("""UPDATE arrivals SET scored_sla=(
            SELECT sla_minutes FROM sources WHERE sources.name=arrivals.source)
            WHERE scored_sla IS NULL""")
        conn.execute("UPDATE arrivals SET created_at=? WHERE created_at IS NULL", (now_iso(),))
        if conn.execute("SELECT COUNT(*) c FROM policies").fetchone()["c"] == 0:
            for s in conn.execute("SELECT * FROM sources ORDER BY id").fetchall():
                conn.execute(
                    "INSERT INTO policies(source,version,sla_minutes,path,status,created_at) VALUES (?,?,?,?,?,?)",
                    (s["name"], s["policy_version"], s["sla_minutes"], CUTOVER_PATH, "sealed", now_iso()),
                )


def level_of(lag, sla):
    if lag <= sla:
        return "正常"
    if lag <= sla * 2:
        return "告警"
    return "违约"


def mode_of(level):
    return {"正常": "可写", "告警": "降级只读", "违约": "冻结"}[level]


def log_event(source: str, kind: str, seat: str, detail: str):
    """留痕独立提交，不随翻档事务回退。"""
    with tx() as conn:
        conn.execute(
            "INSERT INTO lag_events(kind,source,seat,detail,created_at) VALUES (?,?,?,?,?)",
            (kind, source, seat, detail, now_iso()),
        )


def _rescore_pending(conn, source: str, new_sla: int, new_version: int):
    """重评未结到达；已结到达保持封档时的旧档位。"""
    rows = conn.execute(
        "SELECT id,lag_minutes FROM arrivals WHERE source=? AND settled=0 ORDER BY id", (source,)
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE arrivals SET level=?,policy_version=?,scored_sla=? WHERE id=?",
            (level_of(row["lag_minutes"], new_sla), new_version, new_sla, row["id"]),
        )
    latest = conn.execute(
        "SELECT level FROM arrivals WHERE source=? AND settled=0 ORDER BY id DESC LIMIT 1", (source,)
    ).fetchone()
    return len(rows), (mode_of(latest["level"]) if latest else None)


def _source_or_404(conn, name: str):
    row = conn.execute("SELECT * FROM sources WHERE name=?", (name,)).fetchone()
    if not row:
        raise UnknownSource(name)
    return row


def _source_dict(row) -> dict:
    return dict(row)


@app.on_event("startup")
def _s():
    init_db()


@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "sources": n,
            "db": str(DB_PATH), "cutover_path": CUTOVER_PATH}


@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?",
                        (body.username, sha(body.password))).fetchone()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16)
    SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}


@app.get("/api/sources")
def sources(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [_source_dict(r) for r in
                          c.execute("SELECT * FROM sources ORDER BY id").fetchall()]}


class SourcePatch(BaseModel):
    expected_config_version: int | None = None
    seat: str = "配置席"
    sla_minutes: int | None = None
    collect_window_minutes: int | None = None
    path: str | None = None
    display_only: bool | None = None
    rescore: bool | None = None


@app.patch("/api/sources/{source}")
def patch_source(source: str, body: SourcePatch,
                 authorization: str | None = Header(default=None)):
    require_user(authorization)
    seat = body.seat if body.seat in SEATS else "配置席"

    # 偏离滞后切断口径：只改显示 / 明确不改计分 / 走未封档的乙路径，全部拦下。
    deviation = None
    if body.path is not None and body.path != CUTOVER_PATH:
        deviation = f"请求路径 {body.path} 与已封档路径 {CUTOVER_PATH} 不同"
    elif body.display_only:
        deviation = "只改显示不改计分"
    elif body.rescore is False:
        deviation = "显式关闭未结到达重评分"
    if deviation:
        log_event(source, "滞后偏了", seat, deviation)
        raise HTTPException(409, "偏离滞后切断口径：" + deviation)

    with connect() as c:
        current = c.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
    if not current:
        raise HTTPException(404, "未知源")
    if (body.sla_minutes is None or body.sla_minutes == current["sla_minutes"]) \
            and (body.collect_window_minutes is None
                 or body.collect_window_minutes == current["collect_window_minutes"]):
        raise HTTPException(400, "没有可落库的变更")

    policy_change = body.sla_minutes is not None and body.sla_minutes != current["sla_minutes"]
    try:
        with tx() as conn:
            row = _source_or_404(conn, source)
            if body.expected_config_version is None:
                raise Contention("缺配置版本令牌，无法占滞后锁")
            expected = body.expected_config_version
            if expected != row["config_version"]:
                raise Contention(
                    f"配置版本已由 {expected} 变为 {row['config_version']}")
            collect = (body.collect_window_minutes
                       if body.collect_window_minutes is not None
                       else row["collect_window_minutes"])
            if not policy_change:
                # 采集配置可验收：只抬配置版本，不动策略档位与计分。
                conn.execute(
                    "UPDATE sources SET collect_window_minutes=?,config_version=? WHERE name=?",
                    (collect, row["config_version"] + 1, source))
                updated = _source_or_404(conn, source)
                return {"source": source, "path": CUTOVER_PATH, "resecored": 0,
                        "policy_version": updated["policy_version"],
                        "config_version": updated["config_version"],
                        "sla_minutes": updated["sla_minutes"],
                        "collect_window_minutes": updated["collect_window_minutes"]}

            from_version = row["policy_version"]
            to_version = from_version + 1
            new_sla = int(body.sla_minutes)
            conn.execute(
                """UPDATE sources SET sla_minutes=?,collect_window_minutes=?,
                   policy_version=?,config_version=? WHERE name=?""",
                (new_sla, collect, to_version, row["config_version"] + 1, source))
            # 甲路径：改口、重评分、封卡同事务；任何一步异常整体回退。
            resecored, mode = _rescore_pending(conn, source, new_sla, to_version)
            if mode is not None:
                conn.execute("UPDATE sources SET output_mode=? WHERE name=?", (mode, source))
            conn.execute(
                """INSERT INTO policies(source,version,sla_minutes,path,status,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (source, to_version, new_sla, CUTOVER_PATH, "sealed", now_iso()))
            conn.execute(
                """INSERT INTO cutover_cards(source,path,from_version,to_version,from_sla,to_sla,
                   resecored,status,created_at,sealed_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (source, CUTOVER_PATH, from_version, to_version, row["sla_minutes"], new_sla,
                 resecored, "sealed", now_iso(), now_iso()))
            updated = _source_or_404(conn, source)
            return {"source": source, "path": CUTOVER_PATH, "resecored": resecored,
                    "policy_version": updated["policy_version"],
                    "config_version": updated["config_version"],
                    "sla_minutes": updated["sla_minutes"],
                    "collect_window_minutes": updated["collect_window_minutes"]}
    except Contention as e:
        log_event(source, "时限滞后争用", seat, str(e))
        raise HTTPException(409, "时限滞后争用：" + str(e))
    except UnknownSource:
        raise HTTPException(404, "未知源")
    except Exception as e:  # 翻档失败：正式档位与未结计分均已回退
        log_event(source, "翻档失败", seat, f"{CUTOVER_PATH}路径翻档回退: {e}")
        raise HTTPException(500, "翻档失败，正式档位保持旧值")


class ArrivalIn(BaseModel):
    source: str
    source_clock: str
    ingest_clock: str
    seat: str = "补采席"
    expected_config_version: int | None = None


@app.post("/api/arrivals")
def add_arrival(body: ArrivalIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    seat = body.seat if body.seat in SEATS else "补采席"
    try:
        lag = int((datetime.fromisoformat(body.ingest_clock)
                   - datetime.fromisoformat(body.source_clock)).total_seconds() // 60)
    except ValueError:
        raise HTTPException(400, "时钟格式不合法")
    if lag < 0:
        lag = 0
    try:
        with tx() as conn:
            src = _source_or_404(conn, body.source)
            if body.expected_config_version is None:
                raise Contention("缺配置版本令牌，无法占滞后锁")
            expected = body.expected_config_version
            if expected != src["config_version"]:
                raise Contention(
                    f"配置版本已由 {expected} 变为 {src['config_version']}")
            lv = level_of(lag, src["sla_minutes"])
            mode = mode_of(lv)
            cur = conn.execute(
                """INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level,
                   settled,policy_version,scored_sla,seat,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (body.source, body.source_clock, body.ingest_clock, lag, lv, 0,
                 src["policy_version"], src["sla_minutes"], seat, now_iso()))
            conn.execute("UPDATE sources SET output_mode=? WHERE name=?", (mode, body.source))
            return {"id": cur.lastrowid, "lag_minutes": lag, "level": lv,
                    "output_mode": mode, "policy_version": src["policy_version"],
                    "scored_sla": src["sla_minutes"], "settled": 0}
    except Contention as e:
        log_event(body.source, "时限滞后争用", seat, str(e))
        raise HTTPException(409, "时限滞后争用：" + str(e))
    except UnknownSource:
        raise HTTPException(404, "未知源")


@app.get("/api/arrivals")
def arrivals(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM arrivals ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/history")
def history(source: str | None = None, authorization: str | None = Header(default=None)):
    require_user(authorization)
    sql = "SELECT * FROM arrivals WHERE settled=1"
    args: tuple = ()
    if source:
        sql += " AND source=?"
        args = (source,)
    sql += " ORDER BY id"
    with connect() as c:
        rows = c.execute(sql, args).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/policies")
def policies(source: str | None = None, authorization: str | None = Header(default=None)):
    require_user(authorization)
    sql = "SELECT * FROM policies"
    args: tuple = ()
    if source:
        sql += " WHERE source=?"
        args = (source,)
    sql += " ORDER BY source,version"
    with connect() as c:
        rows = c.execute(sql, args).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/cutover-cards")
def cutover_cards(source: str | None = None, authorization: str | None = Header(default=None)):
    require_user(authorization)
    sql = "SELECT * FROM cutover_cards"
    args: tuple = ()
    if source:
        sql += " WHERE source=?"
        args = (source,)
    sql += " ORDER BY id"
    with connect() as c:
        rows = c.execute(sql, args).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.api_route("/api/cutover-cards/{card_id}", methods=["PUT", "PATCH", "DELETE"])
def cutover_card_immutable(card_id: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    raise HTTPException(405, "滞后切断卡封档后不得改口")


@app.get("/api/watermark")
def watermark(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        srcs = c.execute("SELECT * FROM sources ORDER BY id").fetchall()
        out = []
        for s in srcs:
            counts = {"正常": 0, "告警": 0, "违约": 0}
            for r in c.execute(
                    "SELECT level,COUNT(*) n FROM arrivals WHERE source=? AND settled=0 GROUP BY level",
                    (s["name"],)).fetchall():
                if r["level"] in counts:
                    counts[r["level"]] = r["n"]
            settled = c.execute(
                "SELECT COUNT(*) n FROM arrivals WHERE source=? AND settled=1", (s["name"],)
            ).fetchone()["n"]
            out.append({"source": s["name"], "mode": s["output_mode"],
                        "policy_version": s["policy_version"],
                        "normal": counts["正常"], "alert": counts["告警"],
                        "breach": counts["违约"], "settled": settled})
    return {"items": out}


@app.get("/api/lag/events")
def lag_events(kind: str | None = None, authorization: str | None = Header(default=None)):
    require_user(authorization)
    sql = "SELECT * FROM lag_events"
    args: tuple = ()
    if kind:
        sql += " WHERE kind=?"
        args = (kind,)
    sql += " ORDER BY id"
    with connect() as c:
        rows = c.execute(sql, args).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/catchup/{source}")
def catchup(source: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    now = now_iso()
    try:
        with tx() as c:
            _source_or_404(c, source)
            c.execute("UPDATE arrivals SET settled=1 WHERE source=? AND settled=0", (source,))
            c.execute("UPDATE sources SET output_mode='可写' WHERE name=?", (source,))
            c.execute("INSERT INTO catchups(source,status,restored_mode,note,created_at) VALUES (?,?,?,?,?)",
                      (source, "成功", "可写", "补齐后恢复可写", now))
            return {"ok": True, "output_mode": "可写"}
    except UnknownSource:
        raise HTTPException(404, "未知源")


@app.get("/api/catchups")
def catchups(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM catchups ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
