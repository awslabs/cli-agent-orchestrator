/**
 * The CLI Agent Orchestrator mark, inlined rather than loaded as an asset.
 *
 * At 32px this is smaller than the HTTP request that would fetch it, and the
 * dashboard build output is force-included into the Python wheel
 * (`src/cli_agent_orchestrator/web_ui/**` in pyproject.toml), so keeping the
 * logo out of the asset pipeline means one less packaged file to lose.
 *
 * These are the dark-surface colors from docusaurus/static/img/cao-mark-dark.svg
 * because the dashboard chrome is always dark. The navy of the light variant
 * contrasts at roughly 2:1 on this header and is not an option here.
 *
 * docusaurus/static/img/cao-mark.svg is the canonical geometry. Four files carry
 * a copy of these paths, each with a different color treatment: that one, its
 * dark counterpart, cao-social-card.svg, and this component. Only the colors are
 * meant to differ, so a change to the shape has to land in all four.
 */
export function CaoMark({ size = 32 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 120 120"
      role="img"
      aria-label="CLI Agent Orchestrator"
    >
      <g fill="none" strokeWidth="11" strokeLinecap="round" strokeLinejoin="round">
        <path stroke="#3B90F0" d="M17.91 60V35.7L60 11.4" />
        <path stroke="#C9D9EC" d="M60 11.4l42.09 24.3V60" />
        <path stroke="#16A0B5" d="M102.09 60v24.3L60 108.6" />
        <path stroke="#C9D9EC" d="M60 108.6L17.91 84.3V60" />
      </g>
      <g fill="none" strokeWidth="7" strokeLinecap="round">
        <path stroke="#C9D9EC" d="M67 42L32 79" />
        <path stroke="#16A0B5" d="M72 79L41 50" />
        <path stroke="#C9D9EC" d="M41 50l26-8" />
      </g>
      <circle cx="41" cy="50" r="7.5" fill="#3B90F0" />
      <circle cx="72" cy="79" r="9.5" fill="#16A0B5" />
      <circle cx="67" cy="42" r="10.5" fill="#C9D9EC" />
    </svg>
  )
}
