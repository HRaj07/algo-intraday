import json
with open('logs/paper_state_v2.json', 'r') as f:
    state = json.load(f)

for ticker in ['SUNTV.NS', 'WELCORP.NS']:
    if ticker in state['positions']:
        state['positions'][ticker]['exit_cfg']['use_target'] = True

with open('logs/paper_state_v2.json', 'w') as f:
    json.dump(state, f, indent=2)
print("Fixed use_target for active trades.")
