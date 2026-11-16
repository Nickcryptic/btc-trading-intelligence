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
const ctx = vm.createContext({document,asset,Date});
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
// Never synthesize a TEST LONG from scores alone when the backend has no trigger.
asset.bias=100;asset.timing=100;asset.bias_label='BUY';asset.timing_label='BUY';
asset.signal_checks=Array.from({length:6},(_,i)=>({label:'Check '+i,ok:i!==2,detail:'Waiting'}));
asset.direction='NO TRADE';asset.setup_reason='No completed 15M trigger';
vm.runInContext('renderDecision(asset,false)',ctx);
assert.equal(elements['signal-status'].textContent,'BULLISH WATCH');
assert.equal(elements['plan-status'].textContent,'NO SETUP');
asset.signal_checks.forEach(c=>c.ok=true);
asset.direction='LONG CANDIDATE';
asset.levels={entry:100,stop:99,tp1:101,tp2:102,tp3:103};
vm.runInContext('renderDecision(asset,false)',ctx);
assert.equal(elements['signal-status'].textContent,'TEST LONG');
assert.equal(elements['plan-status'].textContent,'TEST CANDIDATE');
console.log('Distant walls, separate inferred zones and strict six-check signal gating render.');
