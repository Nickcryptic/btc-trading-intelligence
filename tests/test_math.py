from app import ema, rsi, fvg_zones, score_label, kraken_result, parse_kraken_ohlc

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
