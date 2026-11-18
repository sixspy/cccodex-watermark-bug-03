(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "lineage";
  let templates = [];
  let batches = [];
  let reports = [];
  let edges = [];
  let journals = [];
  let selected = null;
  let stampValue = "";
  let traceValue = "WM-GMV-141";
  let traceResult = null;

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    return j;
  }

  function setMsg(t) {
    msgEl.textContent = t || "";
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  async function refresh() {
    const meta = await api("/api/meta");
    document.getElementById("lab-name").textContent = meta.lab_name || "血缘水印台";
    templates = (await api("/api/templates")).items || [];
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/gap-reports")).items || [];
    edges = (await api("/api/lineage/edges")).items || [];
    journals = (await api("/api/lineage/journals")).items || [];
  }

  function syncDock() {
    document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
      btn.classList.toggle("on", btn.dataset.tab === tab);
    });
  }

  function edgeChip(e) {
    if (e.edge_status === "已隔离")
      return `<span class="chip chip-isolate" title="${esc(e.isolated_certificate || "")}">已隔离 · ${esc(e.isolated_certificate || "-")}</span>`;
    if (e.is_broken) return `<span class="chip chip-broken">断链 · 待盖</span>`;
    if (e.edge_status === "已盖印")
      return `<span class="chip chip-stamp">已盖印${e.same_source === false ? " · 不同源!" : ""}</span>`;
    return `<span class="chip chip-pending">待盖印</span>`;
  }

  function renderTemplates() {
    return `
      <h2>水印模板</h2>
      <table>
        <thead><tr><th>表</th><th>列</th><th>版本</th><th>状态</th></tr></thead>
        <tbody>
          ${templates
            .map(
              (t) => `<tr>
            <td>${esc(t.table_name)}</td>
            <td>${esc(t.column_name)}</td>
            <td>${t.version_no}</td>
            <td><span class="chip">${esc(t.status)}</span></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderBatches() {
    return `
      <h2>运行批次</h2>
      <p class="muted">主链：选批盖印 → 用水印追索下游 → 缺口对账</p>
      <table>
        <thead><tr><th>编号</th><th>表</th><th>运行码</th><th>业务日</th><th>状态</th><th>水印</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td>${b.id}</td>
            <td>${esc(b.table_name)}</td>
            <td>${esc(b.run_code)}</td>
            <td>${esc(b.biz_date)}</td>
            <td><span class="chip">${esc(b.status)}</span></td>
            <td>${esc(b.watermark_value || "-")}</td>
            <td>${
              b.status === "待盖印"
                ? `<button type="button" class="ghost" data-pick="${b.id}">盖印</button>`
                : ""
            }</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      ${
        selected
          ? `<div class="cardish">
        <h3>盖印</h3>
        <p>${esc(selected.run_code)} · ${esc(selected.table_name)}</p>
        <label>水印值<input id="stamp-input" value="${esc(stampValue)}" /></label>
        <button type="button" id="btn-stamp">写入盖印</button>
      </div>`
          : ""
      }`;
  }

  function renderLineage() {
    const brokenCount = edges.filter((e) => e.is_broken).length;
    return `
      <h2>血缘队列 <span class="muted" style="font-size:.85rem">断链 ${brokenCount} 条 · 共 ${edges.length} 条边</span></h2>
      <p class="muted">上游卡片看缺口，下游附页看落印；断链边可选「回滚」（下游恢复待盖）或「隔离」（上游挂凭证放行、保留已盖）。</p>
      <table>
        <thead><tr><th>边号</th><th>上游卡片</th><th>下游附页</th><th>边印</th><th>修复结局</th><th>操作</th></tr></thead>
        <tbody>
          ${edges
            .map((e) => {
              const upCard = `<div class="card-line">
                  <strong>${esc(e.upstream_run_code)}</strong>
                  <span class="chip ${e.upstream_status === "待盖印" ? "chip-pending" : "chip-stamp"}">${esc(e.upstream_status || "-")}</span>
                  <span class="muted">${esc(e.upstream_watermark || "无水印")}</span>
                </div>`;
              const downPage = `<div class="card-line">
                  <strong>${esc(e.downstream_run_code)}</strong>
                  <span class="chip ${e.downstream_status === "已盖印" ? "chip-stamp" : "chip-pending"}">${esc(e.downstream_status || "-")}</span>
                  <span class="muted">${esc(e.downstream_watermark || "未盖印")}</span>
                </div>`;
              const actions = e.is_broken
                ? `<div class="row" style="margin:0">
                    <button type="button" class="btn-rollback" data-repair="${e.id}" data-action="回滚">回滚</button>
                    <button type="button" class="btn-isolate" data-repair="${e.id}" data-action="隔离">隔离</button>
                  </div>`
                : `<span class="muted">—</span>`;
              return `<tr class="${e.is_broken ? "row-broken" : ""}">
                <td>${e.id}</td>
                <td>${upCard}</td>
                <td>${downPage}</td>
                <td>${edgeChip(e)}</td>
                <td class="outcome" data-outcome="${e.id}">${e.is_broken ? '<span class="muted">待修复</span>' : edgeChip(e)}</td>
                <td>${actions}</td>
              </tr>`;
            })
            .join("")}
        </tbody>
      </table>
      <h3 style="margin-top:1.2rem">编排流水</h3>
      <table>
        <thead><tr><th>序</th><th>边</th><th>结局</th><th>凭证</th><th>操作人</th><th>时间</th><th>说明</th></tr></thead>
        <tbody>
          ${
            journals.length
              ? journals
                  .map(
                    (j) => `<tr>
                <td>${j.id}</td>
                <td>#${j.edge_id}</td>
                <td><span class="chip ${j.action === "回滚" ? "chip-rollback" : "chip-isolate"}">${esc(j.action)}</span></td>
                <td>${esc(j.isolated_certificate || "-")}</td>
                <td>${esc(j.operator)}</td>
                <td>${esc(j.created_at)}</td>
                <td>${esc(j.outcome)}</td>
              </tr>`
                  )
                  .join("")
              : `<tr><td colspan="7" class="muted">暂无修复流水</td></tr>`
          }
        </tbody>
      </table>`;
  }

  function renderTrace() {
    const src = (traceResult && traceResult.sources) || [];
    const down = (traceResult && traceResult.downstream) || [];
    const tEdges = (traceResult && traceResult.edges) || [];
    const pending = (traceResult && traceResult.upstream_pending) || [];
    return `
      <h2>下游追索（追索席）</h2>
      <label>水印值<input id="trace-input" value="${esc(traceValue)}" placeholder="例如 WM-GMV-141" /></label>
      <div class="row"><button type="button" id="btn-trace">追索</button></div>
      ${
        traceResult
          ? `<div>
        <h3>源批次</h3>
        <ul>${src.map((s) => `<li>${esc(s.run_code)} · ${esc(s.table_name)}</li>`).join("")}</ul>
        ${pending.length ? `<p class="muted">待盖上游（断链追索到此断点）：${pending.map(esc).join("、")}</p>` : ""}
        <h3>血缘边结局（与队列芯片一致）</h3>
        <table>
          <thead><tr><th>上游</th><th>下游</th><th>边印</th><th>修复结局</th></tr></thead>
          <tbody>
            ${tEdges
              .map((e) => {
                const chip =
                  e.edge_status === "已隔离"
                    ? `<span class="chip chip-isolate">已隔离 · ${esc(e.isolated_certificate || "")}</span>`
                    : e.edge_status === "已盖印"
                    ? `<span class="chip chip-stamp">已盖印</span>`
                    : `<span class="chip chip-pending">待盖印</span>`;
                const out = e.repair_outcome
                  ? `<span class="chip ${e.repair_action === "回滚" ? "chip-rollback" : "chip-isolate"}">${esc(e.repair_action)}</span><span class="muted"> ${esc(e.repair_outcome)}</span>`
                  : '<span class="muted">未修复</span>';
                return `<tr><td>${esc(e.upstream_run_code)}</td><td>${esc(e.downstream_run_code)}</td><td>${chip}</td><td>${out}</td></tr>`;
              })
              .join("")}
          </tbody>
        </table>
        <h3>下游</h3>
        <ul>${down
          .map(
            (d) =>
              `<li>${esc(d.run_code)} · ${esc(d.status)} · ${esc(d.watermark_value || "未盖印")}</li>`
          )
          .join("")}</ul>
      </div>`
          : ""
      }`;
  }

  function renderGaps() {
    return `
      <h2>缺口对账</h2>
      <button type="button" id="btn-gap">生成对账</button>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>应盖</th><th>已盖</th><th>缺口</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${r.id}</td>
            <td>${esc(r.as_of_date)}</td>
            <td>${r.expected_count}</td>
            <td>${r.stamped_count}</td>
            <td>${r.gap_count}</td>
            <td>${esc(r.summary)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function render() {
    if (tab === "templates") view.innerHTML = renderTemplates();
    else if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "lineage") view.innerHTML = renderLineage();
    else if (tab === "trace") view.innerHTML = renderTrace();
    else view.innerHTML = renderGaps();

    view.querySelectorAll("[data-pick]").forEach((btn) => {
      btn.onclick = () => {
        const id = Number(btn.dataset.pick);
        selected = batches.find((b) => b.id === id) || null;
        stampValue = selected ? "WM-" + selected.run_code.toUpperCase() : "";
        render();
      };
    });
    view.querySelectorAll("[data-repair]").forEach((btn) => {
      btn.onclick = async () => {
        const id = Number(btn.dataset.repair);
        const action = btn.dataset.action;
        setMsg("");
        try {
          const r = await api(`/api/lineage/edges/${id}/repair`, {
            method: "POST",
            body: JSON.stringify({ action }),
          });
          await refresh();
          render();
          setMsg(r.journal ? r.journal.outcome : "修复完成");
        } catch (e) {
          setMsg(e.message);
        }
      };
    });
    const stampInput = document.getElementById("stamp-input");
    if (stampInput) {
      stampInput.oninput = () => (stampValue = stampInput.value);
      document.getElementById("btn-stamp").onclick = async () => {
        setMsg("");
        try {
          await api("/api/batches/" + selected.id + "/stamp", {
            method: "POST",
            body: JSON.stringify({ watermark_value: stampValue }),
          });
          selected = null;
          await refresh();
          setMsg("盖印完成（边印已同源回写）");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    const traceInput = document.getElementById("trace-input");
    if (traceInput) {
      traceInput.oninput = () => (traceValue = traceInput.value);
      document.getElementById("btn-trace").onclick = async () => {
        setMsg("");
        try {
          traceResult = await api("/api/trace", {
            method: "POST",
            body: JSON.stringify({ watermark_value: traceValue }),
          });
          setMsg("追索完成");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    const gapBtn = document.getElementById("btn-gap");
    if (gapBtn) {
      gapBtn.onclick = async () => {
        setMsg("");
        try {
          const r = await api("/api/gap-reports", {
            method: "POST",
            body: JSON.stringify({ as_of_date: "2026-11-16" }),
          });
          await refresh();
          setMsg(r.report.summary);
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
  }

  document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      syncDock();
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(() => {
      syncDock();
      render();
    })
    .catch((e) => setMsg(e.message));
})();
