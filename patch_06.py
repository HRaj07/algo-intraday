import json

# 1. Update params_live.json - stop loss to 0.6%
with open('params_live.json', 'r') as f:
    params = json.load(f)
params['params']['stop_pct_floor'] = 0.006
with open('params_live.json', 'w') as f:
    json.dump(params, f, indent=2)
print("params_live.json updated: stop_pct_floor = 0.006")

# 2. Update open trades target to 0.6%
with open('logs/paper_state_v2.json', 'r') as f:
    state = json.load(f)
for ticker, pos in state['positions'].items():
    entry = pos['entry_price']
    t1 = round(entry * 1.006, 2)
    pos['t1'] = t1
    pos['t1_fraction'] = 1.0
    pos['exit_cfg']['use_target'] = True
    print(f"{ticker}: entry=₹{entry:.2f}, new T1=₹{t1:.2f}")
with open('logs/paper_state_v2.json', 'w') as f:
    json.dump(state, f, indent=2)
