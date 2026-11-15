import tempfile
from pathlib import Path

import app
from app import ema, rsi, fvg_zones, score_label, kraken_result, parse_kraken_ohlc, parse_stablecoin_growth, halving_context, calc_asset, order_book_snapshot, observed_signal_status, recent_tape_snapshot, liquidity_observation

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


def test_provisional_confluence_requires_two_scores():
    def candles(step):
        return [{'ts':i*60000,'open':80_000+i*step-2,'high':80_000+i*step+12,
                 'low':80_000+i*step-12,'close':80_000+i*step,'volume':2.0} for i in range(260)]
    upward=candles(3)
    bid_heavy={'bids':[['80777','9.0',0]],'asks':[['80779','1.0',0]]}
    ticker={'lastPrice':'80777','priceChangePercent':'2.4'}
    result=calc_asset('BTCUSDT',upward,upward,upward,upward,ticker,bid_heavy,
                      [{'qty':'4','isBuyerMaker':False}],None,None)
    assert result['direction']=='LONG CANDIDATE'
    assert result['test_confirmation'] is True
    assert result['confirmed'] is False
    assert result['levels']['tp1']>result['levels']['entry']>result['levels']['stop']
    assert set(result['fvg_by_timeframe'])=={'15m','1h','1d'}
    assert all(isinstance(result['fvg_by_timeframe'][tf],list) for tf in ('15m','1h','1d'))
    assert result['order_book']['bid_share']>50

    # A bullish regime with bearish fast timing must produce no trade.
    negative_trades=[{'qty':'10','isBuyerMaker':True}]
    ask_heavy={'bids':[['80777','1.0',0]],'asks':[['80779','9.0',0]]}
    mixed=calc_asset('BTCUSDT',candles(-10),upward,upward,upward,ticker,ask_heavy,
                     negative_trades,None,None)
    assert mixed['direction']=='NO TRADE'
    assert mixed['test_confirmation'] is False
    assert mixed['levels'] is None


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
    long={'status':'TRACKING','side':'LONG','stop':95,'tp1':105,'tp2':110,'tp3':115}
    assert observed_signal_status(long,104)=='TRACKING'
    assert observed_signal_status(long,105)=='TP1 SEEN'
    assert observed_signal_status({**long,'status':'TP1 SEEN'},94)=='SL AFTER TP1'
    assert observed_signal_status(long,115)=='TP3 SEEN'
    short={'status':'TRACKING','side':'SHORT','stop':105,'tp1':95,'tp2':90,'tp3':85}
    assert observed_signal_status(short,94)=='TP1 SEEN'
    assert observed_signal_status(short,106)=='SL SEEN'


def test_signal_journal_de_duplicates_immediate_candidates():
    previous_path=app.DB_PATH
    with tempfile.TemporaryDirectory() as temp:
        app.DB_PATH=Path(temp)/'signals-test.db'
        try:
            app.init_db()
            levels={'entry':100,'stop':95,'tp1':105,'tp2':110,'tp3':115}
            asset={'direction':'LONG CANDIDATE','levels':levels,'price':100,'bias':54,'timing':44}
            first=app.record_signal_log('BTCUSDT',asset,'NO TRADE')
            assert len(first)==1
            assert first[0]['status']=='TRACKING'
            second=app.record_signal_log('BTCUSDT',asset,'LONG CANDIDATE')
            assert len(second)==1
            asset['price']=106
            reached=app.record_signal_log('BTCUSDT',asset,'LONG CANDIDATE')
            assert reached[0]['status']=='TP1 SEEN'
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
