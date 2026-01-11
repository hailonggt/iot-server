const $ = (id) => document.getElementById(id);

/*
  API_BASE lấy từ config.js
  Nếu config để trống thì tự dùng origin hiện tại
*/
const API_BASE = (window.IOT_API_BASE && window.IOT_API_BASE.trim())
  ? window.IOT_API_BASE.trim()
  : `${location.protocol}//${location.host}`;

const state = {
  token: localStorage.getItem("iot_token") || "",
  chart: null,
};

function formatNumber(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  const n = Number(v);
  if (Number.isNaN(n)) return "--";
  return digits === 0 ? String(Math.round(n)) : n.toFixed(digits);
}

function formatTimeFromTs(ts) {
  if (!ts) return "--:--:--";
  const d = new Date(Number(ts) * 1000);
  return d.toLocaleTimeString("vi-VN", { hour12: false });
}

function setBadge(el, text) {
  el.textContent = text || "--";

  el.classList.remove("badge-safe", "badge-warn", "badge-danger");

  if (text === "AN TOÀN") el.classList.add("badge-safe");
  else if (text === "CẢNH BÁO") el.classList.add("badge-warn");
  else if (text === "NGUY HIỂM") el.classList.add("badge-danger");
}

function rowClassByStatus(status) {
  if (status === "AN TOÀN") return "row-safe";
  if (status === "CẢNH BÁO") return "row-warn";
  if (status === "NGUY HIỂM") return "row-danger";
  return "";
}

function authHeaders() {
  const h = { "Content-Type": "application/json" };
  if (state.token) h.Authorization = `Bearer ${state.token}`;
  return h;
}

function initChart() {
  const ctx = $("historyChart").getContext("2d");

  state.chart = new Chart(ctx, {
    type: "line",
    data: {
      labels: [],
      datasets: [
        { label: "Khói MQ2", data: [], tension: 0.35, borderWidth: 3, pointRadius: 3 },
        { label: "Nhiệt độ °C", data: [], tension: 0.35, borderWidth: 3, pointRadius: 3 },
        { label: "Độ ẩm %", data: [], tension: 0.35, borderWidth: 3, pointRadius: 3 },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      plugins: { legend: { labels: { font: { weight: "700" } } } },
      scales: {
        x: { ticks: { font: { weight: "700" } }, grid: { color: "rgba(15,23,42,0.08)" } },
        y: { ticks: { font: { weight: "700" } }, grid: { color: "rgba(15,23,42,0.08)" } },
      },
    },
  });
}

function renderTable(items) {
  const tbody = $("historyBody");
  tbody.innerHTML = "";

  for (const it of items) {
    const tr = document.createElement("tr");
    tr.className = rowClassByStatus(it.status);

    const tdTime = document.createElement("td");
    tdTime.textContent = formatTimeFromTs(it.timestamp);

    const tdSmoke = document.createElement("td");
    tdSmoke.textContent = formatNumber(it.smoke, 0);

    const tdTemp = document.createElement("td");
    tdTemp.textContent = formatNumber(it.temperature, 1);

    const tdHum = document.createElement("td");
    tdHum.textContent = formatNumber(it.humidity, 0);

    const tdStatus = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = "badge";
    setBadge(badge, it.status);
    tdStatus.appendChild(badge);

    tr.appendChild(tdTime);
    tr.appendChild(tdSmoke);
    tr.appendChild(tdTemp);
    tr.appendChild(tdHum);
    tr.appendChild(tdStatus);

    tbody.appendChild(tr);
  }
}

function updateChart(items) {
  const asc = [...items].reverse();

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

  setBadge($("aiBadge"), cur.status || "--");
}

async function fetchHistory() {
  const res = await fetch(`${API_BASE}/api/history?limit=20`);
  const js = await res.json();
  if (!js.ok) return;

  renderTable(js.items);
  updateChart(js.items);
}

function showLoginModal(show) {
  $("loginModal").classList.toggle("hidden", !show);
  $("loginHint").textContent = "";
}

function refreshAuthUI() {
  $("btnLoginOpen").classList.toggle("hidden", !!state.token);
  $("btnLogout").classList.toggle("hidden", !state.token);
}

async function doLogin() {
  const username = $("loginUser").value.trim();
  const password = $("loginPass").value.trim();

  $("loginHint").textContent = "";

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
  if (!state.token) return;

  await fetch(`${API_BASE}/api/logout`, {
    method: "POST",
    headers: authHeaders(),
  }).catch(() => {});

  state.token = "";
  localStorage.removeItem("iot_token");
  refreshAuthUI();
}

async function exportExcel() {
  if (!state.token) {
    alert("Cần đăng nhập để xuất Excel");
    return;
  }

  const res = await fetch(`${API_BASE}/api/admin/export_excel?limit=1000`, {
    headers: authHeaders(),
  });

  if (!res.ok) {
    alert("Xuất Excel thất bại");
    return;
  }

  const blob = await res.blob();
  const url = URL.createObjectURL(blob);

  const a = document.createElement("a");
  a.href = url;
  a.download = "iot_history.xlsx";
  document.body.appendChild(a);
  a.click();
  a.remove();

  URL.revokeObjectURL(url);
}

async function deleteAllHistory() {
  if (!state.token) {
    alert("Cần đăng nhập để xóa dữ liệu");
    return;
  }

  const ok = confirm("Xóa toàn bộ lịch sử? Không thể phục hồi");
  if (!ok) return;

  const res = await fetch(`${API_BASE}/api/admin/delete_history`, {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({ mode: "all" }),
  });

  const js = await res.json();
  if (!js.ok) {
    alert(js.error || "Xóa thất bại");
    return;
  }

  await fetchHistory();
  alert("Đã xóa lịch sử");
}

async function trainAI() {
  if (!state.token) {
    alert("Cần đăng nhập để huấn luyện AI");
    return;
  }

  const res = await fetch(`${API_BASE}/api/admin/train_ai`, {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({ limit: 1500 }),
  });

  const js = await res.json();
  if (!js.ok) {
    alert(js.error || "Huấn luyện thất bại");
    return;
  }

  alert(`Huấn luyện xong, mẫu AI: ${js.trained_samples}`);
}

function bindEvents() {
  $("btnLoginOpen").addEventListener("click", () => showLoginModal(true));
  $("btnLoginClose").addEventListener("click", () => showLoginModal(false));
  $("btnLogin").addEventListener("click", doLogin);
  $("btnLogout").addEventListener("click", doLogout);

  $("btnExportExcel").addEventListener("click", exportExcel);
  $("btnDeleteAll").addEventListener("click", deleteAllHistory);
  $("btnTrainAI").addEventListener("click", trainAI);

  $("loginModal").addEventListener("click", (e) => {
    if (e.target === $("loginModal")) showLoginModal(false);
  });
}

/*
  Loop tối ưu
  current poll 3 giây
  history poll 10 giây
*/
async function tickCurrent() {
  try { await fetchCurrent(); } catch (e) { console.log("current err", e); }
}

async function tickHistory() {
  try { await fetchHistory(); } catch (e) { console.log("history err", e); }
}

function start() {
  initChart();
  bindEvents();
  refreshAuthUI();

  tickCurrent();
  tickHistory();

  setInterval(tickCurrent, 3000);
  setInterval(tickHistory, 10000);
}

start();
