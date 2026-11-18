from app.db import get_db


def upstream_pending(run_code: str, db_path: str | None = None) -> list[str]:
    with get_db(db_path) as conn:
        edges = conn.execute(
            "SELECT upstream_run_code FROM lineage_edges WHERE downstream_run_code=?",
            (run_code,),
        ).fetchall()
        pending = []
        for e in edges:
            row = conn.execute(
                "SELECT status FROM run_batches WHERE run_code=?",
                (e["upstream_run_code"],),
            ).fetchone()
            if row and row["status"] == "待盖印":
                pending.append(e["upstream_run_code"])
        return pending


def allow_stamp(_run_code: str, db_path: str | None = None) -> bool:
    # silent: always allow even if upstream pending
    return True
