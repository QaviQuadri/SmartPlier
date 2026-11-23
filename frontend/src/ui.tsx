import {useEffect,useState} from 'react'
import type {ReactNode} from 'react'
export type Obj=Record<string, any>
export type Post=(path:string,data?:Obj,message?:string)=>Promise<any>
export type Go=(path:string)=>void
export const format=(s: unknown)=>String(s??'').replaceAll('_',' ').replace(/^./,c=>c.toUpperCase())
export const time=(s?:string)=>s?new Date(s).toLocaleString():'—'
export const safe=(s:string)=>{try{return ['https:','http:'].includes(new URL(s).protocol)?s:undefined}catch{return undefined}}
export async function request(path:string, data?:Obj, csrf?:string){
 const response=await fetch('/api'+path,data?{method:'POST',body:new URLSearchParams({csrf:csrf??'',payload:JSON.stringify(data)})}:undefined)
 const result=await response.json()
 if(!response.ok)throw new Error(result.error??'Request failed.')
 return result
}
export function useData(path:string,version:number){
 const [value,set]=useState<Obj|null>(null),[error,setError]=useState('')
 useEffect(()=>{let live=true;setError(''); request(path).then(x=>{if(live)set(x)}).catch(e=>{if(live)setError(e.message)});return()=>{live=false}},[path,version])
 return {value,error}
}
export function Panel({title,children,action}:{title?:string;children:ReactNode;action?:ReactNode}){
 return <section className="panel">{title&&<div className="panel-head"><h2>{title}</h2>{action}</div>}{children}</section>
}
export function Heading({title,intro,action}:{title:string;intro?:string;action?:ReactNode}){
 return <header className="heading"><div><h1>{title}</h1>{intro&&<p>{intro}</p>}</div>{action}</header>
}
export function Empty({children}:{children:ReactNode}){return <div className="empty">{children}</div>}
export function Badge({value}:{value:string}){return <span className={'badge '+value}>{format(value)}</span>}
export function Loading(){return <div className="stack" aria-label="Loading"><div className="skeleton"/><div className="skeleton"/><div className="skeleton"/></div>}
export function Stat({label,value}:{label:string;value:ReactNode}){return <div className="panel stat"><span>{label}</span><strong>{value}</strong></div>}
