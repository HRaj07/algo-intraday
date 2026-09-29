with open("engine/risk_manager.py", "r") as f:
    text = f.read()

replacement = """
        if ticker in positions:
            return False, "already holding this name"

        today = str(now.date())
        entries_today = len([
            t for t in self.state.get("trade_history", [])
            if t["ticker"] == ticker and str(t.get("entry_time", "")).startswith(today)
        ])
        if entries_today >= 2:
            return False, f"max 2 entries per day for {ticker}"

        if len(positions) >= RISK["max_concurrent_positions"]:
"""

text = text.replace("""
        if ticker in positions:
            return False, "already holding this name"

        if len(positions) >= RISK["max_concurrent_positions"]:
""", replacement)

with open("engine/risk_manager.py", "w") as f:
    f.write(text)
