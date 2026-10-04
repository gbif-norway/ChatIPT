'use client'

import { planDiagram } from '../utils/conversionPlan.mjs'

const WIDTH = 720
const NODE_WIDTH = 228
const NODE_HEIGHT = 52
const GAP = 12
const clip = (text, length) => text.length > length ? `${text.slice(0, length - 1)}…` : text
const rowsLabel = count => `${count.toLocaleString()} ${count === 1 ? 'row' : 'rows'}`
const columnsLabel = count => count ? `${count.toLocaleString()} ${count === 1 ? 'column' : 'columns'} mapped` : 'linked records'

// A drawing of the saved plan, not of converted data: source tables on the left, the Darwin Core Data Package
// tables they will fill on the right. It follows the current choices, so it changes as the user answers.
export default function ConversionPlanDiagram({ state, selected }) {
  const { sources, targets, edges } = planDiagram(state, selected)
  if (!sources.length) return null
  const rows = Math.max(sources.length, targets.length)
  const height = rows * (NODE_HEIGHT + GAP) - GAP
  const top = (count, index) => (height - (count * (NODE_HEIGHT + GAP) - GAP)) / 2 + index * (NODE_HEIGHT + GAP)
  const sourceY = index => top(sources.length, index)
  const targetY = name => top(targets.length, targets.findIndex(target => target.name === name))
  const targetsOf = index => edges.filter(edge => edge.source === index).map(edge => targets.find(target => target.name === edge.target)?.title)
  return <section className="conversion-plan mb-4" aria-labelledby="conversion-plan-heading">
    <h2 className="h5 mb-1" id="conversion-plan-heading">What the conversion will create</h2>
    <p className="small text-body-secondary mb-2">A plan based on your current choices, drawn before anything is converted. Your files → Darwin Core Data Package tables.
      Your original files are always kept in full.</p>
    {targets.length === 0
      ? <p className="small">No table of your files has a Darwin Core Data Package field yet. Make the choices below and the plan will appear here.</p>
      : <>
        <svg className="conversion-plan-svg d-none d-md-block" viewBox={`0 0 ${WIDTH} ${height}`} aria-hidden="true" focusable="false">
          {edges.map(edge => {
            const y1 = sourceY(edge.source) + NODE_HEIGHT / 2
            const y2 = targetY(edge.target) + NODE_HEIGHT / 2
            const x1 = NODE_WIDTH
            const x2 = WIDTH - NODE_WIDTH
            return <path key={`${edge.source}-${edge.target}`} className="conversion-plan-edge" d={`M${x1} ${y1} C${x1 + 110} ${y1} ${x2 - 110} ${y2} ${x2} ${y2}`}
              style={{ strokeWidth: Math.min(1.5 + edge.columns / 6, 5) }} />
          })}
          {sources.map(source => <g key={source.index} className={`conversion-plan-node ${source.retained ? 'conversion-plan-node-kept' : ''}`} transform={`translate(0 ${sourceY(source.index)})`}>
            <rect width={NODE_WIDTH} height={NODE_HEIGHT} rx="8" />
            <text x="12" y="22" className="conversion-plan-name">{clip(source.name, 30)}</text>
            <text x="12" y="40" className="conversion-plan-detail">{rowsLabel(source.rows)}{source.retained ? ' · kept as original' : ''}</text>
          </g>)}
          {targets.map(target => <g key={target.name} className="conversion-plan-node conversion-plan-node-target" transform={`translate(${WIDTH - NODE_WIDTH} ${targetY(target.name)})`}>
            <rect width={NODE_WIDTH} height={NODE_HEIGHT} rx="8" />
            <text x="12" y="22" className="conversion-plan-name">{clip(target.title, 30)}</text>
            <text x="12" y="40" className="conversion-plan-detail">{columnsLabel(target.columns)}</text>
          </g>)}
        </svg>
        <ul className="conversion-plan-list mb-0">
          {sources.map(source => <li key={source.index}>
            <strong>{source.name}</strong> ({rowsLabel(source.rows)}){' '}
            {source.retained ? 'stays in your original files.' : targetsOf(source.index).length ? `fills ${targetsOf(source.index).join(', ')}.` : 'has no table to fill yet.'}
          </li>)}
        </ul>
      </>}
  </section>
}
