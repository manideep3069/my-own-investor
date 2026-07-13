"""Market view toggle (US $ / India ₹) and live USD→INR conversion.

Every stored money value is USD (IBKR account currency). The sidebar toggle only
changes *display*: in India mode amounts are converted at the latest USD→INR rate
and rendered with Indian digit grouping (lakh/crore). Pure formatting helpers stay
Streamlit-free so they can be unit-tested.
"""

from __future__ import annotations

import httpx
import streamlit as st

# Last-resort rate if every FX source is down; the sidebar flags it as stale.
FALLBACK_RATE = 95.0

US, IN = "US", "IN"


# --------------------------------------------------------------------------- #
# Pure formatting (no Streamlit)
# --------------------------------------------------------------------------- #
def indian_group(value: float, decimals: int = 0) -> str:
    """Format with Indian digit grouping: 1234567.8 → '12,34,567.8'."""
    neg = value < 0
    txt = f"{abs(value):.{decimals}f}"
    whole, _, frac = txt.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join([*parts, tail])
    out = whole + (f".{frac}" if frac else "")
    return ("-" if neg else "") + out


def format_money(usd: float, market_code: str, usd_inr: float, decimals: int = 0) -> str:
    sign = "-" if usd < 0 else ""
    if market_code == IN:
        return f"{sign}₹{indian_group(abs(usd) * usd_inr, decimals)}"
    return f"{sign}${abs(usd):,.{decimals}f}"


def format_money_compact(usd: float, market_code: str, usd_inr: float) -> str:
    """Large amounts: '$1.2M' or '₹1.2 Cr' (falls back to lakh below one crore)."""
    if market_code == IN:
        inr = usd * usd_inr
        if abs(inr) >= 1e7:
            return f"₹{inr / 1e7:,.1f} Cr"
        return f"₹{inr / 1e5:,.1f} L"
    return f"${usd / 1e6:,.1f}M"


# --------------------------------------------------------------------------- #
# Live FX
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_usd_inr() -> tuple[float, str] | None:
    """Latest USD→INR as (rate, source note); None if every source fails."""
    try:
        r = httpx.get("https://open.er-api.com/v6/latest/USD", timeout=6)
        r.raise_for_status()
        data = r.json()
        if data.get("result") == "success":
            return float(data["rates"]["INR"]), f"updated {data['time_last_update_utc'][:16]}"
    except Exception:  # any network/parse failure → try the next source
        pass
    try:
        r = httpx.get("https://api.frankfurter.dev/v1/latest?base=USD&symbols=INR", timeout=6)
        r.raise_for_status()
        data = r.json()
        return float(data["rates"]["INR"]), f"ECB, {data['date']}"
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Streamlit state + widget
# --------------------------------------------------------------------------- #
def market() -> str:
    return st.session_state.get("market", US)


def usd_inr() -> float:
    return st.session_state.get("usd_inr", FALLBACK_RATE)


def symbol() -> str:
    return "₹" if market() == IN else "$"


def conv(usd: float) -> float:
    """Numeric USD → display-currency conversion (for chart axes)."""
    return usd * usd_inr() if market() == IN else usd


def money(usd: float, decimals: int = 0) -> str:
    return format_money(usd, market(), usd_inr(), decimals)


def money_compact(usd: float) -> str:
    return format_money_compact(usd, market(), usd_inr())


def market_widget() -> None:
    """Sidebar market toggle + live USD→INR rate readout."""
    choice = st.sidebar.radio(
        "Market view", ["🇺🇸 US ($)", "🇮🇳 India (₹)"], horizontal=True, key="market_choice"
    )
    st.session_state["market"] = IN if "India" in choice else US

    fx = fetch_usd_inr()
    if fx:
        rate, note = fx
        st.session_state["usd_inr"] = rate
        st.sidebar.caption(f"1 USD = ₹{rate:.2f} · {note}")
        if market() == IN and st.sidebar.button("↻ Refresh rate"):
            fetch_usd_inr.clear()
            st.rerun()
    else:
        st.session_state["usd_inr"] = FALLBACK_RATE
        if market() == IN:
            st.sidebar.warning(f"Live FX unavailable — using stale ₹{FALLBACK_RATE:.0f}/$.")
