"""API 层：修复端点、队列/流水端点、失败白话、未登录拦截。"""

import pytest


@pytest.mark.asyncio
async def test_lineage_endpoints_and_repair_flow(authed):
    # 血缘队列
    r = await authed.get("/api/lineage/edges")
    assert r.status == 200
    edges = (await r.json())["items"]
    broken = [e for e in edges if e["is_broken"]]
    assert len(broken) == 2

    # 隔离修复
    edge_id = broken[0]["id"]
    rep = await authed.post(
        f"/api/lineage/edges/{edge_id}/repair", json={"action": "隔离"}
    )
    assert rep.status == 200
    body = await rep.json()
    assert body["ok"] is True
    assert body["edge"]["edge_status"] == "已隔离"
    assert body["certificate"].startswith("ISO-CERT")
    assert "隔离" in body["journal"]["outcome"]

    # 流水入库可核
    j = await authed.get("/api/lineage/journals")
    journals = (await j.json())["items"]
    assert any(x["edge_id"] == edge_id and x["action"] == "隔离" for x in journals)

    # 重复修复 -> 409 白话
    again = await authed.post(
        f"/api/lineage/edges/{edge_id}/repair", json={"action": "回滚"}
    )
    assert again.status == 409
    assert "error" in await again.json()


@pytest.mark.asyncio
async def test_repair_invalid_action_plain_message(authed):
    edges = (await (await authed.get("/api/lineage/edges")).json())["items"]
    edge_id = next(e["id"] for e in edges if e["is_broken"])
    r = await authed.post(
        f"/api/lineage/edges/{edge_id}/repair", json={"action": "乱来"}
    )
    assert r.status == 400
    body = await r.json()
    assert body["ok"] is False and body["error"]


@pytest.mark.asyncio
async def test_repair_rollback_endpoint(authed):
    edges = (await (await authed.get("/api/lineage/edges")).json())["items"]
    edge_id = next(e for e in edges if e["is_broken"])["id"]
    r = await authed.post(
        f"/api/lineage/edges/{edge_id}/repair", json={"action": "回滚"}
    )
    assert r.status == 200
    body = await r.json()
    assert body["edge"]["edge_status"] == "待盖印"
    # 下游已回退为待盖印
    batches = (await (await authed.get("/api/batches")).json())["items"]
    gmv = next(b for b in batches if b["run_code"] == "gmv-20261114-01")
    assert gmv["status"] == "待盖印"
    assert gmv["watermark_value"] is None


@pytest.mark.asyncio
async def test_lineage_requires_login(client):
    r = await client.get("/api/lineage/edges")
    assert r.status == 401
    from blacksheep.contents import JSONContent

    r2 = await client.post(
        "/api/lineage/edges/1/repair", content=JSONContent({"action": "隔离"})
    )
    # 未登录在鉴权层即被拦（401），不会触及修复逻辑
    assert r2.status == 401


@pytest.mark.asyncio
async def test_trace_carries_repair_outcome(authed):
    edges = (await (await authed.get("/api/lineage/edges")).json())["items"]
    # ord-13 已盖印、其边当前同源已盖；先把该边改成断链态再隔离，
    # 这样按 WM-GMV-141 / WM-ORD-131 追索时该边在结果内且带修复结局。
    target = next(e for e in edges if e["upstream_run_code"] == "ord-20261113-01")
    # 通过直接构造断链：把边退回待盖印（模拟历史遗留），再走隔离修复
    import sqlite3
    from app.config import Config
    db = Config.database()
    conn = sqlite3.connect(db)
    conn.execute("UPDATE lineage_edges SET edge_status='待盖印', edge_watermark=NULL WHERE id=?", (target["id"],))
    conn.commit(); conn.close()

    rep = await authed.post(
        f"/api/lineage/edges/{target['id']}/repair", json={"action": "隔离"}
    )
    assert rep.status == 200

    r = await authed.post("/api/trace", json={"watermark_value": "WM-ORD-131"})
    body = await r.json()
    traced = next(e for e in body["edges"] if e["id"] == target["id"])
    assert traced["edge_status"] == "已隔离"
    assert traced["repair_action"] == "隔离"
    assert traced["repair_outcome"]
