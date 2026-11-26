# alpha-catalyst-eval

`alpha-catalyst-eval` scans public-market catalysts, records both deterministic
playbook and semantic classifications, and evaluates closed signals against
SPY. Its CLI entry point is `alpha_engine.py`; the original `ideaengine.py`
remains available as a compatible entry point.

## Setup

Use Python 3.9 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Set `EXA_API_KEY` to enable Exa semantic extraction. Without the key, or with
`--no-exa`, the engine uses a local structured fallback. Set `SEC_USER_AGENT`
to a contact-identifying User-Agent for direct SEC filing requests. Finnhub,
Alpha Vantage, and Polygon remain optional and are enabled with
`FINNHUB_API_KEY`, `ALPHAVANTAGE_API_KEY`, and `POLYGON_API_KEY`.

The CLI loads a project-root `.env` file automatically without replacing
variables already exported in the shell. Start from `.env.example`, add your
own key locally, and keep `.env` out of version control.

## Run

```bash
python alpha_engine.py --scan
python alpha_engine.py --scan --limit 20
python alpha_engine.py --scan --no-exa --scan-only
python alpha_engine.py --evaluate
python alpha_engine.py --scan --refresh
```

With no action flag, the CLI scans and then evaluates. `--scan-only` suppresses
that automatic evaluation. `--refresh` bypasses cached Exa responses; otherwise
all Exa calls use `.alpha_cache.json` with a 24-hour TTL and atomic replacement.

## Architecture

- **Ingestion:** SEC EDGAR 8-K filings, targeted and general RSS feeds, and
  optional Finnhub, Alpha Vantage, and Polygon APIs are preserved. SEC filing
  documents are fetched directly with an identifying User-Agent. News URLs can
  be summarized with Exa highlights, requested with `text=False`. Existing
  yfinance price/volume validation remains part of signal screening.
- **Rule vs. semantic extraction:** `check_all_playbooks()` remains the
  baseline rule signal. Exa structured output (or the local fallback) records
  `rule_direction`, `semantic_direction`, and `agreement` independently.
  The ledger records whether each semantic result came from Exa or the local
  fallback so the report does not mislabel fallback measurements as Exa results.
  The local fallback intentionally reuses the event playbook direction for
  most catalysts, so agreement is expected in `--no-exa` mode; use Exa-backed
  scans for a more independent comparison. Even then, agreement alone is not
  an accuracy measure—compare both against closed outcomes.
  The single-keyword fallback uses exact whole-phrase matches; fuzzy similarity
  is intentionally not used to create directional signals.
  Explicit negations such as “CEO rules out guidance cut” are neutralized
  before they can become bearish signals.
- **Ticker resolution:** Full company names and unique short aliases are
  indexed longest-first, so longer names such as “General Motors” win before
  shorter overlapping names.
- **Trading alignment:** Entries use the next eligible NYSE market open when
  the scan is after 09:30 ET or on a non-trading day. Exits are aligned to the
  configured number of NYSE trading sessions later. Timestamps are stored as
  Singapore-local naive values for spreadsheet compatibility.
- **Closed-loop evaluation:** `signals_ledger.csv` is the primary atomic
  and only signal ledger. Due OPEN rows are evaluated from daily open prices
  for the ticker and SPY over the same
  dates. A legacy scheduled entry or exit that falls on a non-trading day is
  moved forward to the next NYSE session; the actual dates and adjustment
  reason are recorded separately from the original schedule. Failed price
  downloads or missing bars on the adjusted exact dates become
  `UNVERIFIABLE`, include a reason in `evaluation_note`, and are excluded from
  later retries and performance totals. To retry one manually, change its
  `status` back to `OPEN` in the CSV. Each evaluation run writes
  `performance_report.md` with closed-signal performance, classifier agreement,
  OPEN/UNVERIFIABLE counts, and date adjustments or unevaluable-row reasons.
  Returns are stored both as an unflipped `raw_stock_return_pct` and as the
  directional `signal_return_pct`. Market-neutral alpha is stock minus SPY for
  bullish signals, and SPY minus stock for bearish signals. Neutral signals
  have no directional alpha; their absolute stock/SPY relative move is tagged
  `NOISE_CONFIRMED` at 2% or less, otherwise `MATERIAL_SURPRISE`.

## Validation

Run the focused regression suite with:

```bash
python -m unittest discover -s tests -v
```

Financial signal evaluation is an empirical benchmark, not investment advice
or a guarantee of future performance.
