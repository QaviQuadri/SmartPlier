import {useEffect,useState} from 'react'
import type {FormEvent} from 'react'
import {createRoot} from 'react-dom/client'
import {LayoutDashboard,Radar,BriefcaseBusiness,FileText,Send,ChartNoAxesCombined,Settings2,ArrowUpRight,ChevronLeft,ChevronRight,Plus,Search,Zap,Menu,X} from 'lucide-react'
import {BarChart,Bar,XAxis,YAxis,CartesianGrid,Tooltip,ResponsiveContainer} from 'recharts'
import {Button} from './button'
import './style.css'

import {format,time,safe,request,useData,Panel,Heading,Empty,Badge,Loading,Stat} from './ui'
import type {Obj,Go,Post} from './ui'
import {Discovery,Settings} from './discovery-pages'
import {AutoApply} from './auto-apply-page'
type Task={id:string;name:string;status:string;current:string;completed:number;total:number;successes:number;errors:string[];results:{label:string;result:Obj}[]}
const nav=[['/','Overview',LayoutDashboard],['/discover','Discover',Radar],['/jobs','Jobs',BriefcaseBusiness],['/resumes','Resume Studio',FileText],['/outreach','Outreach',Send],['/auto-apply','Auto-Apply',Zap],['/analytics','Analytics',ChartNoAxesCombined],['/settings','Settings',Settings2]] as const
const tabs: [string,string,string[]][]=[['to-apply','To apply',['to_apply']],['review','Needs review',['review']],['unscored','Unscored',['unscored']],['in-progress','In progress',['applied','interview','offer']],['low-filtered','Low-filtered',['low','filtered']],['closed','Closed',['rejected','withdrawn','closed']],['all','All',[]]]
const states=['unscored','review','to_apply','low','filtered','applied','interview','offer','rejected','withdrawn','closed']
const active=(t:Task)=>['queued','running'].includes(t.status)
const logo='/assets/smartplier-logo.png'
function App(){
 const [location,setLocation]=useState(window.location.pathname+window.location.search),[revision,bump]=useState(0),[tasks,setTasks]=useState<Task[]>([]),[notice,setNotice]=useState(''),[failure,setFailure]=useState(''),[busy,setBusy]=useState(false),[mobile,setMobile]=useState(false)
 const overview=useData('/overview',revision)
 function go(path:string){history.pushState(null,'',path);setLocation(path);setMobile(false)}
 useEffect(()=>{const back=()=>setLocation(window.location.pathname+window.location.search);addEventListener('popstate',back);return()=>removeEventListener('popstate',back)},[])
 useEffect(()=>{
  const stream=new EventSource('/api/events');let done=''
  stream.onmessage=e=>{const incoming:Task[]=JSON.parse(e.data);setTasks(incoming);const next=incoming.map(t=>t.id+':'+t.status).join();if(next!==done){done=next;bump(v=>v+1)}}
  return()=>stream.close()
 },[])
 async function post(path:string,data:Obj={}, message='Saved.'){
  setBusy(true);setFailure('');setNotice('')
  try{let result=await request(path,data,overview.value?.csrf);setNotice(result.task?'Task queued. Follow its progress in Activity.':message);bump(v=>v+1);return result}
  catch(e){setFailure((e as Error).message);return null} finally{setBusy(false)}
 }
 const [page,search]=location.split('?'), id=new URLSearchParams(search).get('id')
 const open=(n:number,dest='/jobs')=>go(dest+'?id='+n)
 const content=id&&['/jobs','/resumes','/outreach'].includes(page)
  ?<JobDetail key={page+id} id={Number(id)} area={page} revision={revision} post={post} go={go} busy={busy}/>
  :page==='/discover'?<Discovery revision={revision} post={post} go={go} busy={busy}/>
  :page==='/auto-apply'?<AutoApply revision={revision} post={post} busy={busy}/>
  :page==='/settings'?<Settings revision={revision} post={post} busy={busy}/>
  :page==='/jobs'?<Jobs revision={revision} post={post} open={open} busy={busy}/>
  :page==='/analytics'?<Analytics revision={revision}/>
  :page==='/resumes'||page==='/outreach'?<Library page={page} revision={revision} open={open} go={go}/>
  :<Overview data={overview.value} error={overview.error} go={go} open={open} post={post} busy={busy}/>
 return <div className="app">
  <aside className={'sidebar '+(mobile?'expanded':'')}><a className="brand" href="/" onClick={e=>{e.preventDefault();go('/')}}><img src={logo} alt="Smartplier" width={1536} height={1024}/></a>
  <nav aria-label="Workspace">{nav.map(([url,label,Icon])=><a key={url} href={url} aria-current={page===url?'page':undefined} onClick={e=>{e.preventDefault();go(url)}}><Icon size={19}/> {label}</a>)}</nav>
  <div className="side-bottom"><span className="dot"/> Local workspace</div></aside>
  <div className="work"><div className="top"><Button className="mobile-menu" onClick={()=>setMobile(!mobile)} aria-label="Toggle navigation"><Menu size={18}/></Button>
   <a className="mobile-brand" href="/" onClick={e=>{e.preventDefault();go('/')}}><img src={logo} alt="Smartplier" width={1536} height={1024}/></a>
    <span>{nav.find(x=>x[0]===page)?.[1]??'Overview'}</span><div className="toolbar"><span className="muted">{overview.value?.gemini?'Gemini configured':'Gemini key needed'}</span></div></div>
   <main>{(notice||failure)&&<div role={failure?'alert':'status'} className={'notice '+(failure?'bad':'')}><span>{failure||notice}</span><button onClick={()=>{setFailure('');setNotice('')}} aria-label="Dismiss notification"><X size={16}/></button></div>}{content}</main></div>
  <details className="activity"><summary>Activity <span>{tasks.filter(active).length} active</span></summary>
    {tasks.length===0?<Empty>No background tasks yet.</Empty>:tasks.slice(-12).reverse().map(t=><section key={t.id} className="task"><div className="row"><strong>{t.name}</strong><Badge value={t.status}/></div><progress value={t.completed} max={t.total}/><p>{t.completed}/{t.total} complete · {t.total-t.completed} remaining · {t.successes} successful · {t.errors.length} failed</p>{t.current&&<p>Running {t.current}</p>}{t.errors.map((error,i)=><p className="danger" key={i}>{error}</p>)}{t.results.map(x=><p key={x.label}>{x.label} · {Object.entries(x.result).filter(([,v])=>v!==null&&v!==false&&String(v)!=='').map(([k,v])=>format(k)+': '+String(v)).join(' · ')}</p>)}</section>)}
  </details>
 </div>
}
function Overview({data,error,go,open,post,busy}:{data:Obj|null;error:string;go:Go;open:(n:number)=>void;post:Post;busy:boolean}){
 if(error)return <Empty>{error}</Empty>
 if(!data)return <Loading/>
 const total=Object.values(data.counts).reduce((a:number,b)=>a+Number(b),0)
 return <><Heading title="Job search overview" intro="Review new opportunities and keep applications moving." action={<Button variant="primary" disabled={busy} onClick={()=>post('/discover')}><Radar size={17}/> Discover Jobs Now</Button>}/>
 <div className="stats"><Stat label="Tracked jobs" value={total}/><Stat label="To apply" value={data.counts.to_apply??0}/><Stat label="Needs review" value={data.counts.review??0}/><Stat label="Follow-ups due" value={data.due}/></div>
 <div className="split"><Panel title="Recent jobs" action={<Button onClick={()=>go('/jobs')}>View jobs <ArrowUpRight size={15}/></Button>}><JobRows rows={data.recent} open={open}/></Panel>
 <Panel title="Application outcomes" action={<Button onClick={()=>go('/analytics')}>Analytics</Button>}><Outcome report={data.analytics}/></Panel></div></>
}
function JobRows({rows,open}:{rows:Obj[];open:(n:number)=>void}){
 if(!rows.length)return <Empty>No jobs match this view.</Empty>
 return <div className="table-wrap"><table><thead><tr><th>Job</th><th>Status</th><th>Fit</th></tr></thead><tbody>{rows.map(j=><tr key={j.id}><td><button className="text-link" onClick={()=>open(j.id)}>{j.title}</button><small>{j.company}{j.location?' · '+j.location:''}</small></td><td><Badge value={j.status}/></td><td><strong>{j.score??'—'}</strong>{j.score_method&&<small>{format(j.score_method)}</small>}</td></tr>)}</tbody></table></div>
}
function Jobs({revision,post,open,busy}:{revision:number;post:Post;open:(n:number)=>void;busy:boolean}){
 const [q,Q]=useState(''),[tab,T]=useState('all'),[sort,S]=useState('fit'),[page,P]=useState(1),[adding,A]=useState(false)
 const {value,error}=useData('/jobs?'+new URLSearchParams({q,tab,sort,page:String(page)}),revision)
 async function add(e:FormEvent<HTMLFormElement>){e.preventDefault();const result=await post('/jobs',Object.fromEntries(new FormData(e.currentTarget)));if(result)open(result.id)}
 return <><Heading title="Jobs" intro="Find, score and manage your opportunities." action={<div className="toolbar"><Button disabled={busy} onClick={()=>post('/score')}><Zap size={16}/> Score unscored</Button><Button variant="primary" onClick={()=>A(!adding)}><Plus size={17}/> Add job</Button></div>}/>
 {adding&&<Panel title="Add a posting"><form onSubmit={add} className="form-grid">{['title','company','location','url'].map(k=><label key={k}>{k==='url'?'Posting URL':format(k)}<input name={k} required={['title','company'].includes(k)}/></label>)}<label className="wide">Description<textarea name="description" required rows={5}/></label><Button variant="primary" disabled={busy}>Save posting</Button></form></Panel>}
 <div className="tabs">{tabs.map(([k,l,ss])=><button key={k} aria-pressed={tab===k} onClick={()=>{T(k);P(1)}}>{l} <span>{value?(ss.length?ss.reduce((n,s)=>n+(value.counts[s]??0),0):Object.values(value.counts).reduce((a:number,b)=>a+Number(b),0)):0}</span></button>)}</div>
 <Panel><div className="table-tools"><label className="search"><Search size={17}/><input aria-label="Search jobs" placeholder="Search jobs" value={q} onChange={e=>{Q(e.target.value);P(1)}}/></label><select aria-label="Sort jobs" value={sort} onChange={e=>{S(e.target.value);P(1)}}><option value="fit">Highest fit</option><option value="recent">Newest first</option><option value="oldest">Oldest first</option><option value="title">Title A–Z</option></select></div>
 {error?<Empty>{error}</Empty>:value?<JobRows rows={value.rows} open={open}/>:<Loading/>}
 {value&&<div className="table-tools"><span>{value.total} jobs · Page {page}</span><div className="toolbar"><Button aria-label="Previous page" disabled={page<=1} onClick={()=>P(page-1)}><ChevronLeft size={16}/></Button><Button aria-label="Next page" disabled={page*25>=value.total} onClick={()=>P(page+1)}><ChevronRight size={16}/></Button></div></div>}</Panel></>
}
function JobDetail({id,area,revision,post,go,busy}:{id:number;area:string;revision:number;post:Post;go:Go;busy:boolean}){
 const {value,error}=useData('/jobs/'+id,revision);const [edit,E]=useState<Obj|null>(null)
 if(error)return <Empty>{error}</Empty>
 if(!value)return <Loading/>
 const {job:j,history,source,application,outreach:o,people,links,resume}=value, base='/jobs/'+id
 const ats=value.preflight?.ats??value.ats
 const submit=(e:FormEvent<HTMLFormElement>,action:string,extra:Obj={})=>{e.preventDefault();return post(base+'/'+action,{...Object.fromEntries(new FormData(e.currentTarget)),...extra})}
 const copy=async(text:string)=>{await navigator.clipboard.writeText(text);E(p=>({...p,copied:true}))}
 return <><button className="text-link muted" onClick={()=>go(area)}>Back to {area==='/resumes'?'Resume Studio':area==='/outreach'?'Outreach':'Jobs'}</button>
 <Heading title={j.title} intro={j.company+' · '+(j.location||'Location not supplied')} action={<Badge value={j.status}/>}/>
 <div className="tabs">{[['/jobs','Details'],['/resumes','Resume Studio'],['/outreach','Outreach']].map(([p,label])=><button key={p} aria-pressed={p===area} onClick={()=>go(p+'?id='+id)}>{label}</button>)}</div>
 {area==='/resumes'?<Panel title="Tailored resume" action={<Button variant="primary" disabled={busy} onClick={()=>post(base+'/resume',{force:resume[0]})}>{resume[0]?'Regenerate':'Generate resume'}</Button>}>
 {resume[0]?<><div className="toolbar pad"><Button asChild><a href={'/api'+base+'/preview'} target="_blank" rel="noreferrer">Open preview</a></Button><Button asChild><a href={'/api'+base+'/html'}>Download HTML</a></Button>{resume[1]&&<Button asChild><a href={'/api'+base+'/tex'}>Download LaTeX</a></Button>}</div><iframe title="Resume preview" className="preview" sandbox="" src={'/api'+base+'/preview?v='+revision}/></>:<Empty>Generate a resume to preview and download it.</Empty>}</Panel>
 :area==='/outreach'?<div className="split"><Panel title="Find a contact"><div className="pad stack">{people.length>0&&<p>{people.join(', ')}</p>}<p>{j.contacts.emails.join(', ')}</p>{links.map(([label,url]:[string,string])=><a key={label} className="text-link" href={safe(url)} target="_blank" rel="noreferrer">{label} ↗</a>)}{!o?.sent_at&&<form className="stack" onSubmit={e=>submit(e,'draft')}><label>Contact name<input name="contact" defaultValue={o?.drafts.contact}/></label><Button variant="primary" disabled={busy}>{o?'Refresh drafts':'Draft outreach'}</Button></form>}</div></Panel>
 <Panel title="Messages">{o?<div className="pad stack">{['subject','email','note'].map(k=><label key={k}>{format(k)}<textarea rows={k==='email'?7:2} readOnly={!!o.sent_at} value={edit?.[k]??o.drafts[k]} onChange={e=>E({...o.drafts,...edit,[k]:e.target.value,copied:false})}/><Button onClick={()=>copy(edit?.[k]??o.drafts[k])}>Copy {k}</Button></label>)}{edit?.copied&&<p role="status">Copied.</p>}{!o.sent_at&&<><Button disabled={!edit||busy} onClick={async()=>{if(await post(base+'/edit-draft',edit??{}))E(null)}}>Save draft edits</Button><form className="inline" onSubmit={e=>submit(e,'outreach',{action:'sent'})}><select aria-label="Channel" name="channel"><option value="email">Email</option><option value="linkedin">LinkedIn</option></select><Button disabled={busy}>Record as sent</Button></form></>}{o.sent_at&&<p>Sent {time(o.sent_at)} · {o.channel}</p>}{o.replied_at&&<p>Reply recorded {time(o.replied_at)}</p>}{o.sent_at&&!o.replied_at&&<Button disabled={busy} onClick={()=>post(base+'/outreach',{action:'replied'})}>Record reply</Button>}{o.followed_up_at&&<p>Follow-up recorded {time(o.followed_up_at)}</p>}{o.followup&&<><p>Follow-up due {time(o.due)}</p><textarea aria-label="Follow-up draft" rows={5} maxLength={4000} value={edit?.followup??o.followup[1]} onChange={e=>E({...edit,followup:e.target.value})}/><div className="toolbar"><Button onClick={()=>copy(edit?.followup??o.followup[1])}>Copy follow-up</Button><Button disabled={edit?.followup===undefined||busy} onClick={async()=>{if(await post(base+'/edit-draft',{followup:edit?.followup}))E(null)}}>Save follow-up edits</Button></div><Button disabled={busy} onClick={()=>post(base+'/outreach',{action:'followed-up'})}>Record follow-up sent</Button></>}</div>:<Empty>Draft outreach to prepare an email and LinkedIn note.</Empty>}</Panel></div>
 :<div className="split"><div className="stack"><Panel title="Fit assessment" action={<Button variant="primary" disabled={busy} onClick={()=>post(base+'/score')}><Zap size={16}/> {j.score_method?'Re-score':'Score job'}</Button>}><div className="pad stack"><div className="score">{j.score??'—'} <small>{j.score_method?format(j.score_method):'Unscored'}</small></div>{j.score_details&&<><p>{j.score_details.reason}</p><p>Ghost-job risk · {j.score_details.ghost_job_risk}</p><div className="split three">{['matched','missing','red_flags'].map(k=><div key={k}><h3>{format(k)}</h3>{j.score_details[k].map((x:string,i:number)=><p key={i}>{x}</p>)}</div>)}</div></>}</div></Panel>
 <Panel title="Posting"><div className="pad stack">{source&&<p>Source · {source.source} {source.flags.join(' ')}</p>}{safe(j.url)&&<a className="text-link" href={safe(j.url)} target="_blank" rel="noreferrer">Open original posting ↗</a>}<p className="pre">{j.description}</p></div></Panel>
 <Panel title="Status history"><div className="pad stack">{history.map((h:Obj,i:number)=><div key={i}><strong>{format(h.status)}</strong><small>{time(h.changed_at)} · {h.reason}</small></div>)}</div></Panel></div>
 <div className="stack"><Panel title="Job actions"><div className="pad stack"><form className="stack" onSubmit={e=>submit(e,'status')}><label>Status<select name="status" defaultValue={j.status} key={j.status}>{states.map(s=><option value={s} key={s}>{format(s)}</option>)}</select></label><label>Reason<input name="reason"/></label>{!application&&<label>Resume variant when applying<input name="variant" placeholder="e.g. Tailored" maxLength={50}/></label>}<Button disabled={busy}>Update status</Button></form><form className="stack" onSubmit={e=>submit(e,'location')}><label>Explicit location<input name="location" defaultValue={j.location} key={j.location}/></label><Button disabled={busy}>Save location</Button></form><Button onClick={()=>go('/resumes?id='+id)}>Open Resume Studio</Button></div></Panel>
 <Panel title="Auto-Apply preflight"><div className="pad stack"><Button disabled={busy} onClick={()=>post(base+'/auto-preflight')}>Run Auto-Apply for this job</Button><small>Checks this job only. Review its payload before submitting.</small>
 {safe(j.url)&&!value.attempt&&!['applied','interview','offer','closed','rejected','withdrawn'].includes(j.status)&&<Button disabled={busy} onClick={()=>post(base+'/auto-assisted')}>Open assisted application</Button>}
 {ats&&<div><strong>Application platform · {ats.platform}</strong><small>{ats.confidence} confidence · {ats.verification}</small>{ats.ambiguity&&<small>{ats.ambiguity}</small>}{ats.evidence.map((item:string)=><small key={item}>{item}</small>)}</div>}
 {value.preflight&&<><strong>{value.preflight.detail}</strong><small>Checked {time(value.preflight.at)}</small>
 {value.preflight.age!==undefined&&<p>Posting age: {value.preflight.age} days{value.preflight.age>value.preflight.threshold?' · Above the stale-risk threshold':''}</p>}
 <p>{value.preflight.verification??'Official ATS activity has not been verified.'}</p>
 <p>Resume: {value.preflight.resume?(value.preflight.pdf_validated?'One-page PDF validated':'Tailored HTML prepared; PDF not yet verified')+' ('+value.preflight.resume.slice(0,12)+')':'Not prepared'}</p>
 {value.preflight.resume&&<Button asChild><a href={'/api'+base+'/preview'} target="_blank" rel="noreferrer">Review tailored resume</a></Button>}
 <p>Verified profile fields: {value.preflight.fields.join(', ')||'None reported'}</p>
 <p>Unanswered mandatory fields: {value.preflight.inspected?(value.preflight.unanswered.join(', ')||'None'):'Form inspection incomplete'}</p>
 {value.preflight.controls?.length>0&&<details><summary>Inspected form fields</summary>{value.preflight.controls.map((field:Obj,i:number)=><small key={i}>{field.label||field.type} · {field.type}{field.required?' · required':''}</small>)}</details>}
 {value.preflight.warnings?.map((warning:string,i:number)=><small key={i}>{warning}</small>)}
 {value.preflight.snapshot&&<Payload snapshot={value.preflight.snapshot}/>}
 {value.preflight.autopilot_due&&!value.attempt&&<div className="stack"><p>Scheduled after {time(value.preflight.autopilot_due)}</p><Button disabled={busy} onClick={()=>post(base+'/auto-cancel')}>Cancel scheduled submission</Button></div>}
 {value.preflight.ready&&value.preflight.snapshot&&!value.attempt&&j.status!=='applied'&&<Button variant="primary" disabled={busy||!value.auto_enabled} onClick={()=>post(base+'/auto-submit')}>Submit this job only</Button>}</>}
 {value.freshness_override&&<p>Override recorded {time(value.freshness_override.recorded_at)} · {value.freshness_override.reason}</p>}
 <form className="stack" onSubmit={e=>submit(e,'freshness-override')}><label>Freshness override reason<textarea name="reason" rows={2} maxLength={500} required/></label><Button disabled={busy}>Save freshness override</Button><small>Overrides document why the age risk is acceptable. Official board and application verification are still required.</small></form>
 </div></Panel>
 {(value.attempt||value.diagnostics?.length>0)&&<Panel title="Application evidence"><div className="pad stack">
  {value.attempt&&<><p>Confirmation state · {format(value.attempt.confirmation_state)}</p><Payload snapshot={value.attempt.snapshot}/></>}
  {value.diagnostics.map((entry:Obj)=><div className="toolbar" key={entry.id}><small>{entry.phase} · {time(entry.at)}</small>
   {entry.png&&<a className="text-link" href={'/api'+base+'/diagnostic/'+entry.id+'/png'}>Screenshot</a>}
   {entry.html&&<a className="text-link" href={'/api'+base+'/diagnostic/'+entry.id+'/html'}>Page source</a>}</div>)}
 </div></Panel>}
 {!['applied','interview','offer','closed','rejected','withdrawn'].includes(j.status)&&(value.preflight||value.attempt||value.diagnostics?.length>0)&&<Button disabled={busy} onClick={()=>post(base+'/auto-manual-done')}>Mark as submitted by me</Button>}
 {application&&<Panel title="Application"><div className="pad stack"><p>Applied {time(application.applied_at)}</p><form className="stack" onSubmit={e=>submit(e,'application',{action:'variant'})}><label>Resume variant<input name="variant" defaultValue={application.variant} required maxLength={50}/></label><Button disabled={busy}>Save variant</Button></form>{application.response_at?<p>Reply recorded {time(application.response_at)}</p>:<Button disabled={busy} onClick={()=>post(base+'/application',{action:'reply'})}>Record application reply</Button>}</div></Panel>}
 <Panel title="Contacts"><div className="pad stack">{j.contacts.emails.length?j.contacts.emails.join(', '):'No email in the posting.'}{j.contacts.linkedin_profiles.map((u:string)=><a className="text-link" key={u} href={safe(u)} target="_blank" rel="noreferrer">{u}</a>)}</div></Panel></div></div>}</>
}
function Payload({snapshot}:{snapshot:Obj}){
 return <details><summary>Review application payload</summary>
  <p className="pre">Destination · {snapshot.action}</p>
  <p>Resume version · {snapshot.resume_version?.slice(0,12)}</p>
  {snapshot.consents?.map((label:string)=><small key={label}>Standing consent · {label}</small>)}
  <dl>{snapshot.fields.map(([name,value]:[string,string|Obj],index:number)=><div key={index}><dt>{name}</dt>
   <dd className="pre">{typeof value==='string'?value:value.redacted?'Hidden or token value (redacted)':value.file||'No file'}</dd></div>)}</dl>
 </details>
}
function Outcome({report}:{report:Obj}){
 if(!report.applications)return <Empty>Mark a job Applied to start tracking outcomes.</Empty>
 return <div className="chart"><ResponsiveContainer width="100%" height={255}><BarChart data={report.groups.role.map(([name,applied,replies]:[string,number,number])=>({name,Applied:applied,Replies:replies}))}><CartesianGrid stroke="var(--border)" vertical={false}/><XAxis dataKey="name" tick={{fill:'var(--muted)',fontSize:11}}/><YAxis allowDecimals={false} tick={{fill:'var(--muted)'}}/><Tooltip contentStyle={{background:'var(--surface)',color:'var(--ink)'}}/><Bar dataKey="Applied" fill="var(--accent)" radius={[4,4,0,0]} isAnimationActive={false}/><Bar dataKey="Replies" fill="var(--blue)" radius={[4,4,0,0]} isAnimationActive={false}/></BarChart></ResponsiveContainer></div>
}
function Analytics({revision}:{revision:number}){
 const {value,error}=useData('/analytics',revision)
 return <><Heading title="Application analytics" intro="Track replies across roles, sources, score bands and resume variants."/>{error?<Empty>{error}</Empty>:!value?<Loading/>:<><div className="stats"><Stat label="Applications" value={value.applications}/><Stat label="Replies" value={value.responses}/><Stat label="Response rate" value={value.applications?Math.round(value.responses/value.applications*100)+'%':'—'}/></div><Panel title="Applications by role"><Outcome report={value}/></Panel><div className="split">{[['role','Role type'],['source','Source'],['score_band','Score band'],['variant','Resume variant']].map(([k,label])=><Panel title={label} key={k}>{value.groups[k].length?<div className="table-wrap"><table><thead><tr><th>Group</th><th>Applied</th><th>Replies</th><th>Rate</th></tr></thead><tbody>{value.groups[k].map(([name,count,replies]:[string,number,number])=><tr key={name}><td>{name}</td><td>{count}</td><td>{replies}</td><td>{Math.round(replies/count*100)}%</td></tr>)}</tbody></table></div>:<Empty>No application records yet.</Empty>}</Panel>)}</div></>}</>
}
function Library({page,revision,open,go}:{page:string;revision:number;open:(n:number,path:string)=>void;go:Go}){
 const {value,error}=useData(page,revision), resume=page==='/resumes'
 return <><Heading title={resume?'Resume Studio':'Outreach'} intro={resume?'Generate, review and download evidence-based resumes.':'Find contacts, draft messages and track follow-ups.'} action={<Button onClick={()=>go('/jobs')}>Choose a job</Button>}/>
 {error?<Empty>{error}</Empty>:!value?<Loading/>:resume?<Panel title="Generated resumes"><Entries rows={value as unknown as Obj[]} open={id=>open(id,page)}/></Panel>:<><Panel title="Follow-ups due"><Entries rows={value.due} open={id=>open(id,page)}/></Panel><Panel title="Saved outreach"><Entries rows={value.drafts} open={id=>open(id,page)}/></Panel></>}</>
}
function Entries({rows,open}:{rows:Obj[];open:(id:number)=>void}){
 return rows.length?<div className="pad stack">{rows.map(r=><div className="row" key={r.id}><div><strong>{r.title}</strong><small>{r.company}</small></div><Button onClick={()=>open(r.id)}>Open <ArrowUpRight size={15}/></Button></div>)}</div>:<Empty>Nothing to review here yet.</Empty>
}
createRoot(document.getElementById('root')!).render(<App/>)
