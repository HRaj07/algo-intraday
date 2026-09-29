import json
with open('logs/paper_state_v2.json', 'r') as f:
    state = json.load(f)

for ticker, pos in state['positions'].items():
    entry = pos['entry_price']
    t1 = round(entry * 1.005, 2)
    pos['t1'] = t1
    pos['t1_fraction'] = 1.0
    pos['exit_cfg']['use_target'] = True
    print(f"{ticker}: entry=₹{entry:.2f}, new T1=₹{t1:.2f}")

with open('logs/paper_state_v2.json', 'w') as f:
    json.dump(state, f, indent=2)
