const API = "http://localhost:8001";
const COLORS = { sweden: "#2a9d4b", germany: "#4a6fa5", poland: "#e08a1e" };
const HOUR = 3600000;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = v => (v == null ? "–" : Number(v).toFixed(1));
const hhmm = t => t.slice(8, 10) + " " + t.slice(11, 16);   // "2026-10-14T02:00:00Z" -> "14 02:00"

let tasks = [], selectedId = null, clock = null, carbon = null;
let lineChart = null, barChart = null, barKey = "", carbonT0 = 0;

// Draws the green band (chosen slot) behind the lines and a red dashed "now" line on top.
const marksPlugin = {
  id: "marks",
  beforeDatasetsDraw(chart) {
    const m = chart.$marks, x = chart.scales.x, y = chart.scales.y;
    if (!m || !m.slot) return;
    const a = Math.max(x.left, x.getPixelForValue(m.slot[0]));
    const b = Math.min(x.right, x.getPixelForValue(m.slot[1]));
    if (b <= a) return;
    chart.ctx.save();
    chart.ctx.fillStyle = "rgba(42,157,75,0.18)";
    chart.ctx.fillRect(a, y.top, b - a, y.bottom - y.top);
    chart.ctx.restore();
  },
  afterDatasetsDraw(chart) {
    const m = chart.$marks, x = chart.scales.x, y = chart.scales.y;
    if (!m || m.now == null) return;
    const p = x.getPixelForValue(m.now);
    if (p < x.left || p > x.right) return;
    const ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = "#d33";
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.moveTo(p, y.top);
    ctx.lineTo(p, y.bottom);
    ctx.stroke();
    ctx.restore();
  },
};

const idxOf = iso => (Date.parse(iso) - carbonT0) / HOUR;   // hours since the first data row

function buildLine() {
  const regions = Object.keys(carbon);
  if (!regions.length) return;
  const times = carbon[regions[0]].map(p => p.t);
  carbonT0 = Date.parse(times[0]);
  const datasets = regions.map(r => {
    const byTime = Object.fromEntries(carbon[r].map(p => [p.t, p.g]));
    const color = COLORS[r] || "#666";
    return { label: r, data: times.map(t => byTime[t] ?? null), borderColor: color,
             backgroundColor: color, borderWidth: 2, pointRadius: 0, tension: 0.2 };
  });
  lineChart = new Chart($("lineChart"), {
    type: "line",
    data: { labels: times.map(hhmm), datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { ticks: { maxTicksLimit: 12 }, title: { display: true, text: "Simulated time (day hh:mm UTC)" } },
        y: { title: { display: true, text: "gCO2 per kWh" } },
      },
      plugins: { legend: { position: "bottom" } },
    },
    plugins: [marksPlugin],
  });
}

function updateMarks(t) {
  if (!lineChart) return;
  const marks = {};
  if (clock) marks.now = idxOf(clock.now);
  if (t && t.start) {
    const s = idxOf(t.start);
    marks.slot = [s, s + Math.ceil(t.duration_hours || 1)];
  }
  lineChart.$marks = marks;
  lineChart.update("none");
}

function drawBar(t) {
  const key = t ? [t.id, t.carbon_g, t.naive_carbon_g].join("|") : "";
  if (key === barKey) return;                    // unchanged: don't redraw every 2 seconds
  barKey = key;
  if (barChart) { barChart.destroy(); barChart = null; }
  if (!t || t.naive_carbon_g == null || t.carbon_g == null) return;
  barChart = new Chart($("barChart"), {
    type: "bar",
    data: {
      labels: [`Run now in ${t.naive_region}`, `Carbon-aware (${t.region})`],
      datasets: [{ data: [t.naive_carbon_g, t.carbon_g], backgroundColor: ["#8a94a6", "#2a9d4b"] }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true, title: { display: true, text: "grams CO2" } } },
    },
  });
}

function renderDetail(t) {
  if (!t) {
    $("headline").textContent = "Submit a task, or click a row, to compare.";
    $("reason").textContent = "";
    drawBar(null);
    updateMarks(null);
    return;
  }
  let head;
  if (t.status === "Failed") {
    head = `Task failed: ${t.error || "unknown error"}`;
  } else if (t.saved_pct == null) {
    head = "No run-now comparison available for this task.";
  } else {
    const word = t.saved_pct >= 0 ? "less" : "MORE";
    head = `${Math.abs(t.saved_pct).toFixed(1)}% ${word} carbon than running now in ${t.naive_region} ` +
           `(${fmt(t.naive_carbon_g)} g → ${fmt(t.carbon_g)} g)`;
    if (t.status !== "Completed") head += ` · estimate, task is ${t.status}`;
  }
  $("headline").textContent = head;
  const skipped = (t.skipped || []).map(s => `${s.region}: ${s.reason}`).join("; ");
  $("reason").textContent = (t.reason || "") + (skipped ? ` · skipped: ${skipped}` : "");
  drawBar(t);
  updateMarks(t);
}

function renderTable() {
  $("rows").innerHTML = [...tasks].sort((a, b) => b.id - a.id).map(x => `
    <tr data-id="${esc(x.id)}" class="${x.id === selectedId ? "sel" : ""}">
      <td>${esc(x.id)}</td>
      <td>${esc(x.name ?? "task " + x.id)}</td>
      <td>${esc(x.region_key)}</td>
      <td>${esc(String(x.start ?? "").replace("T", " ").slice(0, 16))}</td>
      <td class="${esc(String(x.status).toLowerCase())}" title="${esc(x.error)}">${esc(x.status)}</td>
      <td>${fmt(x.carbon_g)}</td>
      <td>${fmt(x.naive_carbon_g)}</td>
      <td>${x.saved_pct == null ? "–" : Number(x.saved_pct).toFixed(1) + "%"}</td>
    </tr>`).join("");
}

function renderSummary(s) {
  $("sumCount").textContent = s.tasks_counted;
  $("sumNaive").textContent = fmt(s.naive_g);
  $("sumAware").textContent = fmt(s.aware_g);
  $("sumPct").textContent = s.saved_pct == null ? "–" : s.saved_pct.toFixed(1) + "%";
}

async function loadCarbon() {
  carbon = await (await fetch(`${API}/carbon`)).json();
  buildLine();
}

async function refresh() {
  try {
    const [c, t, s] = await Promise.all(
      [fetch(`${API}/clock`), fetch(`${API}/tasks`), fetch(`${API}/summary`)]);
    clock = await c.json();
    tasks = await t.json();
    const summary = await s.json();
    if (!carbon) await loadCarbon();
    if (selectedId == null && tasks.length) selectedId = tasks[tasks.length - 1].id;
    $("sim-clock").textContent =
      `Simulated time (UTC): ${clock.now.replace("T", " ").slice(0, 16)}  |  speed ${clock.speed}x`;
    renderSummary(summary);
    renderTable();
    renderDetail(tasks.find(x => x.id === selectedId) || null);
  } catch (e) {
    $("sim-clock").textContent = "API not reachable on port 8001";
  }
}

$("rows").addEventListener("click", e => {
  const tr = e.target.closest("tr");
  if (!tr) return;
  selectedId = Number(tr.dataset.id);
  refresh();
});

$("task-form").addEventListener("submit", async e => {
  e.preventDefault();
  $("msg").textContent = "";
  const f = new FormData(e.target);
  try {
    const now = await (await fetch(`${API}/clock`)).json();
    // deadlines are in SIMULATED time: simulated "now" + the chosen number of hours
    const deadline = new Date(Date.parse(now.now) + Number(f.get("deadline_hours")) * HOUR).toISOString();
    const res = await fetch(`${API}/tasks`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: f.get("name"), image: f.get("image"),
        duration_hours: Number(f.get("duration_hours")), deadline,
        power_kw: Number(f.get("power_kw")), needs_gpu: f.get("needs_gpu") === "on",
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      $("msg").textContent = typeof data.detail === "string" ? data.detail : "Invalid input: " + JSON.stringify(data.detail);
      return;
    }
    selectedId = data.id;
    refresh();
  } catch (err) {
    $("msg").textContent = "API not reachable on port 8001";
  }
});

refresh();
setInterval(refresh, 2000);