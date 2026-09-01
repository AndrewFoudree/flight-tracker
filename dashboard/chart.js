/* Reads data/prices.csv and data/routes.json at load and renders one chart per
   route. No build step: the price workflow commits both files weekly, and Pages
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
  return series.map(([, value], i) => {
    if (value === null) return null;                 // no data, no average
    const slice = series
      .slice(Math.max(0, i - window + 1), i + 1)
      .filter(([, v]) => v !== null);
    return slice.length ? slice.reduce((sum, [, v]) => sum + v, 0) / slice.length : null;
  });
}

/* Days the tracker ran but got nothing. Drawn as gaps rather than dropped, so a
   blind stretch is visible instead of looking like flat prices. */
function naDaysFor(runs, routeId) {
  return new Set(
    runs
      .filter((r) => r.route_id === routeId && r.status !== "ok")
      .map((r) => r.observed_at.slice(0, 10))
  );
}

function mergeSeries(priced, naDays) {
  const byDay = new Map(priced);
  for (const day of naDays) if (!byDay.has(day)) byDay.set(day, null);
  return [...byDay.entries()].sort((a, b) => a[0].localeCompare(b[0]));
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

/* Everything observed on the most recent day we checked. */
function latestPull(rows, route) {
  const mine = rows.filter((r) => r.route_id === route.id);
  if (!mine.length) return null;
  const day = mine.map((r) => r.observed_at.slice(0, 10)).sort().pop();
  const todays = mine.filter((r) => r.observed_at.startsWith(day));

  const isParty = (r) =>
    Number(r.adults) === route.adults &&
    Number(r.children) === route.children &&
    Number(r.infants) === route.infants;
  const isSingle = (r) =>
    Number(r.adults) === 1 && Number(r.children) === 0 && Number(r.infants) === 0;

  const byDeparture = new Map();
  for (const row of todays) {
    const key = row.depart_date;
    if (!byDeparture.has(key)) {
      byDeparture.set(key, { depart: key, return_date: row.return_date });
    }
    const entry = byDeparture.get(key);
    const price = Number(row.total_price);
    if (!Number.isFinite(price)) continue;
    if (isParty(row) && (entry.party === undefined || price < entry.party)) {
      entry.party = price;
      entry.carrier = row.carrier;
      entry.stops = row.stops === "" ? null : Number(row.stops);
    }
    if (isSingle(row) && (entry.single === undefined || price < entry.single)) {
      entry.single = price;
    }
  }
  const departures = [...byDeparture.values()]
    .filter((e) => e.party !== undefined)
    .sort((a, b) => a.depart.localeCompare(b.depart));
  return departures.length ? { day, departures } : null;
}

function renderLatestPull(card, route, rows, runs) {
  const mine = runs.filter((r) => r.route_id === route.id);
  const newest = mine.length ? mine[mine.length - 1] : null;
  const pull = latestPull(rows, route);

  if (newest && newest.status !== "ok") {
    const section = document.createElement("div");
    section.className = "pull";
    section.innerHTML = `
      <h3>Most recent pull</h3>
      <p class="when">Checked ${fmtDay(newest.observed_at.slice(0, 10))}</p>
      <p class="na-box"><strong>NA &mdash; no data returned.</strong> ${
        newest.note || newest.status
      }.${
        pull ? ` Last successful pull was ${fmtDay(pull.day)}; its figures are below.` : ""
      }</p>`;
    card.appendChild(section);
  }
  if (!pull) return;

  const seats = route.adults + route.children;          // a lap infant buys no seat
  const cheapest = Math.min(...pull.departures.map((d) => d.party));
  let anyWide = false;

  const body = pull.departures.map((d) => {
    const split = d.single === undefined ? null : d.single * seats;
    const spread = split === null ? null : d.party - split;
    // A party fare well above N single fares means the cheap bucket no longer
    // holds N seats. That leads the price, so it is the number worth watching.
    const wide = spread !== null && spread > d.party * 0.01;
    if (wide) anyWide = true;
    const stops =
      d.stops === null ? "&mdash;" : d.stops === 0 ? "nonstop" : `${d.stops} stop${d.stops > 1 ? "s" : ""}`;
    return `<tr class="${d.party === cheapest ? "best" : ""}">
      <td>${fmtDay(d.depart)}</td>
      <td>${d.return_date ? fmtDay(d.return_date) : "one way"}</td>
      <td>${money(d.party, route.currency)}</td>
      <td>${split === null ? "&mdash;" : money(split, route.currency)}</td>
      <td class="${wide ? "wide" : ""}">${
        spread === null ? "&mdash;" : (spread >= 0 ? "+" : "") + money(spread, route.currency)
      }</td>
      <td>${d.carrier || "&mdash;"}</td>
      <td>${stops}</td>
    </tr>`;
  }).join("");

  const section = document.createElement("div");
  section.className = "pull";
  section.innerHTML = `
    <h3>${newest && newest.status !== "ok" ? "Last successful pull" : "Most recent pull"}</h3>
    <p class="when">Checked ${fmtDay(pull.day)} &middot; ${pull.departures.length} departure(s) priced</p>
    <div class="scroll"><table>
      <thead><tr>
        <th>Depart</th><th>Return</th><th>Party fare</th>
        <th>${seats} &times; single</th><th>Spread</th><th>Carrier</th><th>Stops</th>
      </tr></thead>
      <tbody>${body}</tbody>
    </table></div>
    <p class="legend">${
      anyWide
        ? "A wide spread means the cheap fare bucket no longer holds all " + seats +
          " seats, so the group is being priced up a tier. Cheap inventory is draining."
        : "Spreads are near zero, so the cheap fare buckets still hold all " + seats + " seats."
    }</p>`;
  card.appendChild(section);
}

function fmtDay(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString(undefined, {
    weekday: "short", day: "2-digit", month: "short", timeZone: "UTC",
  });
}

function renderRoute(container, route, rows, runs) {
  const priced = dailyMinimum(partyRows(rows, route));
  const naDays = naDaysFor(runs, route.id);
  const series = mergeSeries(priced, naDays);
  const card = document.createElement("section");
  card.className = "route";

  if (!priced.length) {
    card.innerHTML = `<header><h2>${route.origin} &rarr; ${route.destination}
      <small>${route.id}</small></h2></header>
      <p class="note">No whole-party observations recorded yet.</p>`;
    container.appendChild(card);
    return;
  }

  const values = series.filter(([, v]) => v !== null);
  const latest = values[values.length - 1][1];
  const cheapest = Math.min(...values.map(([, v]) => v));
  const recent = values.slice(-30);
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
    <p class="note">${values.length} day(s) of history &middot; tracking since ${series[0][0]}${
      naDays.size ? ` &middot; <span class="na">${naDays.size} day(s) with no data</span>` : ""
    }</p>`;
  container.appendChild(card);

  const ink = getComputedStyle(document.body).getPropertyValue("--text").trim();
  const grid = getComputedStyle(document.body).getPropertyValue("--line").trim();

  renderLatestPull(card, route, rows, runs);

  new Chart(card.querySelector("canvas"), {
    type: "line",
    data: {
      labels: series.map(([day]) => day),
      datasets: [
        {
          label: "Cheapest that day",
          data: series.map(([, price]) => price),
          spanGaps: false,                    // a blind day breaks the line
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
            label: (ctx) =>
              ctx.parsed.y === null || ctx.parsed.y === undefined
                ? `${ctx.dataset.label}: no data`
                : `${ctx.dataset.label}: ${money(ctx.parsed.y, route.currency)}`,
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
    // Optional: absent until the first run that records an outcome.
    let runs = [];
    try {
      runs = parseCsv(await firstThatLoads("runs.csv"));
    } catch (_) { /* no run log yet */ }
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
    routes.forEach((route) => renderRoute(container, route, rows, runs));
  } catch (error) {
    container.innerHTML = `<div class="error">${error.message}</div>`;
    subtitle.textContent = "";
  }
}

main();
