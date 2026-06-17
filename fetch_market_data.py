#!/usr/bin/env python3
"""
RCG Market Data Fetcher
=======================
Fetches live freight market data from free public APIs and writes
data/market.json — run daily by GitHub Actions at 6 AM ET.

APIs used (both free, no rate limits for daily calls):
  • EIA Open Data  — U.S. retail diesel price (weekly)
  • FRED           — Federal Reserve Economic Data (transport indices)

Keys required (set as GitHub Secrets):
  EIA_API_KEY   — https://www.eia.gov/opendata/register.php
  FRED_API_KEY  — https://fred.stlouisfed.org/docs/api/api_key.html

Author: RCG / Claude
"""

import os
import json
import datetime
import urllib.request
import urllib.error

# ── Config ────────────────────────────────────────────────────────────────────
EIA_KEY  = os.environ.get('EIA_API_KEY',  'YOUR_EIA_KEY_HERE')
FRED_KEY = os.environ.get('FRED_API_KEY', 'YOUR_FRED_KEY_HERE')

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), 'data', 'market.json')

# EIA series: EMD_EPD2D_PTE_NUS_DPG = US retail diesel, all grades, $/gallon
EIA_SERIES  = 'EMD_EPD2D_PTE_NUS_DPG'

# FRED series used:
#   DCOILWTICO   — WTI crude oil (proxy for oil cost pressure)
#   TSIFRGHT     — Cass Freight Shipment Index (demand proxy, monthly)
FRED_DIESEL  = 'DCOILWTICO'    # Daily crude — we use as directional context
FRED_FREIGHT = 'TSIFRGHT'      # Monthly freight volume index

# ── Helpers ───────────────────────────────────────────────────────────────────
def fetch_json(url, label):
    """Fetch a URL and return parsed JSON, or None on error."""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'RCG-DataBot/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            print(f'  ✓ {label}')
            return data
    except urllib.error.HTTPError as e:
        print(f'  ✗ {label} — HTTP {e.code}')
    except Exception as e:
        print(f'  ✗ {label} — {e}')
    return None


def calc_fsc(diesel_price):
    """
    Calculate fuel surcharge % from retail diesel price.
    Uses a simplified ATA-style sliding scale.
    Returns float rounded to 1 decimal, or None.
    """
    try:
        d = float(diesel_price)
        if d < 2.00:
            return 0.0
        raw = ((d - 2.00) / 0.06) * 1.0
        return round(min(raw, 50.0), 1)
    except (TypeError, ValueError):
        return None


def week_period(iso_date_str):
    """Return 'Mon DD – Mon DD' for the 7-day window starting on iso_date_str."""
    try:
        d = datetime.date.fromisoformat(iso_date_str)
        end = d + datetime.timedelta(days=6)
        return f"{d.strftime('%b %-d')} – {end.strftime('%b %-d')}"
    except Exception:
        return ''


# ── EIA Diesel ────────────────────────────────────────────────────────────────
def get_eia_diesel():
    """
    Returns dict: { price: float, price_prev: float, date: str, date_prev: str }
    """
    url = (
        f'https://api.eia.gov/v2/petroleum/pri/gnd/data/'
        f'?api_key={EIA_KEY}'
        f'&frequency=weekly'
        f'&data[0]=value'
        f'&facets[series][]={EIA_SERIES}'
        f'&sort[0][column]=period'
        f'&sort[0][direction]=desc'
        f'&length=4'
        f'&offset=0'
    )
    raw = fetch_json(url, 'EIA diesel price')
    if not raw:
        return None

    try:
        rows = raw['response']['data']
        # rows are sorted desc — [0] = latest, [1] = previous week
        latest   = rows[0]
        previous = rows[1] if len(rows) > 1 else rows[0]
        return {
            'price':      float(latest['value']),
            'price_prev': float(previous['value']),
            'date':       latest['period'],       # 'YYYY-MM-DD'
            'date_prev':  previous['period'],
        }
    except (KeyError, IndexError, TypeError, ValueError) as e:
        print(f'  ✗ EIA parse error: {e}')
        return None


# ── FRED ──────────────────────────────────────────────────────────────────────
def get_fred_series(series_id, label, n=2):
    """
    Returns list of { date, value } dicts, most recent first.
    """
    url = (
        f'https://api.stlouisfed.org/fred/series/observations'
        f'?series_id={series_id}'
        f'&api_key={FRED_KEY}'
        f'&file_type=json'
        f'&sort_order=desc'
        f'&limit={n}'
    )
    raw = fetch_json(url, f'FRED {label} ({series_id})')
    if not raw:
        return None

    try:
        obs = raw['observations']
        results = []
        for o in obs:
            try:
                results.append({
                    'date':  o['date'],
                    'value': float(o['value']),
                })
            except (KeyError, ValueError):
                pass  # skip missing values
        return results if results else None
    except (KeyError, TypeError) as e:
        print(f'  ✗ FRED {series_id} parse error: {e}')
        return None


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    today = datetime.date.today().isoformat()
    now   = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

    print(f'\n[RCG Data Fetch] {now}')
    print('─' * 48)

    # 1. EIA diesel
    print('\nFetching EIA diesel price...')
    diesel_data = get_eia_diesel()

    # 2. FRED crude (directional context only)
    print('\nFetching FRED crude oil...')
    crude_obs = get_fred_series(FRED_DIESEL, 'WTI Crude', n=2)

    # 3. FRED freight index
    print('\nFetching FRED freight index...')
    freight_obs = get_fred_series(FRED_FREIGHT, 'Freight Index', n=2)

    # ── Build output ──────────────────────────────────────────────────────────
    result = {
        'fetched_at': now,
        'date':       today,   # fallback date; overwritten by EIA date below
    }

    # Diesel — live from EIA
    if diesel_data:
        price      = diesel_data['price']
        price_prev = diesel_data['price_prev']
        fsc        = calc_fsc(price)
        fsc_prev   = calc_fsc(price_prev)

        result.update({
            'date':             diesel_data['date'],
            'date_prev':        diesel_data['date_prev'],
            'diesel_price':     round(price, 3),
            'diesel_price_prev':round(price_prev, 3),
            'diesel_delta':     round(price - price_prev, 3),
            'diesel_trend':     'rising' if price > price_prev else
                                'falling' if price < price_prev else 'stable',
            'fsc_pct':          fsc,
            'fsc_pct_prev':     fsc_prev,
            'fsc_delta':        round((fsc or 0) - (fsc_prev or 0), 1),
            'fsc_period':       week_period(diesel_data['date']),
        })
        print(f'\n  Diesel: ${price:.3f}/gal  ({result["diesel_trend"]})')
        print(f'  FSC:    {fsc}%')
    else:
        print('\n  ⚠ EIA diesel unavailable — using fallback values')
        result.update({
            'diesel_price':      3.84,
            'diesel_price_prev': 3.84,
            'diesel_delta':      0.0,
            'diesel_trend':      'stable',
            'fsc_pct':           30.7,
            'fsc_pct_prev':      30.7,
            'fsc_delta':         0.0,
            'fsc_period':        '',
        })

    # Crude oil (FRED) — directional context, shown in extended dashboard
    if crude_obs and len(crude_obs) >= 1:
        result['crude_wti']       = crude_obs[0]['value']
        result['crude_wti_date']  = crude_obs[0]['date']
        if len(crude_obs) >= 2:
            result['crude_wti_prev'] = crude_obs[1]['value']
            result['crude_wti_delta'] = round(crude_obs[0]['value'] - crude_obs[1]['value'], 2)

    # Freight index (FRED) — monthly, shows demand direction
    if freight_obs and len(freight_obs) >= 1:
        result['freight_index']      = freight_obs[0]['value']
        result['freight_index_date'] = freight_obs[0]['date']
        if len(freight_obs) >= 2:
            result['freight_index_prev']  = freight_obs[1]['value']
            result['freight_index_delta'] = round(
                freight_obs[0]['value'] - freight_obs[1]['value'], 2)

    # ── Write output ──────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2)

    print(f'\n✓ Written → {OUTPUT_PATH}')
    print('─' * 48)


if __name__ == '__main__':
    main()
