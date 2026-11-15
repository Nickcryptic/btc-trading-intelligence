"""BTC/ETH intelligence dashboard. Signals are research-only, not execution instructions."""
import asyncio
import json
import logging
import math
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from secrets import compare_digest
from base64 import b64decode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent
DB_PATH = Path(os.getenv('DB_PATH', str(ROOT / 'signals.db')))
SYMBOLS = ('BTCUSDT', 'ETHUSDT')
LOG = logging.getLogger('market')
logging.basicConfig(level=logging.INFO)
STATE = {'updated': None, 'assets': {}, 'macro': {}, 'source_errors': {}, 'wintermute': [], 'decision_log': {}, 'signal_log': {}, 'previous_direction': {}, 'version': '0.4.0'}
LOCK = asyncio.Lock()
MACRO_CACHE = {'expires': 0, 'value': {}}


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.execute('CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT, side TEXT, entry REAL, stop REAL, tp1 REAL, tp2 REAL, tp3 REAL, bias REAL, timing REAL, status TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS wintermute (id INTEGER PRIMARY KEY, ts TEXT, signal TEXT, note TEXT)')


def record_wintermute_unavailable():
    """An observation is NOT a buy/sell classification when no wallet feed exists."""
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as db:
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
    for side,rows in (('BID',bids),('ASK',asks)):
        bands={}
        for p,q in rows:
            bucket=math.floor(p/step)
            bands[bucket]=bands.get(bucket,0)+p*q
        for bucket,amount in bands.items():
            level=round((bucket+0.5)*step,2)
            clusters.append({'side':side,'price':level,'usd':round(amount,2),
                             'distance_pct':round((level/price-1)*100,3)})
    clusters=sorted(clusters,key=lambda x:x['usd'],reverse=True)[:8]
    def ladder(rows):
        return [{'price':round(p,2),'quantity':round(q,6),'usd':round(p*q,2)} for p,q in rows[:7]]
    return {'source':'Spot L2 order book · top 100 levels per side',
            'best_bid':round(bids[0][0],2),'best_ask':round(asks[0][0],2),
            'bid_usd':round(bid_usd,2),'ask_usd':round(ask_usd,2),
            'bid_share':round(bid_usd/total*100,1) if total else None,
            'bids':ladder(bids),'asks':ladder(asks),'clusters':clusters,
            'band_width':round(step,2)}


def observed_signal_status(row, price):
    """Update only from observed poll prices; never claim that a limit order filled."""
    old=row['status']
    if old in ('SL SEEN','SL AFTER TP1','SL AFTER TP2','TP3 SEEN'):
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


def record_signal_log(symbol, asset, previous_direction):
    """Store distinct/test candidate observations, no more than once per 30m for unchanged bias."""
    now=datetime.now(timezone.utc)
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory=sqlite3.Row
        existing=list(db.execute('SELECT * FROM signals WHERE symbol=? ORDER BY id DESC LIMIT 50',(symbol,)))
        for row in existing:
            next_status=observed_signal_status(row,asset['price'])
            if next_status!=row['status']:
                db.execute('UPDATE signals SET status=? WHERE id=?',(next_status,row['id']))
        direction=asset['direction']
        levels=asset['levels']
        if levels and direction in ('LONG CANDIDATE','SHORT CANDIDATE'):
            last=existing[0] if existing else None
            elapsed=(now-datetime.fromisoformat(last['ts'])).total_seconds() if last else float('inf')
            side='LONG' if direction.startswith('LONG') else 'SHORT'
            newly_issued=previous_direction!=direction
            if not last or (newly_issued and (last['side']!=side or elapsed>=120)) or elapsed>=1800:
                db.execute('INSERT INTO signals(ts,symbol,side,entry,stop,tp1,tp2,tp3,bias,timing,status) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (now.isoformat(),symbol,side,levels['entry'],levels['stop'],
                            levels['tp1'],levels['tp2'],levels['tp3'],asset['bias'],asset['timing'],'TRACKING'))
        db.execute('DELETE FROM signals WHERE id NOT IN (SELECT id FROM signals ORDER BY id DESC LIMIT 100)')
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
    MACRO_CACHE.update({'expires':time.monotonic()+600,'value':current})
    return current


def calc_asset(symbol, k15, k1h, k4h, kd, ticker, book, trades, funding, oi, spot_source='Binance spot', market_pair=None, macro=None):
    macro=macro or {}
    c15=[x['close'] for x in k15]; c1=[x['close'] for x in k1h]; c4=[x['close'] for x in k4h]; cd=[x['close'] for x in kd]
    price=float(ticker['lastPrice'])
    daily_ma50=sum(cd[-50:])/50 if len(cd)>=50 else None
    daily_ma200=sum(cd[-200:])/200 if len(cd)>=200 else None
    e1=ema(c1[-101:-1],21); e4=ema(c4[-101:-1],21)
    # Bias uses closed candles, with daily regime and 4h trend; never fill missing inputs with fake values.
    bias_parts=[]
    if daily_ma200: bias_parts.append((35, 1 if cd[-2]>daily_ma200 else -1))
    if daily_ma50 and daily_ma200: bias_parts.append((25,1 if daily_ma50>daily_ma200 else -1))
    if e4: bias_parts.append((40,1 if c4[-2]>e4 else -1))
    fear=macro.get('fear_greed')
    stable=macro.get('stablecoins')
    if fear:
        sentiment=(fear['value']-50)*2
        bias_parts.append((10,1 if sentiment>=30 else -1 if sentiment<=-30 else 0))
    if stable:
        supply=stable['change30d']
        bias_parts.append((10,1 if supply>=0.5 else -1 if supply<=-0.5 else 0))
    bias=round(sum(w*s for w,s in bias_parts)/sum(w for w,s in bias_parts)*100) if bias_parts else None
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
    timing_parts=[]
    if e1: timing_parts.append((30,1 if c1[-2]>e1 else -1))
    if change15 is not None: timing_parts.append((25,1 if change15>0 else -1 if change15<0 else 0))
    if trade_imbalance is not None: timing_parts.append((25,max(-1,min(1,trade_imbalance*5))))
    if imbalance is not None: timing_parts.append((20,max(-1,min(1,imbalance*4))))
    timing=round(sum(w*s for w,s in timing_parts)/sum(w for w,s in timing_parts)*100) if timing_parts else None
    direction='NO TRADE'
    if bias is not None and timing is not None:
        if bias>=30 and timing>=30: direction='LONG CANDIDATE'
        if bias<=-30 and timing<=-30: direction='SHORT CANDIDATE'
    a=atr(k15[:-1]); support=min(x['low'] for x in k1h[-26:-1]); resistance=max(x['high'] for x in k1h[-26:-1]);
    # Candidate levels are indicative only; do not issue actionable alerts until validation gates exist.
    levels=None
    if direction!='NO TRADE' and a:
        side=1 if direction.startswith('LONG') else -1
        entry=price; stop=entry-side*max(1.5*a,price*.003)
        risk=abs(entry-stop)
        levels={'entry':round(entry,2),'stop':round(stop,2),'tp1':round(entry+side*risk,2),'tp2':round(entry+side*2*risk,2),'tp3':round(entry+side*3*risk,2),'status':'ILLUSTRATIVE / NOT CONFIRMED'}
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
        {'name':'Funding Rate','value':f'{float(funding)*100:.4f}%' if funding is not None else None,'signal':'NEUTRAL' if funding is not None else 'UNAVAILABLE','source':'Binance futures (if accessible)'},
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
    return {'symbol':symbol,'market_source':spot_source,'market_pair':market_pair or symbol,'price':round(price,2),'change24h':round(float(ticker['priceChangePercent']),2),'bias':bias,'timing':timing,'bias_label':score_label(bias),'timing_label':score_label(timing),'direction':direction,'test_confirmation':direction!='NO TRADE','levels':levels,'indicators':indicators,'fvg':zones,'fvg_by_timeframe':zones_by_timeframe,'order_book':order_book_snapshot(book,price),'support':round(support,2),'resistance':round(resistance,2),'rsi15':rsi15,'spread_bps':round(spread,2) if spread is not None else None,'imbalance':round(imbalance*100,1) if imbalance is not None else None,'data_status':'PARTIAL — unconnected sources are excluded','updated':datetime.now(timezone.utc).isoformat(),'confirmed':False,'confirmation_reason':'Test confluence evaluated live; production-grade signal confirmation is pending validation'}


def parse_klines(rows):
    return [{'ts':int(x[0]),'open':float(x[1]),'high':float(x[2]),'low':float(x[3]),'close':float(x[4]),'volume':float(x[5])} for x in rows]


async def fetch_json(client,url,params=None):
    r=await client.get(url,params=params,timeout=12)
    r.raise_for_status()
    return r.json()


async def fetch_asset_binance(client,symbol,macro=None):
    base='https://api.binance.com/api/v3/'
    fut='https://fapi.binance.com/fapi/v1/'
    queries=[fetch_json(client,base+'klines',{'symbol':symbol,'interval':tf,'limit':limit}) for tf,limit in [('15m',160),('1h',160),('4h',160),('1d',260)]]
    queries += [fetch_json(client,base+'ticker/24hr',{'symbol':symbol}),fetch_json(client,base+'depth',{'symbol':symbol,'limit':100}),fetch_json(client,base+'trades',{'symbol':symbol,'limit':1000}),fetch_json(client,fut+'premiumIndex',{'symbol':symbol}),fetch_json(client,fut+'openInterest',{'symbol':symbol})]
    out=await asyncio.gather(*queries,return_exceptions=True)
    if any(isinstance(x,Exception) for x in out[:7]):
        raise RuntimeError('Core market feed unavailable: '+', '.join(str(x)[:80] for x in out[:7] if isinstance(x,Exception)))
    k15,k1,k4,kd=[parse_klines(x) for x in out[:4]]
    funding=None if isinstance(out[7],Exception) else out[7].get('lastFundingRate')
    oi=None if isinstance(out[8],Exception) else float(out[8].get('openInterest',0))
    return calc_asset(symbol,k15,k1,k4,kd,out[4],out[5],out[6],funding,oi,market_pair=symbol.replace('USDT','/USDT'),macro=macro)


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
                [('Ticker',{}),('Depth',{'count':100}),('Trades',{'count':1000})]]
    responses = await asyncio.gather(*queries,return_exceptions=True)
    if any(isinstance(x,Exception) for x in responses):
        raise RuntimeError('Kraken market feed unavailable: '+', '.join(str(x)[:80] for x in responses if isinstance(x,Exception)))
    k15,k1,k4,kd = [parse_kraken_ohlc(kraken_result(x)) for x in responses[:4]]
    if min(len(k15),len(k1),len(k4),len(kd)) < 201:
        raise RuntimeError('Insufficient Kraken candle history for 200D regime')
    raw_ticker, book, raw_trades = [kraken_result(x) for x in responses[4:]]
    last_price = float(raw_ticker['c'][0])
    # Kraken ticker's opening price is midnight UTC; calculate an approximate rolling 24h change instead.
    change24h = (last_price/float(k1[-25]['close'])-1)*100
    ticker = {'lastPrice':last_price,'priceChangePercent':change24h}
    trades = [{'qty':x[1],'isBuyerMaker':x[3]=='s'} for x in raw_trades]
    result = calc_asset(symbol,k15,k1,k4,kd,ticker,book,trades,None,None,
                        spot_source='Kraken spot',market_pair=('BTC/USD' if symbol=='BTCUSDT' else 'ETH/USD'),macro=macro)
    result['change_approximate']=True
    result['provider_note']='Kraken USD spot; Binance unavailable on this deployment. No futures funding/OI.'
    return result


async def fetch_asset(client,symbol,macro=None):
    provider=os.getenv('MARKET_PROVIDER','auto').lower()
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
    async with httpx.AsyncClient(headers={'User-Agent':'BTC-Intelligence/0.4'}) as client:
        macro=await fetch_macro(client)
        results=await asyncio.gather(*(fetch_asset(client,s,macro) for s in SYMBOLS),return_exceptions=True)
    async with LOCK:
        for symbol,result in zip(SYMBOLS,results):
            if isinstance(result,Exception):
                STATE['source_errors'][symbol]=str(result)[:180]
                LOG.warning('%s refresh failed: %s',symbol,result)
            else:
                previous=STATE['previous_direction'].get(symbol)
                STATE['assets'][symbol]=result
                STATE['source_errors'].pop(symbol,None)
                STATE['signal_log'][symbol]=record_signal_log(symbol,result,previous)
                STATE['previous_direction'][symbol]=result['direction']
                history=STATE['decision_log'].setdefault(symbol,[])
                ts=datetime.now(timezone.utc)
                if not history or (ts-datetime.fromisoformat(history[0]['ts'])).total_seconds()>=300:
                    history.insert(0,{'ts':ts.isoformat(),'direction':result['direction'],
                                      'bias':result['bias'],'timing':result['timing']})
                    del history[10:]
        STATE['macro']=macro
        STATE['updated']=datetime.now(timezone.utc).isoformat()


async def loop():
    while True:
        try: await refresh()
        except Exception: LOG.exception('Refresh error')
        await asyncio.sleep(120)


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
    yield
    task.cancel()
    wintermute_task.cancel()
    try: await task
    except asyncio.CancelledError: pass
    try: await wintermute_task
    except asyncio.CancelledError: pass

app=FastAPI(title='BTC Intelligence',version='0.4.0',lifespan=lifespan)


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
async def index(): return FileResponse(ROOT/'static'/'index.html')

@app.get('/api/health')
async def health(): return {'ok':True,'version':STATE['version'],'updated':STATE['updated'],'assets_live':list(STATE['assets'])}

@app.get('/api/state')
async def state():
    async with LOCK:
        return json.loads(json.dumps(STATE))

@app.get('/api/signals')
async def signals():
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory=sqlite3.Row
        return {'signals':[dict(r) for r in db.execute('SELECT * FROM signals ORDER BY id DESC LIMIT 50')]}


@app.get('/api/wintermute')
async def wintermute():
    async with LOCK:
        return {'observations':list(STATE['wintermute']),'has_verified_trade_feed':False}
