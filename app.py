"""BTC/ETH intelligence dashboard. Signals are research-only, not execution instructions."""
import asyncio
import json
import logging
import math
import os
import sqlite3
import psycopg
from psycopg.rows import dict_row
import time
import re
from html.parser import HTMLParser
from statistics import median
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from secrets import compare_digest
from base64 import b64decode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent
DB_PATH = Path(os.getenv('DB_PATH', str(ROOT / 'signals.db')))
SYMBOLS = ('BTCUSDT', 'ETHUSDT')
LOG = logging.getLogger('market')
logging.basicConfig(level=logging.INFO)
STRATEGY_VERSION = '0.9.9'
STATE = {'updated': None, 'assets': {}, 'macro': {}, 'source_errors': {}, 'wintermute': [], 'decision_log': {}, 'signal_log': {}, 'previous_direction': {}, 'liquidity_monitor': {}, 'liquidity_history': {}, 'observation_candles': {}, 'version': STRATEGY_VERSION, 'etf': {'status':'pending','assets':{},'updated':None}}
LOCK = asyncio.Lock()
MACRO_CACHE = {'expires': 0, 'value': {}}
REPLAY_CACHE = {}


class DatabaseConnection:
    """SQLite for local development; PostgreSQL for durable Render storage."""
    def __init__(self):
        self.pg=bool(os.getenv('DATABASE_URL'))
        self.connection=(psycopg.connect(os.environ['DATABASE_URL'],row_factory=dict_row)
                         if self.pg else sqlite3.connect(DB_PATH))
    def __enter__(self):
        self.connection.__enter__()
        return self
    def __exit__(self,*args):
        return self.connection.__exit__(*args)
    @property
    def row_factory(self):
        return None
    @row_factory.setter
    def row_factory(self,value):
        if not self.pg:
            self.connection.row_factory=value
    def execute(self,sql,params=()):
        if self.pg:
            sql=sql.replace('?', '%s')
        return self.connection.execute(sql,params)


def open_db():
    return DatabaseConnection()


def init_db():
    if not os.getenv('DATABASE_URL'):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG.warning('Journal is using ephemeral SQLite. Configure DATABASE_URL for cross-device persistence.')
    with open_db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS signals (id '+('BIGSERIAL PRIMARY KEY' if db.pg else 'INTEGER PRIMARY KEY')+', ts TEXT, symbol TEXT, side TEXT, entry REAL, stop REAL, tp1 REAL, tp2 REAL, tp3 REAL, bias REAL, timing REAL, status TEXT)')
        # Keep old paper entries intact while distinguishing the new strategy.
        columns=({row['column_name'] for row in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='signals'")} if db.pg else {row[1] for row in db.execute('PRAGMA table_info(signals)')})
        if 'strategy_version' not in columns:
            db.execute("ALTER TABLE signals ADD COLUMN strategy_version TEXT DEFAULT 'LEGACY'")
        if 'candle_ts' not in columns:
            db.execute('ALTER TABLE signals ADD COLUMN candle_ts INTEGER')
        if 'model_score' not in columns:
            db.execute('ALTER TABLE signals ADD COLUMN model_score INTEGER')
        if 'setup_type' not in columns:
            db.execute('ALTER TABLE signals ADD COLUMN setup_type TEXT')
        for column,kind in (('checked_until_ms','INTEGER'),('observed_at','TEXT'),
                            ('observation_source','TEXT'),('coverage','TEXT'),('verification_note','TEXT')):
            if column not in columns:
                db.execute(f'ALTER TABLE signals ADD COLUMN {column} {kind}')
        # Only current-model paper signals belong in the five-entry FIFO journal.
        # Retire pre-v0.8.2 rows, including LEGACY rows, without re-importing them.
        # Preserve prior versions across releases; the UI filters to the current version.
        # Old rows are pruned by the per-asset five-entry FIFO below.
        for symbol in SYMBOLS:
            db.execute('DELETE FROM signals WHERE symbol=? AND id NOT IN (SELECT id FROM signals WHERE symbol=? ORDER BY id DESC LIMIT 5)',(symbol,symbol))
        db.execute('CREATE TABLE IF NOT EXISTS wintermute (id '+('BIGSERIAL PRIMARY KEY' if db.pg else 'INTEGER PRIMARY KEY')+', ts TEXT, signal TEXT, note TEXT)')


def record_wintermute_unavailable():
    """An observation is NOT a buy/sell classification when no wallet feed exists."""
    now = datetime.now(timezone.utc).isoformat()
    with open_db() as db:
        db.execute('INSERT INTO wintermute(ts, signal, note) VALUES (?, ?, ?)',
                   (now, 'UNAVAILABLE', 'Verified attributed wallet/order feed not connected'))
        db.execute('DELETE FROM wintermute WHERE id NOT IN (SELECT id FROM wintermute ORDER BY id DESC LIMIT 10)')
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute('SELECT ts, signal, note FROM wintermute ORDER BY id DESC LIMIT 10')]


def ema(values, n):
    if not values:
        return None
    v = values[0]
    a = 2 / (n + 1)
    for p in values[1:]:
        v = a * p + (1 - a) * v
    return v


def rsi(values, n=14):
    if len(values) <= n:
        return None
    changes = [b-a for a,b in zip(values[-n-1:-1], values[-n:])]
    up = sum(max(x,0) for x in changes)/n
    down = sum(max(-x,0) for x in changes)/n
    return round(100 - 100/(1 + up/down), 2) if down else 100.0


def atr(candles, n=14):
    if len(candles) < n+1:
        return None
    tr=[]
    for i in range(1,len(candles)):
        h,l,pc=candles[i]['high'],candles[i]['low'],candles[i-1]['close']
        tr.append(max(h-l,abs(h-pc),abs(l-pc)))
    return sum(tr[-n:])/n


def fvg_zones(candles, limit=8):
    zones=[]
    for i in range(2,len(candles)):
        first,third=candles[i-2],candles[i]
        if third['low'] > first['high']:
            kind='bullish'; low=first['high']; high=third['low']
        elif third['high'] < first['low']:
            kind='bearish'; low=third['high']; high=first['low']
        else:
            continue
        later=candles[i+1:]
        filled = any(c['low']<=low if kind=='bullish' else c['high']>=high for c in later)
        zones.append({'kind':kind,'low':round(low,2),'high':round(high,2),'status':'filled' if filled else 'open','at':third['ts']})
    return zones[-limit:][::-1]


def order_book_snapshot(book, price):
    """Public spot L2 resting orders; NOT leveraged position liquidation levels."""
    def parse_side(side, reverse):
        rows=[]
        for row in book.get(side, [])[:100]:
            try:
                level, qty=float(row[0]),float(row[1])
                if math.isfinite(level) and math.isfinite(qty) and level>0 and qty>0:
                    rows.append((level,qty))
            except (TypeError,ValueError,IndexError):
                continue
        return sorted(rows,reverse=reverse)
    bids,asks=parse_side('bids',True),parse_side('asks',False)
    if not bids or not asks:
        return None
    bid_usd=sum(p*q for p,q in bids)
    ask_usd=sum(p*q for p,q in asks)
    total=bid_usd+ask_usd
    step=max(price*0.0005,0.01)  # 5-bp price bands, not liquidation estimates
    clusters=[]
    cluster_map={'bids':[], 'asks':[]}
    for side,rows in (('BID',bids),('ASK',asks)):
        bands={}
        for p,q in rows:
            bucket=math.floor(p/step)
            band=bands.setdefault(bucket,{'usd':0,'low':p,'high':p,'levels':0})
            band['usd']+=p*q
            band['low']=min(band['low'],p)
            band['high']=max(band['high'],p)
            band['levels']+=1
        side_clusters=[]
        for bucket,band in bands.items():
            level=round((bucket+0.5)*step,2)
            side_clusters.append({'side':side,'price':level,'usd':round(band['usd'],2),
                                  'low':round(band['low'],2),'high':round(band['high'],2),
                                  'levels':band['levels'],
                                  'distance_pct':round(((band['low']+band['high'])/2/price-1)*100,3)})
        clusters.extend(side_clusters)
        # Preserve both sides even if one dominates the global top-eight ranking.
        cluster_map['bids' if side=='BID' else 'asks']=sorted(
            side_clusters,key=lambda x:x['usd'],reverse=True)[:6]
    clusters=sorted(clusters,key=lambda x:x['usd'],reverse=True)[:8]
    def ladder(rows):
        return [{'price':round(p,2),'quantity':round(q,6),'usd':round(p*q,2)} for p,q in rows[:7]]
    return {'source':'Spot L2 order book · top 100 levels per side',
            'best_bid':round(bids[0][0],2),'best_ask':round(asks[0][0],2),
            'bid_usd':round(bid_usd,2),'ask_usd':round(ask_usd,2),
            'bid_share':round(bid_usd/total*100,1) if total else None,
            'bids':ladder(bids),'asks':ladder(asks),'clusters':clusters,
            'cluster_map':cluster_map,
            'coverage':{'low':round(bids[-1][0],2),'high':round(asks[-1][0],2),
                        'bid_levels':len(bids),'ask_levels':len(asks)},
            'band_width':round(step,2)}


def wide_liquidity_snapshot(book, reference, source, quote='USD', max_levels=1000):
    """Only report actual distant limit orders. Never extrapolate unobserved book depth."""
    if not math.isfinite(reference) or reference<=0:
        return None
    sides={}
    for name in ('bids','asks'):
        valid=[]
        for row in book.get(name,[])[:max_levels]:
            try:
                price,quantity=float(row[0]),float(row[1])
                if math.isfinite(price) and math.isfinite(quantity) and price>0 and quantity>0:
                    valid.append((price,quantity))
            except (ValueError,TypeError,IndexError):
                continue
        sides[name]=sorted(valid,reverse=(name=='bids'))
    if not sides['bids'] or not sides['asks'] or sides['bids'][0][0]>=sides['asks'][0][0]:
        return None
    step=reference*0.0025  # 0.25% wide bands: material distance rather than near-tick noise.
    result={'source':source,'quote':quote,'reference_price':round(reference,2),
            'range_min_pct':0.5,'range_max_pct':5.0,'band_pct':0.25,
            'coverage':{'low':round(sides['bids'][-1][0],2),'high':round(sides['asks'][-1][0],2),
                        'bid_levels':len(sides['bids']),'ask_levels':len(sides['asks'])},
            'bids':[],'asks':[]}
    for side,rows in sides.items():
        bands={}
        for p,q in rows:
            distance=(p/reference-1)*100
            # A bid above market or ask below market is not a valid distant wall.
            if side=='bids' and not -5<=distance<=-0.5: continue
            if side=='asks' and not 0.5<=distance<=5: continue
            bucket=math.floor(p/step)
            zone=bands.setdefault(bucket,{'notional':0,'low':p,'high':p,'levels':0})
            zone['notional']+=p*q
            zone['low']=min(zone['low'],p)
            zone['high']=max(zone['high'],p)
            zone['levels']+=1
        zones=[]
        for zone in bands.values():
            midpoint=(zone['low']+zone['high'])/2
            zones.append({'side':'BID' if side=='bids' else 'ASK',
                          'price':round(midpoint,2),'low':round(zone['low'],2),
                          'high':round(zone['high'],2),'usd':round(zone['notional'],2),
                          'levels':zone['levels'],
                          'distance_pct':round((midpoint/reference-1)*100,2)})
        typical=median(z['usd'] for z in zones) if zones else 0
        for zone in zones:
            zone['relative_strength']=round(zone['usd']/typical,1) if typical else None
        # Strongest six *within a verified distant range*; no invented liquidity.
        result[side]=sorted(zones,key=lambda x:x['usd'],reverse=True)[:6]
    return result


def structure_liquidity_targets(k1h,k4h,price):
    """Candle swing levels are *inferred potential stop targets*, not resting orders."""
    pivots=[]
    for timeframe,candles in (('1H',k1h[:-1][-145:]),('4H',k4h[:-1][-75:])):
        for i in range(2,len(candles)-2):
            window=candles[i-2:i+3]
            candle=candles[i]
            if all(candle['high']>c['high'] for j,c in enumerate(window) if j!=2):
                pivots.append(('above',candle['high'],timeframe))
            if all(candle['low']<c['low'] for j,c in enumerate(window) if j!=2):
                pivots.append(('below',candle['low'],timeframe))
    out={'above':[],'below':[]}
    for side in out:
        candidates=sorted((p,tf) for s,p,tf in pivots if s==side and
                          0.65<=abs((p/price-1)*100)<=7 and
                          ((p>price) if side=='above' else (p<price)))
        zones=[]
        for p,tf in candidates:
            match=next((z for z in zones if abs(p/z['price']-1)<=0.003),None)
            if match:
                count=match['touches']
                match['price']=(match['price']*count+p)/(count+1)
                match['low']=min(match['low'],p)
                match['high']=max(match['high'],p)
                match['touches']+=1
                if tf not in match['timeframes']:match['timeframes'].append(tf)
            else:
                zones.append({'price':p,'low':p,'high':p,'touches':1,'timeframes':[tf]})
        for z in zones:
            z['distance_pct']=round((z['price']/price-1)*100,2)
            for key in ('price','low','high'): z[key]=round(z[key],2)
        out[side]=sorted(zones,key=lambda z:(-min(4,z['touches']),abs(z['distance_pct'])))[:4]
    return out


def recent_tape_snapshot(trades, now=None):
    """Observed taker-side trades in the last 120s; flag truncated REST batches."""
    now=time.time() if now is None else now
    start=now-120
    valid=[]
    for t in trades:
        try:
            timestamp=float(t['ts'])
            if timestamp>1e12: timestamp/=1000  # Binance milliseconds
            price=float(t['price']); qty=float(t['qty'])
            if all(math.isfinite(x) for x in (timestamp,price,qty)) and price>0 and qty>0:
                valid.append((timestamp,price*qty,bool(t['isBuyerMaker'])))
        except (KeyError,TypeError,ValueError):
            continue
    if not valid:
        return {'quality':'UNAVAILABLE','window_seconds':0,'trades':0,'buy_usd':0,'sell_usd':0,
                'buy_share':None,'delta_usd':None}
    selected=[x for x in valid if start<=x[0]<=now+5]
    buy=sum(v for _,v,buyer_maker in selected if not buyer_maker)
    sell=sum(v for _,v,buyer_maker in selected if buyer_maker)
    total=buy+sell
    span=min(120,max(0,now-min(ts for ts,_,_ in valid)))
    complete=min(ts for ts,_,_ in valid)<=start
    latest=max(ts for ts,_,_ in valid)
    quality='COMPLETE' if complete and latest>=now-40 and len(selected)>=10 else 'PARTIAL'
    return {'quality':quality,'window_seconds':round(span),'trades':len(selected),
            'buy_usd':round(buy,2),'sell_usd':round(sell,2),
            'buy_share':round(buy/total*100,1) if total else None,
            'delta_usd':round(buy-sell,2) if total else None}


def liquidity_observation(asset, previous=None):
    """Independent book/tape pressure proxy; MUST NOT enter the final signal model."""
    book=asset.get('order_book')
    tape=asset.get('recent_tape') or {}
    ts=asset['updated']
    base={'ts':ts,'price':asset['price'],'pressure':'WARMING UP','score':None,
          'bid_share':book.get('bid_share') if book else None,
          'taker_buy_share':tape.get('buy_share'),
          'tape_quality':tape.get('quality','UNAVAILABLE'),
          'trade_count':tape.get('trades',0),'tape_seconds':tape.get('window_seconds',0),
          'bid_delta_pct':None,'ask_delta_pct':None,'price_delta_pct':None,
          'walls_retained':None,'walls_checked':None,'absorption':'NOT ESTABLISHED',
          'reason':'Collecting a second comparable order-book snapshot. This monitor is separate from the trade decision.'}
    if not book:
        return {**base,'pressure':'UNAVAILABLE','reason':'No verified spot order book available.'}
    if not previous or not previous.get('book_snapshot'):
        return {**base,'book_snapshot':book}
    seconds=(datetime.fromisoformat(ts)-datetime.fromisoformat(previous['ts'])).total_seconds()
    if not 45<=seconds<=480:
        return {**base,'book_snapshot':book,
                'reason':'Snapshots are too far apart for a meaningful liquidity trend; collecting a new baseline.'}
    old=previous['book_snapshot']
    bid_change=(book['bid_usd']/old['bid_usd']-1)*100 if old['bid_usd'] else None
    ask_change=(book['ask_usd']/old['ask_usd']-1)*100 if old['ask_usd'] else None
    price_change=(asset['price']/previous['price']-1)*100 if previous['price'] else None
    width=max(book['band_width'],old['band_width'])
    old_clusters=old.get('clusters',[])
    current_clusters=book.get('clusters',[])
    retained=[c for c in old_clusters if any(n['side']==c['side'] and abs(n['price']-c['price'])<=width for n in current_clusters)]
    score=(book['bid_share']-50)*1.4
    if bid_change is not None and ask_change is not None:
        score+=max(-35,min(35,(bid_change-ask_change)*0.5))
    usable_tape=tape.get('quality')=='COMPLETE' and tape.get('buy_share') is not None
    if usable_tape:
        score+=(tape['buy_share']-50)*0.7
    score=round(max(-100,min(100,score)))
    pressure='BUY PRESSURE' if score>=22 else 'SELL PRESSURE' if score<=-22 else 'MIXED / NEUTRAL'
    if bid_change is not None and ask_change is not None and bid_change<=-25 and ask_change<=-25:
        pressure='LIQUIDITY WITHDRAWING'
    buy_wall_retained=any(c['side']=='BID' for c in retained)
    sell_wall_retained=any(c['side']=='ASK' for c in retained)
    absorption='NOT ESTABLISHED'
    if usable_tape and price_change is not None and abs(price_change)<=0.2:
        if tape['buy_share']<=35 and bid_change is not None and bid_change>=-10 and buy_wall_retained:
            absorption='POSSIBLE BUY ABSORPTION'
        elif tape['buy_share']>=65 and ask_change is not None and ask_change>=-10 and sell_wall_retained:
            absorption='POSSIBLE SELL ABSORPTION'
    if pressure=='LIQUIDITY WITHDRAWING':
        reason='Visible depth shrank on both sides since the prior poll; this can reflect cancellation, execution, or shifting price ranges.'
    else:
        reason=('Bid-side pressure has the stronger sampled evidence.' if pressure=='BUY PRESSURE' else
                'Ask-side pressure has the stronger sampled evidence.' if pressure=='SELL PRESSURE' else
                'Depth and taker flow are mixed or below the pressure threshold.')
        if not usable_tape:
            reason+=' Trade tape is incomplete or too thin, so its flow is excluded from this pressure score.'
    return {**base,'book_snapshot':book,'pressure':pressure,'score':score,
            'bid_delta_pct':round(bid_change,1) if bid_change is not None else None,
            'ask_delta_pct':round(ask_change,1) if ask_change is not None else None,
            'price_delta_pct':round(price_change,2) if price_change is not None else None,
            'walls_retained':len(retained),'walls_checked':len(old_clusters),
            'absorption':absorption,'reason':reason}


def observed_signal_status(row, price):
    """Legacy spot-sample helper, kept for compatibility; journal uses 1M OHLC."""
    old=row['status']
    if old in ('SL SEEN','SL AFTER TP1','SL AFTER TP2','TP3 SEEN','EXPIRED / UNRESOLVED'):
        return old
    long=row['side']=='LONG'
    stop_hit=price<=row['stop'] if long else price>=row['stop']
    if stop_hit:
        return ('SL AFTER TP2' if old=='TP2 SEEN' else
                'SL AFTER TP1' if old=='TP1 SEEN' else 'SL SEEN')
    for target in (3,2,1):
        reached=price>=row[f'tp{target}'] if long else price<=row[f'tp{target}']
        if reached:
            if old in ('TP1 SEEN','TP2 SEEN') and int(old[2])>=target:
                return old
            return f'TP{target} SEEN'
    return old


TERMINAL_OBSERVATIONS = {'SL SEEN', 'SL AFTER TP1', 'SL AFTER TP2', 'TP3 SEEN',
                         'AMBIGUOUS SL/TP'}


def reconcile_signal(row, candles, source='Exchange spot · 1M OHLC'):
    """Replay *completed* one-minute bar extremes in time order, with honest gaps.

    The issuance minute is excluded when the paper signal arrived mid-bar: its
    earlier high/low could have occurred before the signal existed. This is a
    price-level observation, not an order fill or a provably ordered trade tape.
    """
    result=dict(row)
    if result['status'] in TERMINAL_OBSERVATIONS:
        return result
    try:
        issued=datetime.fromisoformat(result['ts'].replace('Z','+00:00'))
        if issued.tzinfo is None:
            issued=issued.replace(tzinfo=timezone.utc)
        issued_ms=int(issued.timestamp()*1000)
    except (TypeError,ValueError,OverflowError):
        result.update(coverage='GAP',verification_note='Invalid signal timestamp; result cannot be reconstructed.')
        return result
    first_full_minute=((issued_ms+59999)//60000)*60000
    cursor=max(first_full_minute,int(result.get('checked_until_ms') or first_full_minute))
    gap=result.get('coverage')=='GAP'
    status='PENDING ENTRY' if result['status']=='EXPIRED / UNRESOLVED' else result['status']
    activated=status not in ('TRACKING','PENDING ENTRY')
    if not candles:
        result.update(coverage='GAP' if gap else 'FEED UNAVAILABLE',
                      verification_note='1-minute candles unavailable; results are not verified.')
        return result
    bars=sorted((c for c in candles if isinstance(c.get('ts'),int)),key=lambda c:c['ts'])
    if not bars:
        result.update(coverage='FEED UNAVAILABLE',verification_note='No completed 1-minute candles available.')
        return result
    if cursor<bars[0]['ts']:
        gap=True
        cursor=bars[0]['ts']
    if not result.get('checked_until_ms') and not gap and status=='TRACKING':
        # Legacy TP1/TP2 sampled milestones can be replayed accurately only
        # when the full post-issuance minute sequence is still available.
        status='TRACKING'
    for candle in bars:
        start=candle['ts']
        if start<cursor:
            continue
        if start>cursor:
            gap=True
        high,low=candle['high'],candle['low']
        long=result['side']=='LONG'
        entry_hit=low<=result['entry']<=high
        newly_activated=not activated and entry_hit
        if not activated:
            if not entry_hit:
                status='PENDING ENTRY'
                cursor=start+60000
                continue
            activated=True
            status='ENTRY SEEN'
        stop_hit=low<=result['stop'] if long else high>=result['stop']
        targets=[i for i in (1,2,3) if (high>=result[f'tp{i}'] if long else low<=result[f'tp{i}'])]
        reached=max(targets,default=0)
        known=int(status[2]) if status.startswith('TP') and status[2].isdigit() else 0
        next_status=status
        if newly_activated and (stop_hit or reached>known) and known==0:
            next_status='AMBIGUOUS SL/TP'
        elif stop_hit and reached>known:
            next_status='AMBIGUOUS SL/TP'
        elif stop_hit:
            next_status='SL AFTER TP2' if known>=2 else 'SL AFTER TP1' if known>=1 else 'SL SEEN'
        elif reached>known:
            next_status=f'TP{reached} SEEN'
        cursor=start+60000
        if next_status!=status:
            status=next_status
            result.update(observed_at=datetime.fromtimestamp(cursor/1000,timezone.utc).isoformat(),
                          observation_source=source)
        if status in TERMINAL_OBSERVATIONS:
            break
    if cursor<bars[-1]['ts']+60000 and status not in TERMINAL_OBSERVATIONS:
        gap=True
    result['status']=status
    result['checked_until_ms']=cursor
    result['coverage']='GAP' if gap else 'COMPLETE (EXCEPT ISSUANCE MINUTE)'
    note='Completed 1-minute high/low; candle close time shown, not exact touch or execution time.'
    if issued_ms%60000:
        note+=' Partial issuance minute excluded.'
    if gap:
        note+=' Earlier or intermediate candle history missing: event ordering may be incomplete.'
    result['verification_note']=note
    return result


def record_signal_log(symbol, asset, previous_direction, candles=None):
    """Five newest distinct paper entries per asset, FIFO; preserve their outcomes."""
    now=datetime.now(timezone.utc)
    with open_db() as db:
        db.row_factory=sqlite3.Row
        existing=list(db.execute("SELECT * FROM signals WHERE symbol=? AND strategy_version=? ORDER BY id DESC LIMIT 5",(symbol,STRATEGY_VERSION)))
        for row in existing:
            observed=reconcile_signal(row,candles,asset.get('observation_source','Exchange spot · 1M OHLC'))
            db.execute('''UPDATE signals SET status=?,checked_until_ms=?,observed_at=?,
                          observation_source=?,coverage=?,verification_note=? WHERE id=?''',
                       (observed['status'],observed.get('checked_until_ms'),observed.get('observed_at'),
                        observed.get('observation_source'),observed.get('coverage'),
                        observed.get('verification_note'),row['id']))
        direction=asset['direction']
        levels=asset['levels']
        if levels and direction in ('LONG CANDIDATE','SHORT CANDIDATE'):
            last=existing[0] if existing else None
            side='LONG' if direction.startswith('LONG') else 'SHORT'
            candle_ts=asset.get('signal_candle_ts')
            # One signal per completed 15M trigger candle, never repeated by 2M polling.
            # Each subsequent qualifying candle can create a new journal entry.
            seen=bool(candle_ts is not None and any(row['candle_ts']==candle_ts for row in existing))
            # No time-based cooldown: new qualifying candle may trigger immediately.
            if not seen:
                db.execute('INSERT INTO signals(ts,symbol,side,entry,stop,tp1,tp2,tp3,bias,timing,status,strategy_version,candle_ts,model_score,setup_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (now.isoformat(),symbol,side,levels['entry'],levels['stop'],
                            levels['tp1'],levels['tp2'],levels['tp3'],asset['bias'],asset['timing'],
                            'PENDING ENTRY',STRATEGY_VERSION,candle_ts,asset.get('signal_score'),asset.get('setup_trigger')))
        # FIFO: newest five remain, oldest removed only when a sixth is inserted.
        db.execute('DELETE FROM signals WHERE symbol=? AND id NOT IN (SELECT id FROM signals WHERE symbol=? ORDER BY id DESC LIMIT 5)',(symbol,symbol))
        return [dict(row) for row in db.execute('SELECT * FROM signals WHERE symbol=? ORDER BY id DESC LIMIT 5',(symbol,))]


def score_label(score):
    if score is None: return 'UNAVAILABLE'
    if score >= 30: return 'BUY'
    if score <= -30: return 'SELL'
    return 'NEUTRAL'


def parse_stablecoin_growth(rows):
    """DefiLlama's aggregate USD-pegged circulating supply, compared 30 days apart."""
    points = []
    for row in rows:
        if not isinstance(row,dict):
            continue
        usd = row.get('totalCirculatingUSD') or row.get('totalCirculating') or {}
        value = usd.get('peggedUSD') if isinstance(usd, dict) else None
        try:
            if value is not None and float(value) > 0:
                points.append((int(row['date']), float(value)))
        except (TypeError, ValueError, KeyError):
            continue
    points.sort()
    if not points:
        return None
    last_ts, last_value = points[-1]
    target = last_ts - 30*86400
    earlier = [value for ts,value in points if ts <= target]
    if not earlier:
        return None
    change = (last_value / earlier[-1] - 1)*100
    return {'value':round(last_value,2),'change30d':round(change,2), 'timestamp':last_ts,
            'signal':score_label(change*60),'source':'DefiLlama · stablecoin supply'}


def halving_context(height):
    next_height=(height//210000+1)*210000
    remaining=next_height-height
    return {'height':height,'next_height':next_height,'blocks_remaining':remaining,
            'estimated_days':round(remaining/144,0),'signal':'NEUTRAL','source':'mempool.space · block height'}


async def fetch_macro(client):
    """Macro series are slow: refresh periodically, not on every 2-minute tick."""
    if time.monotonic() < MACRO_CACHE['expires']:
        return MACRO_CACHE['value']
    async def stablecoins():
        errors=[]
        for base in ('https://api.llama.fi','https://stablecoins.llama.fi'):
            try:
                series=await fetch_json(client,base+'/stablecoincharts/all')
                parsed=parse_stablecoin_growth(series)
                if parsed:
                    return parsed
                errors.append('Missing comparable 30-day observations')
            except Exception as exc:
                errors.append(str(exc)[:80])
        raise RuntimeError('; '.join(errors))
    async def halving():
        response=await client.get('https://mempool.space/api/blocks/tip/height',timeout=12)
        response.raise_for_status()
        return halving_context(int(response.text))
    jobs=(fetch_json(client,'https://api.alternative.me/fng/',{'limit':1}),stablecoins(),halving())
    results=await asyncio.gather(*jobs,return_exceptions=True)
    previous=MACRO_CACHE['value']
    current=dict(previous)
    names=('fear_greed','stablecoins','halving')
    for name,result in zip(names,results):
        if isinstance(result,Exception):
            LOG.warning('%s feed unavailable: %s',name,result)
            continue
        if name=='fear_greed':
            try:
                raw=result['data'][0]
                v=int(raw['value'])
                current[name]={'value':v,'classification':raw['value_classification'],
                               'timestamp':int(raw['timestamp']),'signal':score_label((v-50)*2),
                               'source':'Alternative.me · Fear & Greed'}
            except (KeyError,ValueError,IndexError,TypeError):
                LOG.warning('Fear & Greed response not valid')
        else:
            current[name]=result
        if name in current and current[name] is not previous.get(name):
            current[name]['fetched_at']=datetime.now(timezone.utc).isoformat()
    MACRO_CACHE.update({'expires':time.monotonic()+600,'value':current})
    return current


def calc_asset(symbol, k15, k1h, k4h, kd, ticker, book, trades, funding, oi, spot_source='Binance spot', market_pair=None, macro=None, fetched_at=None):
    macro=macro or {}
    c15=[x['close'] for x in k15]; c1=[x['close'] for x in k1h]; c4=[x['close'] for x in k4h]; cd=[x['close'] for x in kd]
    price=float(ticker['lastPrice'])
    daily_ma50=sum(cd[-51:-1])/50 if len(cd)>=51 else None
    daily_ma200=sum(cd[-201:-1])/200 if len(cd)>=201 else None
    e1=ema(c1[:-1],21); e4=ema(c4[:-1],21)
    fear=macro.get('fear_greed')
    stable=macro.get('stablecoins')
    bid_depth=sum(float(x[1]) for x in book.get('bids',[]))
    ask_depth=sum(float(x[1]) for x in book.get('asks',[]))
    imbalance=(bid_depth-ask_depth)/(bid_depth+ask_depth) if bid_depth+ask_depth else None
    spread=(float(book['asks'][0][0])-float(book['bids'][0][0]))/price*10000 if book.get('asks') and book.get('bids') else None
    # Aggressor delta is a recent-trade proxy, NOT cumulative volume delta from a complete trade history.
    buy=sum(float(t['qty']) for t in trades if not t['isBuyerMaker'])
    sell=sum(float(t['qty']) for t in trades if t['isBuyerMaker'])
    trade_delta=buy-sell
    trade_imbalance=(buy-sell)/(buy+sell) if buy+sell else None
    change1=(c1[-2]/c1[-3]-1)*100 if len(c1)>3 else None
    change15=(c15[-2]/c15[-3]-1)*100 if len(c15)>3 else None
    support=min(x['low'] for x in k1h[-26:-1]); resistance=max(x['high'] for x in k1h[-26:-1]);
    structure_targets=structure_liquidity_targets(k1h,k4h,price)
    decision=professional_decision(k15,k1h,k4h,kd,price,spread,structure_targets,imbalance,trade_imbalance)
    bias=decision['bias']; timing=decision['timing']
    direction=decision['direction']; levels=decision['levels']
    zones_by_timeframe={
        '15m':fvg_zones(k15[:-1],12),
        '1h':fvg_zones(k1h[:-1],12),
        '1d':fvg_zones(kd[:-1],12),
    }
    zones=zones_by_timeframe['15m']
    nearby=[z for z in zones if z['status']=='open' and abs(price-(z['low']+z['high'])/2)/price<0.015]
    closest=min(nearby,key=lambda z:abs(price-(z['low']+z['high'])/2)) if nearby else None
    fvg_signal=('BUY' if closest['kind']=='bullish' else 'SELL') if closest else 'NEUTRAL'
    support_signal='BUY' if price>resistance*1.002 else 'SELL' if price<support*0.998 else 'NEUTRAL'
    rsi15=rsi(c15[:-1])
    indicators=[
        {'name':'200D Moving Average','value':f'${daily_ma200:,.0f}' if daily_ma200 else None,'signal':score_label(50 if daily_ma200 and price>daily_ma200 else -50 if daily_ma200 else None),'source':spot_source},
        {'name':'50D Moving Average','value':f'${daily_ma50:,.0f}' if daily_ma50 else None,'signal':score_label(50 if daily_ma50 and price>daily_ma50 else -50 if daily_ma50 else None),'source':spot_source},
        {'name':'4H Trend','value':f'EMA21 ${e4:,.0f}' if e4 else None,'signal':score_label(50 if e4 and c4[-2]>e4 else -50 if e4 else None),'source':spot_source+' · 4H'},
        {'name':'Funding Rate','value':f'{float(funding)*100:.4f}%' if funding is not None else None,'signal':'NEUTRAL' if funding is not None else 'UNAVAILABLE','source':spot_source},
        {'name':'Open Interest','value':f'{oi:,.0f} units' if oi is not None else None,'signal':'NEUTRAL' if oi is not None else 'UNAVAILABLE','source':'Binance futures (if accessible)'},
        {'name':'Order Book Imbalance','value':f'{imbalance*100:+.1f}%' if imbalance is not None else None,'signal':score_label(imbalance*400 if imbalance is not None else None),'source':spot_source+' top levels'},
        {'name':'Bid/Ask Spread','value':f'{spread:.2f} bps' if spread is not None else None,'signal':'NEUTRAL' if spread is not None else 'UNAVAILABLE','source':spot_source+' top of book'},
        {'name':'Recent Trade Delta (CVD proxy)','value':f'{trade_delta:+,.3f} units','signal':score_label(trade_imbalance*500 if trade_imbalance is not None else None),'source':spot_source+' recent trades'},
        {'name':'1H Momentum','value':f'{change1:+.2f}%' if change1 is not None else None,'signal':score_label(change1*100 if change1 is not None else None),'source':spot_source+' candles'},
        {'name':'15M Momentum','value':f'{change15:+.2f}%' if change15 is not None else None,'signal':score_label(change15*100 if change15 is not None else None),'source':spot_source+' candles'},
        {'name':'15M RSI','value':f'{rsi15:.1f}' if rsi15 is not None else None,'signal':score_label((rsi15-50)*6 if rsi15 is not None else None),'source':spot_source+' · closed 15M'},
        {'name':'Support / Resistance','value':f'${support:,.0f} / ${resistance:,.0f}','signal':support_signal,'source':spot_source+' · prior 1H levels'},
        {'name':'Fair Value Gaps','value':f'{sum(z["status"]=="open" for z in zones)} open · 15M','signal':fvg_signal,'source':spot_source+' · 3-candle zones'},
    ]
    if fear:
        indicators.append({'name':'Fear & Greed','value':f'{fear["value"]}/100 · {fear["classification"]}',
                           'signal':fear['signal'],'source':fear['source']+' · daily'})
    if stable:
        indicators.append({'name':'Stablecoin Supply (30D)','value':f'${stable["value"]/1e9:,.1f}B · {stable["change30d"]:+.2f}%',
                           'signal':stable['signal'],'source':stable['source']+' · daily'})
    halving=macro.get('halving')
    if halving:
        indicators.append({'name':'Bitcoin Halving Cycle','value':f'{halving["blocks_remaining"]:,} blocks left',
                           'signal':'NEUTRAL','source':halving['source']+' · context only'})
    # A candle timestamp is its OPEN: the value is only final at the close.
    # Snapshot-only feeds have a retrieval time but no trustworthy source clock.
    fetched_at=fetched_at or datetime.now(timezone.utc).isoformat()
    closed_candle_times={
        'daily':datetime.fromtimestamp((kd[-2]['ts']+86400000)/1000,timezone.utc).isoformat(),
        '4h':datetime.fromtimestamp((k4h[-2]['ts']+14400000)/1000,timezone.utc).isoformat(),
        '1h':datetime.fromtimestamp((k1h[-2]['ts']+3600000)/1000,timezone.utc).isoformat(),
        '15m':datetime.fromtimestamp((k15[-2]['ts']+900000)/1000,timezone.utc).isoformat(),
    }
    timeframe_names={
        '200D Moving Average':'daily','50D Moving Average':'daily','4H Trend':'4h',
        '1H Momentum':'1h','Support / Resistance':'1h','15M Momentum':'15m',
        '15M RSI':'15m','Fair Value Gaps':'15m',
    }
    macro_names={'Fear & Greed':'fear_greed','Stablecoin Supply (30D)':'stablecoins',
                 'Bitcoin Halving Cycle':'halving'}
    valid_trade_times=[]
    for trade in trades:
        try:
            ts=float(trade['ts'])
            if math.isfinite(ts):
                valid_trade_times.append(ts/1000 if ts>1e11 else ts)
        except (KeyError,TypeError,ValueError):
            pass
    for indicator in indicators:
        name=indicator['name']
        macro_reading=macro.get(macro_names[name]) if name in macro_names else None
        # Unavailable readings never receive a made-up freshness timestamp.
        indicator['fetched_at']=(macro_reading.get('fetched_at') if macro_reading else fetched_at)
        if name in ('Funding Rate','Open Interest') and indicator['signal']=='UNAVAILABLE':
            indicator['fetched_at']=None
        if name in timeframe_names:
            indicator['source_as_of']=closed_candle_times[timeframe_names[name]]
        elif macro_reading and macro_reading.get('timestamp'):
            indicator['source_as_of']=datetime.fromtimestamp(macro_reading['timestamp'],timezone.utc).isoformat()
        elif name=='Recent Trade Delta (CVD proxy)' and valid_trade_times:
            indicator['source_as_of']=datetime.fromtimestamp(max(valid_trade_times),timezone.utc).isoformat()
        else:
            indicator['source_as_of']=None
    # Recompute costly historical replay only on a new completed 15M candle.
    replay_key=(symbol,k15[-2]['ts'])
    cached=REPLAY_CACHE.get(symbol)
    if cached and cached['key']==replay_key:
        replay=cached['result']
    else:
        try:
            replay=replay_recent_candles(k15,k1h,k4h,kd)
        except Exception:
            LOG.exception('Historical replay failed for %s',symbol)
            replay={'status':'UNAVAILABLE','trades':0,'note':'Historical replay unavailable; live feed unaffected.'}
        REPLAY_CACHE[symbol]={'key':replay_key,'result':replay}
    return {'symbol':symbol,'market_source':spot_source,'market_pair':market_pair or symbol,'price':round(price,2),'change24h':round(float(ticker['priceChangePercent']),2),'bias':bias,'timing':timing,'bias_label':score_label(bias),'timing_label':score_label(timing),'direction':direction,'test_confirmation':direction!='NO TRADE','levels':levels,'signal_checks':decision['checks'],'signal_score':decision['signal_score'],'signal_grade':decision['signal_grade'],'signal_candle_ts':decision['signal_candle_ts'],'watch_direction':decision['watch_direction'],'watch_levels':decision['watch_levels'],'trigger_price':decision['trigger_price'],'setup_reason':decision['reason'],'setup_trigger':decision['trigger'],'setup_volume_ratio':decision['volume_ratio'],'model_replay':replay,'structure_targets':structure_targets,'indicators':indicators,'fvg':zones,'fvg_by_timeframe':zones_by_timeframe,'order_book':order_book_snapshot(book,price),'recent_tape':recent_tape_snapshot(trades),'support':round(support,2),'resistance':round(resistance,2),'rsi15':rsi15,'spread_bps':round(spread,2) if spread is not None else None,'imbalance':round(imbalance*100,1) if imbalance is not None else None,'data_status':'PARTIAL — unconnected sources are excluded','updated':datetime.now(timezone.utc).isoformat(),'confirmed':False,'confirmation_reason':'Weighted 1D/4H/1H/15M evidence and explicit safety vetoes evaluated live; no validated profitability or fill guarantee.'}


def parse_klines(rows):
    return [{'ts':int(x[0]),'open':float(x[1]),'high':float(x[2]),'low':float(x[3]),'close':float(x[4]),'volume':float(x[5])} for x in rows]


async def fetch_json(client,url,params=None,timeout=12):
    r=await client.get(url,params=params,timeout=timeout)
    r.raise_for_status()
    return r.json()


def parse_bybit_wide_book(payload,symbol,now_ms=None):
    """Reject API errors and stale/crossed snapshots rather than invent wall data."""
    if payload.get('retCode')!=0:
        raise RuntimeError('Bybit orderbook request rejected')
    result=payload.get('result') or {}
    if result.get('s')!=symbol:
        raise RuntimeError('Bybit orderbook symbol mismatch')
    now_ms=round(time.time()*1000) if now_ms is None else now_ms
    ts=int(result['ts'])
    if abs(now_ms-ts)>120000:
        raise RuntimeError('Bybit orderbook timestamp is stale')
    bids,asks=result['b'],result['a']
    if not bids or not asks:
        raise RuntimeError('Bybit orderbook is empty')
    bid,ask=float(bids[0][0]),float(asks[0][0])
    if not 0<bid<ask:
        raise RuntimeError('Bybit orderbook is crossed or malformed')
    return {'bids':bids,'asks':asks},(bid+ask)/2


def build_broad_liquidity(payload,symbol,fallback_book,price,fallback_source):
    if not isinstance(payload,Exception):
        try:
            wide,reference=parse_bybit_wide_book(payload,symbol)
            return wide_liquidity_snapshot(wide,reference,
                                           'Bybit spot · up to 1,000 levels per side','USDT')
        except (KeyError,TypeError,ValueError,RuntimeError) as exc:
            LOG.info('%s Bybit wide book unavailable: %s',symbol,exc)
    return wide_liquidity_snapshot(fallback_book,price,fallback_source,'USD')


def professional_decision(k15,k1h,k4h,kd,price,spread,targets=None,imbalance=None,trade_imbalance=None):
    """Adaptive, *unvalidated* trend/pullback model; points are evidence, not win odds.

    Only completed 15M/1H/4H/1D candles influence direction; the live quote
    influences entry distance and stop/target geometry. Resting liquidity is
    intentionally excluded. A WATCH plan is conditional and never journaled.
    """
    result={'bias':None,'timing':None,'direction':'NO TRADE','levels':None,
            'watch_direction':None,'watch_levels':None,'signal_score':0,
            'signal_grade':'NONE','signal_candle_ts':None,'trigger':'NONE',
            'trigger_price':None,'volume_ratio':None,'checks':[],
            'reason':'Insufficient completed candle history.'}
    c15,c1,c4,cd=(k15[:-1],k1h[:-1],k4h[:-1],kd[:-1])
    if min(len(c15),len(c1),len(c4))<70 or len(cd)<200 or not math.isfinite(price) or price<=0:
        return result
    v15=[c['close'] for c in c15]; v1=[c['close'] for c in c1]
    v4=[c['close'] for c in c4]; vd=[c['close'] for c in cd]
    a=atr(c15)
    if not a or a<=0:
        return result
    e4=ema(v4,21); p4=ema(v4[:-3],21)
    e1=ema(v1,21); p1=ema(v1[:-3],21); slow1=ema(v1,55)
    e9=ema(v15,9); e21=ema(v15,21)
    r15=rsi(v15)
    ma200=sum(vd[-200:])/200; ma50=sum(vd[-50:])/50
    sign=lambda yes,no:1 if yes else -1 if no else 0
    four=sign(v4[-1]>e4 and e4>p4,v4[-1]<e4 and e4<p4)
    hour=sign(v1[-1]>e1>slow1 and e1>p1,v1[-1]<e1<slow1 and e1<p1)
    fifteen=sign(v15[-1]>e9>e21,v15[-1]<e9<e21)
    daily=sign(vd[-1]>ma200,vd[-1]<ma200)
    cross=sign(ma50>ma200,ma50<ma200)
    hour_move=sign(v1[-1]>v1[-5],v1[-1]<v1[-5])
    # A run of completed hourly candles against the 4H trend is a live reversal
    # warning: never keep issuing counter-momentum entries just because 4H/1D lag.
    green_hour=sum(c['close']>c['open'] for c in c1[-6:])
    red_hour=sum(c['close']<c['open'] for c in c1[-6:])
    hourly_reversal=1 if green_hour>=4 and v1[-1]>v1[-5] else -1 if red_hour>=4 and v1[-1]<v1[-5] else 0
    rsi_direction=sign(50<=r15<78,22<r15<=50)
    result.update({'bias':60*four+25*daily+15*cross,
                   'timing':45*hour+30*fifteen+15*hour_move+10*rsi_direction,
                   'signal_candle_ts':c15[-1]['ts']})
    last=c15[-1]
    typical_volume=median(c['volume'] for c in c15[-21:-1])
    ratio=last['volume']/typical_volume if typical_volume>0 else 0
    result['volume_ratio']=round(ratio,2)
    prior=c15[-13:-1]; recent=c15[-4:]

    def pattern(side):
        if side==1:
            if last['close']>max(c['high'] for c in prior)+0.05*a:
                return '15M BREAKOUT'
            if min(c['low'] for c in recent)<=e21+0.45*a and last['close']>e9 and last['close']>c15[-2]['close']:
                return '15M PULLBACK / RECLAIM'
            if last['close']>e9 and last['close']>c15[-4]['close'] and sum(x['close']>x['open'] for x in recent)>=2:
                return '15M MOMENTUM CONTINUATION'
        else:
            if last['close']<min(c['low'] for c in prior)-0.05*a:
                return '15M BREAKDOWN'
            if max(c['high'] for c in recent)>=e21-0.45*a and last['close']<e9 and last['close']<c15[-2]['close']:
                return '15M PULLBACK / REJECTION'
            if last['close']<e9 and last['close']<c15[-4]['close'] and sum(x['close']<x['open'] for x in recent)>=2:
                return '15M MOMENTUM CONTINUATION'
        return 'NONE'

    def score(side):
        p=pattern(side)
        momentum_ok=hour_move==side or (v15[-1]-v15[-4])*side>0
        rsi_ok=(40<=r15<=78 if side==1 else 18<=r15<=60)
        pts=(25 if four==side else 0)+(25 if hour==side else 0)
        pts+=(15 if fifteen==side else 0)
        pts+=15 if hourly_reversal==side else 0
        pts+=(15 if p in ('15M BREAKOUT','15M BREAKDOWN','15M PULLBACK / RECLAIM','15M PULLBACK / REJECTION')
              else 10 if p!='NONE' else 0)
        pts+=10 if rsi_ok and momentum_ok else 5 if rsi_ok or momentum_ok else 0
        pts+=5 if ratio>=1 else 3 if ratio>=0.7 else 0
        pts+=5 if daily==side else 0
        return pts,p

    # v0.9.8: volatility-adjusted impulse detection, using CLOSED candles only.
    # An impulse is a candidate, not a guaranteed entry; extended moves stay WATCH.
    impulse_side=0
    impulse_strength=0.0
    if len(c15)>=9 and len(c1)>=5:
        move_1h=(v15[-1]-v15[-5])/a
        move_2h=(v15[-1]-v15[-9])/a
        impulse_side=1 if move_1h>=2.0 and move_2h>=2.8 else -1 if move_1h<=-2.0 and move_2h<=-2.8 else 0
        if impulse_side:
            matching=sum((c['close']-c['open'])*impulse_side>0 for c in c15[-5:])
            if matching<3 or (v1[-1]-v1[-3])*impulse_side<=0:
                impulse_side=0
            else:
                impulse_strength=round(abs(move_1h),2)

    long_score,long_pattern=score(1)
    short_score,short_pattern=score(-1)
    if impulse_side==1:
        long_score+=20
        if long_pattern=='NONE':long_pattern='15M RAPID UPSIDE IMPULSE'
    elif impulse_side==-1:
        short_score+=20
        if short_pattern=='NONE':short_pattern='15M RAPID DOWNSIDE IMPULSE'
    # Do not prefer an old bearish regime over a clear six-hour bullish reversal.
    if hourly_reversal==1:
        short_score=max(0,short_score-35)
    elif hourly_reversal==-1:
        long_score=max(0,long_score-35)
    if long_score==short_score:
        result['reason']='LONG and SHORT scores tied; no directional edge.'
        return result
    side=1 if long_score>short_score else -1
    points=max(long_score,short_score)
    trigger=long_pattern if side==1 else short_pattern
    other=min(long_score,short_score)
    result.update({'signal_score':points,'signal_grade':'HIGH' if points>=85 else 'MODERATE' if points>=65 else 'DEVELOPING' if points>=45 else 'NONE',
                   'trigger':trigger})
    if points<45 or points-other<15:
        result['reason']='No directional edge: trend and momentum scores are mixed.'
        return result

    # Order-book snapshots are supporting evidence, not standalone signals.
    flow_support=(imbalance is not None and trade_imbalance is not None and imbalance*side>=0.08 and trade_imbalance*side>=0.08)
    flow_opposes=(imbalance is not None and trade_imbalance is not None and imbalance*side < -0.18 and trade_imbalance*side < -0.18)
    structure_break=(last['close']>max(c['high'] for c in c15[-5:-1]) if side==1 else last['close']<min(c['low'] for c in c15[-5:-1]))
    # Completed-candle continuation is valid without demanding an additional fresh
    # breakout or an unrelated order-book snapshot on every 15M bar.
    continuation_confirmed=(trigger=='15M MOMENTUM CONTINUATION' and
                            fifteen==side and hour_move==side and
                            sum((c['close']-c['open'])*side>0 for c in c15[-4:])>=3 and
                            ratio>=0.65)
    impulse_confirmed_price=(impulse_side==side and fifteen==side and
                             hour_move==side and
                             abs(price-last['close'])<=0.9*a and
                             ratio>=0.65)
    pattern_confirmed=trigger!='NONE' and (structure_break or flow_support or
                       ratio>=1.15 or continuation_confirmed or impulse_confirmed_price)
    reversal_confirmed=hourly_reversal==side and fifteen==side and (structure_break or flow_support)
    impulse_confirmed=(impulse_side==side and fifteen==side and hour_move==side and
                       (structure_break or impulse_confirmed_price))
    continuation_anchor=(hour==side and fifteen==side and continuation_confirmed)
    anchor=(hour==side and (four==side or fifteen==side)) or reversal_confirmed or impulse_confirmed or continuation_anchor
    # Safety checks are vetoes; volume and a perfect 1D alignment are not.
    counter_hourly_reversal=hourly_reversal==-side
    volatility=0.06<=a/price*100<=2.8
    spread_ok=spread is not None and math.isfinite(spread) and 0<=spread<=5
    # Permit a fresh impulse only near the last completed candle close;
    # never chase an already extended move far from the 15M mean.
    impulse_near=(impulse_side==side and abs(price-last['close'])<=0.75*a and abs(price-e21)<=5.0*a)
    continuation_near=(continuation_confirmed and abs(price-last['close'])<=0.8*a and
                       abs(price-e21)<=4.6*a)
    not_chasing=abs(price-last['close'])<=1.6*a and (abs(price-e21)<=3.8*a or impulse_near or continuation_near)
    not_exhausted=16<r15<86
    def plan(entry):
        swing=(min(c['low'] for c in c15[-7:]) if side==1 else
               max(c['high'] for c in c15[-7:]))
        stop=(min(swing-0.12*a,entry-1.25*a) if side==1 else
              max(swing+0.12*a,entry+1.25*a))
        risk=abs(entry-stop)
        valid=0.12<=risk/entry*100<=2.2 and risk<=3.2*a
        if not valid:
            return None
        return {'entry':round(entry,2),'stop':round(stop,2),
                'tp1':round(entry+side*1.2*risk,2),
                'tp2':round(entry+side*2*risk,2),
                'tp3':round(entry+side*3*risk,2),
                'risk_pct':round(100*risk/entry,2),
                'status':'PAPER RESEARCH — NOT FILLED'}

    # A watch presents a precise *future trigger*, never mistaken for a market fill.
    watch_entry=(max(price+0.1*a,max(c['high'] for c in c15[-5:])+0.05*a)
                 if side==1 else min(price-0.1*a,min(c['low'] for c in c15[-5:])-0.05*a))
    watch=plan(watch_entry)
    active=plan(price)
    risk_ok=active is not None
    required_ok=anchor and pattern_confirmed and not flow_opposes and not counter_hourly_reversal and spread_ok and volatility and not_chasing and not_exhausted and risk_ok
    checks=[
        {'label':'4H trend', 'ok':four==side or impulse_confirmed,'detail':'IMPULSE OVERRIDE' if impulse_confirmed and four!=side else 'ALIGNED' if four==side else 'COUNTERTREND'},
        {'label':'Rapid impulse', 'ok':impulse_confirmed,'detail':f'{impulse_strength:.1f} ATR / 1H' if impulse_confirmed else 'NOT CONFIRMED'},
        {'label':'1H execution trend','ok':hour==side,'detail':'ALIGNED' if hour==side else 'MIXED'},
        {'label':'15M price action','ok':trigger!='NONE','detail':trigger if trigger!='NONE' else 'DEVELOPING'},
        {'label':'15M participation','ok':ratio>=0.7,'detail':f'{ratio:.2f}× median · bonus only'},
        {'label':'Spread + ATR + no chase','ok':spread_ok and volatility and not_chasing and not_exhausted,
         'detail':'SAFE RANGE' if spread_ok and volatility and not_chasing and not_exhausted else 'RISK FILTER'},
        {'label':'Stop geometry','ok':risk_ok,'detail':f'{active["risk_pct"]:.2f}% risk' if active else 'TOO WIDE / TIGHT'}]
    result['checks']=checks
    result['trigger_price']=round(watch_entry,2)
    result['checks'].append({'label':'Price / order flow','ok':pattern_confirmed and not flow_opposes,'detail':'CONFIRMED' if pattern_confirmed and not flow_opposes else 'WAIT'})
    result['watch_direction']='LONG' if side==1 else 'SHORT'
    result['watch_levels']=watch
    if required_ok and points>=65:
        result.update({'direction':'LONG CANDIDATE' if side==1 else 'SHORT CANDIDATE',
                       'levels':active,
                       'reason':f'{points}/100 evidence · {trigger.lower()} · {active["risk_pct"]:.2f}% defined stop. Paper signal, not a verified edge.'})
    else:
        blockers=[]
        if not anchor:blockers.append('1H/4H anchor not yet aligned')
        if counter_hourly_reversal:blockers.append('last six completed 1H candles oppose this trade')
        if points<65:blockers.append(f'evidence {points}/100, needs 65')
        if not pattern_confirmed:blockers.append('price/volume or order-flow confirmation missing')
        if flow_opposes:blockers.append('order flow opposes this trade')
        if not (spread_ok and volatility and not_chasing and not_exhausted):blockers.append('spread / volatility / chase filter')
        if not risk_ok:blockers.append('stop geometry')
        if not volatility:blockers.append('ATR volatility outside safe range')
        if not spread_ok:blockers.append('spread unavailable or too wide')
        result['reason']=f'Conditional {result["watch_direction"]} above/below ${watch_entry:,.2f}; waiting: '+', '.join(blockers)+'.'
    return result


def replay_recent_candles(k15,k1h,k4h,kd,round_trip_cost_bps=20):
    """Small, conservative OHLC sanity check, NOT an out-of-sample performance claim.

    At each 15M close only *then-completed* higher-timeframe candles are shown
    to the model. Assumes a synthetic 1-bp spread for historical decisions,
    next-candle-open fills, stop-first intrabar ties, 2R exit/12H expiry, and
    a fixed round-trip fee+slippage. Does not reconstruct real order-book fills.
    """
    closed=k15[:-1]
    if len(closed)<180:
        return {'status':'INSUFFICIENT HISTORY','trades':0,'note':'Need more than 180 completed 15M candles.'}
    hold_bars=48
    earliest=max(80,len(closed)-480)
    cutoff=len(closed)-hold_bars-1
    if cutoff<=earliest:
        return {'status':'INSUFFICIENT HISTORY','trades':0,'note':'Not enough forward candles for the 12H holding window.'}
    costs=round_trip_cost_bps/10000
    positions=[]
    i=earliest
    while i<cutoff:
        asof=closed[i]['ts']+900_000
        h1=[x for x in k1h if x['ts']+3_600_000<=asof]
        h4=[x for x in k4h if x['ts']+14_400_000<=asof]
        hd=[x for x in kd if x['ts']+86_400_000<=asof]
        if min(len(h1),len(h4))<70 or len(hd)<200:
            i+=1
            continue
        # Dummy currently forming bars are deliberately duplicates of known
        # candles; professional_decision discards the final element on each TF.
        dummy=lambda rows:[*rows,dict(rows[-1])]
        evaluated=professional_decision(dummy(closed[:i+1]),dummy(h1),dummy(h4),dummy(hd),closed[i]['close'],1)
        if evaluated['direction'] not in ('LONG CANDIDATE','SHORT CANDIDATE'):
            i+=1
            continue
        side=1 if evaluated['direction']=='LONG CANDIDATE' else -1
        next_bar=closed[i+1]
        entry=next_bar['open']
        original=evaluated['levels']['entry']
        stop=evaluated['levels']['stop']
        risk=abs(entry-stop)
        a=atr(closed[:i+1])
        if risk<=0 or (entry-stop)*side<=0 or abs(entry-original)>0.5*a:
            i+=1
            continue
        target=entry+side*2*risk
        exit_i=i+hold_bars
        gross_r=side*(closed[exit_i]['close']-entry)/risk
        for j in range(i+1,exit_i+1):
            bar=closed[j]
            # If both limits are crossed within one 15M candle, assume loss.
            if (bar['low']<=stop if side==1 else bar['high']>=stop):
                gross_r=-1
                exit_i=j
                break
            if (bar['high']>=target if side==1 else bar['low']<=target):
                gross_r=2
                exit_i=j
                break
        net_r=gross_r-entry*costs/risk
        positions.append(net_r)
        i=max(i+4,exit_i+1)  # One position at a time, no overlapping paper trades.
    days=round((closed[cutoff]['ts']-closed[earliest]['ts'])/86_400_000,1)
    if not positions:
        return {'status':'NO HISTORICAL CANDIDATES','trades':0,'lookback_days':days,
                'cost_bps':round_trip_cost_bps,'note':'No completed, non-overlapping candidates in this limited candle window.'}
    return {'status':'LIMITED HISTORICAL REPLAY','trades':len(positions),'lookback_days':days,
            'win_rate_pct':round(sum(r>0 for r in positions)/len(positions)*100,1),
            'net_r':round(sum(positions),2),'avg_net_r':round(sum(positions)/len(positions),2),
            'cost_bps':round_trip_cost_bps,
            'note':'Next 15M open entry; 2R target, stop-first ties, max 12H; 20-bp assumed round-trip costs and synthetic 1-bp spread. Short window, not independent validation.'}


async def fetch_asset_binance(client,symbol,macro=None):
    base='https://api.binance.com/api/v3/'
    fut='https://fapi.binance.com/fapi/v1/'
    queries=[fetch_json(client,base+'klines',{'symbol':symbol,'interval':tf,'limit':limit}) for tf,limit in [('15m',160),('1h',160),('4h',160),('1d',260)]]
    queries += [fetch_json(client,base+'ticker/24hr',{'symbol':symbol}),fetch_json(client,base+'depth',{'symbol':symbol,'limit':1000}),fetch_json(client,base+'trades',{'symbol':symbol,'limit':1000}),fetch_json(client,fut+'premiumIndex',{'symbol':symbol}),fetch_json(client,fut+'openInterest',{'symbol':symbol}),fetch_json(client,'https://api.bybit.com/v5/market/orderbook',{'category':'spot','symbol':symbol,'limit':1000},timeout=5)]
    queries.append(fetch_json(client,base+'klines',{'symbol':symbol,'interval':'1m','limit':1000}))
    out=await asyncio.gather(*queries,return_exceptions=True)
    fetched_at=datetime.now(timezone.utc).isoformat()
    if any(isinstance(x,Exception) for x in out[:7]):
        raise RuntimeError('Core market feed unavailable: '+', '.join(str(x)[:80] for x in out[:7] if isinstance(x,Exception)))
    k15,k1,k4,kd=[parse_klines(x) for x in out[:4]]
    funding=None if isinstance(out[7],Exception) else out[7].get('lastFundingRate')
    oi=None if isinstance(out[8],Exception) else float(out[8].get('openInterest',0))
    trades=[{**t,'ts':t.get('time')} for t in out[6]]
    asset=calc_asset(symbol,k15,k1,k4,kd,out[4],out[5],trades,funding,oi,market_pair=symbol.replace('USDT','/USDT'),macro=macro,fetched_at=fetched_at)
    asset['liquidity_map']=build_broad_liquidity(out[9],symbol,out[5],asset['price'],'Binance spot · up to 1,000 levels per side')
    asset['_observation_candles']=parse_klines(out[10])[:-1] if not isinstance(out[10],Exception) else None
    asset['observation_source']='Binance spot · completed 1M OHLC'
    return asset


def kraken_result(payload):
    """Select a Kraken pair payload while rejecting API-level errors."""
    if payload.get('error'):
        raise RuntimeError('Kraken API: '+str(payload['error'])[:120])
    result = payload.get('result', {})
    pairs = [value for name,value in result.items() if name != 'last']
    if len(pairs) != 1:
        raise RuntimeError('Expected one Kraken market pair')
    return pairs[0]


def parse_kraken_ohlc(rows):
    """Convert Kraken OHLC (seconds) into our candle format (milliseconds)."""
    return [{'ts':int(x[0])*1000,'open':float(x[1]),'high':float(x[2]),
             'low':float(x[3]),'close':float(x[4]),'volume':float(x[6])} for x in rows]


async def fetch_asset_kraken(client,symbol,macro=None):
    """Public USD spot feed when Binance is geographically unavailable."""
    pair = 'XBTUSD' if symbol == 'BTCUSDT' else 'ETHUSD'
    base = 'https://api.kraken.com/0/public/'
    queries = [fetch_json(client,base+'OHLC',{'pair':pair,'interval':interval}) for interval in (15,60,240,1440)]
    queries += [fetch_json(client,base+endpoint,{'pair':pair,**extra}) for endpoint,extra in
                [('Ticker',{}),('Depth',{'count':500}),('Trades',{'count':1000})]]
    queries.append(fetch_json(client,'https://api.bybit.com/v5/market/orderbook',
                              {'category':'spot','symbol':symbol,'limit':1000},timeout=5))
    queries.append(fetch_json(client,base+'OHLC',{'pair':pair,'interval':1}))
    responses = await asyncio.gather(*queries,return_exceptions=True)
    fetched_at=datetime.now(timezone.utc).isoformat()
    if any(isinstance(x,Exception) for x in responses[:7]):
        raise RuntimeError('Kraken market feed unavailable: '+', '.join(str(x)[:80] for x in responses[:7] if isinstance(x,Exception)))
    k15,k1,k4,kd = [parse_kraken_ohlc(kraken_result(x)) for x in responses[:4]]
    if min(len(k15),len(k1),len(k4),len(kd)) < 201:
        raise RuntimeError('Insufficient Kraken candle history for 200D regime')
    raw_ticker, book, raw_trades = [kraken_result(x) for x in responses[4:7]]
    last_price = float(raw_ticker['c'][0])
    # Kraken ticker's opening price is midnight UTC; calculate an approximate rolling 24h change instead.
    change24h = (last_price/float(k1[-25]['close'])-1)*100
    ticker = {'lastPrice':last_price,'priceChangePercent':change24h}
    trades = [{'price':x[0],'qty':x[1],'ts':x[2],'isBuyerMaker':x[3]=='s'} for x in raw_trades]
    result = calc_asset(symbol,k15,k1,k4,kd,ticker,book,trades,None,None,
                        spot_source='Kraken spot',market_pair=('BTC/USD' if symbol=='BTCUSDT' else 'ETH/USD'),macro=macro,fetched_at=fetched_at)
    result['liquidity_map']=build_broad_liquidity(responses[7],symbol,book,last_price,
                                                'Kraken spot · up to 500 levels per side')
    try:
        result['_observation_candles']=parse_kraken_ohlc(kraken_result(responses[8]))[:-1]
    except (TypeError,ValueError,RuntimeError,AttributeError) as exc:
        LOG.warning('%s 1M candle verification unavailable: %s',symbol,exc)
        result['_observation_candles']=None
    result['observation_source']='Kraken spot · completed 1M OHLC'
    result['change_approximate']=True
    result['provider_note']='Kraken USD spot; Binance unavailable on this deployment. No futures funding/OI.'
    return result


def parse_kraken_futures_candles(payload):
    if not isinstance(payload,dict) or not isinstance(payload.get('candles'),list):
        raise RuntimeError('Kraken Futures charts returned no candles')
    candles=[]
    for x in payload['candles']:
        timestamp=x.get('time')
        if isinstance(timestamp,str):
            ts=int(datetime.fromisoformat(timestamp.replace('Z','+00:00')).timestamp()*1000)
        else:
            ts=int(timestamp)
            if ts<100000000000:
                ts*=1000
        candles.append({'ts':ts,'open':float(x['open']),'high':float(x['high']),
                        'low':float(x['low']),'close':float(x['close']),
                        'volume':float(x.get('volume') or 0)})
    return sorted(candles,key=lambda x:x['ts'])


async def fetch_asset_kraken_futures(client,symbol,macro=None):
    """All price-derived indicators, levels and depth from Kraken USD perpetuals."""
    contract='PF_XBTUSD' if symbol=='BTCUSDT' else 'PF_ETHUSD'
    base='https://futures.kraken.com'
    now=int(time.time())
    timeframes=[('15m',160,900),('1h',160,3600),('4h',160,14400),('1d',260,86400)]
    requests=[fetch_json(client,f'{base}/api/charts/v1/trade/{contract}/{tf}',
                         {'from':now-count*seconds-2*seconds})
              for tf,count,seconds in timeframes]
    requests.extend([
        fetch_json(client,base+'/derivatives/api/v3/tickers'),
        fetch_json(client,base+'/derivatives/api/v3/orderbook',{'symbol':contract}),
        fetch_json(client,f'{base}/api/charts/v1/trade/{contract}/1m',
                   {'from':now-24*3600}),
    ])
    responses=await asyncio.gather(*requests,return_exceptions=True)
    if any(isinstance(x,Exception) for x in responses[:6]):
        raise RuntimeError('Kraken Futures core feed unavailable: '+
                           ', '.join(str(x)[:70] for x in responses[:6] if isinstance(x,Exception)))
    k15,k1,k4,kd=[parse_kraken_futures_candles(x) for x in responses[:4]]
    if min(len(k15),len(k1),len(k4))<71 or len(kd)<201:
        raise RuntimeError(f'Kraken Futures insufficient history: 15m={len(k15)}, 1h={len(k1)}, 4h={len(k4)}, 1d={len(kd)}; need 71 intraday and 201 daily')
    ticker_row=next((x for x in responses[4].get('tickers',[]) if x.get('symbol')==contract),None)
    if not ticker_row or ticker_row.get('last') is None:
        raise RuntimeError('Kraken Futures perpetual ticker unavailable')
    price=float(ticker_row['last'])
    previous_24h=float(ticker_row.get('open24h') or ticker_row.get('open24Hour') or 0)
    if previous_24h<=0:
        previous_24h=float(k1[-25]['close'])
    ticker={'lastPrice':price,'priceChangePercent':(price/previous_24h-1)*100}
    rawbook=responses[5]
    if rawbook.get('result') not in (None,'success'):
        raise RuntimeError('Kraken Futures order book rejected')
    rawbook=rawbook.get('orderBook') or rawbook
    def levels(name):
        return [[str(x['price']),str(x.get('qty',x.get('size',0)))] if isinstance(x,dict)
                else [str(x[0]),str(x[1])] for x in rawbook.get(name,[])]
    book={'bids':levels('bids'),'asks':levels('asks')}
    if not book['bids'] or not book['asks']:
        raise RuntimeError('Kraken Futures order book empty')
    fetched_at=datetime.now(timezone.utc).isoformat()
    funding=ticker_row.get('fundingRate')
    oi=ticker_row.get('openInterest')
    asset=calc_asset(symbol,k15,k1,k4,kd,ticker,book,[],
                     funding,float(oi) if oi is not None else None,
                     spot_source='Kraken Futures perpetual',market_pair=contract,
                     macro=macro,fetched_at=fetched_at)
    asset['liquidity_map']=build_broad_liquidity(RuntimeError('futures-only orderbook'),symbol,book,price,
                                                 'Kraken Futures perpetual · order book')
    asset['mark_price']=float(ticker_row['markPrice']) if ticker_row.get('markPrice') is not None else None
    asset['price_type']='LAST TRADE'
    asset['provider_note']='Kraken USD perpetual futures; no spot-price substitution.'
    asset['observation_source']='Kraken Futures perpetual · completed 1M traded-price OHLC'
    if isinstance(responses[6],Exception):
        asset['_observation_candles']=None
    else:
        asset['_observation_candles']=parse_kraken_futures_candles(responses[6])[:-1]
    return asset


async def fetch_asset(client,symbol,macro=None):
    provider=os.getenv('MARKET_PROVIDER','auto').lower()
    if provider=='kraken_futures':
        return await fetch_asset_kraken_futures(client,symbol,macro)
    if provider=='kraken':
        return await fetch_asset_kraken(client,symbol,macro)
    if provider=='binance':
        return await fetch_asset_binance(client,symbol,macro)
    try:
        return await fetch_asset_binance(client,symbol,macro)
    except Exception as exc:
        LOG.warning('%s Binance unavailable, trying Kraken: %s',symbol,exc)
        return await fetch_asset_kraken(client,symbol,macro)


async def refresh():
    async with httpx.AsyncClient(headers={'User-Agent':'BTC-Intelligence/0.7'}) as client:
        macro=await fetch_macro(client)
        results=await asyncio.gather(*(fetch_asset(client,s,macro) for s in SYMBOLS),return_exceptions=True)
    async with LOCK:
        for symbol,result in zip(SYMBOLS,results):
            if isinstance(result,Exception):
                STATE['source_errors'][symbol]=str(result)[:180]
                LOG.warning('%s refresh failed: %s',symbol,result)
            else:
                previous=STATE['previous_direction'].get(symbol)
                candles=result.pop('_observation_candles',None)
                STATE['observation_candles'][symbol]=candles
                STATE['assets'][symbol]=result
                STATE['source_errors'].pop(symbol,None)
                STATE['signal_log'][symbol]=record_signal_log(symbol,result,previous,candles)
                STATE['previous_direction'][symbol]=result['direction']
                liquidity_history=STATE['liquidity_history'].setdefault(symbol,[])
                observation=liquidity_observation(result,liquidity_history[0] if liquidity_history else None)
                liquidity_history.insert(0,observation)
                del liquidity_history[10:]
                STATE['liquidity_monitor'][symbol]={k:v for k,v in observation.items() if k!='book_snapshot'}
                history=STATE['decision_log'].setdefault(symbol,[])
                ts=datetime.now(timezone.utc)
                if not history or (ts-datetime.fromisoformat(history[0]['ts'])).total_seconds()>=300:
                    history.insert(0,{'ts':ts.isoformat(),'direction':result['direction'],
                                      'bias':result['bias'],'timing':result['timing']})
                    del history[10:]
        STATE['macro']=macro
        STATE['updated']=datetime.now(timezone.utc).isoformat()


async def log_kraken_futures_connectivity():
    """Verify Kraken Futures public endpoints from the actual Render host."""
    endpoints = [
        ('instruments','https://futures.kraken.com/derivatives/api/v3/instruments',None),
        ('tickers','https://futures.kraken.com/derivatives/api/v3/tickers',None),
        ('btc_history','https://futures.kraken.com/api/charts/v1/trade/PF_XBTUSD/1m',{'from':int(time.time())-300}),
        ('eth_history','https://futures.kraken.com/api/charts/v1/trade/PF_ETHUSD/1m',{'from':int(time.time())-300}),
    ]
    async with httpx.AsyncClient(timeout=9) as client:
        for name,url,params in endpoints:
            try:
                response=await client.get(url,params=params)
                LOG.warning('KRAKEN_FUTURES_CHECK endpoint=%s status=%s content_type=%s',
                            name,response.status_code,response.headers.get('content-type','')[:40])
                if response.status_code==200 and name in ('instruments','tickers'):
                    payload=response.json()
                    rows=payload.get('instruments' if name=='instruments' else 'tickers',[])
                    matched=[x.get('symbol') for x in rows if x.get('symbol') in ('PF_XBTUSD','PF_ETHUSD')]
                    LOG.warning('KRAKEN_FUTURES_CHECK endpoint=%s contracts=%s',name,','.join(matched))
            except (httpx.HTTPError,ValueError,TypeError) as exc:
                LOG.warning('KRAKEN_FUTURES_CHECK endpoint=%s error=%s',name,type(exc).__name__)


async def log_binance_futures_connectivity():
    """Run once on Render, report only HTTP statuses (never credentials)."""
    async with httpx.AsyncClient(timeout=7) as client:
        for endpoint in ('ticker/price','premiumIndex','klines'):
            params={'symbol':'BTCUSDT'}
            if endpoint=='klines':
                params.update(interval='1m',limit=2)
            for host in ('https://fapi.binance.com','https://fapi1.binance.com'):
                try:
                    response=await client.get(host+'/fapi/v1/'+endpoint,params=params)
                    LOG.warning('BINANCE_FUTURES_CHECK host=%s endpoint=%s status=%s',
                                host,endpoint,response.status_code)
                except httpx.HTTPError as exc:
                    LOG.warning('BINANCE_FUTURES_CHECK host=%s endpoint=%s error=%s',
                                host,endpoint,type(exc).__name__)



# ETF feeds are daily US fund flows, NOT real-time futures executions.
# No API key is required for the public Farside tables. Never turn blanks into zero.
ETF_SOURCES = {
    'BTC': ('https://farside.co.uk/bitcoin-etf-flow-all-data/', ['IBIT','FBTC','BITB','ARKB','BTCO','EZBC','BRRR','HODL','BTCW','MSBT','GBTC','BTC']),
    'ETH': ('https://farside.co.uk/eth/', ['ETHA','ETHB','FETH','ETHW','TETH','ETHV','QETH','EZET','ETHE','ETH']),
    'SOL': ('https://farside.co.uk/sol/', ['BSOL','VSOL','FSOL','TSOL','SOEZ','GSOL']),
    'HYPE': ('https://farside.co.uk/hyp/', ['BHYP','THYP','HYPG']),
}
ETF_ISSUERS = {'IBIT':'BlackRock','ETHA':'BlackRock','ETHB':'BlackRock','FBTC':'Fidelity','FETH':'Fidelity','FSOL':'Fidelity','GBTC':'Grayscale','BTC':'Grayscale','ETHE':'Grayscale','ETH':'Grayscale','GSOL':'Grayscale','ARKB':'ARK 21Shares','MSBT':'Morgan Stanley','BTCW':'WisdomTree','BITB':'Bitwise','BSOL':'Bitwise','BHYP':'Bitwise'}
class _ETFTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables=[]; self.table=None; self.row=None; self.cell=None; self.depth=0
    def handle_starttag(self,tag,attrs):
        if tag=='table':
            if self.table is None: self.table=[]; self.depth=1
            else: self.depth+=1
        elif self.table is not None and tag=='tr': self.row=[]
        elif self.row is not None and tag in ('td','th'): self.cell=[]
    def handle_data(self,data):
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self,tag):
        if tag in ('td','th') and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split())); self.cell=None
        elif tag=='tr' and self.row is not None:
            if self.row: self.table.append(self.row)
            self.row=None
        elif tag=='table' and self.table is not None:
            self.depth-=1
            if self.depth==0: self.tables.append(self.table); self.table=None
def _etf_number(value):
    v=str(value or '').strip().replace(',','').replace(chr(36),'')

    if not v or v in ('-','—','N/A','Pending'): return None
    if v.startswith('(') and v.endswith(')'): v='-'+v[1:-1]
    try: return float(v)
    except ValueError: return None
def _parse_etf_table(html, tickers):
    parser=_ETFTableParser(); parser.feed(html)
    found=[]
    for table in parser.tables:
        # Find the actual ticker header rather than assuming table/column position.
        headers=[]
        for row in table[:10]:
            matches=[(i,cell.upper()) for i,cell in enumerate(row) if cell.upper() in tickers]
            if len(matches)>=2: headers=matches; break
        if not headers: continue
        daily=[]
        for row in table:
            if not row: continue
            try: date=datetime.strptime(row[0].strip(),'%d %b %Y').date().isoformat()
            except ValueError: continue
            funds=[]
            for i,ticker in headers:
                if i<len(row):
                    v=_etf_number(row[i])
                    funds.append({'ticker':ticker,'issuer':ETF_ISSUERS.get(ticker,ticker),'flow_musd':v})
            # A blank row is not a confirmed zero-flow session.
            valid=[x['flow_musd'] for x in funds if x['flow_musd'] is not None]
            if not valid: continue
            total=_etf_number(row[-1])
            if total is None: continue
            daily.append({'date':date,'total_musd':total,'funds':funds,
                          'complete':len(valid)==len(funds)})
        if daily: found.extend(daily)
    if not found: raise ValueError('No valid dated ETF fund-flow table')
    unique={x['date']:x for x in found}
    return [unique[k] for k in sorted(unique,reverse=True)][:30]
def _coinboss_etf_rows(detail, totals):
    """Normalize documented CoinBoss USD-million matrix and aggregate histories."""
    if not isinstance(detail, dict) or detail.get('success') is not True:
        raise ValueError('CoinBoss detail returned no successful payload')
    if detail.get('unit') != 'million_usd':
        raise ValueError('Unexpected ETF detail units')
    matrix = detail.get('data')
    if not isinstance(matrix, dict):
        raise ValueError('Missing ETF per-fund matrix')
    columns = detail.get('tickers') or []
    if not isinstance(columns, list):
        raise ValueError('Invalid ETF ticker list')
    issuers = {c.get('ticker'): c.get('issuer') for c in columns
               if isinstance(c, dict) and c.get('ticker') and c.get('ticker') != 'TOTAL'}
    flow_by_date = {}
    if isinstance(totals, dict) and totals.get('success') is True and totals.get('unit') == 'million_usd':
        for rec in totals.get('data', []):
            if isinstance(rec, dict) and isinstance(rec.get('date'), str):
                val = rec.get('flow')
                if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val):
                    flow_by_date[rec['date']] = float(val)
    dates = set(matrix) | set(flow_by_date)
    rows = []
    for date in sorted(dates, reverse=True):
        try: datetime.strptime(date, '%Y-%m-%d')
        except (ValueError, TypeError): continue
        cells = matrix.get(date) or {}
        if not isinstance(cells, dict): cells = {}
        funds = []
        for ticker, issuer in issuers.items():
            val = cells.get(ticker)
            flow = (float(val) if isinstance(val, (int,float)) and not isinstance(val,bool) and math.isfinite(val) else None)
            funds.append({'ticker':ticker,'issuer':issuer or ETF_ISSUERS.get(ticker,ticker),'flow_musd':flow})
        total = cells.get('TOTAL')
        if not isinstance(total,(int,float)) or isinstance(total,bool) or not math.isfinite(total):
            total = flow_by_date.get(date)
        if total is None: continue
        rows.append({'date':date,'total_musd':float(total),'funds':funds,
                     'complete':bool(funds) and all(f['flow_musd'] is not None for f in funds)})
    if not rows:
        raise ValueError('No verified dated ETF flows')
    return rows[:30]


async def refresh_etf():
    """Daily ETF flow monitor; isolated from the 2-minute futures signal engine."""
    results = {}; errors = {}
    async with httpx.AsyncClient(timeout=18,follow_redirects=True,
            headers={'User-Agent':'BTC-Intelligence/0.9.7','Accept':'application/json'}) as client:
        async def one(asset):
            base='https://www.coinboss.com/api/etf/'
            detail_url=base+'detail'
            flows_url=base+'flows'
            try:
                detail_resp, flow_resp = await asyncio.gather(
                    client.get(detail_url,params={'asset':asset,'days':30}),
                    client.get(flows_url,params={'asset':asset,'days':30}))
                detail_resp.raise_for_status()
                flow_resp.raise_for_status()
                rows=_coinboss_etf_rows(detail_resp.json(),flow_resp.json())
                return asset,{'date':rows[0]['date'],'total_musd':rows[0]['total_musd'],
                              'funds':rows[0]['funds'],'complete':rows[0]['complete'],
                              'history':rows,'source':detail_url+'?asset='+asset+'&days=30',
                              'source_name':'CoinBoss'}
            except Exception as exc:
                LOG.warning('CoinBoss ETF %s unavailable: %s',asset,str(exc)[:180])
                return asset,{'error':str(exc)[:130]}
        data=await asyncio.gather(*(one(asset) for asset in ('BTC','ETH','SOL','HYPE')))
    for asset,value in data:
        if 'error' in value: errors[asset]=value['error']
        else: results[asset]=value
    async with LOCK:
        previous=STATE.get('etf',{}).get('assets',{})
        previous.update(results)
        STATE['etf']={'status':'online' if results else 'unavailable',
                      'assets':previous,'errors':errors,
                      'updated':datetime.now(timezone.utc).isoformat(),
                      'unsupported':['ZEC','LINK','DOGE','NEAR','TRX','DOT','LTC','BNB','AVAX','XRP'],
                      'note':'Daily US spot ETF flows, USD millions; delayed and subject to revision. Missing is not zero. Excluded from LONG/SHORT scoring.'}


async def etf_loop():
    while True:
        try: await refresh_etf()
        except Exception: LOG.exception('ETF refresh error')
        await asyncio.sleep(1800)

async def loop():
    while True:
        started=asyncio.get_running_loop().time()
        try: await refresh()
        except Exception: LOG.exception('Refresh error')
        elapsed=asyncio.get_running_loop().time()-started
        await asyncio.sleep(max(1,120-elapsed))


async def wintermute_loop():
    while True:
        try:
            observations = record_wintermute_unavailable()
            async with LOCK:
                STATE['wintermute'] = observations
        except Exception:
            LOG.exception('Wintermute observation logging error')
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app):
    init_db()
    task=asyncio.create_task(loop())
    wintermute_task=asyncio.create_task(wintermute_loop())
    etf_task=asyncio.create_task(etf_loop())
    yield
    task.cancel()
    wintermute_task.cancel()
    etf_task.cancel()
    try: await task
    except asyncio.CancelledError: pass
    try: await wintermute_task
    except asyncio.CancelledError: pass
    try: await etf_task
    except asyncio.CancelledError: pass

app=FastAPI(title='BTC Intelligence',version=STRATEGY_VERSION,lifespan=lifespan)


@app.middleware('http')
async def optional_private_access(request: Request, call_next):
    password = os.getenv('DASHBOARD_PASSWORD', '')
    if password and request.url.path != '/api/health':
        auth = request.headers.get('Authorization', '')
        username, received = '', ''
        if auth.startswith('Basic '):
            try:
                username, received = b64decode(auth[6:]).decode('utf-8').split(':', 1)
            except (ValueError, UnicodeDecodeError):
                pass
        if not (compare_digest(username, 'trader') and compare_digest(received, password)):
            return Response(status_code=401, headers={'WWW-Authenticate':'Basic realm="Market Intelligence"'})
    return await call_next(request)
app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')

@app.get('/')
async def index(): return FileResponse(ROOT/'static'/'index.html',headers={'Cache-Control':'no-store, max-age=0, must-revalidate'})

@app.get('/api/futures-feed-check')
async def futures_feed_check():
    """Read-only reachability check from the actual Render host; no fallback to spot."""
    endpoints = {
        'binance_usdm_last': 'https://fapi.binance.com/fapi/v1/ticker/price',
        'binance_usdm_mark': 'https://fapi.binance.com/fapi/v1/premiumIndex',
        'binance_usdm_candles': 'https://fapi.binance.com/fapi/v1/klines',
        'binance_usdm_alternate': 'https://fapi1.binance.com/fapi/v1/ticker/price',
    }
    results = {}
    async with httpx.AsyncClient(timeout=6) as client:
        for name,url in endpoints.items():
            try:
                params={'symbol':'BTCUSDT'}
                if name=='binance_usdm_candles':
                    params.update(interval='1m',limit=2)
                response=await client.get(url,params=params)
                results[name]={'reachable':response.status_code==200,'http_status':response.status_code}
            except httpx.HTTPError as exc:
                results[name]={'reachable':False,'error':type(exc).__name__}
    return {'version':STRATEGY_VERSION,'results':results}


@app.get('/api/health')
async def health(): return {'ok':True,'version':STATE['version'],'updated':STATE['updated'],'assets_live':list(STATE['assets'])}

@app.get('/api/state')
async def state():
    async with LOCK:
        data=json.loads(json.dumps(STATE))
        data.pop('observation_candles',None)
        for rows in data['liquidity_history'].values():
            for row in rows:
                row.pop('book_snapshot',None)
        # Never expose migrated legacy rows, including any stale in-memory snapshot.
        data['signal_log']={symbol:[row for row in rows if row.get('strategy_version') in ('0.9.2', STRATEGY_VERSION)][:5]
                            for symbol,rows in data['signal_log'].items()}
        return JSONResponse(data,headers={'Cache-Control':'no-store, max-age=0'})

 
@app.post('/api/reconcile-cached-signals')
async def reconcile_cached_signals(request: Request):
    """Re-check browser-only paper rows after ephemeral server DB replacement.

    Read-only: browser-provided signals are never inserted in server history.
    """
    raw=await request.body()
    if len(raw)>12000:
        return JSONResponse({'error':'Request too large'},status_code=413)
    try:
        payload=json.loads(raw)
        symbol=payload['symbol']
        rows=payload['rows']
        if symbol not in SYMBOLS or not isinstance(rows,list) or len(rows)>5:
            raise ValueError('Unsupported symbol or row count')
        cleaned=[]
        for row in rows:
            if row.get('side') not in ('LONG','SHORT') or not isinstance(row.get('ts'),str):
                raise ValueError('Invalid signal metadata')
            values={key:float(row[key]) for key in ('entry','stop','tp1','tp2','tp3')}
            if not all(math.isfinite(value) and value>0 for value in values.values()):
                raise ValueError('Invalid price levels')
            cleaned.append({**{key:row.get(key) for key in ('ts','symbol','side','status',
                            'strategy_version','candle_ts','model_score','setup_type',
                            'checked_until_ms','observed_at','observation_source',
                            'coverage','verification_note')},**values})
        async with LOCK:
            candles=STATE['observation_candles'].get(symbol)
            source=STATE['assets'].get(symbol,{}).get('observation_source','Exchange spot · 1M OHLC')
        results=[reconcile_signal({**row,'status':row['status'] or 'TRACKING'},candles,source) for row in cleaned]
        for row in results:
            row['history_source']='BROWSER RECOVERED'
        return {'rows':results}
    except (KeyError,ValueError,TypeError,OverflowError,AttributeError) as exc:
        return JSONResponse({'error':str(exc)[:100]},status_code=400)

@app.get('/api/signals')
async def signals():
    with open_db() as db:
        db.row_factory=sqlite3.Row
        return {'signals':[dict(r) for r in db.execute('SELECT * FROM signals ORDER BY id DESC LIMIT 50')]}


@app.get('/api/wintermute')
async def wintermute():
    async with LOCK:
        return {'observations':list(STATE['wintermute']),'has_verified_trade_feed':False}
