"""BTC/ETH intelligence dashboard. Signals are research-only, not execution instructions."""
import asyncio
import json
import logging
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
STATE = {'updated': None, 'assets': {}, 'fear_greed': None, 'source_errors': {}, 'wintermute': [], 'version': '0.2.0'}
LOCK = asyncio.Lock()


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


def score_label(score):
    if score is None: return 'UNAVAILABLE'
    if score >= 30: return 'BUY'
    if score <= -30: return 'SELL'
    return 'NEUTRAL'


def calc_asset(symbol, k15, k1h, k4h, kd, ticker, book, trades, funding, oi):
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
    indicators=[
        {'name':'200D Moving Average','value':f'${daily_ma200:,.0f}' if daily_ma200 else None,'signal':score_label(50 if daily_ma200 and price>daily_ma200 else -50 if daily_ma200 else None),'source':'Binance spot'},
        {'name':'50D Moving Average','value':f'${daily_ma50:,.0f}' if daily_ma50 else None,'signal':score_label(50 if daily_ma50 and price>daily_ma50 else -50 if daily_ma50 else None),'source':'Binance spot'},
        {'name':'Funding Rate','value':f'{float(funding)*100:.4f}%' if funding is not None else None,'signal':'NEUTRAL' if funding is not None else 'UNAVAILABLE','source':'Binance futures'},
        {'name':'Open Interest','value':f'{oi:,.0f} units' if oi is not None else None,'signal':'NEUTRAL' if oi is not None else 'UNAVAILABLE','source':'Binance futures'},
        {'name':'Order Book Imbalance','value':f'{imbalance*100:+.1f}%' if imbalance is not None else None,'signal':score_label(imbalance*400 if imbalance is not None else None),'source':'Binance top 100 levels'},
        {'name':'Bid/Ask Spread','value':f'{spread:.2f} bps' if spread is not None else None,'signal':'NEUTRAL' if spread is not None else 'UNAVAILABLE','source':'Binance top of book'},
        {'name':'Recent Trade Delta (CVD proxy)','value':f'{trade_delta:+,.3f} units','signal':score_label(trade_imbalance*500 if trade_imbalance is not None else None),'source':'Binance last 1000 trades'},
        {'name':'1H Momentum','value':f'{change1:+.2f}%' if change1 is not None else None,'signal':score_label(change1*100 if change1 is not None else None),'source':'Binance candles'},
        {'name':'15M Momentum','value':f'{change15:+.2f}%' if change15 is not None else None,'signal':score_label(change15*100 if change15 is not None else None),'source':'Binance candles'},
    ]
    return {'symbol':symbol,'price':round(price,2),'change24h':round(float(ticker['priceChangePercent']),2),'bias':bias,'timing':timing,'bias_label':score_label(bias),'timing_label':score_label(timing),'direction':direction,'levels':levels,'indicators':indicators,'chart':[{'ts':x['ts'],'close':x['close']} for x in k15[-96:]],'fvg':fvg_zones(k15[:-1]),'support':round(support,2),'resistance':round(resistance,2),'rsi15':rsi(c15[:-1]),'spread_bps':round(spread,2) if spread is not None else None,'imbalance':round(imbalance*100,1) if imbalance is not None else None,'data_status':'PARTIAL — premium feeds not connected','updated':datetime.now(timezone.utc).isoformat(),'confirmed':False,'confirmation_reason':'22-indicator market coverage and backtesting incomplete'}


def parse_klines(rows):
    return [{'ts':int(x[0]),'open':float(x[1]),'high':float(x[2]),'low':float(x[3]),'close':float(x[4]),'volume':float(x[5])} for x in rows]


async def fetch_json(client,url,params=None):
    r=await client.get(url,params=params,timeout=12)
    r.raise_for_status()
    return r.json()


async def fetch_asset(client,symbol):
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
    return calc_asset(symbol,k15,k1,k4,kd,out[4],out[5],out[6],funding,oi)


async def refresh():
    async with httpx.AsyncClient(headers={'User-Agent':'BTC-Intelligence/0.1'}) as client:
        results=await asyncio.gather(*(fetch_asset(client,s) for s in SYMBOLS),return_exceptions=True)
        fg=None
        try:
            raw=await fetch_json(client,'https://api.alternative.me/fng/',{'limit':1})
            fg={'value':int(raw['data'][0]['value']),'classification':raw['data'][0]['value_classification'],'updated':int(raw['data'][0]['timestamp'])}
        except Exception as exc: LOG.warning('Fear/greed unavailable: %s',exc)
    async with LOCK:
        for symbol,result in zip(SYMBOLS,results):
            if isinstance(result,Exception):
                STATE['source_errors'][symbol]=str(result)[:180]
                LOG.warning('%s refresh failed: %s',symbol,result)
            else:
                STATE['assets'][symbol]=result
                STATE['source_errors'].pop(symbol,None)
        STATE['fear_greed']=fg
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

app=FastAPI(title='BTC Intelligence',version='0.2.0',lifespan=lifespan)


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
