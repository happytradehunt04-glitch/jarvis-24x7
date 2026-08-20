def analyse(sym, name):
    df = get_df(sym)
    if df is None: return None, None, "WAIT"
    close = df['Close']; price = float(close.iloc[-1])
    ema50 = close.ewm(50).mean(); ema200 = close.ewm(200).mean()
    delta = close.diff(); gain = delta.where(delta>0,0).rolling(14).mean(); loss = -delta.where(delta<0,0).rolling(14).mean(); rsi = 100 - (100/(1+gain/loss))
    rsi_val = float(rsi.iloc[-1]); e50 = float(ema50.iloc[-1]); e200 = float(ema200.iloc[-1])
    asian = close.iloc[-32:-8]; ah = float(asian.max()); al = float(asian.min())
    
    # --- NEWS FILTER (Simple) ---
    is_news_time = datetime.now().weekday() == 4 and 18 <= datetime.now().hour <= 19 # Friday 6-7 PM NFP time

    bias="WAIT"; reason=""
    if is_news_time:
        reason = "🔴 NFP NEWS - WAIT karo, news ke baad entry"
    elif price > ah and e50 > e200 and rsi_val > 55:
        bias="BUY"; reason=f"✅ Asian High {ah:.2f} BREAKOUT\n✅ EMA50 {e50:.2f} > EMA200 {e200:.2f} Bullish\n✅ RSI {rsi_val:.1f} Strong (55+)\n✅ London Session Active"
    elif price < al and e50 < e200 and rsi_val < 45:
        bias="SELL"; reason=f"✅ Asian Low {al:.2f} BREAKDOWN\n✅ EMA50 {e50:.2f} < EMA200 {e200:.2f} Bearish\n✅ RSI {rsi_val:.1f} Weak (45-)\n✅ London Session Active"
    else:
        reason=f"❌ Price Asian Range me hai ({al:.2f} - {ah:.2f})\n❌ EMA Trend match nahi\n❌ RSI {rsi_val:.1f} Neutral"

    # Chart
    buf = io.BytesIO()
    fig, ax = plt.subplots(figsize=(6,3))
    ax.plot(close[-60:], color='black', linewidth=1)
    ax.plot(ema50[-60:], color='blue', linewidth=0.8, label='EMA50')
    ax.plot(ema200[-60:], color='orange', linewidth=0.8, label='EMA200')
    ax.axhline(ah, color='green', ls='--', lw=0.8, label=f'AH {ah:.0f}'); ax.axhline(al, color='red', ls='--', lw=0.8, label=f'AL {al:.0f}')
    ax.set_title(f"{name} {price:.2f} RSI {rsi_val:.1f}"); ax.legend(fontsize=6)
    plt.tight_layout(); plt.savefig(buf, format='png', dpi=130); buf.seek(0); plt.close(fig)

    if bias=="WAIT":
        msg=f"""**{name} | {price:.2f} | {bias}**

**Reason - Buy/Sell Kyu Nahi:**
{reason}

**Levels:**
Asian High: {ah:.2f}
Asian Low: {al:.2f}
EMA50: {e50:.2f} | EMA200: {e200:.2f}

**News:** {'NFP Time - Avoid' if is_news_time else 'No Major News'}
**Action:** WAIT - No Trade
"""
    else:
        sl_p=0.004; tp1_p=0.008; tp2_p=0.015
        sl=price*(1-sl_p) if bias=="BUY" else price*(1+sl_p)
        tp1=price*(1+tp1_p) if bias=="BUY" else price*(1-tp1_p)
        tp2=price*(1+tp2_p) if bias=="BUY" else price*(1-tp2_p)
        msg=f"""**🚨 {bias} {name} | {price:.2f}**

**ENTRY:** {price:.2f}
**SL:** {sl:.2f} (-0.4%)
**TP1:** {tp1:.2f} (+0.8%)
**TP2:** {tp2:.2f} (+1.5%)
**RR:** 1:2

**Buy/Sell Ka Reason:**
{reason}

**News Check:** {'⚠️ News Time - Risky' if is_news_time else '✅ No News - Safe'}

**Lot:** 0.01 for $50 (1% Risk)
**Act as:** Professional London Breakout Trader
"""
    return buf, msg, bias
