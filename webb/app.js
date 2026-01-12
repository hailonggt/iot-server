const $ = (id) => document.getElementById(id);

const API_BASE = (window.IOT_API_BASE && window.IOT_API_BASE.trim())
  ? window.IOT_API_BASE.trim()
  : `${location.protocol}//${location.host}`;

const state = {
  token: localStorage.getItem("iot_token") || "",
  chart: null,
};

function formatNumber(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "...";
  const n = Number(v);
  if (Number.isNaN(n)) return "...";
  return digits === 0 ? String(Math.round(n)) : n.toFixed(digits);
}

function formatTimeFromTs(ts) {
  if (!ts || Number(ts) <= 0) return "00:00:00";
  const d = new Date(Number(ts) * 1000);
  return d.toLocaleTimeString("vi-VN", { hour12: false });
}

function setBadge(el, text) {
  el.textContent = text || "...";
  el.classList.remove("badge-safe", "badge-warn", "badge-danger");

  if (text === "AN TOÀN") el.classList.add("badge-safe");
  else if (text === "CẢNH BÁO") el.classList.add("badge-warn");
  else if (text === "NGUY HIỂM") el.classList.add("badge-danger");
}

function authHeaders() {
  const h = { "Content-Type": "application/json" };
  if (state.token) h.Authorization = `Bearer ${state.token}`;
  return h;
}

function initChart() {
  const canvas = $("historyChart");
  if (!canvas) return;

  const ctx = canvas.getContext("2d");

  state.chart = new Chart(ctx, {
    type: "line",
    data: {
      labels: [],
      datasets: [
        { label: "Khói MQ2", data: [], tension: 0.35, borderWidth: 3 },
        { label: "Nhiệt độ °C", data: [], tension: 0.35, borderWidth: 3 },
        { label: "Độ ẩm %", data: [], tension: 0.35, borderWidth: 3 },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: true,
    },
  });
}

function updateChart(items) {
  if (!state.chart) return;

  const asc = [...items];
  state.chart.data.labels = asc.map((x) => formatTimeFromTs(x.timestamp));
  state.chart.data.datasets[0].data = asc.map((x) => Number(x.smoke || 0));
  state.chart.data.datasets[1].data = asc.map((x) => Number(x.temperature || 0));
  state.chart.data.datasets[2].data = asc.map((x) => Number(x.humidity || 0));

  state.chart.update();
}

async function fetchCurrent() {
  const res = await fetch(`${API_BASE}/api/current`);
  const cur = await res.json();

  $("tempValue").textContent = formatNumber(cur.temperature, 1);
  $("humValue").textContent = formatNumber(cur.humidity, 0);
  $("smokeValue").textContent = formatNumber(cur.smoke, 0);

  $("lastUpdateText").textContent = formatTimeFromTs(cur.timestamp);
  $("onlineText").textContent = cur.online ? "Online" : "Offline";

  setBadge($("aiBadge"), cur.status || "...");
}

async function fetchHistory() {
  const res = await fetch(`${API_BASE}/api/history?limit=20`);
  const js = await res.json();
  if (!js.ok) return;

  const tbody = $("historyBody");
  tbody.innerHTML = "";

  for (const it of js.items) {
    const tr = document.createElement("tr");

    const st = it.status || "...";
    if (st === "AN TOÀN") tr.classList.add("row-safe");
    else if (st === "CẢNH BÁO") tr.classList.add("row-warn");
    else if (st === "NGUY HIỂM") tr.classList.add("row-danger");

    const badgeClass =
      st === "AN TOÀN" ? "badge badge-safe"
      : st === "CẢNH BÁO" ? "badge badge-warn"
      : st === "NGUY HIỂM" ? "badge badge-danger"
      : "badge";

    tr.innerHTML = `
      <td>${formatTimeFromTs(it.timestamp)}</td>
      <td>${formatNumber(it.smoke, 0)}</td>
      <td>${formatNumber(it.temperature, 1)}</td>
      <td>${formatNumber(it.humidity, 0)}</td>
      <td><span class="${badgeClass}">${st}</span></td>
    `;

    tbody.appendChild(tr);
  }

  updateChart(js.items);
}

function showLoginModal(show) {
  $("loginModal").classList.toggle("hidden", !show);
  $("loginHint").textContent = "";
}

function refreshAuthUI() {
  $("btnLoginOpen").classList.toggle("hidden", !!state.token);
  $("btnLogout").classList.toggle("hidden", !state.token);

  const need = !state.token;
  $("btnTrainAI").disabled = need;
  $("btnExportExcel").disabled = need;
  $("btnDeleteAll").disabled = need;
}

async function doLogin() {
  const username = $("loginUser").value.trim();
  const password = $("loginPass").value.trim();

  const res = await fetch(`${API_BASE}/api/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });

  const js = await res.json();

  if (!js.ok) {
    $("loginHint").textContent = js.error || "Đăng nhập thất bại";
    return;
  }

  state.token = js.token;
  localStorage.setItem("iot_token", state.token);

  showLoginModal(false);
  refreshAuthUI();
}

async function doLogout() {
  await fetch(`${API_BASE}/api/logout`, {
    method: "POST",
    headers: authHeaders(),
  }).catch(() => {});

  state.token = "";
  localStorage.removeItem("iot_token");
  refreshAuthUI();
}

async function doTrainAI() {
  if (!state.token) return;

  const res = await fetch(`${API_BASE}/api/admin/train_ai`, {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({ limit: 3000 }),
  });

  const js = await res.json();
  if (!js.ok) {
    alert(js.error || "Huấn luyện thất bại");
    return;
  }
  alert("Huấn luyện AI xong");
}

async function doExportExcel() {
  if (!state.token) return;

  const res = await fetch(`${API_BASE}/api/admin/export_excel?limit=2000`, {
    headers: authHeaders(),
  });

  if (!res.ok) {
    alert("Xuất Excel thất bại");
    return;
  }

  const blob = await res.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "iot_history.xlsx";
  a.click();
}

async function doDeleteAll() {
  if (!state.token) return;

  const ok = confirm("Bạn chắc chắn muốn xóa toàn bộ lịch sử?");
  if (!ok) return;

  const res = await fetch(`${API_BASE}/api/admin/delete_history`, {
    method: "POST",
    headers: authHeaders(),
  });

  const js = await res.json();
  if (!js.ok) {
    alert(js.error || "Xóa thất bại");
    return;
  }

  alert("Đã xóa dữ liệu");
  await fetchHistory();
}

function bindEvents() {
  $("btnLoginOpen").addEventListener("click", () => showLoginModal(true));
  $("btnLoginClose").addEventListener("click", () => showLoginModal(false));
  $("btnLogin").addEventListener("click", doLogin);
  $("btnLogout").addEventListener("click", doLogout);

  $("btnTrainAI").addEventListener("click", doTrainAI);
  $("btnExportExcel").addEventListener("click", doExportExcel);
  $("btnDeleteAll").addEventListener("click", doDeleteAll);
}

async function tick() {
  try {
    await fetchCurrent();
    await fetchHistory();
  } catch (e) {
    console.log("fetch error", e);
  }
}

function start() {
  initChart();
  bindEvents();
  refreshAuthUI();
  tick();
  setInterval(tick, 5000);
}

window.addEventListener("DOMContentLoaded", start);
