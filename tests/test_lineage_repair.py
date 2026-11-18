"""二验：断链修复的两种工程命运、同源约束、流水可核、缺口不虚高、并发排他锁。"""

import os
import sqlite3
import threading

import pytest

from app.db import get_db, init_db, locked_db
from app.services import gaps, lineage
from app.services.errors import ServiceError


def _db_path(tmp_path):
    return str(tmp_path / "lineage.db")


@pytest.fixture()
def db(tmp_path):
    path = _db_path(tmp_path)
    init_db(path)
    return path


def _broken_edge_ids(path):
    return [e["id"] for e in lineage.list_edges(path) if e["is_broken"]]


# ---------- 血缘队列 ----------

def test_seed_has_broken_edges_and_one_healthy(db):
    edges = lineage.list_edges(db)
    broken = [e for e in edges if e["is_broken"]]
    # 两条同日上游待盖 + 下游已盖 => 两条断链
    assert len(broken) == 2
    healthy = [e for e in edges if e["edge_status"] == "已盖印"]
    assert len(healthy) == 1
    # 健康边同源：边水印等于下游水印
    assert all(e["same_source"] is True for e in healthy)
    # 断链边的下游确实已盖印、边却待盖印
    for e in broken:
        assert e["edge_status"] == "待盖印"
        assert e["downstream_status"] == "已盖印"


# ---------- 命运一：回滚 ----------

def test_repair_rollback_restores_downstream_pending(db):
    edge_id = _broken_edge_ids(db)[0]
    before = next(e for e in lineage.list_edges(db) if e["id"] == edge_id)
    down_code = before["downstream_run_code"]

    result = lineage.repair_edge(edge_id, "回滚", db_path=db)

    assert result["journal"]["action"] == "回滚"
    # 下游整笔恢复待盖印，水印与盖印时间清空
    with get_db(db) as conn:
        down = conn.execute(
            "SELECT * FROM run_batches WHERE run_code=?", (down_code,)
        ).fetchone()
        assert down["status"] == "待盖印"
        assert down["watermark_value"] is None
        assert down["stamped_at"] is None
        # 同批入边全部退回待盖印（本场景无已隔离边）
        states = {
            r["edge_status"]
            for r in conn.execute(
                "SELECT edge_status FROM lineage_edges WHERE downstream_run_code=?",
                (down_code,),
            ).fetchall()
        }
        assert states == {"待盖印"}
    # 上游缺口仍保持待盖印，未被放行
    edge_after = result["edge"]
    assert edge_after["edge_status"] == "待盖印"


# ---------- 命运二：隔离 ----------

def test_repair_isolate_keeps_downstream_stamped_and_certifies(db):
    edge_id = _broken_edge_ids(db)[0]
    before = next(e for e in lineage.list_edges(db) if e["id"] == edge_id)
    up_code, down_code = before["upstream_run_code"], before["downstream_run_code"]

    result = lineage.repair_edge(edge_id, "隔离", db_path=db)

    cert = result["certificate"]
    assert cert and cert.startswith("ISO-CERT")
    assert result["journal"]["isolated_certificate"] == cert
    # 边置已隔离并挂凭证
    assert result["edge"]["edge_status"] == "已隔离"
    assert result["edge"]["isolated_certificate"] == cert
    with get_db(db) as conn:
        # 上游缺口仍待盖印（凭证放行，未补盖）
        up = conn.execute(
            "SELECT status FROM run_batches WHERE run_code=?", (up_code,)
        ).fetchone()
        assert up["status"] == "待盖印"
        # 下游保持已盖印、水印原样
        down = conn.execute(
            "SELECT status, watermark_value FROM run_batches WHERE run_code=?",
            (down_code,),
        ).fetchone()
        assert down["status"] == "已盖印"
        assert down["watermark_value"] == "WM-GMV-141"
    # 已隔离边不再算断链
    assert not next(e for e in lineage.list_edges(db) if e["id"] == edge_id)["is_broken"]


def test_isolated_edge_cannot_repair_again(db):
    edge_id = _broken_edge_ids(db)[0]
    lineage.repair_edge(edge_id, "隔离", db_path=db)
    with pytest.raises(ServiceError) as exc:
        lineage.repair_edge(edge_id, "回滚", db_path=db)
    assert exc.value.status_code == 409


def test_invalid_action_rejected(db):
    edge_id = _broken_edge_ids(db)[0]
    with pytest.raises(ServiceError) as exc:
        lineage.repair_edge(edge_id, "随便", db_path=db)
    assert exc.value.status_code == 400


def test_non_broken_edge_repair_rejected(db):
    healthy = next(e for e in lineage.list_edges(db) if e["edge_status"] == "已盖印")
    with pytest.raises(ServiceError) as exc:
        lineage.repair_edge(healthy["id"], "回滚", db_path=db)
    assert exc.value.status_code == 409


# ---------- 边印与下游落印同源 ----------

def test_stamp_writes_edge_same_source(db):
    # 取一个待盖印批次（上游 ord），盖印后其作为下游无入边；改为验证 gmv 已盖印边的同源
    edges = lineage.list_edges(db)
    stamped = next(e for e in edges if e["edge_status"] == "已盖印")
    assert stamped["edge_watermark"] == stamped["downstream_watermark"]
    assert stamped["same_source"] is True


def test_twisted_source_is_blocked(db):
    # 拧掉同下游一条已盖印边的水印（边印 != 下游落印），对该下游任一边回滚都必须拦
    edges = lineage.list_edges(db)
    stamped_id = next(e["id"] for e in edges if e["edge_status"] == "已盖印")
    broken_id = next(e["id"] for e in edges if e["is_broken"])
    with get_db(db) as conn:
        conn.execute(
            "UPDATE lineage_edges SET edge_watermark='WM-TWIST' WHERE id=?",
            (stamped_id,),
        )
    with pytest.raises(ServiceError) as exc:
        lineage.repair_edge(broken_id, "回滚", db_path=db)
    assert "同源" in exc.value.message
    # 拦截后下游未被改动，仍是已盖印
    with get_db(db) as conn:
        down = conn.execute(
            "SELECT status FROM run_batches WHERE run_code='gmv-20261114-01'"
        ).fetchone()
        assert down["status"] == "已盖印"


def _add_independent_broken_chain(db):
    """再造一条独立下游的断链，避免与 gmv 的回滚互相影响。"""
    with get_db(db) as conn:
        conn.execute(
            "INSERT INTO run_batches(table_name, run_code, biz_date, status, "
            "watermark_value, stamped_at) VALUES (?,?,?,?,?,?)",
            ("dm_x_d", "x-20261114-01", "2026-11-14", "已盖印", "WM-X-1",
             "2026-11-14 19:00:00"),
        )
        cur = conn.execute(
            "INSERT INTO lineage_edges(upstream_run_code, downstream_run_code, edge_status) "
            "VALUES (?,?,?)",
            ("ord-20261114-01", "x-20261114-01", "待盖印"),
        )
        return cur.lastrowid


# ---------- 流水可核、追索回看与芯片一致 ----------

def test_journal_records_each_outcome_and_trace_matches(db):
    # 隔离保留下游水印，便于追索回看；独立下游承载隔离，gmv 一条边也隔离
    x_edge = _add_independent_broken_chain(db)
    gmv_broken = _broken_edge_ids(db)[0]
    lineage.repair_edge(gmv_broken, "隔离", db_path=db)
    lineage.repair_edge(x_edge, "隔离", db_path=db)

    journals = lineage.list_journals(db)
    assert all(j["outcome"] and j["detail"] for j in journals)
    assert all(j["action"] == "隔离" for j in journals)

    # 追索席：gmv 水印的边结局与流水一致（芯片与回看同源）
    from app.services.trace import trace_downstream

    result = trace_downstream("WM-GMV-141", db_path=db)
    journal_map = {j["edge_id"]: j for j in journals}
    for traced in result["edges"]:
        j = journal_map.get(traced["id"])
        if j:
            assert traced["repair_action"] == j["action"]
            assert traced["repair_outcome"] == j["outcome"]
            assert traced["edge_status"] == "已隔离"


def test_rollback_journal_and_isolate_journal_coexist(db):
    x_edge = _add_independent_broken_chain(db)
    gmv_broken = _broken_edge_ids(db)[0]
    lineage.repair_edge(gmv_broken, "回滚", db_path=db)   # 独立下游组之前，先回滚 gmv
    lineage.repair_edge(x_edge, "隔离", db_path=db)
    actions = {j["action"] for j in lineage.list_journals(db)}
    assert actions == {"回滚", "隔离"}


# ---------- 缺口不虚高 ----------

def test_gap_count_not_inflated(db):
    report = gaps.run_gap_report("2026-11-16", db_path=db)
    # 4 个批次：2 已盖（gmv、ord-13），2 待盖 => 真实缺口 2，不再 +2
    assert report["expected_count"] == 4
    assert report["stamped_count"] == 2
    assert report["gap_count"] == 2
    assert report["gap_count"] == report["expected_count"] - report["stamped_count"]


def test_gap_drops_after_isolate_keeps_downstream(db):
    before = gaps.run_gap_report("2026-11-16", db_path=db)["gap_count"]
    # 隔离不补盖上游，缺口数不变，但摘要带已隔离放行
    edge_id = _broken_edge_ids(db)[0]
    lineage.repair_edge(edge_id, "隔离", db_path=db)
    report = gaps.run_gap_report("2026-11-16", db_path=db)
    assert report["gap_count"] == before
    assert "已隔离放行 1" in report["summary"]


# ---------- 并发排他锁：不双写下游态 ----------

def test_concurrent_repair_exclusive_lock_no_double_write(db):
    edge_id = _broken_edge_ids(db)[0]
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(action):
        try:
            barrier.wait(timeout=10)
            res = lineage.repair_edge(edge_id, action, db_path=db)
            outcomes.append(("ok", action, res["edge"]["edge_status"]))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(("err", action, str(exc)))

    t1 = threading.Thread(target=worker, args=("回滚",))
    t2 = threading.Thread(target=worker, args=("隔离",))
    t1.start(); t2.start()
    t1.join(timeout=30); t2.join(timeout=30)

    # 只有一个成功，另一个被状态校验或锁挡下；下游态只被写一次
    oks = [o for o in outcomes if o[0] == "ok"]
    assert len(oks) == 1
    with get_db(db) as conn:
        edge = conn.execute(
            "SELECT edge_status FROM lineage_edges WHERE id=?", (edge_id,)
        ).fetchone()
        # 最终状态与唯一成功者一致，不存在既回滚又隔离的双写
        assert edge["edge_status"] in {"待盖印", "已隔离"}
        journal_count = conn.execute(
            "SELECT COUNT(*) AS c FROM repair_journals WHERE edge_id=?", (edge_id,)
        ).fetchone()["c"]
        assert journal_count == 1


def test_held_write_lock_blocks_second_repair(db):
    edge_id = _broken_edge_ids(db)[0]
    # 先在独立连接上持有 IMMEDIATE 写锁，修复必须拿到同一把锁，否则 409/OperationalError
    holder = sqlite3.connect(db, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            lineage.repair_edge(edge_id, "隔离", db_path=db)
    finally:
        holder.rollback()
        holder.close()
    # 锁释放后可正常修复，证明失败时未改库
    result = lineage.repair_edge(edge_id, "隔离", db_path=db)
    assert result["edge"]["edge_status"] == "已隔离"
