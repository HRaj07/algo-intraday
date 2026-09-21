"""
Trading universe.

WHY THIS IS BIG NOW
-------------------
v2 shipped with 25 names, cut from v1's 39 on my judgment about liquidity and
event-risk. Replaying v1's 188 logged signals through v2's filters exposed the
problem: of the 19 that cleared the 1.22% deviation floor, 13 were in names I
had removed. The floor demands large dislocations; the names that produce them
are the volatile ones I cut. The two rules were fighting each other, and the
result was roughly 4 trades a month - 25 months to accumulate enough history to
learn anything.

The fix is not a smaller floor. It is more names, with the RUNTIME filters doing
the rejecting:

  - turnover floor (Rs2cr median per 15-min bar) rejects illiquid names daily
  - RVOL ceiling rejects the ones moving on news that day
  - gap filter rejects overnight events
  - cost hurdle rejects setups that cannot pay for themselves

Those evaluate each name on the day's actual conditions. My blanket judgment
evaluated them on a guess, made in a session where I could not even fetch their
volume data. The filters are better at this than I am.

More names does NOT mean more trades per day: the caps are still 2 concurrent
and 3 entries, and candidates are ranked by reward:risk after costs. It means a
better CHOICE among candidates, and enough trades to learn from.

TICKER ACCURACY - READ THIS
---------------------------
These symbols were written from knowledge, not verified against a live feed
(the session that produced them had no market data access). Some may be wrong:
NSE symbols get renamed after mergers and rebrands, and a few names here are
recent listings.

Run `python check_data.py` before trusting this list. It reports every ticker
that fails to fetch and every one that fails the turnover floor, so the list can
be pruned from measurement rather than from my assumptions. A ticker that does
not resolve is simply skipped by the fetcher - it degrades the universe, it does
not break the bot.
"""

# --------------------------------------------------------------------------
# Core: carried over from v1 and v2, all previously observed fetching cleanly
# --------------------------------------------------------------------------
_CORE = {
    "HDFCBANK.NS": "BANK", "ICICIBANK.NS": "BANK", "SBIN.NS": "BANK",
    "AXISBANK.NS": "BANK", "KOTAKBANK.NS": "BANK", "INDUSINDBK.NS": "BANK",
    "BAJFINANCE.NS": "NBFC", "BAJAJFINSV.NS": "NBFC", "SHRIRAMFIN.NS": "NBFC",

    "TCS.NS": "IT", "INFY.NS": "IT", "WIPRO.NS": "IT", "HCLTECH.NS": "IT",
    "TECHM.NS": "IT",

    "RELIANCE.NS": "DIVERSIFIED", "LT.NS": "INFRA", "BHARTIARTL.NS": "TELECOM",
    "ITC.NS": "FMCG", "ADANIENT.NS": "DIVERSIFIED", "TRENT.NS": "RETAIL",

    "SUNPHARMA.NS": "PHARMA", "DIVISLAB.NS": "PHARMA", "CIPLA.NS": "PHARMA",
    "DRREDDY.NS": "PHARMA",

    "MARUTI.NS": "AUTO", "HEROMOTOCO.NS": "AUTO", "BAJAJ-AUTO.NS": "AUTO",
    "EICHERMOT.NS": "AUTO", "M&M.NS": "AUTO",

    "NTPC.NS": "POWER", "POWERGRID.NS": "POWER", "ONGC.NS": "ENERGY",
    "BPCL.NS": "ENERGY",

    "TATASTEEL.NS": "METAL", "HINDALCO.NS": "METAL", "JSWSTEEL.NS": "METAL",

    "TITAN.NS": "CONSUMER", "ASIANPAINT.NS": "CONSUMER",
    "ULTRACEMCO.NS": "CEMENT",
}

# --------------------------------------------------------------------------
# Expansion: liquid NSE large and mid caps. Verify with check_data.py.
# --------------------------------------------------------------------------
_EXPANSION = {
    # Banks & financials
    "BANKBARODA.NS": "BANK", "PNB.NS": "BANK", "CANBK.NS": "BANK",
    "IDFCFIRSTB.NS": "BANK", "AUBANK.NS": "BANK", "FEDERALBNK.NS": "BANK",
    "CHOLAFIN.NS": "NBFC", "MUTHOOTFIN.NS": "NBFC", "LICHSGFIN.NS": "NBFC",
    "RECLTD.NS": "NBFC", "PFC.NS": "NBFC", "SBICARD.NS": "NBFC",
    "HDFCLIFE.NS": "INSURANCE", "SBILIFE.NS": "INSURANCE",
    "ICICIGI.NS": "INSURANCE", "ICICIPRULI.NS": "INSURANCE",
    "HDFCAMC.NS": "NBFC",

    # IT
    "LTIM.NS": "IT", "PERSISTENT.NS": "IT", "COFORGE.NS": "IT",
    "MPHASIS.NS": "IT",

    # Pharma & healthcare
    "APOLLOHOSP.NS": "PHARMA", "LUPIN.NS": "PHARMA", "AUROPHARMA.NS": "PHARMA",
    "ALKEM.NS": "PHARMA", "TORNTPHARM.NS": "PHARMA", "ZYDUSLIFE.NS": "PHARMA",
    "BIOCON.NS": "PHARMA", "GLENMARK.NS": "PHARMA", "LAURUSLABS.NS": "PHARMA",

    # Auto & components
    "TATAMOTORS.NS": "AUTO", "TVSMOTOR.NS": "AUTO", "ASHOKLEY.NS": "AUTO",
    "BHARATFORG.NS": "AUTO", "MOTHERSON.NS": "AUTO", "BALKRISIND.NS": "AUTO",
    "MRF.NS": "AUTO",

    # Energy, power & metals
    "ADANIPORTS.NS": "INFRA", "ADANIPOWER.NS": "POWER",
    "TATAPOWER.NS": "POWER", "IOC.NS": "ENERGY", "GAIL.NS": "ENERGY",
    "COALINDIA.NS": "ENERGY", "NHPC.NS": "POWER",
    "VEDL.NS": "METAL", "NMDC.NS": "METAL", "SAIL.NS": "METAL",
    "JINDALSTEL.NS": "METAL", "APLAPOLLO.NS": "METAL",

    # FMCG & consumer
    "HINDUNILVR.NS": "FMCG", "NESTLEIND.NS": "FMCG", "BRITANNIA.NS": "FMCG",
    "DABUR.NS": "FMCG", "MARICO.NS": "FMCG", "GODREJCP.NS": "FMCG",
    "COLPAL.NS": "FMCG", "TATACONSUM.NS": "FMCG", "UNITDSPR.NS": "FMCG",
    "VBL.NS": "FMCG", "DMART.NS": "RETAIL",

    # Cement & materials
    "GRASIM.NS": "CEMENT", "SHREECEM.NS": "CEMENT", "AMBUJACEM.NS": "CEMENT",
    "ACC.NS": "CEMENT", "DALBHARAT.NS": "CEMENT",
    "PIDILITE.NS": "CHEMICAL", "SRF.NS": "CHEMICAL", "PIIND.NS": "CHEMICAL",
    "UPL.NS": "CHEMICAL", "DEEPAKNTR.NS": "CHEMICAL", "TATACHEM.NS": "CHEMICAL",

    # Industrials & capital goods
    "SIEMENS.NS": "INDUSTRIAL", "ABB.NS": "INDUSTRIAL", "BEL.NS": "INDUSTRIAL",
    "HAL.NS": "INDUSTRIAL", "CUMMINSIND.NS": "INDUSTRIAL",
    "HAVELLS.NS": "INDUSTRIAL", "VOLTAS.NS": "INDUSTRIAL",
    "POLYCAB.NS": "INDUSTRIAL", "ASTRAL.NS": "INDUSTRIAL",

    # Real estate, travel, other
    "DLF.NS": "REALTY", "GODREJPROP.NS": "REALTY", "OBEROIRLTY.NS": "REALTY",
    "INDHOTEL.NS": "CONSUMER", "JUBLFOOD.NS": "CONSUMER",
    "PAGEIND.NS": "CONSUMER", "INDIGO.NS": "TRANSPORT", "IRCTC.NS": "TRANSPORT",
    "IRFC.NS": "NBFC", "BSE.NS": "FINANCIAL", "MCX.NS": "FINANCIAL",
}


# --------------------------------------------------------------------------
# Second expansion: liquid NSE mid caps, to reach ~200 names.
#
# Measured from v1's logs: ~0.0148 qualifying signals per name per day. At 200
# names that is ~3/day of candidates, which keeps the daily entry cap (5) as the
# binding constraint rather than signal scarcity. Past ~200 the universe stops
# mattering - the caps bind first.
#
# Ticker risk is highest in this block: mid caps rename and re-list more often
# than large caps. RUN check_data.py AND PRUNE. A symbol that does not resolve
# is skipped by the fetcher with a warning; it costs a fetch, not a failure.
# --------------------------------------------------------------------------
_EXPANSION_2 = {
    # Banks, NBFCs, market infrastructure
    "UNIONBANK.NS": "BANK", "INDIANB.NS": "BANK", "BANKINDIA.NS": "BANK",
    "RBLBANK.NS": "BANK", "BANDHANBNK.NS": "BANK", "CUB.NS": "BANK",
    "YESBANK.NS": "BANK", "KARURVYSYA.NS": "BANK",
    "MANAPPURAM.NS": "NBFC", "PEL.NS": "NBFC", "ABCAPITAL.NS": "NBFC",
    "POONAWALLA.NS": "NBFC", "IIFL.NS": "NBFC",
    "CDSL.NS": "FINANCIAL", "KFINTECH.NS": "FINANCIAL",
    "ANGELONE.NS": "FINANCIAL",

    # IT & tech
    "OFSS.NS": "IT", "KPITTECH.NS": "IT", "TATAELXSI.NS": "IT",
    "CYIENT.NS": "IT", "BSOFT.NS": "IT", "TATATECH.NS": "IT",
    "HAPPSTMNDS.NS": "IT", "SONACOMS.NS": "AUTO",

    # Pharma & healthcare
    "NATCOPHARM.NS": "PHARMA", "AJANTPHARM.NS": "PHARMA",
    "IPCALAB.NS": "PHARMA", "ABBOTINDIA.NS": "PHARMA",
    "GRANULES.NS": "PHARMA", "SYNGENE.NS": "PHARMA",
    "FORTIS.NS": "PHARMA", "MAXHEALTH.NS": "PHARMA",

    # Auto & components
    "EXIDEIND.NS": "AUTO", "SUNDRMFAST.NS": "AUTO", "TIINDIA.NS": "AUTO",
    "UNOMINDA.NS": "AUTO", "APOLLOTYRE.NS": "AUTO", "CEATLTD.NS": "AUTO",
    "ESCORTS.NS": "AUTO", "ENDURANCE.NS": "AUTO",

    # Metals, energy, gas
    "HINDZINC.NS": "METAL", "NATIONALUM.NS": "METAL", "HINDCOPPER.NS": "METAL",
    "MOIL.NS": "METAL", "RATNAMANI.NS": "METAL", "WELCORP.NS": "METAL",
    "OIL.NS": "ENERGY", "PETRONET.NS": "ENERGY", "IGL.NS": "ENERGY",
    "MGL.NS": "ENERGY", "GUJGASLTD.NS": "ENERGY", "CASTROLIND.NS": "ENERGY",

    # Consumer, retail, durables
    "BATAINDIA.NS": "CONSUMER", "RELAXO.NS": "CONSUMER",
    "WHIRLPOOL.NS": "CONSUMER", "BLUESTARCO.NS": "CONSUMER",
    "CROMPTON.NS": "CONSUMER", "DIXON.NS": "INDUSTRIAL",
    "AMBER.NS": "INDUSTRIAL", "KAYNES.NS": "INDUSTRIAL",
    "SYRMA.NS": "INDUSTRIAL",

    # Infra, realty, transport
    "NBCC.NS": "INFRA", "RVNL.NS": "INFRA", "IRCON.NS": "INFRA",
    "NCC.NS": "INFRA", "KEC.NS": "INFRA",
    "PRESTIGE.NS": "REALTY", "BRIGADE.NS": "REALTY", "PHOENIXLTD.NS": "REALTY",
    "SOBHA.NS": "REALTY", "DELHIVERY.NS": "TRANSPORT",

    # Chemicals & fertilisers
    "AARTIIND.NS": "CHEMICAL", "ATUL.NS": "CHEMICAL",
    "VINATIORGA.NS": "CHEMICAL", "FINEORG.NS": "CHEMICAL",
    "NAVINFLUOR.NS": "CHEMICAL", "ALKYLAMINE.NS": "CHEMICAL",
    "GNFC.NS": "CHEMICAL", "CHAMBLFERT.NS": "CHEMICAL",
    "COROMANDEL.NS": "CHEMICAL",

    # Media, internet, services
    "SUNTV.NS": "MEDIA", "PVRINOX.NS": "MEDIA", "NAUKRI.NS": "INTERNET",
    "JUSTDIAL.NS": "INTERNET",
}

SECTOR = {**_CORE, **_EXPANSION, **_EXPANSION_2}
INTRADAY_UNIVERSE = sorted(SECTOR)

# A ticker missing from SECTOR falls back to "OTHER", which shares one slot
# under the per-sector cap - deliberately conservative.
DEFAULT_SECTOR = "OTHER"


def sector_of(ticker: str) -> str:
    return SECTOR.get(ticker, DEFAULT_SECTOR)


def stats() -> dict:
    from collections import Counter
    c = Counter(SECTOR.values())
    return {"names": len(INTRADAY_UNIVERSE), "sectors": len(c),
            "largest_sector": c.most_common(1)[0]}
