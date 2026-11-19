import { useId, type ReactNode } from 'react'

export function Section({ title, count, empty, children }: { title: string; count: number; empty?: string; children?: ReactNode }) {
  const id = useId()
  return (
    <section aria-labelledby={id}>
      <h2 className="cs-sec" id={id}>
        {title} ({count})
      </h2>
      {count === 0 ? empty ? <p className="cs-none">{empty}</p> : null : <div className="cs-grid">{children}</div>}
    </section>
  )
}
