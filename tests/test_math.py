from app import ema, rsi, fvg_zones, score_label, kraken_result, parse_kraken_ohlc, parse_stablecoin_growth, halving_context, calc_asset

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

    # A bullish regime with bearish fast timing must produce no trade.
    negative_trades=[{'qty':'10','isBuyerMaker':True}]
    ask_heavy={'bids':[['80777','1.0',0]],'asks':[['80779','9.0',0]]}
    mixed=calc_asset('BTCUSDT',candles(-10),upward,upward,upward,ticker,ask_heavy,
                     negative_trades,None,None)
    assert mixed['direction']=='NO TRADE'
    assert mixed['test_confirmation'] is False
    assert mixed['levels'] is None
