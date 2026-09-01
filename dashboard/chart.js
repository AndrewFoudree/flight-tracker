/* Reads data/prices.csv, data/routes.json and data/runs.csv at load and renders
   one chart per route. No build step: the price workflow commits those files
   weekly, and Pages republishes them. Every one of them has to be staged into
   _site/data by the deploy step, or it 404s here and the page quietly loses
   whatever it carried -- runs.csv is what draws the NA gaps. */

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
   embedded commas are possible in booking_url and in fare_notes. */
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
  const mine = runs.filter((r) => r.route_id === routeId);
  // A day is only blind if nothing succeeded on it. A route can run twice in a
  // day -- an NA when the allowance ran short, then an ok on a re-run -- and
  // counting the failure alone reports a day with data as a day without.
  const answered = new Set(
    mine.filter((r) => r.status === "ok").map((r) => r.observed_at.slice(0, 10))
  );
  return new Set(
    mine
      .filter((r) => r.status !== "ok")
      .map((r) => r.observed_at.slice(0, 10))
      .filter((day) => !answered.has(day))
  );
}

function mergeSeries(priced, naDays) {
  const byDay = new Map(priced);
  for (const day of naDays) if (!byDay.has(day)) byDay.set(day, null);
  return [...byDay.entries()].sort((a, b) => a[0].localeCompare(b[0]));
}

function esc(text) {
  return String(text === null || text === undefined ? "" : text).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

/* Google's search response carries no fare-brand field -- the Booking Options
   endpoint has one and costs a search per itinerary -- so the attribute strings
   it does return are the only free signal. Deliberately narrow: "Checked
   baggage for a fee" is normal on a main-cabin fare, and flagging that would
   make the column mean nothing. */
const RESTRICTED = /basic economy|carry-on bag not included|no carry-on/i;
/* What a live response actually returns on these routes. Not a Basic Economy
   marker, but for seven people a checked-bag fee each way is real money, so it
   is worth surfacing rather than collapsing into "no flag". */
const BAG_FEE = /checked baggage for a fee/i;

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

/* Everything observed on the most recent day we checked, plus what the same
   departure cost on the pull before it. The chart answers "is this route moving";
   this answers "which departure moved", which is the one you act on. */
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

  // departure -> day -> cheapest party fare, so the previous pull is a lookup
  // rather than a rescan. Keyed by day because a run writes several rows.
  const partyHistory = new Map();
  for (const row of mine) {
    if (!isParty(row)) continue;
    const price = Number(row.total_price);
    if (!Number.isFinite(price)) continue;
    if (!partyHistory.has(row.depart_date)) partyHistory.set(row.depart_date, new Map());
    const days = partyHistory.get(row.depart_date);
    const seen = row.observed_at.slice(0, 10);
    if (!days.has(seen) || price < days.get(seen)) days.set(seen, price);
  }

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
      // Belongs to this fare, not to the departure: a cheaper quote found later
      // has its own search URL and its own attributes.
      entry.booking_url = row.booking_url;
      entry.fare_notes = row.fare_notes;
    }
    if (isSingle(row) && (entry.single === undefined || price < entry.single)) {
      entry.single = price;
    }
  }
  // The previous pull is the last day before this one that priced this exact
  // departure -- not simply the day before, which may have been an NA or may
  // never have covered this date at all.
  for (const entry of byDeparture.values()) {
    const days = partyHistory.get(entry.depart);
    if (!days) continue;
    const earlier = [...days.keys()].filter((d) => d < day).sort().pop();
    if (earlier === undefined) continue;
    entry.prev = days.get(earlier);
    entry.prevDay = earlier;
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
      <h3>Most recent pull &middot; ${patternLabel(route)}</h3>
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
  let anyMove = false;
  let anyBasic = false;
  let anyBagFee = false;

  const body = pull.departures.map((d) => {
    const split = d.single === undefined ? null : d.single * seats;
    const spread = split === null ? null : d.party - split;
    // A party fare well above N single fares means the cheap bucket no longer
    // holds N seats. That leads the price, so it is the number worth watching.
    const wide = spread !== null && spread > d.party * 0.01;
    if (wide) anyWide = true;
    const stops =
      d.stops === null ? "&mdash;" : d.stops === 0 ? "nonstop" : `${d.stops} stop${d.stops > 1 ? "s" : ""}`;
    const move = d.prev === undefined ? null : d.party - d.prev;
    if (move !== null) anyMove = true;
    const notes = d.fare_notes || "";
    const basic = RESTRICTED.test(notes);
    const bagFee = !basic && BAG_FEE.test(notes);
    if (basic) anyBasic = true;
    if (bagFee) anyBagFee = true;
    const depart = fmtDay(d.depart);
    // The stored URL is the six-seat search that produced this fare, so the
    // link lands on the party price rather than the one-adult one Google shows
    // by default.
    const departCell = d.booking_url
      ? `<a href="${esc(d.booking_url)}" target="_blank" rel="noopener noreferrer">${depart}</a>`
      : depart;
    const classes = [];
    if (d.party <= route.threshold_usd) classes.push("beats");
    if (d.party === cheapest) classes.push("best");
    return `<tr class="${classes.join(" ")}">
      <td>${departCell}</td>
      <td>${d.return_date ? fmtDay(d.return_date) : "one way"}</td>
      <td>${money(d.party, route.currency)}</td>
      <td class="${move === null || move === 0 ? "" : move < 0 ? "down" : "up"}">${
        move === null ? "&mdash;" : move === 0 ? "no change"
          : (move > 0 ? "+" : "") + money(move, route.currency)
      }</td>
      <td>${split === null ? "&mdash;" : money(split, route.currency)}</td>
      <td class="${wide ? "wide" : ""}">${
        spread === null ? "&mdash;" : (spread >= 0 ? "+" : "") + money(spread, route.currency)
      }</td>
      <td>${d.carrier || "&mdash;"}</td>
      <td>${stops}</td>
      <td class="${basic ? "up" : ""}"${notes ? ` title="${esc(notes)}"` : ""}>${
        basic ? "Basic?" : bagFee ? "Bags $" : notes ? "&middot;&middot;&middot;" : "&mdash;"
      }</td>
    </tr>`;
  }).join("");

  const section = document.createElement("div");
  section.className = "pull";
  section.innerHTML = `
    <h3>${newest && newest.status !== "ok" ? "Last successful pull" : "Most recent pull"} &middot; ${patternLabel(route)}</h3>
    <p class="when">Checked ${fmtDay(pull.day)} &middot; ${pull.departures.length} departure(s) priced${
      anyMove ? "" : " &middot; first pull for these departures, so nothing to compare against yet"
    }</p>
    <div class="scroll"><table>
      <thead><tr>
        <th>Depart</th><th>Return</th><th>Party fare</th><th>Since last pull</th>
        <th>${seats} &times; single</th><th>Spread</th><th>Carrier</th><th>Stops</th>
        <th>Fare</th>
      </tr></thead>
      <tbody>${body}</tbody>
    </table></div>
    ${anyBasic
      ? `<p class="legend warn">A fare marked <strong>Basic?</strong> carries an
         attribute like "carry-on bag not included". Google publishes no fare
         brand here, so this is inference from what it does say &mdash; check the
         booking page before assuming six seats together and bags included.</p>`
      : anyBagFee
      ? `<p class="legend"><strong>Bags $</strong> means Google states a checked-bag
         fee on this fare. No Basic Economy attribute appeared, which is not proof
         it is a main-cabin fare, only that Google did not say so. Hover for the
         exact wording. At ${seats} seats a checked bag each way is the difference
         between fares this close together.</p>`
      : ""}
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

/* One card per origin-destination-month, not per route. Saturday and Thursday
   are two ways of buying the same trip, and reading them off two charts on two
   axes is how you miss that one is $250 cheaper. They stay separate series
   rather than one merged line: a Sat->Sat 7-night fare and a Thu->Tue 5-night
   fare are different products, and averaging them would invent a price nobody
   was ever quoted. */
const SERIES_COLORS = ["#5aa9e6", "#7bc47f"];

function patternLabel(route) {
  if (!route.window) return route.depart ? `${route.depart} departure` : route.id;
  const [y, m, d] = route.window.earliest.split("-").map(Number);
  const weekday = new Date(Date.UTC(y, m - 1, d))
    .toLocaleDateString(undefined, { weekday: "long", timeZone: "UTC" });
  return `${weekday} departures, ${route.window.nights} nights`;
}

/* Month of the first departure, so the two weekday patterns of one trip land
   together without needing a naming convention in the route ids. */
function groupKey(route) {
  const anchor = route.window ? route.window.earliest : route.depart;
  return `${route.origin}|${route.destination}|${anchor.slice(0, 7)}`;
}

function groupRoutes(routes) {
  const groups = new Map();
  for (const route of routes) {
    const key = groupKey(route);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(route);
  }
  return [...groups.values()];
}

function groupWindow(routes) {
  const edges = (pick) => routes.map(pick).filter(Boolean).sort();
  const first = edges((r) => (r.window ? r.window.earliest : r.depart))[0];
  const last = edges((r) => (r.window ? r.window.latest : r.depart)).pop();
  return first === last ? first : `${first} to ${last}`;
}

function renderGroup(container, routes, rows, runs) {
  const lead = routes[0];
  const card = document.createElement("section");
  card.className = "route";
  const party = `${lead.adults}a ${lead.children}c ${lead.infants}i`;

  const tracks = routes.map((route) => {
    const priced = dailyMinimum(partyRows(rows, route));
    const naDays = naDaysFor(runs, route.id);
    return { route, priced, naDays, series: mergeSeries(priced, naDays) };
  });
  const withData = tracks.filter((t) => t.priced.length);

  if (!withData.length) {
    card.innerHTML = `<header><h2>${lead.origin} &rarr; ${lead.destination}
      <small>${groupWindow(routes)} &middot; ${party}</small></h2></header>
      <p class="note">No whole-party observations recorded yet.</p>`;
    container.appendChild(card);
    for (const t of tracks) renderLatestPull(card, t.route, rows, runs);
    return;
  }

  // Every day any pattern in the group was observed, so both share one x axis
  // and a point from an earlier pull sits beside a later one instead of being
  // stranded on a chart of its own.
  const labels = [...new Set(tracks.flatMap((t) => t.series.map(([day]) => day)))].sort();
  const observed = withData.flatMap((t) => t.series.filter(([, v]) => v !== null));
  const newestDay = observed.map(([day]) => day).sort().pop();
  const latest = Math.min(...observed.filter(([day]) => day === newestDay).map(([, v]) => v));
  const cheapest = Math.min(...observed.map(([, v]) => v));
  const recentDays = new Set([...new Set(observed.map(([day]) => day))].sort().slice(-30));
  const recent = observed.filter(([day]) => recentDays.has(day));
  const average = recent.reduce((sum, [, v]) => sum + v, 0) / recent.length;
  const naCount = tracks.reduce((n, t) => n + t.naDays.size, 0);
  const beats = latest <= lead.threshold_usd;
  if (beats) card.classList.add("beats");

  card.innerHTML = `
    <header>
      <h2>${lead.origin} &rarr; ${lead.destination}
        <small>${groupWindow(routes)} &middot; ${party}</small></h2>
      <div class="stats">
        ${statBlock("Latest", money(latest, lead.currency), beats ? "under" : "over")}
        ${statBlock("Cheapest seen", money(cheapest, lead.currency))}
        ${statBlock("30-day average", money(average, lead.currency))}
        ${statBlock("Threshold", money(lead.threshold_usd, lead.currency))}
      </div>
    </header>
    ${beats
      ? `<p class="hit">Under the threshold &mdash; ${money(lead.threshold_usd - latest, lead.currency)}
         below the ${money(lead.threshold_usd, lead.currency)} bar.</p>`
      : ""}
    <div class="chart"><canvas></canvas></div>
    <p class="note">${labels.length} day(s) of history &middot; tracking since ${labels[0]}${
      naCount ? ` &middot; <span class="na">${naCount} route-day(s) with no data</span>` : ""
    }</p>`;
  container.appendChild(card);

  const ink = getComputedStyle(document.body).getPropertyValue("--text").trim();
  const grid = getComputedStyle(document.body).getPropertyValue("--line").trim();

  for (const t of tracks) renderLatestPull(card, t.route, rows, runs);

  const datasets = withData.map((t, i) => ({
    label: patternLabel(t.route),
    data: labels.map((day) => {
      const hit = t.series.find(([d]) => d === day);
      return hit ? hit[1] : null;
    }),
    spanGaps: false,                      // a blind day breaks the line
    borderColor: SERIES_COLORS[i % SERIES_COLORS.length],
    backgroundColor: "transparent",
    fill: false, tension: .25, pointRadius: 3, borderWidth: 2,
  }));
  // A trend line only means something against a single series. With two it is
  // four lines on one chart and reads as noise.
  if (withData.length === 1) {
    datasets.push({
      label: "7-day average",
      data: movingAverage(withData[0].series, 7),
      borderColor: "#96a0ad",
      borderWidth: 1.5, borderDash: [4, 3], pointRadius: 0, fill: false,
    });
  }
  datasets.push({
    label: "Threshold",
    data: labels.map(() => lead.threshold_usd),
    borderColor: "#e6a34a",
    borderWidth: 1.5, borderDash: [8, 4], pointRadius: 0, fill: false,
  });

  new Chart(card.querySelector("canvas"), {
    type: "line",
    data: { labels, datasets },
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
                : `${ctx.dataset.label}: ${money(ctx.parsed.y, lead.currency)}`,
          },
        },
      },
      scales: {
        x: { ticks: { color: ink, maxTicksLimit: 10 }, grid: { color: grid } },
        y: {
          ticks: { color: ink, callback: (v) => money(v, lead.currency) },
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
    groupRoutes(routes).forEach((group) => renderGroup(container, group, rows, runs));
  } catch (error) {
    container.innerHTML = `<div class="error">${error.message}</div>`;
    subtitle.textContent = "";
  }
}

main();
