import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { AssignmentLine } from './AssignmentLine'
import { AgentOnMap, BeadOnMap } from '../../stores/starcraftStore'

const mockAgent: AgentOnMap = {
  id: 'a1',
  name: 'test',
  icon: '🔧',
  status: 'PROCESSING',
  position: { x: 100, y: 100 },
  assignedBeadId: 'b1',
  color: '#00ff88'
}

const mockBead: BeadOnMap = {
  id: 'b1',
  title: 'Test',
  priority: 1,
  status: 'wip',
  assigneeId: 'a1',
  position: { x: 200, y: 200 },
  isOrphaned: false
}

describe('AssignmentLine', () => {
  it('renders path between agent and bead', () => {
    const { container } = render(
      <svg><AssignmentLine agent={mockAgent} bead={mockBead} /></svg>
    )
    const path = container.querySelector('path')
    expect(path).toBeTruthy()
    expect(path?.getAttribute('stroke')).toBe('#00ff88')
  })

  it('has dashed stroke', () => {
    const { container } = render(
      <svg><AssignmentLine agent={mockAgent} bead={mockBead} /></svg>
    )
    const path = container.querySelector('path')
    expect(path?.getAttribute('stroke-dasharray')).toBe('5,5')
  })

  it('has marching ants animation', () => {
    const { container } = render(
      <svg><AssignmentLine agent={mockAgent} bead={mockBead} /></svg>
    )
    const animate = container.querySelector('animate')
    expect(animate).toBeTruthy()
    expect(animate?.getAttribute('attributeName')).toBe('stroke-dashoffset')
  })

  it('returns null when bead has no position', () => {
    const beadNoPos = { ...mockBead, position: null }
    const { container } = render(
      <svg><AssignmentLine agent={mockAgent} bead={beadNoPos} /></svg>
    )
    expect(container.querySelector('path')).toBeNull()
  })

  it('uses bezier curve path', () => {
    const { container } = render(
      <svg><AssignmentLine agent={mockAgent} bead={mockBead} /></svg>
    )
    const path = container.querySelector('path')
    expect(path?.getAttribute('d')).toContain('Q') // Quadratic bezier
  })
})
