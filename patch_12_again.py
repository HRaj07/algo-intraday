import json

# 1. Update params_live.json - stop loss to 1.2%
with open('params_live.json', 'r') as f:
    params = json.load(f)
params['params']['stop_pct_floor'] = 0.012
with open('params_live.json', 'w') as f:
    json.dump(params, f, indent=2)

# 2. Update open trades target and stop to 1.2%
with open('logs/paper_state_v2.json', 'r') as f:
    state = json.load(f)
for ticker, pos in state['positions'].items():
    entry = pos['entry_price']
    t1 = round(entry * 1.012, 2)
    sl = round(entry * 0.988, 2)
    pos['t1'] = t1
    pos['stop_loss'] = sl
    pos['initial_stop'] = sl
with open('logs/paper_state_v2.json', 'w') as f:
    json.dump(state, f, indent=2)

# 3. Update strategy to 1.2% target
with open("strategies/momentum_v3.py", "r") as f:
    text = f.read()
text = text.replace('t1 = ref * 1.007', 't1 = ref * 1.012')
with open("strategies/momentum_v3.py", "w") as f:
    f.write(text)
