import json
with open('logs/paper_state_v2.json', 'r') as f:
    state = json.load(f)

if 'SUNTV.NS' in state['positions']:
    pos = state['positions']['SUNTV.NS']
    pos['t1'] = 539.50
    pos['t2'] = 539.50
    pos['exit_cfg']['use_target'] = True
    pos['t1_fraction'] = 1.0  # sell 100% at T1

with open('logs/paper_state_v2.json', 'w') as f:
    json.dump(state, f, indent=2)
print("Updated SUNTV trade target to 539.50")
