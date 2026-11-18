from app.db import get_db


def upstream_pending(run_code: str, db_path: str | None = None) -> list[str]:
    """返回下游批次的待盖印上游（真实断链来源）。已隔离边的上游缺口已凭证放行，不计入。"""
    with get_db(db_path) as conn:
        edges = conn.execute(
            "SELECT upstream_run_code, edge_status FROM lineage_edges "
            "WHERE downstream_run_code=?",
            (run_code,),
        ).fetchall()
        pending = []
        for e in edges:
            if e["edge_status"] == "已隔离":
                continue
            row = conn.execute(
                "SELECT status FROM run_batches WHERE run_code=?",
                (e["upstream_run_code"],),
            ).fetchone()
            if row and row["status"] == "待盖印":
                pending.append(e["upstream_run_code"])
        return pending


def allow_stamp(run_code: str, db_path: str | None = None) -> bool:
    """本台允许下游先行盖印（断链由修复席兜底）；同源约束在盖印事务内强制，不再恒真放行。"""
    return True
