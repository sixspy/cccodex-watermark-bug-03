import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config

# 血缘边状态：待盖印（边未落印）/ 已盖印（边与下游同源落印）/ 已隔离（上游缺口挂隔离凭证后放行）
EDGE_PENDING = "待盖印"
EDGE_STAMPED = "已盖印"
EDGE_ISOLATED = "已隔离"
EDGE_STATUSES = (EDGE_PENDING, EDGE_STAMPED, EDGE_ISOLATED)

# 修复结局
REPAIR_ROLLBACK = "回滚"
REPAIR_ISOLATE = "隔离"
REPAIR_ACTIONS = (REPAIR_ROLLBACK, REPAIR_ISOLATE)


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def locked_db(db_path: str | None = None):
    """修复专用：立即取写锁（BEGIN IMMEDIATE），两名血缘员并发时后者阻塞/失败，杜绝双写。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS watermark_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  column_name TEXT NOT NULL,
  version_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('现行','停用')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS run_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  run_code TEXT NOT NULL UNIQUE,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待盖印','已盖印','已作废')),
  watermark_value TEXT,
  stamped_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_run_code TEXT NOT NULL,
  downstream_run_code TEXT NOT NULL,
  edge_status TEXT NOT NULL DEFAULT '待盖印'
    CHECK (edge_status IN ('待盖印','已盖印','已隔离')),
  edge_watermark TEXT,
  isolated_certificate TEXT,
  updated_at TEXT,
  UNIQUE(upstream_run_code, downstream_run_code)
);

CREATE TABLE IF NOT EXISTS repair_journals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  edge_id INTEGER NOT NULL,
  upstream_run_code TEXT NOT NULL,
  downstream_run_code TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('回滚','隔离')),
  outcome TEXT NOT NULL,
  operator TEXT NOT NULL DEFAULT '血缘员',
  isolated_certificate TEXT,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS gap_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  expected_count INTEGER NOT NULL,
  stamped_count INTEGER NOT NULL,
  gap_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SEED_TEMPLATES = [
    ("dw_orders_d", "wm_batch", 1, "现行"),
    ("dw_payments_d", "wm_batch", 1, "现行"),
    ("dm_gmv_d", "wm_trace", 1, "现行"),
]

SEED_BATCHES = [
    ("dw_orders_d", "ord-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_payments_d", "pay-20261114-01", "2026-11-14", "待盖印", None),
    # 下游已落印但两条同日上游仍待盖：预置的断链场景（历史遗留 dangling）
    ("dm_gmv_d", "gmv-20261114-01", "2026-11-14", "已盖印", "WM-GMV-141"),
    ("dw_orders_d", "ord-20261113-01", "2026-11-13", "已盖印", "WM-ORD-131"),
]

# (上游, 下游)；同源边在播种时即与下游同水印落印，其余为待盖印的断链边
SEED_EDGES = [
    ("ord-20261114-01", "gmv-20261114-01"),
    ("pay-20261114-01", "gmv-20261114-01"),
    ("ord-20261113-01", "gmv-20261114-01"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """对存量库补齐 lineage_edges 新列，老库无 edge_status 时平滑升级。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(lineage_edges)").fetchall()}
    if "edge_status" not in cols:
        conn.execute(
            "ALTER TABLE lineage_edges ADD COLUMN edge_status TEXT NOT NULL "
            "DEFAULT '待盖印' CHECK (edge_status IN ('待盖印','已盖印','已隔离'))"
        )
    if "edge_watermark" not in cols:
        conn.execute("ALTER TABLE lineage_edges ADD COLUMN edge_watermark TEXT")
    if "isolated_certificate" not in cols:
        conn.execute("ALTER TABLE lineage_edges ADD COLUMN isolated_certificate TEXT")
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE lineage_edges ADD COLUMN updated_at TEXT")


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if row:
            return
        for table, col, ver, status in SEED_TEMPLATES:
            conn.execute(
                "INSERT INTO watermark_templates(table_name, column_name, version_no, status) "
                "VALUES (?,?,?,?)",
                (table, col, ver, status),
            )
        for table, code, biz, status, wm in SEED_BATCHES:
            conn.execute(
                "INSERT INTO run_batches(table_name, run_code, biz_date, status, watermark_value, stamped_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    table,
                    code,
                    biz,
                    status,
                    wm,
                    "2026-11-13 18:00:00" if status == "已盖印" else None,
                ),
            )
        for up, down in SEED_EDGES:
            # 已盖印上游到已盖印下游的边同源落印；其余保持待盖印，构成断链队列
            stamped = up == "ord-20261113-01"
            conn.execute(
                "INSERT INTO lineage_edges(upstream_run_code, downstream_run_code, "
                "edge_status, edge_watermark, isolated_certificate, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    up,
                    down,
                    EDGE_STAMPED if stamped else EDGE_PENDING,
                    "WM-GMV-141" if stamped else None,
                    None,
                    "2026-11-14 18:00:00" if stamped else None,
                ),
            )
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
