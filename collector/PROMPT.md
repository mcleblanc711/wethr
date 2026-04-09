# Polymarket Market Research — Structural Edge Discovery

## Objective
Find active Polymarket markets where:
1. **Outcome is externally determined** — no single actor can meaningfully influence resolution (weather, economic data releases, astronomical events, official statistics, sports outcomes, etc.)
2. **Conditional structure exists** — markets where "if X happens, then Y almost certainly follows" or where one market's resolution mechanically constrains another's
3. **Resolution source is verifiable and unambiguous** — clear resolution criteria tied to a specific data source (e.g., BLS releases CPI, WMO confirms temperature, official election results)

## Research Process

### Phase 1: Market Discovery
Search Polymarket's active markets via the Gamma API:
- `GET https://gamma-api.polymarket.com/events?active=true&closed=false&limit=100&offset=0`
- Paginate through all active events
- For each event, fetch its markets: look at `markets` array in event response
- Catalog: event title, market question, current prices, volume, resolution source, end date

### Phase 2: Structural Filtering
For each market, evaluate and score (1-5) on:

| Criterion | What to assess |
|---|---|
| **Exogeneity** | Can any single person/small group determine the outcome? Score 5 = pure external (solar eclipses, sea surface temperature). Score 1 = single decision-maker (will X person do Y?) |
| **Necessity/Sufficiency** | Does the market have logical dependencies on observable preconditions? E.g., "Fed cuts 50bp" is *sufficient* for "Fed cuts at least 25bp" to resolve YES. Look for these chains. |
| **Resolution clarity** | Is the resolution source specific, public, and historically reliable? Beware markets resolved by "Polymarket discretion" or ambiguous criteria. |
| **Forecastability** | Can the outcome be modeled with publicly available data? (ensemble forecasts, futures prices, polling aggregates, base rates from historical data) |
| **Liquidity** | Is there enough volume/depth to actually trade? Thin markets = slippage death. |
| **Time horizon** | When does it resolve? Prefer markets resolving within 1-90 days — long enough to build a position, short enough for capital efficiency. |

### Phase 3: Dependency Mapping
This is the high-value part. Look for:
- **Vertical chains**: Market A resolving YES *mechanically implies* Market B resolves YES (e.g., "CPI > 3.5%" YES → "CPI > 3.0%" YES)
- **Horizontal clusters**: Markets on the same underlying event with bracket structures (temperature brackets, rate decision brackets, vote share brackets) where probabilities must sum to ~100%
- **Cross-event conditionals**: Different events where one outcome strongly predicts another based on causal or statistical relationships (e.g., strong jobs report → Fed hold probability increases)
- **Arbitrage-adjacent**: Cases where the sum of YES prices across mutually exclusive outcomes ≠ 100¢, or where conditional relationships create synthetic positions

### Phase 4: Signal Source Mapping
For each promising market cluster, identify:
- What **free data source** provides the best probability estimate (NOAA ensembles, CME FedWatch, FRED, polling aggregates, historical base rates)
- What **lead time** you get between signal availability and market resolution
- What **known biases** exist in the data source vs. the resolution source

## Output Format

For each market cluster found, produce:
