import { Btn } from '@kirocrew/app-sdk/ui'
import { forwardRef, type ButtonHTMLAttributes } from 'react'
import { caution } from './kit'

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  /** The surface's one main action (host accent fill). */
  primary?: boolean
  /** Stops or ends something: red label, and the guide's caution marker. */
  danger?: boolean
  /** A low-weight action: no border (the host has no quiet variant). */
  quiet?: boolean
  /** A request is in flight: the press is refused but focus stays on the button. */
  busy?: boolean
}

/** Every page button: the host Btn with `type="button"` by default and the `cs-btn` class, which
 *  carries the 44 px touch rule and the open and pressed looks. */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button({ type = 'button', className, quiet, danger, ...rest }, ref) {
  const cls = ['cs-btn', quiet ? 'cs-quiet' : '', className ?? ''].filter(Boolean).join(' ')
  return <Btn ref={ref} type={type} danger={danger} className={cls} {...(danger ? caution : {})} {...rest} />
})
