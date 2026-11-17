import tempfile
import sqlite3
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
import httpx

import app
import math

from app import ema, rsi, fvg_zones, score_label, kraken_result, parse_kraken_ohlc, parse_stablecoin_growth, halving_context, calc_asset, order_book_snapshot, observed_signal_status, reconcile_signal, recent_tape_snapshot, liquidity_observation, wide_liquidity_snapshot, parse_bybit_wide_book, build_broad_liquidity, professional_decision, structure_liquidity_targets, replay_recent_candles

def test_ema_constant():
    assert ema([10]*30,21)==10

def test_rsi_rising():
    assert rsi(list(range(20)))==100

def test_fvg():
    c=[{'high':10,'low':8,'ts':1},{'high':12,'low':10,'ts':2},{'high':14,'low':11,'ts':3}]
    assert fvg_zones(c)[0]['kind']=='bullish'

def test_score():
    assert score_label(31)=='BUY'
    assert score_label(-31)=='SELL'
    assert score_label(None)=='UNAVAILABLE'


def test_kraken_spot_mapping():
    candle = [1688671200, '30306.1', '30306.2', '30305.7', '30305.7', '30306.1', '3.39243896', 23]
    payload = {'error': [], 'result': {'XXBTZUSD': [candle], 'last': 1688672160}}
    assert kraken_result(payload) == [candle]
    mapped = parse_kraken_ohlc(kraken_result(payload))[0]
    assert mapped['ts'] == 1688671200000
    assert mapped['close'] == 30305.7
    assert mapped['volume'] == 3.39243896


def test_kraken_error_is_not_silent():
    try:
        kraken_result({'error': ['EQuery:Unknown asset pair'], 'result': {}})
    except RuntimeError as exc:
        assert 'Unknown asset pair' in str(exc)
    else:
        raise AssertionError('Kraken API error must not be interpreted as market data')


def test_stablecoin_30_day_supply():
    rows = [
        {'date':str(1700000000+i*86400), 'totalCirculatingUSD':{'peggedUSD':100_000_000_000+i*70_000_000}}
        for i in range(32)
    ]
    reading=parse_stablecoin_growth(rows)
    assert reading['signal']=='BUY'
    assert reading['change30d']>0.5
    assert reading['value']>100_000_000_000
    assert parse_stablecoin_growth([]) is None


def test_halving_is_context_only():
    reading=halving_context(840_100)
    assert reading['next_height']==1_050_000
    assert reading['blocks_remaining']==209_900
    assert reading['signal']=='NEUTRAL'


def test_weak_confluence_is_no_longer_a_trade_signal():
    def candles(step):
        return [{'ts':i*60000,'open':80_000+i*step-2,'high':80_000+i*step+12,
                 'low':80_000+i*step-12,'close':80_000+i*step,'volume':2.0} for i in range(260)]
    upward=candles(3)
    bid_heavy={'bids':[['80777','9.0',0]],'asks':[['80779','1.0',0]]}
    ticker={'lastPrice':'80777','priceChangePercent':'2.4'}
    result=calc_asset('BTCUSDT',upward,upward,upward,upward,ticker,bid_heavy,
                      [{'qty':'4','isBuyerMaker':False}],None,None)
    assert result['direction']=='NO TRADE'
    assert result['test_confirmation'] is False
    assert result['confirmed'] is False
    assert result['levels'] is None
    assert result['signal_checks']
    assert set(result['fvg_by_timeframe'])=={'15m','1h','1d'}
    assert all(isinstance(result['fvg_by_timeframe'][tf],list) for tf in ('15m','1h','1d'))
    assert result['order_book']['bid_share']>50
    by_name={indicator['name']:indicator for indicator in result['indicators']}
    assert by_name['200D Moving Average']['source_as_of'].endswith('+00:00')
    assert by_name['15M RSI']['source_as_of'].endswith('+00:00')
    assert by_name['15M RSI']['fetched_at']
    assert by_name['Order Book Imbalance']['source_as_of'] is None
    assert by_name['Funding Rate']['fetched_at'] is None

    # A bullish regime with bearish fast timing must produce no trade.
    negative_trades=[{'qty':'10','isBuyerMaker':True}]
    ask_heavy={'bids':[['80777','1.0',0]],'asks':[['80779','9.0',0]]}
    mixed=calc_asset('BTCUSDT',candles(-10),upward,upward,upward,ticker,ask_heavy,
                     negative_trades,None,None)
    assert mixed['direction']=='NO TRADE'
    assert mixed['test_confirmation'] is False
    assert mixed['levels'] is None


def test_wide_liquidity_rejects_near_market_noise_and_keeps_distant_sides():
    bids=[[str(99.95-i*.05),'1'] for i in range(100)]
    asks=[[str(100.05+i*.05),'1'] for i in range(100)]
    bids[30][1]='70'; asks[40][1]='80'
    reading=wide_liquidity_snapshot({'bids':bids,'asks':asks},100,'Test spot',max_levels=1000)
    assert reading['range_min_pct']==0.5
    assert reading['coverage']['bid_levels']==100
    assert reading['bids'] and reading['asks']
    assert all(-5<=x['distance_pct']<=-0.5 for x in reading['bids'])
    assert all(0.5<=x['distance_pct']<=5 for x in reading['asks'])
    assert reading['bids'][0]['usd']>reading['bids'][-1]['usd']
    assert reading['asks'][0]['usd']>reading['asks'][-1]['usd']
    close=wide_liquidity_snapshot({'bids':[['99.9','100']],'asks':[['100.1','100']]},100,'Test spot')
    assert close['bids']==close['asks']==[]  # Never pass off near-price depth as distant walls.


def test_bybit_depth_validation_and_fallback():
    now=1_700_000_000_000
    payload={'retCode':0,'result':{'s':'BTCUSDT','ts':now,
             'b':[['99','10'],['98','50']], 'a':[['101','10'],['102','50']]}}
    book,ref=parse_bybit_wide_book(payload,'BTCUSDT',now_ms=now)
    assert ref==100 and len(book['bids'])==2
    try: parse_bybit_wide_book(payload,'ETHUSDT',now_ms=now)
    except RuntimeError: pass
    else: raise AssertionError('Symbol mismatch should be rejected')
    try: parse_bybit_wide_book(payload,'BTCUSDT',now_ms=now+180_000)
    except RuntimeError: pass
    else: raise AssertionError('Stale book should be rejected')
    fallback=build_broad_liquidity(RuntimeError('blocked'),'BTCUSDT',book,100,'Kraken spot')
    assert fallback['source']=='Kraken spot'


def test_full_kraken_refresh_keeps_optional_bybit_failure_nonfatal():
    sources={15:trend_candles(step=4),60:trend_candles(step=9),
             240:trend_candles(step=14),1440:trend_candles(step=17)}
    sources[1]=[{'ts':i*60000,'open':100,'high':110,'low':90,'close':100,'volume':1}
                for i in range(260)]
    price=sources[15][-2]['close']
    depth={'bids':[[str(price-.1-i*6),'1',0] for i in range(500)],
           'asks':[[str(price+.1+i*6),'1',0] for i in range(500)]}
    real_fetch=app.fetch_json
    bybit_enabled=True

    async def fake_fetch(client,url,params=None,timeout=12):
        if 'bybit' in url:
            if not bybit_enabled:raise RuntimeError('Regional access blocked')
            book={'b':[[str(price-.2-i*6),'1'] for i in range(1000)],
                  'a':[[str(price+.2+i*6),'1'] for i in range(1000)],
                  's':'BTCUSDT','ts':int(time.time()*1000)}
            return {'retCode':0,'result':book}
        if 'OHLC' in url:
            candles=sources[params['interval']]
            rows=[[c['ts']//1000,c['open'],c['high'],c['low'],c['close'],c['close'],c['volume'],10] for c in candles]
            return {'error':[],'result':{'XXBTZUSD':rows,'last':1}}
        if 'Ticker' in url:return {'error':[],'result':{'XXBTZUSD':{'c':[str(price)]}}}
        if 'Depth' in url:return {'error':[],'result':{'XXBTZUSD':depth}}
        if 'Trades' in url:return {'error':[],'result':{'XXBTZUSD':[],'last':1}}
        raise AssertionError(url)

    try:
        app.fetch_json=fake_fetch
        available=asyncio.run(app.fetch_asset_kraken(None,'BTCUSDT'))
        assert available['liquidity_map']['source'].startswith('Bybit')
        assert len(available['_observation_candles'])==259
        assert available['observation_source']=='Kraken spot · completed 1M OHLC'
        assert available['liquidity_map']['coverage']['bid_levels']==1000
        assert available['liquidity_map']['bids']
        assert available['order_book']['coverage']['bid_levels']==100
        bybit_enabled=False
        fallback=asyncio.run(app.fetch_asset_kraken(None,'BTCUSDT'))
        assert fallback['liquidity_map']['source'].startswith('Kraken')
        assert fallback['liquidity_map']['coverage']['bid_levels']==500
        assert fallback['liquidity_map']['asks']
    finally:
        app.fetch_json=real_fetch


def trend_candles(step=6,wiggle=35,volume=100):
    rows=[]
    for i in range(260):
        close=80000+i*step+wiggle*math.sin(i*.53)
        rows.append({'ts':i*900000,'open':close-12,'high':close+52,
                     'low':close-52,'close':close,'volume':volume})
    return rows


def test_adaptive_model_offers_watch_then_long_without_volume_veto():
    fifteen=trend_candles(step=4,wiggle=38)
    hour=trend_candles(step=9,wiggle=12)
    four=trend_candles(step=14,wiggle=8)
    day=trend_candles(step=17,wiggle=5)
    # A clean higher-timeframe trend with no completed 15M trigger should WAIT.
    base=professional_decision(fifteen,hour,four,day,fifteen[-2]['close'],0.2)
    assert base['direction']=='NO TRADE'
    assert base['watch_direction']=='LONG'
    assert base['watch_levels']['entry']>fifteen[-2]['close']
    assert base['signal_score']>=65
    last=fifteen[-2]
    prior_high=max(c['high'] for c in fifteen[-18:-2])
    last['close']=prior_high+35
    last['open']=last['close']-72
    last['high']=last['close']+52
    last['low']=last['open']-55
    last['volume']=175
    price=last['close']+4
    confirmed=professional_decision(fifteen,hour,four,day,price,0.2)
    assert confirmed['direction']=='LONG CANDIDATE',confirmed
    assert confirmed['trigger']=='15M BREAKOUT'
    assert len(confirmed['checks'])==6
    assert confirmed['signal_score']>=65
    assert confirmed['levels']['tp2']>confirmed['levels']['tp1']>price>confirmed['levels']['stop']
    last['volume']=50
    weak=professional_decision(fifteen,hour,four,day,price,0.2)
    assert weak['direction']=='LONG CANDIDATE'  # Low volume is weak evidence, not an automatic veto.
    assert weak['signal_score']<confirmed['signal_score']
    assert weak['checks'][3]['ok'] is False
    last['volume']=175
    chased=professional_decision(fifteen,hour,four,day,price+300,0.2)
    assert chased['direction']=='NO TRADE'
    assert chased['checks'][4]['ok'] is False


def test_short_signal_uses_symmetric_score_and_risk():
    fifteen=trend_candles(step=-4,wiggle=38)
    hour=trend_candles(step=-9,wiggle=12)
    four=trend_candles(step=-14,wiggle=8)
    day=trend_candles(step=-17,wiggle=5)
    last=fifteen[-2]
    prior_low=min(c['low'] for c in fifteen[-18:-2])
    last['close']=prior_low-35
    last['open']=last['close']+72
    last['high']=last['open']+55
    last['low']=last['close']-52
    last['volume']=175
    price=last['close']-4
    reading=professional_decision(fifteen,hour,four,day,price,0.2)
    assert reading['direction']=='SHORT CANDIDATE',reading
    assert reading['levels']['stop']>price>reading['levels']['tp2']
    assert reading['signal_score']>=65
    assert reading['trigger']=='15M BREAKDOWN'


def test_invalid_spread_or_overextended_quote_stays_conditional():
    fifteen=trend_candles(step=4,wiggle=38)
    hour=trend_candles(step=9,wiggle=12)
    four=trend_candles(step=14,wiggle=8)
    day=trend_candles(step=17,wiggle=5)
    close=fifteen[-2]['close']
    baseline=professional_decision(fifteen,hour,four,day,close,0.2)
    assert baseline['watch_direction']=='LONG'
    # An actual qualified trigger cannot override spread or no-chase safeguards.
    candle=fifteen[-2]
    candle['close']=max(x['high'] for x in fifteen[-14:-2])+35
    candle['open']=candle['close']-60
    candle['high']=candle['close']+20
    candle['low']=candle['open']-20
    guarded=professional_decision(fifteen,hour,four,day,candle['close'],12)
    assert guarded['direction']=='NO TRADE'
    assert guarded['watch_direction']=='LONG'
    assert guarded['checks'][4]['ok'] is False
    far=professional_decision(fifteen,hour,four,day,candle['close']+500,0.2)
    assert far['direction']=='NO TRADE'
    assert far['checks'][4]['ok'] is False


def test_open_candles_cannot_repaint_completed_candle_signal():
    frames=[trend_candles(step,wiggle) for step,wiggle in ((4,38),(9,12),(14,8),(17,5))]
    price=frames[0][-2]['close']
    before=professional_decision(*frames,price,0.2)
    for rows in frames:
        rows[-1].update({'close':1,'high':1_000_000,'low':1,'volume':1_000_000})
    after=professional_decision(*frames,price,0.2)
    assert before['direction']==after['direction']
    assert before['signal_score']==after['signal_score']
    assert before['watch_levels']==after['watch_levels']


def test_live_direction_ignores_independent_book_pressure():
    f=trend_candles(step=4,wiggle=38)
    h=trend_candles(step=9,wiggle=12)
    g=trend_candles(step=14,wiggle=8)
    d=trend_candles(step=17,wiggle=5)
    last=f[-2]
    last['close']=max(c['high'] for c in f[-14:-2])+35
    last['open']=last['close']-72
    last['high']=last['close']+52
    last['low']=last['open']-55
    last['volume']=175
    price=last['close']+2
    ticker={'lastPrice':str(price),'priceChangePercent':'2'}
    def evaluate(bid_qty,ask_qty):
        book={'bids':[[str(price-.2),str(bid_qty)]], 'asks':[[str(price+.2),str(ask_qty)]]}
        return calc_asset('BTCUSDT',f,h,g,d,ticker,book,[],None,None)
    heavy_buy=evaluate(900,1)
    heavy_sell=evaluate(1,900)
    assert heavy_buy['direction']=='LONG CANDIDATE'
    assert heavy_sell['direction']==heavy_buy['direction']
    assert heavy_sell['signal_score']==heavy_buy['signal_score']


def test_replay_reports_costed_research_sample_or_no_history():
    assert replay_recent_candles([],[],[],[])['status']=='INSUFFICIENT HISTORY'
    end=260*86_400_000
    def bars(period_ms,n):
        def value(t):
            return 80_000+t/86_400_000*60+20*math.sin(t/3_600_000*.37)
        return [{'ts':end-(n-i)*period_ms,
                 'open':value(end-(n-i)*period_ms),
                 'high':value(end-(n-i)*period_ms)+90,
                 'low':value(end-(n-i)*period_ms)-90,
                 'close':value(end-(n-i-1)*period_ms),
                 'volume':100+20*math.sin(i*.3)} for i in range(n)]
    replay=replay_recent_candles(bars(900_000,690),bars(3_600_000,600),
                                 bars(14_400_000,700),bars(86_400_000,259))
    assert replay['status']=='LIMITED HISTORICAL REPLAY'
    assert replay['trades']>0
    assert replay['cost_bps']==20
    assert 0<=replay['win_rate_pct']<=100
    assert 'net_r' in replay


def test_order_book_depth_and_price_bands():
    book={'bids':[['100','10'],['99.9','5']],'asks':[['100.1','2'],['100.2','1']]}
    data=order_book_snapshot(book,100)
    assert data['bid_share']>80
    assert data['bids'][0]['price']==100
    assert data['asks'][0]['price']==100.1
    assert data['bid_usd']>data['ask_usd']
    assert any(x['side']=='BID' for x in data['clusters'])
    assert data['cluster_map']['bids'][0]['side']=='BID'
    assert data['cluster_map']['asks'][0]['side']=='ASK'
    assert data['coverage']=={'low':99.9,'high':100.2,'bid_levels':2,'ask_levels':2}
    assert order_book_snapshot({'bids':[],'asks':[]},100) is None


def test_liquidity_map_keeps_both_sides_with_actual_price_ranges():
    # A large number of heavy bid bands can occupy every globally ranked slot.
    book={'bids':[[str(100-i*0.11),'10'] for i in range(12)],
          'asks':[['100.5','0.01'],['100.51','0.02']]}
    data=order_book_snapshot(book,100.25)
    assert len(data['clusters'])==8
    assert all(wall['side']=='BID' for wall in data['clusters'])
    assert len(data['cluster_map']['bids'])==6
    assert len(data['cluster_map']['asks'])>=1
    assert all(wall['side']=='ASK' for wall in data['cluster_map']['asks'])
    for side in ('bids','asks'):
        for wall in data['cluster_map'][side]:
            assert wall['low']<=wall['high']
            assert wall['levels']>=1
            assert wall['usd']>0
    assert data['cluster_map']['asks'][0]['low'] in (100.5,100.51)
    assert data['cluster_map']['asks'][0]['high'] in (100.5,100.51)


def test_signal_milestones_from_polled_prices():
    # Legacy helper remains for compatibility; the journal no longer uses it.
    long={'status':'TRACKING','side':'LONG','stop':95,'tp1':105,'tp2':110,'tp3':115}
    assert observed_signal_status(long,104)=='TRACKING'
    assert observed_signal_status(long,105)=='TP1 SEEN'
    assert observed_signal_status({**long,'status':'TP1 SEEN'},94)=='SL AFTER TP1'
    assert observed_signal_status(long,115)=='TP3 SEEN'
    short={'status':'TRACKING','side':'SHORT','stop':105,'tp1':95,'tp2':90,'tp3':85}
    assert observed_signal_status(short,94)=='TP1 SEEN'
    assert observed_signal_status(short,106)=='SL SEEN'


def test_one_minute_candles_observe_short_stop_after_poll_missed_it():
    issued='2026-11-17T19:31:22+00:00'
    start=(int(datetime.fromisoformat(issued).timestamp()//60)+1)*60000
    short={'ts':issued,'status':'TRACKING','side':'SHORT','entry':1784.69,
           'stop':1805.77,'tp1':1770,'tp2':1750,'tp3':1730}
    below_stop=reconcile_signal(short,[{'ts':start,'high':1804,'low':1790}])
    assert below_stop['status']=='TRACKING'  # WATCH SL != journal stop.
    seen=reconcile_signal(short,[{'ts':start,'high':1806,'low':1785},
                                 {'ts':start+60000,'high':1800,'low':1785}])
    assert seen['status']=='SL SEEN'
    assert seen['observed_at']==datetime.fromtimestamp((start+60000)/1000,timezone.utc).isoformat()
    assert '1M OHLC' in seen['observation_source']


def test_candle_replay_respects_targets_stop_order_and_ambiguous_bar():
    issued='2026-11-17T19:31:22+00:00'
    first=(int(datetime.fromisoformat(issued).timestamp()//60)+1)*60000
    long={'ts':issued,'side':'LONG','status':'TRACKING','entry':100,
          'stop':95,'tp1':105,'tp2':110,'tp3':115}
    first_target=reconcile_signal(long,[{'ts':first,'high':106,'low':99}])
    assert first_target['status']=='TP1 SEEN'
    after_stop=reconcile_signal(first_target,[{'ts':first+60000,'high':104,'low':94}])
    assert after_stop['status']=='SL AFTER TP1'
    ambiguous=reconcile_signal(long,[{'ts':first,'high':106,'low':94}])
    assert ambiguous['status']=='AMBIGUOUS SL/TP'
    assert reconcile_signal(long,[{'ts':first,'high':115,'low':98}])['status']=='TP3 SEEN'


def test_missing_history_displays_unverified_not_false_tracking():
    issued='2026-11-17T19:31:22+00:00'
    first=(int(datetime.fromisoformat(issued).timestamp()//60)+1)*60000
    short={'ts':issued,'side':'SHORT','status':'TRACKING','entry':100,
           'stop':105,'tp1':95,'tp2':90,'tp3':85}
    history=reconcile_signal(short,[{'ts':first+600000,'high':103,'low':99}])
    assert history['status']=='TRACKING' and history['coverage']=='GAP'
    later_stop=reconcile_signal(short,[{'ts':first+600000,'high':106,'low':98}])
    assert later_stop['status']=='SL SEEN' and later_stop['coverage']=='GAP'
    assert reconcile_signal(short,None)['coverage']=='FEED UNAVAILABLE'
    assert reconcile_signal({**history,'coverage':'GAP'},None)['coverage']=='GAP'


def test_browser_recovery_endpoint_rechecks_older_rows_read_only():
    issued='2026-11-17T19:31:22+00:00'
    first=(int(datetime.fromisoformat(issued).timestamp()//60)+1)*60000
    row={'ts':issued,'side':'SHORT','status':'TRACKING','entry':1784.69,
         'stop':1805.77,'tp1':1770,'tp2':1750,'tp3':1730}
    old=app.STATE['observation_candles'].get('ETHUSDT')
    app.STATE['observation_candles']['ETHUSDT']=[{'ts':first,'high':1806,'low':1790}]
    async def request():
        transport=httpx.ASGITransport(app=app.app)
        async with httpx.AsyncClient(transport=transport,base_url='http://test') as client:
            return await client.post('/api/reconcile-cached-signals',json={'symbol':'ETHUSDT','rows':[row]})
    try:
        result=asyncio.run(request())
        assert result.status_code==200
        assert result.json()['rows'][0]['status']=='SL SEEN'
        assert result.json()['rows'][0]['history_source']=='BROWSER RECOVERED'
    finally:
        if old is None:app.STATE['observation_candles'].pop('ETHUSDT',None)
        else:app.STATE['observation_candles']['ETHUSDT']=old


def test_signal_journal_de_duplicates_immediate_candidates():
    previous_path=app.DB_PATH
    with tempfile.TemporaryDirectory() as temp:
        app.DB_PATH=Path(temp)/'signals-test.db'
        try:
            app.init_db()
            levels={'entry':100,'stop':95,'tp1':105,'tp2':110,'tp3':115}
            asset={'direction':'LONG CANDIDATE','levels':levels,'price':100,'bias':54,'timing':44}
            asset.update({'signal_candle_ts':1700000000000,'signal_score':74,'setup_trigger':'15M MOMENTUM CONTINUATION'})
            first=app.record_signal_log('BTCUSDT',asset,'NO TRADE')
            assert len(first)==1
            assert first[0]['status']=='TRACKING'
            assert first[0]['strategy_version']=='0.8.1'
            assert first[0]['model_score']==74
            assert first[0]['candle_ts']==1700000000000
            second=app.record_signal_log('BTCUSDT',asset,'LONG CANDIDATE')
            assert len(second)==1
            issued=datetime.fromisoformat(first[0]['ts'])
            first_bar=(int(issued.timestamp()//60)+1)*60000
            candles=[{'ts':first_bar,'high':106,'low':99}]
            reached=app.record_signal_log('BTCUSDT',asset,'LONG CANDIDATE',candles)
            assert reached[0]['status']=='TP1 SEEN'
            assert len(reached)==1
        finally:
            app.DB_PATH=previous_path


def test_legacy_signal_records_survive_version_migration():
    previous_path=app.DB_PATH
    with tempfile.TemporaryDirectory() as temp:
        app.DB_PATH=Path(temp)/'legacy.db'
        try:
            with sqlite3.connect(app.DB_PATH) as db:
                db.execute('CREATE TABLE signals (id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT, side TEXT, entry REAL, stop REAL, tp1 REAL, tp2 REAL, tp3 REAL, bias REAL, timing REAL, status TEXT)')
                db.execute("INSERT INTO signals(ts,symbol,side,entry,stop,tp1,tp2,tp3,bias,timing,status) VALUES ('2026-11-15T00:00:00+00:00','BTCUSDT','LONG',100,95,105,110,115,70,65,'TRACKING')")
            app.init_db()
            with sqlite3.connect(app.DB_PATH) as db:
                row=db.execute('SELECT strategy_version,status,entry FROM signals').fetchone()
            assert row==('LEGACY','TRACKING',100)
        finally:
            app.DB_PATH=previous_path


def test_timestamped_taker_tape_quality():
    now=1_700_000_000
    trades=[{'price':100,'qty':1,'ts':now-130+i*7,'isBuyerMaker':i%3==0} for i in range(20)]
    reading=recent_tape_snapshot(trades,now=now)
    assert reading['quality']=='COMPLETE'
    assert reading['trades']>=10
    assert reading['buy_share']>50
    assert reading['buy_usd']>reading['sell_usd']
    limited=recent_tape_snapshot(trades[-3:],now=now)
    assert limited['quality']=='PARTIAL'
    assert recent_tape_snapshot([],now=now)['quality']=='UNAVAILABLE'


def test_liquidity_monitor_is_separate_and_needs_history():
    old=order_book_snapshot({'bids':[['100','10']],'asks':[['101','10']]},100.5)
    new=order_book_snapshot({'bids':[['100','15']],'asks':[['101','7']]},100.5)
    previous={'ts':'2026-11-15T00:00:00+00:00','price':100.5,'book_snapshot':old}
    asset={'price':100.53,'updated':'2026-11-15T00:02:00+00:00','order_book':new,
           'recent_tape':{'quality':'COMPLETE','buy_share':20,'trades':90,'window_seconds':120}}
    first=liquidity_observation(asset)
    assert first['pressure']=='WARMING UP'
    monitor=liquidity_observation(asset,previous)
    assert monitor['pressure']=='BUY PRESSURE'
    assert monitor['bid_delta_pct']>0
    assert monitor['ask_delta_pct']<0
    assert monitor['absorption']=='POSSIBLE BUY ABSORPTION'
    assert monitor['walls_retained']>0
    old['bid_usd']=0  # empty historical depth must not cause division errors
    assert liquidity_observation(asset,previous)['bid_delta_pct'] is None
