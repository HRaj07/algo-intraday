import json
with open('logs/paper_state_v2.json', 'r') as f:
    state = json.load(f)

if 'SUNTV.NS' in state['positions']:
    state['positions']['SUNTV.NS']['stop_loss'] = 536.50
    state['positions']['SUNTV.NS']['initial_stop'] = 536.50

if 'WELCORP.NS' in state['positions']:
    state['positions']['WELCORP.NS']['stop_loss'] = 2820.00
    state['positions']['WELCORP.NS']['initial_stop'] = 2820.00

with open('logs/paper_state_v2.json', 'w') as f:
    json.dump(state, f, indent=2)
print("Updated active trades to tight stop losses.")
