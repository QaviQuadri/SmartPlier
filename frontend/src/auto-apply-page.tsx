import {useEffect,useState} from 'react'
import {Button} from './button'
import {format,time,safe,useData,Panel,Heading,Empty,Badge,Loading} from './ui'
import type {Obj,Post} from './ui'
export function AutoApply({revision,post,busy}:{revision:number;post:Post;busy:boolean}){
 const {value,error}=useData('/auto-apply',revision),[config,setConfig]=useState<Obj|null>(null)
 useEffect(()=>{if(value)setConfig(old=>old??value.settings)},[value])
 if(error)return <Empty>{error}</Empty>
 if(!value||!config)return <Loading/>
 return <><Heading title="Auto-Apply" intro="Review application attempts and configure automatic submission." action={<Button variant="primary" disabled={!value.settings.enabled||busy} onClick={()=>post('/auto-apply')}>Prepare batch preflights</Button>}/>
 <Panel title="Submission rules"><form className="pad form-grid" onSubmit={async e=>{e.preventDefault();const result=await post('/auto-apply-settings',{...config,authorize_autopilot:config.autopilot,...Object.fromEntries(['roles','locations'].map(k=>[k,typeof config[k]==='string'?config[k].split('\n').map((x:string)=>x.trim()).filter(Boolean):config[k]]))},'Auto-Apply settings saved.');if(result)setConfig(null)}}>
 <label className="wide check"><input type="checkbox" checked={config.enabled} onChange={e=>setConfig({...config,enabled:e.target.checked,autopilot:e.target.checked&&config.autopilot})}/> Enable selected-job submission and automatic preflights after discovery</label>
 <label className="wide check"><input type="checkbox" checked={config.autopilot} disabled={!config.enabled} onChange={e=>setConfig({...config,autopilot:e.target.checked})}/> Authorize scheduled submission of eligible jobs after the three-minute payload review window</label>
 <label className="wide check"><input type="checkbox" checked={config.privacy_consent} onChange={e=>setConfig({...config,privacy_consent:e.target.checked})}/> Pre-approve standalone privacy and data-processing checkboxes</label>
 <label className="wide check"><input type="checkbox" checked={config.screening_drafts} onChange={e=>setConfig({...config,screening_drafts:e.target.checked})}/> Allow Gemini to select profile evidence for screening drafts</label>
 {([['threshold','Minimum Gemini fit',0,100],['daily_cap','Daily submission cap',1,25],['max_age','Stale-risk threshold in days',1,90]] as const).map(([k,label,min,max])=><label key={k}>{label}<input type="number" min={min} max={max} required value={config[k]} onChange={e=>setConfig({...config,[k]:Number(e.target.value)})}/></label>)}
 {['roles','locations'].map(k=><label className="wide" key={k}>Allowed {k}, one phrase per line<textarea rows={3} value={Array.isArray(config[k])?config[k].join('\n'):config[k]} onChange={e=>setConfig({...config,[k]:e.target.value})}/></label>)}
 <p className="wide muted">Unknown required fields and employer access checks go to Needs review. Browser actions require Python Playwright and Chromium.</p>
 <Button variant="primary" disabled={busy}>Save rules</Button>
 </form></Panel>
 <Panel title="Autopilot and receipts"><div className="pad stack">
  <p>{value.circuit.paused?'Paused · '+value.circuit.reason:value.settings.autopilot?'Scheduled submission enabled':'Scheduled submission disabled'}</p>
  {value.digest.length>0&&<p>Last 24 hours · {value.digest.map((item:Obj)=>format(item.status)+' '+item.count).join(' · ')}</p>}
  <div className="toolbar">
   <Button disabled={busy} onClick={()=>post('/auto-receipts')}>Check Gmail receipts</Button>
   {value.circuit.paused&&<Button disabled={busy} onClick={()=>post('/autopilot-resume')}>Resume autopilot</Button>}
   <Button disabled={busy||value.settings.daily_cap>=25} onClick={async()=>{if(await post('/autopilot-ramp'))setConfig(null)}}>Raise daily cap by one</Button>
  </div></div></Panel>
 <QuestionInbox revision={revision} post={post} busy={busy}/>
 <Panel title="Application queue">{value.rows.length?<div className="table-wrap"><table><thead><tr><th>Job</th><th>State</th><th>Result</th></tr></thead><tbody>{value.rows.map((r:Obj)=><tr key={r.job_id}><td><a className="text-link" href={'/jobs?id='+r.job_id}>{r.title}</a><small>{r.company}</small></td><td><Badge value={r.status}/></td><td>{r.detail}<small>{time(r.updated_at)}{r.submitted_at?' · Confirmed '+time(r.submitted_at):''}</small>{r.confirmation&&<small>{r.confirmation} · Resume {r.resume_version?.slice(0,12)}</small>}{r.application_url&&<a href={safe(r.application_url)} target="_blank" rel="noreferrer">Application page</a>}</td></tr>)}</tbody></table></div>:<Empty>No application attempts yet.</Empty>}</Panel>
 <Panel title="Execution history"><div className="pad stack">{value.history.length?value.history.map((event:Obj)=><p key={event.id}>Job #{event.job_id} · {format(event.status)} · {event.detail}<small>{time(event.at)}</small></p>):<Empty>No execution events yet.</Empty>}</div></Panel></>
}

function QuestionInbox({revision,post,busy}:{revision:number;post:Post;busy:boolean}){
 const {value,error}=useData('/screening',revision)
 return <Panel title="Question inbox">{error?<Empty>{error}</Empty>:!value?<Loading/>:value.questions.length?
  <div className="pad stack">{value.questions.map((question:Obj)=><ScreeningQuestion key={question.id} question={question} canDraft={value.drafts_enabled&&question.can_draft} post={post} busy={busy}/>)}</div>:
  <Empty>No screening questions are waiting for review.</Empty>}</Panel>
}

function ScreeningQuestion({question,canDraft,post,busy}:{question:Obj;canDraft:boolean;post:Post;busy:boolean}){
 const [answer,setAnswer]=useState(''),[approved,setApproved]=useState(false),[result,setResult]=useState('')
 const multiple=question.kind==='select-multiple'
 const choices=['radio','checkbox','select-one','select-multiple'].includes(question.kind)
 const manual=question.protection==='attestation'||question.kind==='file'||choices&&!question.options.length||(question.protection&&
  !['radio','select-one'].includes(question.kind))
 const policy=question.protection==='eeo'?'decline':question.protection==='legal_status'?'verified_legal':''
 const path='/screening/'+question.id
 return <form className="stack" onSubmit={async e=>{
  e.preventDefault()
  const data=new FormData(e.currentTarget)
  const saved=await post(path,{value:multiple?data.getAll('answer'):data.get('answer'),policy},
   'Screening answer saved.')
  if(saved)setResult(saved.task?'Saved. Rechecking parked jobs.':saved.retry?'Saved. Run a new preflight from the job page.':'Saved.')
 }}>
  <strong>{question.label}</strong>
  <small>{question.count} {question.count===1?'job':'jobs'} waiting · {format(question.kind)} · {question.employers.map((x:Obj)=>x.company+' ('+x.ats+')').join(', ')}</small>
  {manual?<p>This field requires manual review.</p>:<>
   {question.options.length?<label>Verified answer<select name="answer" required multiple={multiple}>
    {!multiple&&<option value="">Select an option</option>}
    {question.options.map((option:string,i:number)=><option key={i} value={option}>{option}</option>)}
   </select></label>:<label>Verified answer<textarea name="answer" required maxLength={1200} value={answer} onChange={e=>setAnswer(e.target.value)}/></label>}
   {policy&&<label className="check"><input type="checkbox" checked={approved} onChange={e=>setApproved(e.target.checked)} required/>
    {policy==='decline'?'Use my explicit policy to decline to self-identify where this option exists.':'I verified this answer for the listed job location.'}</label>}
   <div className="toolbar"><Button disabled={busy||!!policy&&!approved}>Save answer</Button>
    {canDraft&&<Button type="button" disabled={busy} onClick={async()=>{const generated=await post(path+'/draft');if(generated){setAnswer(generated.draft);setResult('Generated from profile evidence. Review before saving.')}}}>Draft from evidence</Button>}</div>
  </>}
  {result&&<small role="status">{result}</small>}
 </form>
}
