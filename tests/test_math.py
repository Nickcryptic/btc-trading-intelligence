from app import ema, rsi, fvg_zones, score_label

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
