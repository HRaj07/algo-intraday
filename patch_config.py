with open('config.py', 'r') as f:
    text = f.read()

# 1. Revert risk
text = text.replace('"risk_pct_per_trade": 0.002,        # 0.2% (lowered from 0.4% to reduce margin/risk)', 
                    '"risk_pct_per_trade": 0.004,        # 0.4% = Rs2,000 at Rs5L, and it shrinks')

# 2. Enable use_target in MOMENTUM block
text = text.replace('"use_target": False,', '"use_target": True,')

with open('config.py', 'w') as f:
    f.write(text)
