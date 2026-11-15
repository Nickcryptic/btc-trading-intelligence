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
    $('cluster-coverage').textContent='Visible price range unavailable';
    $('cluster-highlights').innerHTML='';
    $('cluster-list').innerHTML='<p class="placeholder">Liquidity price levels need a valid spot order book.</p>';
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
  const legacy=book.clusters||[];
  const bids=book.cluster_map?.bids||legacy.filter(x=>x.side==='BID').slice(0,6);
  const asks=book.cluster_map?.asks||legacy.filter(x=>x.side==='ASK').slice(0,6);
  const strongest=Math.max(1,...[...bids,...asks].map(x=>Number(x.usd)||0));
  const zonePrice=row=>{
    const low=typeof row.low==='number'?row.low:row.price;
    const high=typeof row.high==='number'?row.high:row.price;
    return low===high?usd(low):usd(low)+' – '+usd(high);
  };
  const spotDistance=row=>{
    const distance=Number(row.distance_pct);
    return Number.isFinite(distance)?(distance>0?'+':'')+distance.toFixed(2)+'% vs spot':'Distance unavailable';
  };
  const coverage=book.coverage;
  $('cluster-coverage').textContent=(coverage?'Visible depth range: '+usd(coverage.low)+' to '+usd(coverage.high)+' · '+coverage.bid_levels+' bid / '+coverage.ask_levels+' ask levels':'Visible exchange depth near current spot only')+(Date.now()-Date.parse(asset.updated)>300000?' · STALE SNAPSHOT':'');
  const highlight=(wall,side)=>'<div class="wall-highlight '+side+'"><small>'+(side==='bid'?'LARGEST VISIBLE BUY WALL':'LARGEST VISIBLE SELL WALL')+'</small><strong>'+(wall?zonePrice(wall):'—')+'</strong><span>'+(wall?compactUsd(wall.usd)+' · '+spotDistance(wall):'No verified levels')+'</span></div>';
  $('cluster-highlights').innerHTML=highlight(bids[0],'bid')+highlight(asks[0],'ask');
  const wallRow=(row,side,largest)=>{
    const width=Math.min(100,Math.max(0,(Number(row.usd)||0)/strongest*100));
    return '<div class="wall-zone '+side+'"><div class="wall-zone-top"><div><strong>'+zonePrice(row)+'</strong><small>'+spotDistance(row)+(row.levels?' · '+htmlSafe(row.levels)+' price levels':'')+(row===largest?' · STRONGEST '+(side==='bid'?'BUY':'SELL'):'')+'</small></div><b>'+compactUsd(row.usd)+'</b></div><div class="wall-zone-bar"><i style="width:'+width.toFixed(1)+'%"></i></div></div>';
  };
  const wallSection=(rows,side)=>'<div class="wall-section-label '+side+'">'+(side==='ask'?'▲ SELL LIMIT ORDERS (ASKS)':'▼ BUY LIMIT ORDERS (BIDS)')+'<span>TOP '+rows.length+' BY VISIBLE USD</span></div>'+(rows.length?[...rows].sort((a,b)=>b.price-a.price).map(row=>wallRow(row,side,rows[0])).join(''):'<p class="placeholder">No '+side+' liquidity in this snapshot.</p>');
  $('cluster-list').innerHTML=wallSection(asks,'ask')+'<div class="spot-price-anchor"><span>● CURRENT SPOT PRICE</span><strong>'+usd(asset.price)+'</strong></div>'+wallSection(bids,'bid');
}
function renderLiquidity(asset,stale){
  const observation=current?.liquidity_monitor?.[selected];
  const history=current?.liquidity_history?.[selected]||[];
  const status=$('liquidity-state');
  const statusText=!observation?'AWAITING DATA':stale?'STALE — WAIT FOR REFRESH':observation.pressure;
  status.textContent=statusText+(observation&&typeof observation.score==='number'&&!stale?' · '+(observation.score>=0?'+':'')+observation.score:'');
  status.className='liquidity-state '+(statusText==='BUY PRESSURE'?'buy':statusText==='SELL PRESSURE'?'sell':statusText==='LIQUIDITY WITHDRAWING'?'caution':statusText==='MIXED / NEUTRAL'?'neutral':'warming');
  $('liquidity-reason').textContent=stale?'Snapshot is older than 5 minutes. Short-term liquidity comparisons are paused.':observation?.reason||'Waiting for the first exchange snapshot.';
  function change(id,v){$(id).textContent=typeof v==='number'?(v>0?'+':'')+v.toFixed(1)+'%':'—';$(id).className=typeof v==='number'?(v>0?'up':v<0?'down':''):''}
  change('liquidity-bid-change',observation?.bid_delta_pct);
  change('liquidity-ask-change',observation?.ask_delta_pct);
  const share=observation?.taker_buy_share;
  $('liquidity-taker-share').textContent=typeof share==='number'?share.toFixed(1)+'%':'—';
  $('liquidity-taker-share').className=typeof share==='number'?(share>=60?'up':share<=40?'down':''):'';
  $('liquidity-tape-quality').textContent=observation?observation.tape_quality+' tape · '+observation.trade_count+' trades · '+observation.tape_seconds+'s window':'Waiting for trade tape';
  $('liquidity-retained').textContent=observation&&observation.walls_checked!==null?observation.walls_retained+' / '+observation.walls_checked:'—';
  const absorption=observation?.absorption||'NOT ESTABLISHED';
  $('liquidity-absorption').textContent=absorption;
  $('liquidity-absorption').className=absorption.includes('BUY')?'up':absorption.includes('SELL')?'down':'';
  $('liquidity-price-drift').textContent='Price change: '+(typeof observation?.price_delta_pct==='number'?(observation.price_delta_pct>0?'+':'')+observation.price_delta_pct.toFixed(2)+'%':'—');
  $('liquidity-history').innerHTML=history.length?history.slice(0,10).map(row=>{
    const label=row.pressure||'UNAVAILABLE';
    const tone=label==='BUY PRESSURE'?'buy':label==='SELL PRESSURE'?'sell':label==='LIQUIDITY WITHDRAWING'?'caution':'neutral';
    const bid=typeof row.bid_share==='number'?row.bid_share.toFixed(1)+'%':'—';
    const taker=typeof row.taker_buy_share==='number'?row.taker_buy_share.toFixed(1)+'%'+(row.tape_quality==='COMPLETE'?'':'*'):'—';
    return '<div class="liquidity-sequence-row"><time>'+shortTime(row.ts)+'</time><span>'+bid+'</span><span>'+taker+'</span><b class="liq-label '+tone+'">'+htmlSafe(label)+'</b></div>';
  }).join(''):'<p class="placeholder">The first observation will appear after a successful exchange poll.</p>';
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
  renderLiquidity(asset,stale);
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
      renderLiquidity(null,true);
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
