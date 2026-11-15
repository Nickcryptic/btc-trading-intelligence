const $ = id => document.getElementById(id);
const htmlSafe = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const usd = value => typeof value === 'number' ? '$' + value.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}) : '—';
const compactUsd = value => typeof value !== 'number' ? '—' : value>=1e9?'$'+(value/1e9).toFixed(2)+'B':value>=1e6?'$'+(value/1e6).toFixed(2)+'M':value>=1e3?'$'+(value/1e3).toFixed(1)+'K':usd(value);
const sideClass = value => value === 'BUY' ? 'buy' : value === 'SELL' ? 'sell' : value === 'NEUTRAL' ? 'neutral' : 'unavailable';
const shortTime = value => value ? new Date(value).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',hour12:false}) : '—';
const londonTime = value => value ? new Date(value).toLocaleString('en-GB',{timeZone:'Europe/London',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',timeZoneName:'short'}) : 'awaiting feed';
const utcTime = value => value ? new Date(value).toLocaleString('en-GB',{timeZone:'UTC',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',hour12:false}) : '—';
let selected='BTCUSDT';
let current=null;
let selectedFvg='15m';

const slowNames = new Set(['200D Moving Average','50D Moving Average','4H Trend','Fear & Greed','Stablecoin Supply (30D)','Bitcoin Halving Cycle']);
const fastNames = new Set(['1H Momentum','15M Momentum','15M RSI','Order Book Imbalance','Bid/Ask Spread','Recent Trade Delta (CVD proxy)','Support / Resistance','Fair Value Gaps']);
const categories = [
  ['BTC vs 200D MA','200D Moving Average'],
  ['Fear & Greed','Fear & Greed'],
  ['Funding Rate','Funding Rate'],
  ['Open Interest / Leverage','Open Interest'],
  ['Total BTC ETF Flows',null],
  ['Exchange Inflows / Outflows',null],
  ['MVRV Ratio',null],
  ['Stablecoin Supply (30D)','Stablecoin Supply (30D)'],
  ['Federal Reserve / Liquidity',null],
  ['US Dollar Index (DXY)',null],
  ['Nasdaq / US Equities',null],
  ['Bitcoin Halving Cycle','Bitcoin Halving Cycle'],
  ['BlackRock IBIT ETF Flow',null],
  ['Wintermute Orders · monitoring only',null],
  ['Whale BTC / ETH Transfers',null],
  ['USDT Minting / Redemption',null],
  ['Liquidation Heatmap',null],
  ['Long-Term Holder Behavior',null],
  ['50D MA + Support / Resistance','50D Moving Average'],
  ['Spot / Futures CVD','Recent Trade Delta (CVD proxy)'],
  ['Order Book Depth & Spreads','Order Book Imbalance'],
  ['Fair Value Gaps','Fair Value Gaps'],
  ['CME Futures Gaps',null]
];
const buildRow = indicator => {
  const value=htmlSafe(indicator.value ?? 'Unavailable');
  const side=htmlSafe(indicator.signal || 'UNAVAILABLE');
  return '<div class="indicator-row"><div><span class="indicator-name">'+htmlSafe(indicator.name)+'</span><span class="indicator-source">'+htmlSafe(indicator.source)+'</span></div><div class="indicator-value">'+value+'</div><span class="signal-pill '+sideClass(side)+'">'+side+'</span></div>';
};
function renderIndicators(asset){
  const values=asset.indicators || [];
  const slow=values.filter(item=>slowNames.has(item.name)&&item.signal!=='UNAVAILABLE');
  const fast=values.filter(item=>fastNames.has(item.name)&&item.signal!=='UNAVAILABLE');
  $('slow-indicators').innerHTML=slow.length?slow.map(buildRow).join(''):'<p class="placeholder">Slow feeds are still loading.</p>';
  $('fast-indicators').innerHTML=fast.length?fast.map(buildRow).join(''):'<p class="placeholder">Execution feeds are still loading.</p>';
}
function renderCoverage(asset){
  const indicators=asset?.indicators || [];
  let count=0;
  const list=categories.map(([name,mapped],idx)=>{
    const item=indicators.find(x=>x.name===mapped);
    const active=Boolean(item&&item.signal!=='UNAVAILABLE');
    const proxy=idx===19&&active;
    if(active)count++;
    const status=active?(proxy?'PARTIAL · RECENT TRADES':'LIVE · VERIFIED SOURCE'):'NOT YET SOURCED';
    return '<div class="coverage-row"><span>'+String(idx+1).padStart(2,'0')+' · '+htmlSafe(name)+'</span><b class="'+(active?'':'pending')+'">'+status+'</b></div>';
  }).join('');
  $('coverage-count').textContent=count+' / 23';
  $('coverage-summary').textContent=count+' / 23 DATA CATEGORIES';
  $('coverage').innerHTML=list;
}
function setScore(key,value,label){
  $(key).textContent=typeof value==='number'?(value>0?'+':'')+value:'—';
  $(key+'-label').textContent=label || 'UNAVAILABLE';
  $(key+'-label').style.color=value>=30?'#37e7bd':value<=-30?'#ff7894':'#f4bf71';
  $(key+'-bar').style.width=typeof value==='number'?Math.max(0,Math.min(100,(value+100)/2))+'%':'50%';
  $(key+'-bar').style.background=value<=-30?'#ff7894':key==='bias'?'#a78bfa':'#37e7bd';
}
function gate(label,ok,detail){
  return '<div class="gate"><span class="gate-icon '+(ok?'pass':'fail')+'">'+(ok?'✓':'–')+'</span><span>'+htmlSafe(label)+'</span><strong>'+htmlSafe(detail)+'</strong></div>';
}
function renderDecision(asset,stale){
  const b=asset.bias,t=asset.timing;
  setScore('bias',b,asset.bias_label);
  setScore('timing',t,asset.timing_label);
  const slow=typeof b==='number'&&Math.abs(b)>=30;
  const fast=typeof t==='number'&&Math.abs(t)>=30;
  const agreement=slow&&fast&&Math.sign(b)===Math.sign(t)&&!stale;
  const passed=[slow,fast,agreement].filter(Boolean).length;
  $('signal-gates').innerHTML=gate('Slow market bias',slow,slow?b>0?'BULLISH':'BEARISH':'MIXED')
    +gate('Fast execution timing',fast,fast?t>0?'BULLISH':'BEARISH':'MIXED')
    +gate('Same direction + current feed',agreement,agreement?'ALIGNED':stale?'STALE':'NO AGREEMENT');
  $('gate-result').textContent=passed+' / 3';
  const status=$('signal-status');
  status.className='signal-status';
  if(agreement){
    const long=b>0;
    status.textContent=long?'TEST LONG':'TEST SHORT';
    status.classList.add(long?'long':'short');
    $('signal-reason').textContent='Both timeframes currently agree on a '+(long?'bullish':'bearish')+' direction. This is a provisional confluence test.';
  }else{
    status.textContent=stale?'FEED STALE':'WAIT / NO TRADE';
    status.classList.add('wait');
    $('signal-reason').textContent=stale?'Last observation is too old. A fresh market check is required.':slow&&!fast?'Regime is directional, but execution timing has not confirmed it.':fast&&!slow?'Execution momentum exists, but the slow market regime does not confirm it.':'The two score thresholds are not aligned. No test trade is generated.';
  }
  const levels=agreement?asset.levels:null;
  for(const id of ['entry','stop','tp1','tp2','tp3']) $(id).textContent=usd(levels?.[id]);
  $('plan-status').textContent=levels?'TEST CANDIDATE':'NO SETUP';
  $('plan-status').className='badge'+(levels?' green':'');
  $('plan-reason').textContent=levels?'Risk distance uses 15M ATR with a 0.3% price floor. All targets are illustrative.':'Waiting for directional agreement. No entry or targets are inferred without a candidate.';
}
function renderZones(asset){
  const zones=asset.fvg_by_timeframe?.[selectedFvg] || (selectedFvg==='15m'?asset.fvg:[]) || [];
  const opened=zones.filter(z=>z.status==='open').slice(0,6);
  $('fvg').innerHTML=opened.length?opened.map(z=>
    '<div class="zone-row"><div><b>'+(z.kind==='bullish'?'Bullish imbalance':'Bearish imbalance')+'</b><small>'+usd(z.low)+' — '+usd(z.high)+' · '+utcTime(z.at?new Date(z.at).toISOString():null)+' UTC</small></div><span class="direction '+(z.kind==='bullish'?'bull':'bear')+'">'+(z.kind==='bullish'?'BULLISH':'BEARISH')+'</span></div>'
  ).join(''):'<p class="placeholder">No open zones in the available completed '+selectedFvg.toUpperCase()+' candles.</p>';
  $('fvg-note').textContent='Scanning the last '+zones.length+' detected 3-candle gaps on completed '+selectedFvg.toUpperCase()+' spot candles. Selection does not change the 15M-based signal score.';
}
function renderOrderBook(asset){
  const book=asset.order_book;
  $('book-source').textContent=book?(asset.market_source||'Spot')+' · up to 100 levels/side · '+shortTime(asset.updated):'Spot depth is unavailable';
  if(!book){
    $('depth-summary').innerHTML='<p class="placeholder">No verified order-book snapshot from this feed.</p>';
    $('bid-ladder').innerHTML='';$('ask-ladder').innerHTML='';
    $('cluster-list').innerHTML='<p class="placeholder">Liquidity bands need a valid spot order book.</p>';
    return;
  }
  const buy=Number(book.bid_share)||0;
  $('depth-summary').innerHTML='<div class="depth-values"><span class="bid-text">BIDS <b>'+compactUsd(book.bid_usd)+'</b></span><span>Spread <b>'+htmlSafe(asset.spread_bps)+' bps</b></span><span class="ask-text">ASKS <b>'+compactUsd(book.ask_usd)+'</b></span></div>'
    +'<div class="depth-track" role="img" aria-label="Bid '+buy+' percent, ask '+(100-buy).toFixed(1)+' percent"><span style="width:'+buy+'%"></span></div>'
    +'<div class="depth-percent"><b class="bid-text">Buy depth '+buy.toFixed(1)+'%</b><b class="ask-text">Sell depth '+(100-buy).toFixed(1)+'%</b></div>';
  const all=[...(book.bids||[]),...(book.asks||[])];
  const max=Math.max(1,...all.map(x=>Number(x.usd)||0));
  function ladder(rows,side){return (rows||[]).map(row=>'<div class="ladder-row '+side+'"><span>'+usd(row.price)+'</span><div class="ladder-meter"><i style="width:'+(row.usd/max*100).toFixed(1)+'%"></i></div><b>'+compactUsd(row.usd)+'</b></div>').join('')||'<p class="placeholder">No levels</p>'}
  $('bid-ladder').innerHTML=ladder(book.bids,'bid');
  $('ask-ladder').innerHTML=ladder(book.asks,'ask');
  const clusters=book.clusters||[];
  const strongest=Math.max(1,...clusters.map(x=>Number(x.usd)||0));
  $('cluster-list').innerHTML=clusters.length?clusters.map(row=>'<div class="cluster-row '+(row.side==='BID'?'bid':'ask')+'"><span class="cluster-side">'+htmlSafe(row.side)+'</span><div class="cluster-main"><div class="cluster-text"><b>'+usd(row.price)+'</b><span>'+(row.distance_pct>0?'+':'')+htmlSafe(row.distance_pct)+'% · '+compactUsd(row.usd)+'</span></div><div class="cluster-bar"><i style="width:'+(row.usd/strongest*100).toFixed(1)+'%"></i></div></div></div>').join(''):'<p class="placeholder">No price-band clusters detected.</p>';
}
function signalLogRows(){
  const fresh=current?.signal_log?.[selected]||[];
  let cached=[];
  const key='btc-intel-signal-journal-v04-'+selected;
  try{cached=JSON.parse(localStorage.getItem(key)||'[]');if(!Array.isArray(cached))cached=[]}catch{cached=[]}
  const ids=new Set();
  const merged=[...fresh,...cached].filter(row=>{
    const id=String(row.ts)+'|'+String(row.entry)+'|'+String(row.side);
    if(ids.has(id))return false;
    ids.add(id);return true;
  }).sort((a,b)=>Date.parse(b.ts)-Date.parse(a.ts)).slice(0,5);
  try{localStorage.setItem(key,JSON.stringify(merged))}catch{/* privacy mode may block storage */}
  return merged;
}
function renderSignalLog(){
  $('signal-log-symbol').textContent=selected.startsWith('BTC')?'BTC':'ETH';
  const rows=signalLogRows();
  $('signal-log').innerHTML=rows.length?rows.map(row=>'<tr><td>'+utcTime(row.ts)+'</td><td><span class="direction '+(row.side==='LONG'?'bull':'bear')+'">'+htmlSafe(row.side)+'</span></td><td>'+usd(row.entry)+'</td><td>'+usd(row.tp1)+'</td><td>'+usd(row.tp2)+'</td><td>'+usd(row.tp3)+'</td><td>'+usd(row.stop)+'</td><td><span class="outcome '+(row.status.includes('SL')?'bad':row.status.includes('TP')?'good':'')+'">'+htmlSafe(row.status)+'</span></td></tr>').join(''):'<tr><td colspan="8" class="no-signals">No test candidates issued yet. New entries appear when both score gates agree.</td></tr>';
}
function renderHistory(){
  const rows=current?.decision_log?.[selected] || [];
  $('decision-history').innerHTML=rows.length?rows.map(x=>
    '<div class="history-row"><span>'+shortTime(x.ts)+'</span><strong>'+htmlSafe(x.direction==='NO TRADE'?'WAIT / NO TRADE':x.direction)+'</strong><em>B '+htmlSafe(x.bias)+' · T '+htmlSafe(x.timing)+'</em></div>'
  ).join(''):'<p class="placeholder">The first snapshot will appear after market data loads.</p>';
  const wintermute=current?.wintermute || [];
  $('wintermute-history').innerHTML='<p>Verified Wintermute wallet attribution and buy/sell order data are not publicly connected. These entries are observation checks only and never affect the scores.</p>'
    +(wintermute.length?wintermute.map(x=>'<div class="history-row"><span>'+shortTime(x.ts)+'</span><strong>'+htmlSafe(x.signal)+'</strong></div>').join(''):'');
}
function renderAsset(asset){
  const stale=Date.now()-Date.parse(asset.updated)>300000;
  $('market-price').textContent=usd(asset.price);
  $('market-source').textContent=(asset.market_source||'Spot')+' · '+(asset.market_pair||'');
  $('market-change').textContent=(asset.change24h>0?'+':'')+asset.change24h+'%'+(asset.change_approximate?' ~':'');
  $('market-change').style.color=asset.change24h>=0?'#37e7bd':'#ff7894';
  $('support').textContent=usd(asset.support);
  $('resistance').textContent=usd(asset.resistance);
  $('feed-note').textContent=stale?'Market readings are older than 5 minutes':'Auto-refresh runs when this free instance is awake';
  renderDecision(asset,stale);
  renderIndicators(asset);
  renderZones(asset);
  renderOrderBook(asset);
  renderCoverage(asset);
  renderSignalLog();
}
async function load(){
  try{
    const response=await fetch('/api/state',{cache:'no-store'});
    if(!response.ok)throw new Error('Server '+response.status);
    current=await response.json();
    const asset=current.assets?.[selected];
    $('updated').textContent=shortTime(asset?.updated||current.updated);
    $('updated-mini').textContent='Updated '+londonTime(asset?.updated);
    const stale=asset&&Date.now()-Date.parse(asset.updated)>300000;
    $('connection').textContent=stale?'● STALE DATA':asset?'● FEEDS ONLINE':'● FEEDS PENDING';
    $('connection').className='live-badge'+(stale?' stale':asset?' online':'');
    if(asset)renderAsset(asset);
    else{
      $('signal-status').textContent='AWAITING DATA';
      $('signal-reason').textContent=current.source_errors?.[selected]||'Waiting for the first completed exchange refresh.';
      renderCoverage(null);
    }
    renderHistory();
  }catch(error){
    $('connection').textContent='● CONNECTION ERROR';
    $('connection').className='live-badge stale';
    $('feed-note').textContent=error.message;
  }
}
document.querySelectorAll('[data-symbol]').forEach(button=>button.addEventListener('click',()=>{
  selected=button.dataset.symbol;
  document.querySelectorAll('[data-symbol]').forEach(x=>x.classList.toggle('active',x===button));
  const asset=current?.assets?.[selected];
  if(asset){renderAsset(asset);renderHistory();$('updated-mini').textContent='Updated '+londonTime(asset.updated)}else load();
}));
document.querySelectorAll('[data-fvg-tf]').forEach(button=>button.addEventListener('click',()=>{
  selectedFvg=button.dataset.fvgTf;
  document.querySelectorAll('[data-fvg-tf]').forEach(x=>{const active=x===button;x.classList.toggle('active',active);x.setAttribute('aria-pressed',String(active))});
  const asset=current?.assets?.[selected];if(asset)renderZones(asset);
}));
$('reload').addEventListener('click',load);
load();
setInterval(load,120000);
