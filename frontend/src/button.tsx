import {Slot} from '@radix-ui/react-slot'
import {cva} from 'class-variance-authority'
import {twMerge} from 'tailwind-merge'
import type {ButtonHTMLAttributes} from 'react'
const variants=cva('inline-flex items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm font-semibold transition-colors focus-visible:outline-2 disabled:opacity-50', {variants:{variant:{primary:'bg-[var(--accent)] text-[var(--accent-ink)] hover:brightness-110', secondary:'border border-[var(--border)] bg-[var(--surface)] hover:bg-[var(--hover)]', danger:'text-[var(--danger)] hover:bg-[var(--hover)]'}},defaultVariants:{variant:'secondary'}})
export function Button({asChild,variant,className,...props}:ButtonHTMLAttributes<HTMLButtonElement>&{asChild?:boolean;variant?:'primary'|'secondary'|'danger'}) {
 const Comp=asChild?Slot:'button'
 return <Comp className={twMerge(variants({variant}),className)} {...props}/>
}
