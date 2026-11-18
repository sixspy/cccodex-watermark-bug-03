from app.db import get_db
from app.services.errors import ServiceError


def trace_downstream(watermark_value: str, db_path: str | None = None) -> dict:
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")
    with get_db(db_path) as conn:
        sources = conn.execute(
            "SELECT * FROM run_batches WHERE watermark_value=? ORDER BY id",
            (value,),
        ).fetchall()
        if not sources:
            raise ServiceError("未找到该水印值的盖印批次", 404)
        codes = [s["run_code"] for s in sources]
        placeholders = ",".join("?" for _ in codes)
        edges = conn.execute(
            f"SELECT * FROM lineage_edges WHERE upstream_run_code IN ({placeholders})",
            codes,
        ).fetchall()

        # 每条边的最近一次修复结局，保证追索回看与血缘队列芯片一致。
        edge_ids = [e["id"] for e in edges]
        outcome_map: dict[int, dict] = {}
        if edge_ids:
            ph = ",".join("?" for _ in edge_ids)
            journals = conn.execute(
                f"SELECT * FROM repair_journals WHERE edge_id IN ({ph}) ORDER BY id",
                edge_ids,
            ).fetchall()
            for j in journals:  # 按 id 升序，后者覆盖前者，留下每条边最近一条
                outcome_map[j["edge_id"]] = dict(j)

        edge_dicts = []
        for e in edges:
            ed = dict(e)
            latest = outcome_map.get(e["id"])
            ed["repair_action"] = latest["action"] if latest else None
            ed["repair_outcome"] = latest["outcome"] if latest else None
            edge_dicts.append(ed)

        down_codes = sorted({e["downstream_run_code"] for e in edges})
        downstream = []
        if down_codes:
            ph2 = ",".join("?" for _ in down_codes)
            downstream = [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM run_batches WHERE run_code IN ({ph2}) ORDER BY id",
                    down_codes,
                ).fetchall()
            ]
        from app.services.lineage_gate import upstream_pending
        broken = []
        for s in sources:
            broken.extend(upstream_pending(s["run_code"], db_path))
        return {
            "watermark_value": value,
            "sources": [dict(s) for s in sources],
            "edges": edge_dicts,
            "downstream": downstream,
            "upstream_pending": sorted(set(broken)),
        }
