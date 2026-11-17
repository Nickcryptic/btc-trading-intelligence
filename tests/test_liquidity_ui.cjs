// Standalone DOM-lite regression test: node tests/test_liquidity_ui.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8').split("document.querySelectorAll('[data-symbol]'")[0];
const elements = {};
const document = {getElementById: id => elements[id] ||= {innerHTML:'',textContent:'',style:{},classList:{add:()=>{}}}};
const asset = {
  price:100,updated:new Date().toISOString(),market_source:'Kraken',spread_bps:1,
  order_book:{bid_share:70,bid_usd:20000,ask_usd:10000,
    bids:[{price:99.9,usd:500}],asks:[{price:100.1,usd:400}],
    coverage:{low:99.9,high:100.1,bid_levels:20,ask_levels:20}},
  liquidity_map:{source:'Bybit spot',quote:'USDT',reference_price:100,
    coverage:{low:94,high:106,bid_levels:1000,ask_levels:1000},
    bids:[{price:98.5,low:98.5,high:98.6,usd:12000,levels:3,distance_pct:-1.45,relative_strength:2.4,side:'BID'}],
    asks:[{price:102,low:102,high:102.2,usd:8000,levels:4,distance_pct:2.1,relative_strength:1.7,side:'ASK'}]},
  structure_targets:{above:[{price:103.1,low:103,high:103.2,distance_pct:3.1,touches:3,timeframes:['1H','4H']}],below:[]}
};
const storage = new Map();
const localStorage = {getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)};
const ctx = vm.createContext({document,asset,Date,localStorage});
vm.runInContext(source+'\nrenderOrderBook(asset);',ctx);
assert.match(elements['cluster-highlights'].innerHTML,/LARGEST OBSERVED BUY BAND/);
assert.match(elements['cluster-highlights'].innerHTML,/LARGEST OBSERVED SELL BAND/);
assert.match(elements['cluster-highlights'].innerHTML,/2\.4× typical depth band/);
assert.match(elements['cluster-list'].innerHTML,/\$98\.50 – \$98\.60/);
assert.match(elements['cluster-list'].innerHTML,/\$102\.00 – \$102\.20/);
assert.match(elements['cluster-list'].innerHTML,/EXCHANGE BOOK MIDPRICE/);
assert.match(elements['cluster-list'].innerHTML,/BUY LIMIT ORDERS/);
assert.match(elements['cluster-list'].innerHTML,/SELL LIMIT ORDERS/);
assert.match(elements['cluster-coverage'].textContent,/1000 bid \/ 1000 ask/);
assert.match(elements['structure-targets'].innerHTML,/PRIOR HIGHS/);
assert.match(elements['structure-targets'].innerHTML,/3 swing touches/);
// Never synthesize a PAPER LONG from bias/timing alone; watches need a backend plan.
asset.bias=100;asset.timing=100;asset.bias_label='BUY';asset.timing_label='BUY';
asset.signal_checks=Array.from({length:6},(_,i)=>({label:'Check '+i,ok:i!==2,detail:'Waiting'}));
asset.direction='NO TRADE';asset.setup_reason='No completed 15M trigger';
vm.runInContext('renderDecision(asset,false)',ctx);
assert.equal(elements['signal-status'].textContent,'NO QUALIFIED SETUP');
assert.equal(elements['plan-status'].textContent,'NO SETUP');
asset.watch_direction='LONG';asset.signal_score=66;asset.signal_grade='MODERATE';
asset.watch_levels={entry:102,stop:100,tp1:104.4,tp2:106,tp3:108};
vm.runInContext('renderDecision(asset,false)',ctx);
assert.equal(elements['signal-status'].textContent,'WATCH LONG');
assert.equal(elements['plan-status'].textContent,'CONDITIONAL WATCH');
assert.equal(elements['entry'].textContent,'$102.00');
asset.direction='LONG CANDIDATE';
asset.levels={entry:100,stop:99,tp1:101,tp2:102,tp3:103};
vm.runInContext('renderDecision(asset,false)',ctx);
assert.equal(elements['signal-status'].textContent,'PAPER LONG');
assert.equal(elements['plan-status'].textContent,'PAPER CANDIDATE');
vm.runInContext('renderDecision(asset,true)',ctx);
assert.equal(elements['signal-status'].textContent,'FEED STALE');
assert.equal(elements['entry'].textContent,'—');
vm.runInContext(`document.getElementById('slow-indicators');renderIndicators({indicators:[
  {name:'200D Moving Average',source:'Kraken spot',value:'$70,000',signal:'BUY',
   fetched_at:'2026-11-17T19:34:21Z',source_as_of:'2026-11-17T00:00:00Z'}]})`,ctx);
assert.match(elements['slow-indicators'].innerHTML,/Pulled:.*19:34:21/);
assert.match(elements['slow-indicators'].innerHTML,/Source as of:.*00:00:00/);
vm.runInContext(`current={signal_log:{BTCUSDT:[{ts:'2026-11-17T19:31:22Z',side:'SHORT',
 entry:100,stop:105,tp1:95,tp2:90,tp3:85,status:'SL SEEN',
 observed_at:'2026-11-17T19:33:00Z',coverage:'COMPLETE (EXCEPT ISSUANCE MINUTE)',
 observation_source:'Kraken spot · completed 1M OHLC'}]}};renderSignalLog()`,ctx);
assert.match(elements['signal-log'].innerHTML,/SL SEEN/);
assert.match(elements['signal-log'].innerHTML,/Observed in 1M bar ending/);
vm.runInContext(`current.signal_log.BTCUSDT[0].status='TRACKING';
current.signal_log.BTCUSDT[0].coverage='GAP';renderSignalLog()`,ctx);
assert.match(elements['signal-log'].innerHTML,/UNVERIFIED HISTORY/);
console.log('Distant liquidity, conditional watches, weighted paper signals, and stale-feed guards render.');
