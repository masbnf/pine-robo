"""Estimate the value of reviving trend-cancelled setups (analysis only).

Usage:
    python tools/analyze_trend_revival.py <label> <m5_ohlc_csv>

The CSV needs time,open,high,low,close M5 rows (e.g. built from tick files
via pine_ob_bot.tick_historical.M5Builder). Replays the month with the live
candle ordering, collects every trend-cancelled BOS order, then simulates
re-arming it during later aligned-trend windows while its OB is still
uninvalidated: same-bar sweep+reclaim, close(+spread) fill, ATR-buffered
stop, fixed-RR target, SL-first candle walk. See
docs/ENTRY_FUNNEL_FINDINGS.md section 3 for the 2026-03 / 2026-06 results
(net ~+1R per two months -> revival rejected as a strategy change).
"""
import sys, csv, math
import pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from pine_ob_bot.config import BotConfig
from pine_ob_bot.paper import PaperBroker, SymbolSpec
from pine_ob_bot.pine_engine import PineSwingOBEngine
from pine_ob_bot.entry_pivot import ClassicEntryPivotDetector
from pine_ob_bot.mtf_context import M15Context
from pine_ob_bot.liquidity_context import LiquidityTracker
from pine_ob_bot.structure_context import ChochContext, DisplacementContext, displacement_snapshot
from pine_ob_bot.trend_filter import trend_from_engine_state
from pine_ob_bot.models import Candle

month=sys.argv[1]; WARMUP=500
cfg=BotConfig(trend_swing_length=9, entry_pivot_left=3, entry_pivot_right=3,
              entry_mode='sweep_reclaim', sweep_reclaim_atr_buffer=0.20, rr=1.5,
              allowed_break_kinds=("BOS",))
broker=PaperBroker(cfg,10_000.0,SymbolSpec()); broker.enable_entry_pivot_gate()
engine=PineSwingOBEngine(cfg)
pivot=ClassicEntryPivotDetector(3,3)
disp=DisplacementContext(100,20); cdisp=DisplacementContext(100,20)

candles=[]
with open(sys.argv[2]) as f:
    for r in csv.DictReader(f):
        candles.append(Candle(r['time'],float(r['open']),float(r['high']),float(r['low']),float(r['close'])))

trend_by_bar={}          # index -> trend value str
ob_dead={}               # ob_id -> bar invalidated
started=False
for candle in candles:
    index=len(engine.candles)
    broker.process_signal_candle(candle,index)
    breaks,formed,invalidated=engine.process(candle)
    broker.update_m5_trend(trend_from_engine_state(engine.trend),candle.time)
    trend_by_bar[index]=broker.current_m5_trend.value
    for p in pivot.process(candle): broker.update_entry_pivot(p)
    for ob in formed:
        dd=displacement_snapshot(candle,ob,breaks)
        rk=(disp if ob.break_kind=="BOS" else cdisp).observe(dd.get("bos_close_through_atr"))
        if index<WARMUP: continue
        pv=engine.swing_low if ob.direction=="bull" else engine.swing_high
        broker.add_ob(ob,{**dd,**rk,"formation_close":candle.close})
    for ob_id in invalidated:
        ob_dead.setdefault(ob_id,index)
        broker.cancel_ob(ob_id)
    if not started and len(engine.candles)>=WARMUP:
        broker.pending.clear(); started=True

n=len(candles)
byid={o.id:o for o in broker.pending}
cancelled_ids=[]
seen=set()
for ev in broker.trend_events:
    if ev.get('event_type')=='order_cancelled_m5_trend_change' and ev['order_id'] not in seen:
        seen.add(ev['order_id']); cancelled_ids.append(ev['order_id'])
cancelled=[byid[i] for i in cancelled_ids if i in byid]
print(f'{month}: bars={n} setups_seen={broker.stats["setups_seen"]} '
      f'cancelled_trend={broker.stats["cancelled_trend_change"]} tracked={len(cancelled)}')

def latest_index(t):  # candle time -> bar index lookup
    return None

# simulate revival for each trend-cancelled order
SPREAD=0.2
res={'revive_window':0,'confirmed':0,'win':0,'loss':0,'open':0,'never_realigned':0,'ob_died_first':0}
added_r=0.0; per_trade=[]
for o in cancelled:
    # cancel bar: first bar >= creation where meta says cancelled — find via time? approximate:
    # find first bar c > created_index where trend != direction-compatible
    want='bullish' if o.direction=='bull' else 'bearish'
    cbar=None
    for i in range(o.created_index, n):
        t=trend_by_bar.get(i)
        if t is not None and t!=want:
            cbar=i; break
    if cbar is None: continue
    dead=ob_dead.get(o.ob_id, math.inf)
    # first re-alignment window after cancel while OB alive
    r0=None
    for i in range(cbar+1, n):
        if i>=dead: break
        if trend_by_bar.get(i)==want:
            r0=i; break
    if r0 is None:
        res['ob_died_first' if dead<n else 'never_realigned']+=1
        continue
    res['revive_window']+=1
    # walk forward: sweep+same-bar reclaim while trend aligned and OB alive
    fill=None
    for i in range(r0, n):
        if i>=dead: break
        if trend_by_bar.get(i)!=want: continue   # paused; resumes if realigns (ob alive)
        c=candles[i]
        if o.direction=='bull':
            if c.low<o.entry and c.close>o.entry: fill=(i,c.close+SPREAD); break
        else:
            if c.high>o.entry and c.close<o.entry: fill=(i,c.close); break
    if not fill: continue
    res['confirmed']+=1
    fi,fp=fill
    atr=o.meta.get('atr_at_formation') or 0.0
    stop=o.stop-atr*0.2 if o.direction=='bull' else o.stop+atr*0.2
    dist=abs(fp-stop)
    if dist<=0: continue
    tgt=fp+1.5*dist if o.direction=='bull' else fp-1.5*dist
    out=None
    for j in range(fi+1,n):
        c=candles[j]
        if o.direction=='bull':
            if c.low<=stop: out='loss'; break
            if c.high>=tgt: out='win'; break
        else:
            if c.high+SPREAD>=stop: out='loss'; break
            if c.low+SPREAD<=tgt: out='win'; break
    if out is None: res['open']+=1; continue
    res[out]+=1
    added_r += 1.5 if out=='win' else -1.0
    per_trade.append((o.direction,candles[fi].time[:16],out))
print(month,'REVIVAL:',res,'added_R= %+.1f'%added_r)
for d,t,o in per_trade: print('  ',d,t,o)
