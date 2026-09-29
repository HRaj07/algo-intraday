with open("config.py", "r") as f:
    text = f.read()

# Make stops tighter (0.5% floor instead of 1.2%, 1x ATR instead of 2x ATR)
text = text.replace('"stop_atr_mult": 2.0,', '"stop_atr_mult": 1.0,')
text = text.replace('"stop_pct_floor": 0.012,', '"stop_pct_floor": 0.005,')

with open("config.py", "w") as f:
    f.write(text)

with open("strategies/momentum_v3.py", "r") as f:
    strat = f.read()

# Change target from 1.5R to 1.0R
strat = strat.replace('t1 = ref + (1.5 * R)', 't1 = ref + (1.0 * R)')

with open("strategies/momentum_v3.py", "w") as f:
    f.write(strat)
