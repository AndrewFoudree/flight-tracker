/* Reads data/prices.csv and data/routes.json at load and renders one chart per
   route. No build step: the price workflow commits both files daily, and Pages
   republishes them. */

const CANDIDATES = ["data/", "../data/"]; // deployed layout, then local checkout

async function firstThatLoads(name) {
  for (const base of CANDIDATES) {
    try {
      const response = await fetch(base + name, { cache: "no-store" });
      if (response.ok) return await response.text();
    } catch (_) { /* try the next base */ }
  }
  throw new Error(`could not load ${name} from ${CANDIDATES.join(" or ")}`);
}

/* Minimal CSV reader. The writer is Python's csv module, so quoted fields with
   embedded commas are possible in booking_url. */
function parseCsv(text) {
  const rows = [];
  let field = "", row = [], quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; } else { quoted = false; }
      } else field += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ",") { row.push(field); field = ""; }
    else if (ch === "\n") { row.push(field); rows.push(row); row = []; field = ""; }
    else if (ch !== "\r") field += ch;
  }
  if (field.length || row.length) { row.push(field); rows.push(row); }
  if (!rows.length) return [];
  const header = rows.shift();
  return rows
    .filter((r) => r.length === header.length)
    .map((r) => Object.fromEntries(header.map((k, i) => [k, r[i]])));
}

/* Whole-party rows only. Single-adult split-booking probes live in the same
   file at a fraction of the price and would flatten every chart. */
function partyRows(rows, route) {
  return rows.filter(
    (r) =>
      r.route_id === route.id &&
      Number(r.adults) === route.adults &&
      Number(r.children) === route.children &&
      Number(r.infants) === route.infants
  );
}

function dailyMinimum(rows) {
  const byDay = new Map();
  for (const row of rows) {
    const day = row.observed_at.slice(0, 10);
    const price = Number(row.total_price);
    if (!Number.isFinite(price)) continue;
    if (!byDay.has(day) || price < byDay.get(day)) byDay.set(day, price);
  }
  return [...byDay.entries()].sort((a, b) => a[0].localeCompare(b[0]));
}

function movingAverage(series, window) {
  return series.map((_, i) => {
    const slice = series.slice(Math.max(0, i - window + 1), i + 1);
    return slice.reduce((sum, [, v]) => sum + v, 0) / slice.length;
  });
}

const money = (value, currency) =>
  new Intl.NumberFormat(undefined, {
    style: "currency", currency, maximumFractionDigits: 0,
  }).format(value);

function statBlock(label, value, tone) {
  return `<div class="stat"><span class="k">${label}</span>
          <span class="v ${tone || ""}">${value}</span></div>`;
}

function describe(route) {
  if (route.depart) return `${route.depart} to ${route["return"] || "one way"}`;
  if (route.window) {
    return `${route.window.earliest} to ${route.window.latest}, ${route.window.nights} nights`;
  }
  return "";
}

function renderRoute(container, route, rows) {
  const series = dailyMinimum(partyRows(rows, route));
  const card = document.createElement("section");
  card.className = "route";

  if (!series.length) {
    card.innerHTML = `<header><h2>${route.origin} &rarr; ${route.destination}
      <small>${route.id}</small></h2></header>
      <p class="note">No whole-party observations recorded yet.</p>`;
    container.appendChild(card);
    return;
  }

  const latest = series[series.length - 1][1];
  const cheapest = Math.min(...series.map(([, v]) => v));
  const recent = series.slice(-30);
  const average = recent.reduce((sum, [, v]) => sum + v, 0) / recent.length;
  const party = `${route.adults}a ${route.children}c ${route.infants}i`;

  card.innerHTML = `
    <header>
      <h2>${route.origin} &rarr; ${route.destination}
        <small>${describe(route)} &middot; ${party}</small></h2>
      <div class="stats">
        ${statBlock("Latest", money(latest, route.currency),
                    latest <= route.threshold_usd ? "under" : "over")}
        ${statBlock("Cheapest seen", money(cheapest, route.currency))}
        ${statBlock("30-day average", money(average, route.currency))}
        ${statBlock("Threshold", money(route.threshold_usd, route.currency))}
      </div>
    </header>
    <div class="chart"><canvas></canvas></div>
    <p class="note">${series.length} day(s) of history &middot; tracking since ${series[0][0]}</p>`;
  container.appendChild(card);

  const ink = getComputedStyle(document.body).getPropertyValue("--text").trim();
  const grid = getComputedStyle(document.body).getPropertyValue("--line").trim();

  new Chart(card.querySelector("canvas"), {
    type: "line",
    data: {
      labels: series.map(([day]) => day),
      datasets: [
        {
          label: "Cheapest that day",
          data: series.map(([, price]) => price),
          borderColor: "#5aa9e6",
          backgroundColor: "rgba(90,169,230,.12)",
          fill: true, tension: .25, pointRadius: 2, borderWidth: 2,
        },
        {
          label: "7-day average",
          data: movingAverage(series, 7),
          borderColor: "#96a0ad",
          borderWidth: 1.5, borderDash: [4, 3], pointRadius: 0, fill: false,
        },
        {
          label: "Threshold",
          data: series.map(() => route.threshold_usd),
          borderColor: "#e6a34a",
          borderWidth: 1.5, borderDash: [8, 4], pointRadius: 0, fill: false,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { color: ink, boxWidth: 12, usePointStyle: true } },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${money(ctx.parsed.y, route.currency)}`,
          },
        },
      },
      scales: {
        x: { ticks: { color: ink, maxTicksLimit: 10 }, grid: { color: grid } },
        y: {
          ticks: { color: ink, callback: (v) => money(v, route.currency) },
          grid: { color: grid },
        },
      },
    },
  });
}

async function main() {
  const container = document.getElementById("routes");
  const subtitle = document.getElementById("subtitle");
  try {
    const [csv, meta] = await Promise.all([
      firstThatLoads("prices.csv"),
      firstThatLoads("routes.json"),
    ]);
    const rows = parseCsv(csv);
    const routes = JSON.parse(meta);
    if (!rows.length) {
      container.innerHTML = `<div class="empty">No prices recorded yet. The first
        <code>check-prices</code> run will populate this page.</div>`;
      subtitle.textContent = `${routes.length} route(s) configured`;
      return;
    }
    const last = rows[rows.length - 1].observed_at;
    subtitle.textContent =
      `${routes.length} route(s) · ${rows.length} observations · last checked ${last}`;
    routes.forEach((route) => renderRoute(container, route, rows));
  } catch (error) {
    container.innerHTML = `<div class="error">${error.message}</div>`;
    subtitle.textContent = "";
  }
}

main();
