import { AgentOnMap, BeadOnMap } from '../../stores/starcraftStore'

interface Props { agent: AgentOnMap; bead: BeadOnMap }

export function AssignmentLine({ agent, bead }: Props) {
  if (!bead.position) return null
  
  const x1 = agent.position.x + 24
  const y1 = agent.position.y + 48
  const x2 = bead.position.x + 50
  const y2 = bead.position.y
  const midX = (x1 + x2) / 2
  const midY = (y1 + y2) / 2 - 20

  return (
    <path
      d={`M ${x1} ${y1} Q ${midX} ${midY} ${x2} ${y2}`}
      stroke={agent.color}
      strokeWidth={2}
      strokeDasharray="5,5"
      fill="none"
      opacity={0.8}
    >
      <animate attributeName="stroke-dashoffset" from="10" to="0" dur="0.5s" repeatCount="indefinite" />
    </path>
  )
}
