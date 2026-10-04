// Plain-language summaries of a conversion plan (docs/dwca-conversion/ai-review-and-chat.md §10–11).
// Everything here is derived from the saved plan and decisions; nothing creates a second decision state.

export const shortTerm = term => String(term || '').split('/').pop()

const plural = (count, singular, pluralForm = `${singular}s`) => `${count.toLocaleString()} ${count === 1 ? singular : pluralForm}`

// The effective value of a decision: the user's or AI's saved choice, else the automatic default, else the fallback.
export function makeSelector(state, decisions) {
  const defaults = Object.fromEntries((state?.plan?.automatic_choices || []).map(choice => [choice.id, choice.default]))
  return (id, fallback) => decisions?.[id] ?? defaults[id] ?? fallback
}

const retainedTable = (state, selected, index) => !state.plan.tables[index]?.core && selected(`table:${index}`) === 'preserve'

// Event details on an additional occurrence table are written only as its occurrence-events choice allows
// ("patch" or "per-row"). Returns that choice for such a column, '' while it is unanswered, and null otherwise.
function eventDetailsChoice(state, selected, column, value) {
  if (!String(value).startsWith('event.') || state.plan.tables[column.table]?.core) return null
  const id = `occurrence-events:${column.table}`
  const exists = [...(state.plan.issues || []), ...(state.plan.automatic_choices || [])].some(item => item.id === id)
  return exists ? selected(id) || '' : null
}

const eventDetailsMapped = choice => choice === null || choice === 'patch' || choice === 'per-row'

const hasTarget = column => (column.options || []).some(option => !['preserve', 'join'].includes(option.value))

// How a column that stays in the original files got there. The backend marks columns with `unmapped`
// ("no-target": the Data Package has no field for it; "unsupported": the converter does not map it yet).
// Without it, a column that offers no target is reported neutrally.
export function unmappedReason(column) {
  if (column.unmapped === 'no-target' || column.unmapped === 'unsupported') return column.unmapped
  if (column.review || hasTarget(column)) return 'chosen'
  return 'no-mapping'
}

// A scientificName column always reaches verbatimIdentification, even when scientificName itself is left empty.
// When the table supplies its own verbatimIdentification, only rows without one receive the copy, so it is not counted.
const writtenTarget = (column, value) => value === 'preserve' && column.verbatim_copy && !column.verbatim_copy_partial ? column.verbatim_copy : value

export function summariseColumns(state, selected) {
  const summary = { total: 0, mapped: 0, review: 0, groups: {}, retainedTables: [] }
  const plan = state?.plan
  if (!plan?.columns) return summary
  const retainedColumns = {}
  for (const column of plan.columns) {
    summary.total += 1
    if (retainedTable(state, selected, column.table)) {
      retainedColumns[column.table] = (retainedColumns[column.table] || 0) + 1
      continue
    }
    const value = writtenTarget(column, selected(column.id, column.default))
    const events = value === 'preserve' ? null : eventDetailsChoice(state, selected, column, value)
    if (value !== 'preserve' && eventDetailsMapped(events)) {
      if (column.review) summary.review += 1; else summary.mapped += 1
      continue
    }
    // An unanswered occurrence-events question is covered by the choices below; "preserve" is the user's choice.
    if (value !== 'preserve' && events === '') { summary.review += 1; continue }
    const reason = value === 'preserve' ? unmappedReason(column) : 'chosen'
    const group = summary.groups[reason] ||= { count: 0, names: [] }
    group.count += 1
    const name = shortTerm(column.term)
    if (!group.names.includes(name)) group.names.push(name)
  }
  summary.retainedTables = plan.tables.map((table, index) => ({ table, index }))
    .filter(({ index }) => retainedTable(state, selected, index))
    .map(({ table, index }) => ({ name: table.name, columns: retainedColumns[index] || table.columns?.length || 0 }))
  return summary
}

const nameList = (names, limit) => names.length > limit
  ? `${names.slice(0, limit).join(', ')}, and ${names.length - limit} more` : names.join(', ')

const GROUP_ORDER = ['no-target', 'unsupported', 'no-mapping', 'chosen']

export function summaryLines(summary, limit = 6) {
  const lines = []
  if (summary.mapped || summary.review) {
    lines.push(`${plural(summary.mapped, 'column')} mapped automatically.` + (summary.review
      ? ` ${plural(summary.review, 'more column')} ${summary.review === 1 ? 'is' : 'are'} covered by the choices below.` : ''))
  }
  for (const key of GROUP_ORDER) {
    const group = summary.groups[key]
    if (!group) continue
    const names = nameList(group.names, limit)
    const columns = plural(group.count, 'column')
    lines.push({
      'no-target': `${columns} ${group.count === 1 ? 'has' : 'have'} no Darwin Core Data Package field and ${group.count === 1 ? 'stays' : 'stay'} in your original files: ${names}.`,
      unsupported: `${columns} can't be mapped by ChatIPT yet and ${group.count === 1 ? 'stays' : 'stay'} in your original files: ${names}.`,
      'no-mapping': `${columns} ${group.count === 1 ? 'has' : 'have'} no mapping and ${group.count === 1 ? 'stays' : 'stay'} in your original files: ${names}.`,
      chosen: `${columns} ${group.count === 1 ? 'is' : 'are'} kept in your original files by choice: ${names}.`,
    }[key])
  }
  if (summary.retainedTables.length) {
    lines.push(`${plural(summary.retainedTables.length, 'additional table')} kept in your original files: ${nameList(summary.retainedTables.map(item => item.name), limit)}.`)
  }
  return lines
}

// One entry per column for the collapsible detail list: where it goes, or why it stays in the original files.
export function columnDetails(state, selected) {
  const plan = state?.plan
  return (plan?.columns || []).map(column => {
    const table = plan.tables[column.table]
    const retained = retainedTable(state, selected, column.table)
    const chosen = selected(column.id, column.default)
    const events = retained || chosen === 'preserve' ? null : eventDetailsChoice(state, selected, column, chosen)
    const eventsKept = !eventDetailsMapped(events)
    const value = retained || eventsKept ? 'preserve' : chosen
    const option = (column.options || []).find(item => item.value === value)
    let outcome
    if (retained) outcome = 'Kept in your original files with its table'
    else if (eventsKept) outcome = events === '' ? 'Waiting for your choice about event details on these rows' : 'Kept in your original files; event details on these rows are not copied'
    else if (value === 'preserve' && column.verbatim_copy) outcome = column.verbatim_copy_partial
      ? `The text goes to ${column.verbatim_copy.split('.').pop()} where your file leaves it empty; scientificName comes from the name check or stays empty`
      : `The text goes to ${column.verbatim_copy.split('.').pop()}; scientificName comes from the name check or stays empty`
    else if (value === 'preserve') outcome = {
      'no-target': 'No Darwin Core Data Package field; kept in your original files',
      unsupported: "Can't be mapped yet; kept in your original files",
      'no-mapping': 'No mapping; kept in your original files',
      chosen: 'Kept in your original files',
    }[unmappedReason(column)]
    else outcome = option?.label || value
    return { id: column.id, table: table?.name || '', name: shortTerm(column.term), outcome, mapped: value !== 'preserve' && !retained }
  })
}

// Warnings the backend raises for automatic keep-in-originals choices repeat what the summary already says.
export function dedupeNotices(state, notices, selected) {
  const automatic = new Set((state?.plan?.automatic_choices || []).filter(choice => selected(choice.id, choice.default) === 'preserve').map(choice => choice.id))
  const seen = new Set()
  return (notices || []).filter(notice => {
    if (notice.id && automatic.has(notice.id)) return false
    const key = notice.id ? `id:${notice.id}` : `text:${notice.title}:${notice.reason}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

export const STEPS = ['Upload', 'Inspect', 'Review', 'Convert', 'Download']

// The current step and whether it stopped. Upload is complete as soon as a conversion exists.
export function conversionStep(state) {
  const status = state?.status
  if (!state) return { index: 1, error: false }
  if (status === 'complete') return { index: 4, error: false }
  if (status === 'converting') return { index: 3, error: false }
  if (status === 'review' || status === 'reviewing') return { index: 2, error: false }
  if (status === 'blocked') return { index: 1, error: true }
  if (status === 'failed') return { index: state.plan?.id ? 3 : 1, error: true }
  return { index: 1, error: false }
}

export function stepStates(state) {
  const { index, error } = conversionStep(state)
  return STEPS.map((label, position) => ({
    label,
    status: position < index ? 'done' : position === index ? (error ? 'error' : 'current') : 'upcoming',
  }))
}

// The line under "Conversion options".
export function statusLine(state, outstanding, blockers = 0) {
  if (state?.status === 'reviewing') return 'The AI reviewer is checking the remaining choices. You can keep answering meanwhile.'
  if (outstanding > 0) return `${plural(outstanding, 'choice')} ${outstanding === 1 ? 'needs' : 'need'} your input. Your answers are saved as you go.`
  if (blockers > 0) return `${plural(blockers, 'AI choice')} to check before you can convert.`
  return 'Everything is set; you can convert. You can still adjust any choice.'
}

// Dataset title, then the uploaded file name, then the plan's upload names.
export function conversionTitle(dataset, state) {
  return dataset?.title?.trim() || dataset?.user_files?.[0]?.filename || state?.plan?.uploads?.[0]?.name || 'Darwin Core Archive conversion'
}

// Plan-based diagram: source tables -> Darwin Core Data Package tables.
const ROLE_TARGETS = {
  occurrence: ['occurrence'],
  identification: ['identification'],
  'occurrence-assertion': ['occurrence-assertion'],
  'event-assertion': ['event-assertion'],
  'declared-assertions': ['occurrence-assertion', 'event-assertion'],
  'resource-relationship': ['resource-relationship'],
  molecular: ['molecular-protocol'],
  'media-occurrence': ['media', 'occurrence-media'],
  'media-event': ['media', 'event-media'],
  'media-unlinked': ['media'],
  identifier: ['occurrence-identifier'],
  reference: ['bibliographic-resource'],
}
const TARGET_ORDER = ['event', 'occurrence', 'material', 'identification', 'organism', 'survey', 'media', 'agent']

const roleTargets = role => ROLE_TARGETS[role] || (String(role).startsWith('humboldt-') ? ['survey'] : [])

// "occurrence.scientificName" -> "occurrence"; "preserve", "join" and descriptive values have no table.
export function targetTable(value) {
  const match = /^([a-z][a-z-]*)\.[A-Za-z_]/.exec(String(value || ''))
  return match ? match[1] : null
}

export const tableTitle = name => { const text = String(name).replaceAll('-', ' '); return text.charAt(0).toUpperCase() + text.slice(1) }

export function planDiagram(state, selected) {
  const tables = state?.plan?.tables || []
  const edges = new Map()
  const add = (source, target, columns) => {
    const key = `${source}|${target}`
    const edge = edges.get(key) || { source, target, columns: 0 }
    edge.columns += columns
    edges.set(key, edge)
  }
  for (const column of state?.plan?.columns || []) {
    if (retainedTable(state, selected, column.table)) continue
    const value = writtenTarget(column, selected(column.id, column.default))
    if (!eventDetailsMapped(eventDetailsChoice(state, selected, column, value))) continue
    const target = targetTable(value)
    if (target) add(column.table, target, 1)
  }
  tables.forEach((table, index) => {
    if (table.core || retainedTable(state, selected, index)) return
    for (const target of roleTargets(selected(`table:${index}`))) add(index, target, 0)
    // Where and when details on an occurrence table: "patch" fills the linked events that exist already;
    // "per-row" creates a child event of the linked event for each occurrence row.
    const details = selected(`occurrence-events:${index}`)
    if (details === 'patch' || details === 'per-row') {
      add(index, 'event', 0)
      if (details === 'per-row') edges.get(`${index}|event`).childEvents = true
    }
  })
  const names = [...new Set([...edges.values()].map(edge => edge.target))]
  const rank = name => TARGET_ORDER.includes(name) ? TARGET_ORDER.indexOf(name) : TARGET_ORDER.length
  names.sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
  const list = [...edges.values()]
  return {
    sources: tables.map((table, index) => ({ index, name: table.name, rows: table.rows, core: Boolean(table.core), retained: retainedTable(state, selected, index) })),
    targets: names.map(name => ({ name, title: tableTitle(name), columns: list.filter(edge => edge.target === name).reduce((sum, edge) => sum + edge.columns, 0) })),
    edges: list,
  }
}
