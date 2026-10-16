// Static demo: answers the dashboard's API calls in the browser, like demo.py does locally.
// demo.py --build inlines this with window.DEMO = {state, built_at, events, originals, result, progress}.
(()=>{
const D=window.DEMO, shift=Date.now()-Date.parse(D.built_at);
// Keep the sample data's times relative to now, so "just now" stays just now.
const fresh=v=>typeof v==='string'&&/^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(v)?new Date(Date.parse(v)+shift).toISOString()
  :Array.isArray(v)?v.map(fresh):v&&typeof v==='object'?Object.fromEntries(Object.entries(v).map(([k,x])=>[k,fresh(x)])):v;
const state=fresh(D.state), events=fresh(D.events), originals=fresh(D.originals);
const json=(v,status=200)=>new Response(JSON.stringify(v),{status,headers:{'Content-Type':'application/json'}});
const later=ms=>new Date(Date.now()+ms).toISOString();
function decide(args){
  const task=state.suggestions.find(t=>t.id===args?.suggestion_id), choice=args?.choice;
  const status={accept:'accepted',complete:'completed',reject:'rejected',other:'instructions',snooze:'snoozed',
    retry:'accepted',undo:task?.closed_from||'pending',reopen:'pending'}[choice];
  if(!task||!status)return json({error:'This action is unavailable in the demo; nothing was sent or run'},400);
  if(choice==='complete'||choice==='reject'){task.closed_from=task.status;task.undo_until=later(300000)}
  task.status=status;
  if(['accept','other','retry'].includes(choice))task.preparation_status='running';
  if(choice==='snooze')task.snoozed_until=args.snoozed_until||later(86400000);
  state.decisions.push({suggestion_id:task.id,choice,feedback:String(args.feedback||'').slice(0,1000),source:'demo',created_at:later(0)});
  return json({demo:true,status});
}
const real=window.fetch.bind(window);
window.fetch=async(input,options={})=>{
  const url=new URL(typeof input==='string'?input:input.url,document.baseURI), path=url.pathname.split('/').pop();
  if(!url.pathname.includes('/api/'))return real(input,options);
  if(path==='status')return json(state);
  if(path==='events')return json(events);
  if(path==='originals')return json({originals});
  if(path==='email')return json({html:''});
  if(path==='result')return json(D.result);
  if(path==='progress')return json(D.progress);
  if(path==='action'&&options.method==='POST'){
    let data;try{data=JSON.parse(options.body)}catch{}
    return data?.action==='decide'?decide(data.args):json({error:'This action is unavailable in the demo; nothing was sent or run'},400);
  }
  return json({error:'Unavailable in the demo'},404);
};
// Result files are links, not fetches: show them from memory.
document.addEventListener('click',e=>{
  const a=e.target.closest?.('a[href^="result-file"]');
  if(!a)return;
  e.preventDefault();
  open(URL.createObjectURL(new Blob([D.result.text],{type:'text/plain;charset=utf-8'})),'_blank');
},true);
})();
