with open("strategies/momentum_v3.py", "r") as f:
    text = f.read()

replacement = """
        # Reference prices. The fill is the NEXT bar's open, which is the first
        # price an order placed now can actually get; these are for sizing and
        # for the record, and the trader recomputes the stop from the real fill.
        ref = close
        stop = ref * (1 - stop_pct)
        R = ref - stop
        t1 = ref + (1.5 * R)
        t2 = ref + (2.0 * R)

        return {
            "ticker": ticker,
            "signal": "BUY",
            "order_type": self.p["order_type"],          # MARKET
            "reference_price": round(ref, 2),
            "stop_pct": round(stop_pct * 100, 3),
            "stop_loss": round(stop, 2),
            "t1": round(t1, 2),
            "t2": round(t2, 2),
            "t1_fraction": 1.0,                          # Exit 100% at T1 for now
"""
text = text.replace("""
        # Reference prices. The fill is the NEXT bar's open, which is the first
        # price an order placed now can actually get; these are for sizing and
        # for the record, and the trader recomputes the stop from the real fill.
        ref = close
        stop = ref * (1 - stop_pct)

        return {
            "ticker": ticker,
            "signal": "BUY",
            "order_type": self.p["order_type"],          # MARKET
            "reference_price": round(ref, 2),
            "stop_pct": round(stop_pct * 100, 3),
            "stop_loss": round(stop, 2),
""", replacement)

with open("strategies/momentum_v3.py", "w") as f:
    f.write(text)
