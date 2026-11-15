// Standalone DOM-lite regression test: node tests/test_liquidity_ui.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8').split("document.querySelectorAll('[data-symbol]'")[0];
const elements = {};
const document = {getElementById: id => elements[id] ||= {innerHTML:'',textContent:''}};
const asset = {
  price:100,updated:new Date().toISOString(),market_source:'Kraken',spread_bps:1,
  order_book:{bid_share:70,bid_usd:20000,ask_usd:10000,
    bids:[{price:99.9,usd:500}],asks:[{price:100.1,usd:400}],
    cluster_map:{
      bids:[{price:99.5,low:99.5,high:99.55,usd:12000,levels:3,distance_pct:-0.48,side:'BID'}],
      asks:[{price:100.5,low:100.5,high:100.6,usd:8000,levels:4,distance_pct:0.55,side:'ASK'}]
    },coverage:{low:99,high:101,bid_levels:20,ask_levels:20}}
};
const ctx = vm.createContext({document,asset,Date});
vm.runInContext(source+'\nrenderOrderBook(asset);',ctx);
assert.match(elements['cluster-highlights'].innerHTML,/LARGEST VISIBLE BUY WALL/);
assert.match(elements['cluster-highlights'].innerHTML,/LARGEST VISIBLE SELL WALL/);
assert.match(elements['cluster-list'].innerHTML,/\$99\.50 – \$99\.55/);
assert.match(elements['cluster-list'].innerHTML,/\$100\.50 – \$100\.60/);
assert.match(elements['cluster-list'].innerHTML,/CURRENT SPOT PRICE/);
assert.match(elements['cluster-list'].innerHTML,/BUY LIMIT ORDERS/);
assert.match(elements['cluster-list'].innerHTML,/SELL LIMIT ORDERS/);
assert.match(elements['cluster-coverage'].textContent,/20 bid \/ 20 ask/);
console.log('Liquidity price map renders both sides and exact price ranges.');
