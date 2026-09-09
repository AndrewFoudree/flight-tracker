# Flight price tracker

A GitHub Actions cron job that queries flight prices weekly, stores the history in
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

The live config spends 32 searches a run. A month can hold five Sundays, so the
worst case is 160 against the 230 spendable - sized to fit every month rather
than the average one. 28 of the 32 go to January, the window actually being
bought. The 70 spare is deliberate: one-way legs, a day either side of each
target date and further origins all cost searches, and each is added on its own
rather than all at once. `tests/test_config.py` reads the cron out of the workflow
and asserts this, so adding a route or changing the cadence fails a test instead
of quietly overrunning the plan mid-cycle.

**A manual sweep is spending the weekly runs' allowance.** This is the one that
has actually bitten: on 2026-08-31 a year survey took 192 of the plan's 250
searches, and the cycle never recovered - 2026-09-06 recorded NA on all six
routes with 6 searches left against a reserve of 20. The sweep was well inside
its own `--max-searches` cap, because that cap only limits what the sweep asks
for and says nothing about what is left or what the scheduled runs still need.

`src/budget.py` is the check that was missing. Every manual entry point calls
`affordable()` before spending: it reads the balance from SerpAPI, subtracts
`--reserve-runs` weekly runs plus `budget.reserve`, and refuses if the sweep does
not fit in what is left. At the live 32 a run and the default 4 reserved runs,
148 searches are protected, so a manual sweep can never take a fresh cycle below
four funded weekly runs. If the balance cannot be read at all it refuses rather
than guessing: unknown is not the same as fine.

Two things multiply the cost, and both are opt-in:

- **`compare_split_booking: true`** doubles a route's SerpAPI cost (party query
  plus single-adult probe). Enable it only on routes you are seriously considering.
- **Flexible windows** cost one SerpAPI search per step through the window. A
  30-day window at the default 7-day step is 6 searches per run. Widen
  `window_step_days` to trade resolution for allowance: a 10-day step covers the
  same month in 4.

**Travelpayouts is thin on regional US routes.** Its cheap-fares endpoints are
built from real Aviasales user searches, so a route nobody searches returns an
empty array. Verified on DSM-DEN and DSM-STT for 2027 departures: no cached fares
at all. It costs nothing to leave enabled in case the cache fills closer to
departure, but do not plan a route around it without checking first.

**Confirmed on 2026-09-08, and it is the routes.** The `Travelpayouts coverage
probe` workflow (`src/tp_probe.py`) prices a control route with known traffic
beside the tracked ones, near-term beside the tracked window, in four query
shapes, with and without an explicit `market`. Three runs settled it:

| | near-term | tracked window |
|---|---|---|
| MOW-LED (control) | 16 fares | 14 fares |
| JFK-LAX (reference) | 17 fares | 6 fares |
| DSM-STT | 0 | 0 |
| DSM-SJU | 0 | 0 |

All on the live query, unchanged. The account, the token, the endpoint and the
query shape are all fine; `market` and every alternative shape changed nothing.
The cache reaches 2027 and holds dense US routes. It has nothing for these two,
and no change on our side will produce any.

**So Travelpayouts is not a fallback for this trip.** Leave it enabled -- it is
free, unmetered, and may fill closer to departure -- but when SerpAPI runs out
the tracker is blind, not degraded. That is what makes the budget guard above the
thing actually protecting the schedule.

One process note worth keeping: the first two runs used DSM-DEN as the control
and it returned nothing either, which read as "our query is broken" and sent two
rounds of work after a bug that did not exist. A control route is only a control
if you are certain it has traffic.

The probe is manual, spends no SerpAPI search, and is therefore safe to run
during a blind stretch. Findings go to `data/source_probe.json` and render as a
coverage panel on the dashboard. Fares it finds are deliberately **not** written
to `prices.csv`: a cached one-adult fare for a reference route is evidence about
an API, not a price for a tracked trip. Fares Travelpayouts returns for a
*tracked* route need nothing extra -- they arrive through the weekly run and
appear in the single-adult columns of the pull table.

Travelpayouts is unmetered as far as this tracker is concerned and is never
budget-limited.

## What the market charges

Every series on the dashboard is SerpAPI, so on its own the page can say a fare
is the lowest *we have seen* and nothing more. Five weeks of history and one
survey cannot answer the question that actually decides a booking: is $2,955 a
good price for DSM-STT, or does that market normally clear lower?

`src/fare_baseline.py` answers it from the US DOT's Airline Origin and
Destination Survey (DB1B) - a 10% sample of tickets actually sold, public domain,
no key, published quarterly. Run it when a new quarter lands:

```bash
python -m src.fare_baseline --year 2025 --quarter 1
```

The download is ~90MB and the CSV inside is ~1.8GB, so it is streamed from the
zip and never extracted; the zip is cached in `.cache/` and gitignored. Only the
summary reaches the repo, in `data/fare_baseline.json`. It reads the city pairs
straight from `config/routes.yaml` rather than through `load_config`, because a
baseline of what a market charges is public data about two airports and has no
business requiring the private `PARTY` split to build.

**2025 Q1, against the tracked floors:**

| | sampled tickets | p10 | median | p90 | our floor |
|---|---|---|---|---|---|
| DSM-STT | 201 | $498 | $791 | $1,462 | $493 - **10th pct** |
| DSM-SJU | 409 | $335 | $642 | $1,158 | $466 - **33rd pct** |

Round-trip equivalents per person. The STT floor is genuinely a bottom-decile
fare; the SJU floor is not. That matters, because the $2,700 SJU threshold was
calibrated to nothing - the README said so - and this is the first evidence that
it sits looser than STT's.

**It found a carrier the weekly run cannot see.** SerpAPI reports what Google
chooses to show; DB1B reports who passengers were actually ticketed on. Frontier
carried roughly 400 passengers on DSM-SJU in Q1 2025, about 9% of the market, and
has never once appeared in 617 tracker quotes. That is the single finding the
*Budget carriers and the two-ticket question* section below could not have
reached from this pipeline alone, and it is the reason the panel flags unseen
carriers at all. Carriers under 1% of a market are dropped as interline noise, so
the flag stays worth reading.

**It is a baseline, not a quote.** DB1B lags a quarter or two, has no month field
(so January is pooled with February and March), prorates round trips into two
directional records, and counts individually sold tickets rather than six seats
in one bucket. Like the coverage probe, none of it is written to `prices.csv` and
none of it reaches the charts: an average of last year's tickets is evidence
about a market, not a price anyone was offered for this trip.

## Two destinations

St. Thomas and San Juan are both tracked. The dashboard renders them
identically; nothing there is destination-aware.

Parity is not free. Both destinations at full coverage - party fare plus
single-adult probe on every departure of both weekday patterns in both windows -
is 68 searches a run and 340 a month against a 230 cap. It does not fit, and the
interesting question is what to give up.

**Two cuts, in this order.** First the single-adult probe, before any departure
date: finding a cheap date needs breadth, and every date the party fare is
checked on is a chance to catch the floor, while the probe answers a
slower-moving question that transfers across nearby dates. It survives only on
the January Saturday routes, where a booking is likeliest.

Then the shoulder window itself. April/May is not being bought yet - it exists so
a January price can be judged genuinely low rather than merely low for January.
That needs a baseline, not a search, so it is one representative midweek
departure a month per destination, Apr 29 and May 13, four searches a run
between them. Apr 22 was dropped as an outlier: $3,711 to St. Thomas against
$2,955 on the other three April dates. Full resolution comes back when this
window becomes the primary one in December.

That leaves 32 a run: 28 on January, 4 on the shoulder baseline.

Two things about San Juan specifically:

- **No survey has run on it.** Everything in *How the tracked month was chosen*
  below is St. Thomas data. The January and April/May windows were inherited so
  the two destinations are comparable, not because SJU was shown to be cheapest
  then. Worth a `Survey a year of fares` run on SJU once the plan renews.
- **It wins as a destination, not as a connection.** San Juan priced $159 to $249
  under St. Thomas on 2026-09-01. That gap belongs to San Juan as the place you
  are going: routing through SJU to reach St. Thomas adds around $1,000 in
  interisland hops for a group this size, so it would have to be $1,000 cheaper
  before it broke even. The Puerto Rico to St. Thomas ferry is not an escape
  from that - it has not operated in years.
- **It has no `absolute_below` rule.** The $2,800 bar is calibrated to St.
  Thomas's $2,955 floor. San Juan is a far larger airport with mainland capacity
  St. Thomas does not have, so that bar would likely either fire every week or
  never. `lowest_in_days` and `percent_drop` carry the route until a few weeks of
  history show where its floor actually sits, then the absolute rule goes in.

## Budget carriers and the two-ticket question

No low-cost carrier has ever appeared in this data. Across 1,993 quotes the only
carriers are American, United, Delta and Southwest. That is not a filter on our
side: `src/fetchers/serpapi.py` sends economy `travel_class` and `deep_search`
with no carrier restriction and no stops cap.

Two reasons, and neither is fixable by adding an API key:

- **Spirit no longer exists.** It ceased operations on 2 May 2026. It was also
  the only carrier flying FLL-STT nonstop, which is part of why Caribbean fares
  sit where they do.
- **Frontier serves DSM and serves SJU, but not as one network.** Low-cost
  carriers do not interline, so there is no through-itinerary for Google Flights
  to return. Their fares can only reach this trip as two separate tickets.

The aggregators that solve this are closed or blind to it. Kiwi's Tequila API is
the right tool - virtual interlining across 800+ carriers with a missed-connection
guarantee - and it closed self-serve access in 2024. Amadeus Self-Service excludes
low-cost carriers outright. Duffel carries Frontier via Travelport with
self-serve signup and free search inside a 1500:1 search-to-book ratio, which a
tracker that never books has no bookings to divide into.

So `src/hub_survey.py` answers the question on the key we already have, by
pricing each leg separately - origin to hub, hub to destination - and comparing
the total against the through-fare already in `prices.csv`:

```
Actions -> Hub survey (two tickets) -> Run workflow
```

Three hubs across four departures is 24 searches. It is **manual only and never
scheduled**, and it writes to `data/hub_survey.csv` rather than `prices.csv`: a
two-ticket itinerary is a different product from a through-fare and must not land
in the same series or the same chart. The weekly tracker stays single-ticket.

It also refuses to run if the spend would starve the scheduled runs, through the
shared guard in `src/budget.py` described under *Budget* above. `--reserve-runs`
(default 4) keeps that many weekly runs funded, and the check reads the balance
from SerpAPI rather than the local ledger, because the ledger counts calendar
months while the plan bills on its own renewal date. If the balance cannot be
read at all, it refuses rather than guessing.

Read the saving as a ceiling. It excludes checked bags for the whole party on a
carrier that prices them separately, assumes the legs connect on the day, and
carries no protection whatsoever if the first ticket runs late. Two tickets is
two contracts.

## Groups with children: what this handles

### Passenger counts

Adults, children, and infants are passed as separate parameters in a single query.
The tracker never queries for one passenger and multiplies, because that answer is
wrong for the reason below.

The split itself is not in this repo. `defaults.passengers.from_env` names an
environment variable holding `adults,children,infants` -- a repository variable
in Actions -- and only the seat count is ever written to `prices.csv`, published
in `routes.json`, or shown on the dashboard. A public price tracker has no reason
to also publish who is in a household. A missing variable fails the run rather
than quietly pricing a different party.

```yaml
defaults:
  passengers:
    from_env: PARTY        # "2,4,1" style, set in Actions -> Variables
```

### Fare buckets and split booking

Airlines sell seats in priced inventory buckets. A search for six passengers only
returns fares where six seats exist in the *same* bucket. If there are two seats
at $200 and four at $300, the group search quotes 6 x $300. Booking individually can
capture the cheaper seats.

Observed live on 2026-09-01: DSM-SJU on 28 January quoted $466 a seat for one
passenger on a 1-stop American routing, but the six-seat search returned $511 a
seat for the same 1-stop itinerary and found $466 only two stops away. Same day,
same route, different flight.

With `compare_split_booking: true` the tracker runs a second single-adult query and
stores both, then compares the party fare against booking each traveller
separately.

The comparison counts **seats, not people**. A lap infant does not buy a seat, so
a group of seven travellers occupies six. Multiplying a single fare by the head
count would invent a fare nobody pays and hide a real saving:

```
six seats plus a lap infant, single fare $348

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
    from_env: PARTY
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

## How the tracked month was chosen

Not guessed. `src/survey.py` swept 90 departure dates across Sep 2026 - Jul 2027
for this exact route and party, then ranked the months by the median cheapest
fare per day. Ranking on the *minimum* is useless here: $2,955 total ($492 a
seat) is a hard floor that nearly every month touches at least once. The median
is what tells you how likely any given date is to be cheap.

| Month | n | Median | Mean | Days at the floor |
|---|---|---|---|---|
| **Jan 2027** | 12 | **$2,970** | **$3,058** | 6/12 |
| Apr 2027 | 11 | $2,985 | $3,145 | 5/11 |
| Mar 2027 | 15 | $2,985 | $3,195 | 7/15 |
| Oct 2026 | 12 | $3,075 | $3,176 | 5/12 |
| May 2027 | 12 | $3,120 | $3,240 | 3/12 |
| Jun 2027 | 12 | $3,150 | $3,375 | 2/12 |
| Feb 2027 | 10 | $3,165 | $3,176 | 2/10 |
| Nov 2026 | 12 | $3,165 | $3,788 | 3/12 |
| Dec 2026 | 12 | $3,201 | $3,889 | 1/12 |

Three findings worth keeping:

- **General "cheapest month" advice did not hold.** It points at Sep-Nov low
  season. September was the *worst* month here ($3,705 median, never at the
  floor) and November ranked eighth. That advice is about the destination and
  assumes one or two travellers from a major hub.
- **Low season is worse for a large party.** Airlines thin capacity when demand
  softens, and six seats must come from one fare bucket. Reduced capacity hurts
  more than soft demand helps.
- **Day of week moves the price more than the month does.** March 16 was $2,955
  and March 14, the same week, was $3,327 - a $372 gap on weekday alone. An
  early sweep sampled only Tuesdays (a 14-day step from a Tuesday never leaves
  Tuesday) and was worthless for comparison until re-run at a 3-day step, which
  rotates through every weekday.
- **The Saturday turnaround is a January effect, not a general one.** Caribbean
  leisure capacity runs Sat -> Sat, which should deepen the cheap bucket. Pooled
  over the whole year it does not: Saturday's median is $3,282 with 4 of 16 days
  at the floor, second worst after Friday, while Monday and Tuesday both sit at
  $2,955. In January, where every date was sampled, it flips - Saturday is the
  best day of the month at 4 of 5 on the floor, against Thursday's 3 of 4. Both
  patterns are tracked rather than one being picked, because the shoulder window
  has almost no Saturday evidence either way (one sample, May 1, off the floor).

| Departure day | n | Median | At the floor | January: at the floor |
|---|---|---|---|---|
| Mon | 18 | $2,955 | 10/18 | 2/4 |
| Tue | 30 | $2,955 | 17/30 | 2/4 |
| Wed | 17 | $2,985 | 1/17 | 1/4 |
| Thu | 18 | $3,201 | 7/18 | 3/4 |
| Fri | 17 | $3,165 | 0/17 | 0/5 |
| Sat | 16 | $3,282 | 4/16 | **4/5** |
| Sun | 18 | $3,267 | 4/18 | 2/5 |

**April and May are tracked as a second window.** April ranked level with
January on the median ($2,985, 5 of 11 sampled days at the floor) and May fifth
($3,120, 3 of 12). The tracker prices the last two Thursdays in April and the
first two in May, on the same shape as the January route - 5 nights, party fare
plus single-adult probe - so the two windows stay directly comparable week to
week, and a Saturday variant of each window tests the turnaround pattern below.
Both windows are priced to St. Thomas and to San Juan; see **Two destinations**
for what that costs and what was traded to afford it.

Trip length was swept the same way, across every January departure:

| Nights | Cheapest |
|---|---|
| 3 | $3,423 |
| 4 to 9 | $2,955 |

$2,955 is a fare-bucket floor, not a coincidence: it recurs across six trip
lengths and roughly 550 quotes. The classic minimum-stay penalty exists but only
below four nights. Above that, length is free, which is why the Saturday
routes run 7 nights (Sat -> Sat) at no premium over the Thursday routes' 5. Day
of week does not discriminate either once a date sits on the floor - Saturday,
Thursday and Tuesday January floor dates price identically. What it changes is
how often a date reaches the floor at all, which is the table above.

Re-run the survey when plans change:

```
Actions -> Survey a year of fares -> Run workflow
```

It refuses if the sweep would leave fewer than `reserve_runs` weekly runs funded,
so a survey can no longer blind the tracker for the rest of a cycle. Lower
`reserve_runs` deliberately if a sweep matters more than the next few Sundays.

## Data

`data/prices.csv` is append-only, one row per route per source per run:

```
observed_at,route_id,source,origin,destination,depart_date,return_date,adults,children,infants,total_price,currency,carrier,stops,booking_url,fare_notes
```

History is never rewritten. A correction goes in as a new row with a later
timestamp. `observed_at` is always UTC.

Single-adult split-booking probes live in the same file, distinguished by their
`adults`/`children`/`infants` columns. Analysis and the dashboard filter them out;
a probe at a seventh of the party price would otherwise win every "lowest ever"
comparison forever.

Two smaller files sit alongside it:

- `data/usage.csv` - API searches consumed, for the budget guard.
- `data/source_probe.json` - secondary-source coverage, written by the probe.
- `data/fare_baseline.json` - what the market charged, written by the fare baseline.
- `fare_notes` holds Google's own attribute strings for a fare, such as
  `Carry-on bag not included`. The search response has no fare-brand field -- the
  Booking Options endpoint has one and costs a search per itinerary -- so these
  strings are the only free signal that a quote is Basic Economy. The dashboard
  flags a fare as `Basic?` on that basis and shows the raw text on hover, because
  it is an inference and should be checkable.
- `data/routes.json` - current route metadata, rewritten each run so the dashboard
  can read `prices.csv` without a build step.

At a few tens of thousands of rows, switch to SQLite committed as a binary blob.
At one route a week that is years away.

## Dashboard

`dashboard/` is one HTML file and one JS file. The weekly price run deploys it
itself, as its second job, so the published page always matches the data that run
committed. `publish-dashboard.yml` covers hand edits to `dashboard/`.

That split is deliberate: **a push made with `GITHUB_TOKEN` does not trigger other
workflows.** GitHub suppresses those events to prevent recursion, so the bot's
weekly data commit can never fire a separate deploy workflow. Wiring the deploy
into the same run is what keeps the page from silently freezing while history
accumulates behind it. The page fetches the CSV at load and parses it
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

### API landscape, as of September 2026

| Source | Status | Free allowance | Notes |
|---|---|---|---|
| SerpAPI Google Flights | Open | ~250 searches/month | Scrapes Google Flights, returns JSON. Primary source. |
| Travelpayouts | Open, affiliate signup | Generous | Cheap-fares endpoints. Works, but holds no cache for DSM-STT or DSM-SJU: see *Budget*. |
| US DOT DB1B | Open, public domain | Unlimited | Historical, not bookable. The only genuinely independent source: see *What the market charges*. |
| Amadeus Self-Service | Closed | n/a | Portal decommissioned 17 July 2026, keys deactivated. No free tier under Enterprise. |
| Kiwi Tequila | Invitation only | n/a | Closed to self-serve signup May 2024. |
| Duffel | Application required | Test mode only | Sandbox returns synthetic fares, so it cannot baseline a real route. |
| FlightAPI.io | Paid | 20 calls, one off | A trial, not a tier: one weekly run is 32. |

**Ruled out, so they stop being re-searched.** AviationStack, Aviation Edge,
FlightLabs and OpenSky have generous free tiers and no fares at all - they track
aircraft, not prices. Unofficial RapidAPI mirrors of Skyscanner and Kiwi are
scrapers carrying the same terms-of-service exposure as scraping Google directly.

**On scraping something other than Google.** The four objections above are
properties of the category, not of Google: changing the target changes the logo.
Southwest is a carrier in this data, has sued both Kiwi.com and Skiplagged over
fare scraping, and won a preliminary injunction on a *breach of contract* theory
that does not require commercial scale. The practical objection is decisive on
its own: a DOM scraper breaks silently, which for a price tracker means quietly
missing the drop it exists to catch. SerpAPI is a scraper whose legal exposure
and maintenance someone else carries. That is the product.

**Travelpayouts prices one adult.** Its v3 prices endpoints return cached
single-adult fares and accept no passenger parameters, so this tracker records them
as `adults=1` quotes and never as a party total. Multiplying a single fare by the
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
  activity for 60 days. The weekly commit counts as activity, so this never trips.

## Disclaimer

Prices are observations, not offers. Fares move between the check and your booking.
Verify on the airline site before paying.
