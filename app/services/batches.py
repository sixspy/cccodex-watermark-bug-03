from app.db import EDGE_ISOLATED, EDGE_STAMPED, get_db
from app.services.errors import ServiceError


def list_batches(db_path: str | None = None, status: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM run_batches WHERE status=? ORDER BY id",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM run_batches ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]


def get_batch(batch_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        return dict(row) if row else None


def stamp_batch(
    batch_id: int,
    watermark_value: str,
    db_path: str | None = None,
) -> dict:
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")
    with get_db(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        if batch["status"] == "已盖印":
            raise ServiceError("批次已盖印", 409)
        if batch["status"] != "待盖印":
            raise ServiceError("仅待盖印批次可盖印", 409)
        tpl = conn.execute(
            "SELECT * FROM watermark_templates WHERE table_name=? AND status='现行'",
            (batch["table_name"],),
        ).fetchone()
        if not tpl:
            raise ServiceError("无现行水印模板", 409)

        # 同源拦截：待盖印批次不应挂着已盖印边；若存在与本次水印拧着的已盖印边，直接拦。
        twisted = conn.execute(
            "SELECT id FROM lineage_edges WHERE downstream_run_code=? "
            "AND edge_status=? AND edge_watermark IS NOT NULL AND edge_watermark!=?",
            (batch["run_code"], EDGE_STAMPED, value),
        ).fetchall()
        if twisted:
            raise ServiceError("边印与本次落印不同源，已拦截盖印", 409)

        # 下游落印
        conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value=?, "
            "stamped_at=datetime('now') WHERE id=?",
            (value, batch_id),
        )
        # 边印与下游落印同源：同事务、同水印值回写所有待盖印入边。
        # 已隔离边是挂凭证放行的另一种命运，保留凭证不被覆盖。
        conn.execute(
            "UPDATE lineage_edges SET edge_status=?, edge_watermark=?, "
            "updated_at=datetime('now') WHERE downstream_run_code=? AND edge_status!=?",
            (EDGE_STAMPED, value, batch["run_code"], EDGE_ISOLATED),
        )
        row = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        return dict(row)
