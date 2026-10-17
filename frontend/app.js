const $ = (id) => document.getElementById(id);
const chat = $("chat");
let convId = null;
let busy = false;

function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html !== undefined) e.innerHTML = html; return e; }
function scroll() { chat.scrollTop = chat.scrollHeight; }

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    $("today").textContent = `today ${s.today} · ${s.wells} wells` + (s.llm_configured ? "" : " · NO LLM KEY");
  } catch (e) { $("today").textContent = "backend unreachable"; }
}

async function init() {
  const users = await api("/api/users");
  $("user").innerHTML = users.map((u) => `<option value="${esc(u.user_id)}">${esc(u.name)} (${esc(u.user_id)})</option>`).join("");
  refreshStatus();
}

function newChat() { convId = null; chat.innerHTML = ""; }

const charts = [];
const palette = ["#3aa0ff", "#2ecc71", "#f1c40f", "#e67e22", "#e74c3c", "#9b59b6", "#1abc9c", "#95a5a6"];

function renderVisual(spec) {
  const box = el("div", "visual");
  box.appendChild(el("h3", null, esc(spec.title)));
  if (spec.subtitle) box.appendChild(el("div", "sub", esc(spec.subtitle)));
  const d = spec.data;
  if (spec.type === "status_card") {
    const c = el("div", "card");
    const cls = d.producing === true ? "ok" : d.producing === false ? "bad" : "";
    c.appendChild(el("div", "head " + cls, esc((d.well_id ? d.well_id + " — " : "") + d.headline)));
    (d.fields || []).forEach((f) => c.appendChild(el("div", "f", `<span>${esc(f.label)}</span>${esc(f.value)}`)));
    (d.notes || []).forEach((n) => c.appendChild(el("div", "f", esc(n))));
    box.appendChild(c);
  } else if (spec.type === "table") {
    const t = el("table");
    t.innerHTML = "<thead><tr>" + d.columns.map((c) => `<th>${esc(c)}</th>`).join("") + "</tr></thead><tbody>" +
      d.rows.map((r) => "<tr>" + r.map((v) => `<td>${esc(v)}</td>`).join("") + "</tr>").join("") + "</tbody>";
    box.appendChild(t);
  } else if (spec.type === "line_chart" || spec.type === "bar_chart") {
    const wrap = el("div", "chartbox");
    const cv = el("canvas");
    wrap.appendChild(cv); box.appendChild(wrap);
    const labels = [...new Set(d.series.flatMap((s) => s.points.map((p) => String(p.x))))];
    const line = spec.type === "line_chart";
    setTimeout(() => charts.push(new Chart(cv, {
      type: line ? "line" : "bar",
      data: {
        labels,
        datasets: d.series.map((s, i) => ({
          label: s.name, borderColor: palette[i % 8], backgroundColor: palette[i % 8] + (line ? "55" : "cc"), tension: 0.2,
          data: labels.map((l) => { const p = s.points.find((q) => String(q.x) === l); return p ? p.y : null; }),
        })),
      },
      options: {
        maintainAspectRatio: false, color: "#e6edf3",
        scales: {
          x: { stacked: !!d.stacked && !line, title: { display: !!spec.x_label, text: spec.x_label, color: "#8b9bab" }, ticks: { color: "#8b9bab" }, grid: { color: "#263442" } },
          y: { stacked: !!d.stacked && !line, title: { display: !!spec.y_label, text: spec.y_label, color: "#8b9bab" }, ticks: { color: "#8b9bab" }, grid: { color: "#263442" } },
        },
        plugins: { legend: { labels: { color: "#e6edf3" } } },
      },
    })), 0);
  } else if (spec.type === "map") {
    const m = el("div", "map");
    box.appendChild(m);
    setTimeout(() => {
      const map = L.map(m);
      L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "© OpenStreetMap", maxZoom: 18 }).addTo(map);
      const col = { producing: "#2ecc71", not_producing: "#ff5d5d", alert: "#f1c40f", unknown: "#95a5a6" };
      const pts = d.markers.map((k) => {
        L.circleMarker([k.lat, k.lon], { radius: 8, color: col[k.status] || "#3aa0ff", fillOpacity: 0.8 })
          .bindPopup(`<b>${esc(k.label)}</b><br>${esc(k.detail || k.status || "")}`).addTo(map);
        return [k.lat, k.lon];
      });
      map.fitBounds(pts, { padding: [30, 30], maxZoom: 15 });
    }, 0);
  } else if (spec.type === "heatmap") {
    const flat = d.values.flat().filter((v) => v !== null);
    const max = Math.max(1, ...flat);
    const g = el("div", "heat");
    g.style.gridTemplateColumns = `auto repeat(${d.x_labels.length}, minmax(60px, 1fr))`;
    g.appendChild(el("div", null, ""));
    d.x_labels.forEach((x) => g.appendChild(el("div", null, `<b>${esc(x)}</b>`)));
    d.y_labels.forEach((y, i) => {
      g.appendChild(el("div", null, `<b>${esc(y)}</b>`));
      d.x_labels.forEach((_, j) => {
        const v = d.values[i]?.[j];
        const c = el("div", null, v === null || v === undefined ? "–" : esc(v));
        c.style.background = v == null ? "#17212b" : `rgba(58,160,255,${0.12 + 0.88 * (v / max)})`;
        g.appendChild(c);
      });
    });
    box.appendChild(g);
    if (spec.x_label || spec.y_label) box.appendChild(el("div", "sub", esc(`${spec.y_label || ""} × ${spec.x_label || ""}`)));
  }
  return box;
}

function renderEvidence(ev) {
  const det = el("details", "evidence");
  det.open = true;
  det.appendChild(el("summary", null, `Evidence (${ev.ids.length})`));
  Object.entries(ev.groups).forEach(([g, ids]) => {
    const row = el("div", null, `<b>${esc(g)}</b> ` + ids.map((i) => `<code>${esc(i)}</code>`).join(""));
    det.appendChild(row);
  });
  (ev.docs || []).forEach((d) => det.appendChild(el("div", null, `📄 ${esc(d.doc_id || "")} — ${esc(d.section || "")} <code>${esc(d.id)}</code>`)));
  return det;
}

async function send(text) {
  busy = true; $("send").disabled = true;
  chat.appendChild(el("div", "msg user", esc(text)));
  const bubble = el("div", "msg assistant");
  const tools = el("div", "tools"), body = el("div", "text");
  bubble.append(tools, body);
  chat.appendChild(bubble); scroll();
  const chips = [];
  try {
    const r = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: $("user").value, message: text, conversation_id: convId }),
    });
    if (!r.ok) throw new Error(await r.text());
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 2);
        if (!line.startsWith("data:")) continue;
        const ev = JSON.parse(line.slice(5));
        if (ev.type === "conversation") convId = ev.id;
        else if (ev.type === "text") body.textContent += ev.text;
        else if (ev.type === "tool_call") {
          const c = el("span", "chip", `⚙ ${esc(ev.name)}(${esc(JSON.stringify(ev.args)).slice(0, 90)})`);
          tools.appendChild(c); chips.push(c);
        } else if (ev.type === "tool_result") {
          const c = chips.shift();
          if (c) { c.classList.add(ev.ok ? "done" : "err"); c.textContent += ` → ${ev.summary}`; }
        } else if (ev.type === "visual") bubble.insertBefore(renderVisual(ev.spec), null);
        else if (ev.type === "evidence") bubble.appendChild(renderEvidence(ev));
        else if (ev.type === "error") body.appendChild(el("div", "err-text", esc(ev.message)));
        scroll();
      }
    }
  } catch (e) {
    body.appendChild(el("div", "err-text", esc(e.message)));
  }
  busy = false; $("send").disabled = false; $("input").focus(); refreshStatus();
}

$("form").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = $("input").value.trim();
  if (!t || busy) return;
  $("input").value = "";
  send(t);
});
$("user").addEventListener("change", newChat);
$("newchat").addEventListener("click", newChat);
$("nextday").addEventListener("click", async () => { await api("/api/next-day", { method: "POST" }); newChat(); refreshStatus(); });
$("reset").addEventListener("click", async () => {
  if (!confirm("Clear all facts, corrections and conversations?")) return;
  await api("/api/reset-memory", { method: "POST" }); newChat(); refreshStatus();
});
init();
