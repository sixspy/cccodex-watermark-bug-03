"""血缘队列与断链修复。

两种工程命运：
- 回滚：把下游已盖印整笔回滚为待盖印（清水印/盖印时间），同批入边一律退回待盖印；
  上游缺口仍保持待盖印，未放行。
- 隔离：给上游缺口落隔离凭证后放行，边置“已隔离”，保留下游已盖印不动。

修复在单个排他事务（BEGIN IMMEDIATE）内一次改库，失败整体回滚并回白话；
边印与下游落印同源（同水印值、同事务写入），拧了直接拦。
"""

from app.db import (
    EDGE_ISOLATED,
    EDGE_PENDING,
    EDGE_STAMPED,
    get_db,
    locked_db,
)
from app.services.errors import ServiceError

# 隔离凭证前缀
CERT_PREFIX = "ISO-CERT"


def _is_broken(edge: dict, downstream: dict | None) -> bool:
    """断链：边还停在待盖印，下游却已经落印。已隔离边视为已放行，不再算断。"""
    if edge["edge_status"] != EDGE_PENDING:
        return False
    return bool(downstream and downstream["status"] == "已盖印")


def list_edges(db_path: str | None = None) -> list[dict]:
    """血缘队列：每条边带上上下游状态、是否断链、同源是否一致。"""
    with get_db(db_path) as conn:
        edges = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM lineage_edges ORDER BY id"
            ).fetchall()
        ]
        result = []
        for e in edges:
            up = conn.execute(
                "SELECT status, watermark_value FROM run_batches WHERE run_code=?",
                (e["upstream_run_code"],),
            ).fetchone()
            down = conn.execute(
                "SELECT status, watermark_value FROM run_batches WHERE run_code=?",
                (e["downstream_run_code"],),
            ).fetchone()
            down_d = dict(down) if down else None
            same_source = None
            if e["edge_status"] == EDGE_STAMPED:
                # 边印与下游落印必须同源：边水印等于下游水印
                same_source = (
                    down_d is not None
                    and down_d["status"] == "已盖印"
                    and e["edge_watermark"] is not None
                    and e["edge_watermark"] == down_d["watermark_value"]
                )
            result.append(
                {
                    **e,
                    "upstream_status": up["status"] if up else None,
                    "upstream_watermark": up["watermark_value"] if up else None,
                    "downstream_status": down_d["status"] if down_d else None,
                    "downstream_watermark": down_d["watermark_value"] if down_d else None,
                    "is_broken": _is_broken(e, down_d),
                    "same_source": same_source,
                }
            )
        return result


def list_journals(db_path: str | None = None) -> list[dict]:
    """编排流水：修复结局逐条可核。"""
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM repair_journals ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def _certificate(conn, edge_id: int, upstream_code: str) -> str:
    seq = conn.execute("SELECT COUNT(*) AS c FROM repair_journals").fetchone()["c"] + 1
    return f"{CERT_PREFIX}-{edge_id:04d}-{seq:04d}"


def repair_edge(
    edge_id: int,
    action: str,
    operator: str = "血缘员",
    db_path: str | None = None,
) -> dict:
    """对一条断链边点修复。排他事务内一次改库，返回修复结局。"""
    from app.db import REPAIR_ACTIONS, REPAIR_ISOLATE, REPAIR_ROLLBACK

    act = (action or "").strip()
    if act not in REPAIR_ACTIONS:
        raise ServiceError("修复方式只支持：回滚 或 隔离", 400)

    # 排他锁：并发点修复时第二个事务在此排队，进入后重读状态，避免双写下游态。
    with locked_db(db_path) as conn:
        edge = conn.execute(
            "SELECT * FROM lineage_edges WHERE id=?", (edge_id,)
        ).fetchone()
        if not edge:
            raise ServiceError("血缘边不存在", 404)
        edge_d = dict(edge)

        downstream = conn.execute(
            "SELECT * FROM run_batches WHERE run_code=?",
            (edge_d["downstream_run_code"],),
        ).fetchone()
        upstream = conn.execute(
            "SELECT * FROM run_batches WHERE run_code=?",
            (edge_d["upstream_run_code"],),
        ).fetchone()
        if not downstream or not upstream:
            raise ServiceError("血缘边关联的批次缺失，无法修复", 409)
        down_d = dict(downstream)
        up_d = dict(upstream)

        # 前置校验：只有“边待盖 + 下游已盖”的断链可修。
        if edge_d["edge_status"] == EDGE_ISOLATED:
            raise ServiceError("该缺口已落隔离凭证，无需重复修复", 409)
        if edge_d["edge_status"] == EDGE_STAMPED:
            raise ServiceError("该血缘边已盖印，不在断链队列", 409)
        if down_d["status"] != "已盖印":
            raise ServiceError("下游尚未落印，不构成断链，无需修复", 409)

        if act == REPAIR_ROLLBACK:
            return _do_rollback(conn, edge_d, down_d, operator)
        return _do_isolate(conn, edge_d, up_d, down_d, operator)


def _do_rollback(conn, edge_d: dict, down_d: dict, operator: str) -> dict:
    """命运一：下游整笔回滚为待盖印；同批入边全退回待盖印；上游仍待盖印。"""
    down_code = down_d["run_code"]
    # 同源校验：回滚只针对真实断链，已盖印边必须与下游同水印，拧了就拦。
    stamped_same = conn.execute(
        "SELECT id FROM lineage_edges WHERE downstream_run_code=? "
        "AND edge_status='已盖印' AND edge_watermark IS NOT NULL "
        "AND edge_watermark!=?",
        (down_code, down_d["watermark_value"]),
    ).fetchall()
    if stamped_same:
        raise ServiceError("边印与下游落印不同源，已拦截回滚", 409)

    # 下游整笔回滚为待盖印
    conn.execute(
        "UPDATE run_batches SET status='待盖印', watermark_value=NULL, "
        "stamped_at=NULL WHERE run_code=?",
        (down_code,),
    )
    # 同批所有入边退回待盖印（已隔离边保留凭证与历史，不回退）
    conn.execute(
        "UPDATE lineage_edges SET edge_status='待盖印', edge_watermark=NULL, "
        "updated_at=datetime('now') WHERE downstream_run_code=? AND edge_status!='已隔离'",
        (down_code,),
    )
    outcome = (
        f"回滚：下游 {down_code} 已整笔恢复为待盖印，同批入边退回待盖印；"
        f"上游缺口 {edge_d['upstream_run_code']} 仍待盖印，未放行"
    )
    cur = conn.execute(
        "INSERT INTO repair_journals(edge_id, upstream_run_code, downstream_run_code, "
        "action, outcome, operator, isolated_certificate, detail) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            edge_d["id"],
            edge_d["upstream_run_code"],
            down_code,
            "回滚",
            outcome,
            operator,
            None,
            "下游已盖印整笔回滚，边与下游同源清空",
        ),
    )
    journal = dict(
        conn.execute("SELECT * FROM repair_journals WHERE id=?", (cur.lastrowid,)).fetchone()
    )
    edge_after = dict(
        conn.execute("SELECT * FROM lineage_edges WHERE id=?", (edge_d["id"],)).fetchone()
    )
    return {"edge": edge_after, "journal": journal}


def _do_isolate(conn, edge_d: dict, up_d: dict, down_d: dict, operator: str) -> dict:
    """命运二：上游缺口落隔离凭证后放行，边置已隔离；保留下游已盖印。"""
    # 同源校验：下游必须确实已盖且带水印，否则不给放行。
    if not down_d.get("watermark_value"):
        raise ServiceError("下游落印缺同源水印，已拦截隔离", 409)

    cert = _certificate(conn, edge_d["id"], up_d["run_code"])
    conn.execute(
        "UPDATE lineage_edges SET edge_status='已隔离', isolated_certificate=?, "
        "updated_at=datetime('now') WHERE id=?",
        (cert, edge_d["id"]),
    )
    # 上游仍保持待盖印（缺口未补盖），仅以隔离凭证放行该边；下游已盖印原样保留。
    outcome = (
        f"隔离：上游缺口 {up_d['run_code']} 落凭证 {cert} 后放行，"
        f"下游 {down_d['run_code']} 保持已盖印"
    )
    cur = conn.execute(
        "INSERT INTO repair_journals(edge_id, upstream_run_code, downstream_run_code, "
        "action, outcome, operator, isolated_certificate, detail) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            edge_d["id"],
            edge_d["upstream_run_code"],
            down_d["run_code"],
            "隔离",
            outcome,
            operator,
            cert,
            "上游缺口挂隔离凭证放行，保留下游已盖印",
        ),
    )
    journal = dict(
        conn.execute("SELECT * FROM repair_journals WHERE id=?", (cur.lastrowid,)).fetchone()
    )
    edge_after = dict(
        conn.execute("SELECT * FROM lineage_edges WHERE id=?", (edge_d["id"],)).fetchone()
    )
    return {"edge": edge_after, "journal": journal, "certificate": cert}
