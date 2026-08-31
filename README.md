# Flight price tracker

A GitHub Actions cron job that queries flight prices daily, stores the history in
this repo, alerts on threshold breaches, and publishes a static dashboard.

No servers, no hosting cost. Compute is a scheduled Actions workflow, storage is a
CSV committed back to the repo, and the dashboard is GitHub Pages. Git history
doubles as an audit log of every price change.

```
config/routes.yaml  ->  fetchers  ->  data/prices.csv  ->  analysis  ->  alert issue
                                            |
                                            +-------->  dashboard (GitHub Pages)
```

## Setup

1. **Create the repo and push.** Public is simplest: Actions minutes are unlimited.
   A private repo works too, well inside the 2000 free minutes a month.
2. **Add the secrets** under Settings > Secrets and variables > Actions:
   - `SERPAPI_KEY` from [serpapi.com](https://serpapi.com) (free plan, ~250 searches/month)
   - `TRAVELPAYOUTS_TOKEN` from [travelpayouts.com](https://www.travelpayouts.com) (affiliate signup, generous allowance)

   `GITHUB_TOKEN` is provided by Actions. Nothing to create, nothing to rotate.
3. **Enable Pages**: Settings > Pages > Source: *GitHub Actions*.
4. **Edit `config/routes.yaml`** with your routes, then run the workflow by hand:
   Actions > Check flight prices > Run workflow. Tick *dry run* first if you only
   want to check that the config parses.

Secrets never go in the config file.

## Adding a route

Adding a route is a config change, never a code change. Two shapes are supported:

```yaml
# Fixed dates
- id: dsm-stt-spring
  origin: DSM
  destination: STT
  depart: "2027-03-14"
  return: "2027-03-21"
  threshold_usd: 2800          # total for the whole party
  alert_on:
    - absolute_below: 2800
    - lowest_in_days: 30
    - percent_drop: 10
  compare_split_booking: true

# Flexible window
- id: dsm-den-flex
  origin: DSM
  destination: DEN
  depart_window: { earliest: "2027-06-01", latest: "2027-06-30" }
  nights: 7
  threshold_usd: 1900
  alert_on:
    - lowest_in_days: 45
  sources: [travelpayouts]     # see the budget note below
```

Everything under `defaults` (`passengers`, `currency`, `cabin`, `sources`,
`window_step_days`) can be overridden per route.

The config is validated with Pydantic on load and unknown keys are rejected, so a
typo in an airport code or a misspelled option fails loudly before any API call is
spent. `python -m src.main --dry-run` is the cheapest way to check a change.

### Alert rules

| Rule | Fires when |
|---|---|
| `absolute_below: N` | The party total is strictly below N. |
| `lowest_in_days: N` | The price beats every observation in the preceding N days. |
| `percent_drop: N` | The price is at least N% below the previous observation. |

`lowest_in_days` stays quiet on a route's first sighting, and whenever there is no
prior observation inside the window. With no recent baseline there is nothing to
call a low.

Comparing against your own history beats a fixed dollar threshold. The threshold
tells you what you want to pay. The history tells you whether this is actually a
good price.

## Alerting

Alerts open a GitHub issue labelled `price-alert`, and GitHub emails it to you. The
title carries the route and price, so the email subject line is self-contained. No
SMTP credentials, no app passwords, nothing to rotate.

De-duplication lives in `data/alert_state.json`, written in the same commit as the
CSV so state and history never diverge. An alert fires only when a trigger
condition is met **and** either the cooldown (7 days) has elapsed or the price has
fallen a further 5% below the last alerted price. A fare that sits below threshold
for two weeks produces two notifications, not fourteen.

`src/notify/email.py` is an optional SMTP path, off unless `SMTP_HOST` is set. Keep
it for the day alerts need to reach somewhere other than this GitHub account.

## Budget

SerpAPI's free plan is roughly 250 searches a month, about 8 a day. One route
checked once daily costs 30 a month, so four or five routes fit comfortably.

Consumption is recorded in `data/usage.csv` and the run stops before the cap, so
the limit is never discovered on the day a fare drops. `budget.reserve` keeps a
buffer in hand.

Two things multiply the cost, and both are opt-in:

- **`compare_split_booking: true`** doubles a route's SerpAPI cost (party query
  plus single-adult probe). Enable it only on routes you are seriously considering.
- **Flexible windows** cost one SerpAPI search per step through the window. A
  30-day window at the default 7-day step is 5 searches per run, which is most of
  a day's allowance. Travelpayouts covers a whole month in one unmetered call, so
  windowed routes default to it in the shipped config.

Travelpayouts is unmetered as far as this tracker is concerned and is never
budget-limited.

## Party of seven: what this handles

### Passenger counts

Adults, children, and infants are passed as separate parameters in a single query.
The tracker never queries for one passenger and multiplies, because that answer is
wrong for the reason below.

### Fare buckets and split booking

Airlines sell seats in priced inventory buckets. A search for seven passengers only
returns fares where seven seats exist in the *same* bucket. If there are two seats
at $200 and five at $300, the group search quotes 7 x $300. Booking individually can
capture the cheaper seats.

With `compare_split_booking: true` the tracker runs a second single-adult query and
stores both, then compares the party fare against booking each traveller
separately.

The comparison counts **seats, not people**. Your party of seven occupies six
seats, because the lap infant does not buy one. Multiplying a single fare by the
head count would invent a fare nobody pays and hide a real saving:

```
2 adults + 4 children + 1 lap infant, single fare $348

seats  (correct)   6 x 348 = $2,088   ->  $712 below a $2,800 party fare
people (wrong)     7 x 348 = $2,436   ->  $364, understating the saving by half
```

On international routes a lap infant is typically around 10% of the adult fare
plus taxes. Set `infant_fare_pct: 10` under `defaults`, or on the route, and the
estimate adds it. It defaults to `0`, which is correct for domestic US flights.

This is an approximation, not a quote. It flags routes worth checking by hand.

**Caveats, for future-you:**

- Split bookings risk non-adjacent seats. With four children that matters.
- Prices can move between individual bookings. The last one may not be the price
  you saw.
- Each booking is a separate reservation. If a flight is disrupted, the airline
  rebooks each reservation on its own, and the party can be split across flights.

### The infant

A child under two travels as a lap infant. Domestic US flights are typically free;
international is typically around 10% of the adult fare plus taxes. The APIs treat
this as a distinct `infants` parameter, so it stays separate from `children` in the
config rather than being folded into the count.

**Age is measured at the flight date, not the booking date.** For a trip more
than a year out, a lap infant today may well be a seated child by departure, and
that is a whole extra fare the quote would otherwise miss.

Give the tracker the birth date and it works this out per route:

```yaml
defaults:
  passengers:
    adults: 2
    children: 4
    infants: 1
    infant_birthdates: ["2025-05-20"]
```

| Trip | Age at travel | Priced as |
|---|---|---|
| STT, March 2027 | 21 months | Lap infant, 6 seats |
| DEN, June 2027 | just turned 2 | Seated child, 7 seats |

The US Virgin Islands are a US territory, so US carriers price STT as a domestic
destination: no passport for US citizens, and a lap infant travels free. Leave
`infant_fare_pct` at `0` for it.

Classification uses the **last** travel date of the itinerary, not the departure.
A child who turns two mid-trip needs a seat for the return, so the whole trip is
priced with that seat. Flexible windows use the latest possible return date, which
is the conservative choice. Each reclassification is logged with the reason.

Leave `infant_birthdates` unset and the configured counts are used as written, but
every run warns that lap-infant status is being taken on trust.

Two more things at booking time:

- Airlines verify age at check-in with a birth certificate or passport.
- A 29 February birthday has no anniversary in a non-leap year. The tracker ages
  the child out on 1 March, which errs toward buying a seat.

## Data

`data/prices.csv` is append-only, one row per route per source per run:

```
observed_at,route_id,source,origin,destination,depart_date,return_date,adults,children,infants,total_price,currency,carrier,stops,booking_url
```

History is never rewritten. A correction goes in as a new row with a later
timestamp. `observed_at` is always UTC.

Single-adult split-booking probes live in the same file, distinguished by their
`adults`/`children`/`infants` columns. Analysis and the dashboard filter them out;
a probe at a seventh of the party price would otherwise win every "lowest ever"
comparison forever.

Two smaller files sit alongside it:

- `data/usage.csv` - API searches consumed, for the budget guard.
- `data/routes.json` - current route metadata, rewritten each run so the dashboard
  can read `prices.csv` without a build step.

At a few tens of thousands of rows, switch to SQLite committed as a binary blob.
At one route a day that is years away.

## Dashboard

`dashboard/` is one HTML file and one JS file, deployed to Pages on every push that
touches `dashboard/` or `data/`. Since the price workflow pushes daily, the
dashboard refreshes itself. The page fetches the CSV at load and parses it
client-side; Chart.js comes from a CDN. No framework, no build step.

Each route gets a line chart of the cheapest observation per day, with the 7-day
average and the threshold drawn as reference lines.

Preview it locally from the repo root:

```bash
python -m http.server 8000
# then open http://localhost:8000/dashboard/
```

The page looks for `data/` beside itself first (the deployed layout) and falls back
to `../data/` (a local checkout), so the same files work in both.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
python -m pytest -q                      # ~80 tests, no network, no API spend
python -m src.main --dry-run             # re-analyse stored history
python -m src.main --route dsm-stt-spring --no-notify
```

Tests run against recorded fixtures in `tests/fixtures/`, so the suite never spends
a search.

### Adding a source

The landscape moves, and one of these will change again. Sources are pluggable:

1. Subclass `Fetcher` in `src/fetchers/`, returning `Quote` objects.
2. Register it in `src/fetchers/registry.py`.
3. Name it in `sources` in the config.

`main.py` fans out across every configured source, collects everything, and takes
the minimum per route. One source failing is caught and logged; the rest of the run
carries on.

### API landscape, as of August 2026

| Source | Status | Free allowance | Notes |
|---|---|---|---|
| SerpAPI Google Flights | Open | ~250 searches/month | Scrapes Google Flights, returns JSON. Primary source. |
| Travelpayouts | Open, affiliate signup | Generous | Cheap-fares endpoints. Good secondary source. |
| Amadeus Self-Service | Closed | n/a | Free tier discontinued July 2026. |
| Kiwi Tequila | Invitation only | n/a | No self-service access for new developers. |
| FlightAPI.io | Paid | n/a | Fallback if the free sources are outgrown. |

**Travelpayouts prices one adult.** Its v3 prices endpoints return cached
single-adult fares and accept no passenger parameters, so this tracker records them
as `adults=1` quotes and never as a party total. Multiplying a single fare by seven
is exactly the fare-bucket error described above. Treat Travelpayouts as a trend
signal and as the free half of the split-booking comparison; SerpAPI is the source
of truth for real party pricing.

**Do not scrape Google Flights directly.** Heavy JavaScript, unstable structure,
active anti-bot measures, and a terms-of-service violation. SerpAPI's allowance
exists precisely so you do not own that problem.

## Things that will bite

- **API responses change shape without notice**, especially the scraper-backed
  ones. Responses are validated on parse and fail loudly rather than writing nulls
  to the CSV.
- **Currency** is pinned in config and asserted on every response. A silent switch
  to EUR would ruin the history.
- **Timezones**: `observed_at` is UTC, always.
- **Rate limits**: consumption is tracked and the run stops before the cap.
- **Cron is not punctual.** Actions queues scheduled jobs; expect anywhere from a
  few minutes to half an hour late. Irrelevant here.
- **Sixty-day dormancy.** GitHub disables scheduled workflows in repos with no
  activity for 60 days. The daily commit counts as activity, so this never trips.

## Disclaimer

Prices are observations, not offers. Fares move between the check and your booking.
Verify on the airline site before paying.
