// The two host-kit exports that are Level-1 additions (docs/app-kit/api-reference.md): a named import
// of a name the host lacks fails the whole bundle at load, so they are read through the namespace
// here, the one place that does so, with a fallback for a host without them.
import * as ui from '@kirocrew/app-sdk/ui'

type SelectProps = ui.SelectProps

/** The guide's caution marker for a control that removes something; nothing on an older host. */
export const caution: { readonly 'data-guide-caution'?: '' } = ui.guideCaution ?? {}

/** The host's Select (a Radix list on desktop, the native list on touch), or a native `<select>` with
 *  the same props where the host has none. */
export function Select(props: SelectProps) {
  const Host = ui.Select
  if (Host) return <Host {...props} />
  const { options, optionLabels, value, onChange, clearLabel, disabled, title, className } = props
  return (
    <select aria-label={props['aria-label']} title={title} disabled={disabled} value={value} onChange={(e) => onChange(e.target.value)} className={className}>
      {clearLabel !== undefined ? <option value="">{clearLabel}</option> : null}
      {options.map((o, i) => (
        <option key={o} value={o}>
          {optionLabels?.[i] ?? o}
        </option>
      ))}
    </select>
  )
}
