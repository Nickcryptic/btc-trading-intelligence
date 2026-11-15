const $ = id => document.getElementById(id);
const htmlSafe = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const usd = value => typeof value === 'number' ? '$' + value.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}) : '—';
const sideClass = value => value === 'BUY' ? 'buy' : value === 'SELL' ? 'sell' : value === 'NEUTRAL' ? 'neutral' : 'unavailable';
const shortTime = value => value ? new Date(value).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',hour12:false}) : '—';
let selected='BTCUSDT';
let current=null;

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
  const opened=(asset.fvg || []).filter(z=>z.status==='open').slice(0,4);
  $('fvg').innerHTML=opened.length?opened.map(z=>
    '<div class="zone-row"><div><b>'+(z.kind==='bullish'?'Bullish imbalance':'Bearish imbalance')+'</b><small>'+usd(z.low)+' — '+usd(z.high)+'</small></div><span class="direction '+(z.kind==='bullish'?'bull':'bear')+'">'+(z.kind==='bullish'?'BULLISH':'BEARISH')+'</span></div>'
  ).join(''):'<p class="placeholder">No currently open zones in the checked 15-minute candles.</p>';
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
  renderCoverage(asset);
}
async function load(){
  try{
    const response=await fetch('/api/state',{cache:'no-store'});
    if(!response.ok)throw new Error('Server '+response.status);
    current=await response.json();
    const asset=current.assets?.[selected];
    $('updated').textContent=shortTime(current.updated);
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
  if(asset){renderAsset(asset);renderHistory()}else load();
}));
$('reload').addEventListener('click',load);
load();
setInterval(load,120000);
