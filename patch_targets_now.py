import json
with open('logs/paper_state_v2.json', 'r') as f:
    state = json.load(f)

if 'SUNTV.NS' in state['positions']:
    state['positions']['SUNTV.NS']['t1'] = 541.0
    state['positions']['SUNTV.NS']['t1_fraction'] = 1.0

if 'WELCORP.NS' in state['positions']:
    state['positions']['WELCORP.NS']['t1'] = 2842.0
    state['positions']['WELCORP.NS']['t1_fraction'] = 1.0

with open('logs/paper_state_v2.json', 'w') as f:
    json.dump(state, f, indent=2)
print("Updated active trades to small quick targets.")
