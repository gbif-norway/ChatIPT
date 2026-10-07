// Plain-language summaries of a conversion plan (docs/dwca-conversion/ai-review-and-chat.md §10–11).
// Everything here is derived from the saved plan and decisions; nothing creates a second decision state.

export const shortTerm = term => String(term || '').split('/').pop()

const plural = (count, singular, pluralForm = `${singular}s`) => `${count.toLocaleString()} ${count === 1 ? singular : pluralForm}`

// The effective value of a decision: the user's or AI's saved choice, else the automatic default, else the fallback.
export function makeSelector(state, decisions) {
  const defaults = Object.fromEntries((state?.plan?.automatic_choices || []).map(choice => [choice.id, choice.default]))
  return (id, fallback) => decisions?.[id] ?? state?.conditional_defaults?.[id]?.value ?? defaults[id] ?? fallback
}

export function glossaryEntry(state, value) {
  if (value === 'preserve') return state?.plan?.glossary?.preserve || null
  return state?.plan?.glossary?.targets?.[value] || null
}

const KIND_FAMILY = {
  'agent-identity': 'agent-role', 'material-identity': 'specimens', 'occurrence-status': 'status',
  'event-grain': 'events', 'occurrence-events': 'events', 'extension-role': 'tables',
  'survey-classification': 'surveys', 'column-mapping': 'columns',
}
const FAMILY_HEADINGS = {
  'agent-role': 'People', specimens: 'Specimens', status: 'Present or absent', events: 'Events', tables: 'Linked tables',
  surveys: 'Surveys', media: 'Media', columns: 'Columns', other: 'Other choices',
}
const FAMILY_ORDER = ['agent-role', 'specimens', 'type-status', 'status', 'media', 'events', 'tables', 'surveys', 'columns', 'other']
const optionLabel = (item, value) => item?.options?.find(option => option.value === value)?.label || value
const formatTemplate = (text, count) => String(text || '').replaceAll('{count}', count.toLocaleString())

// Choices of occurrence records nested in a Taxon-core archive carry a 'taxon-occurrence:<i>:' prefix.
const NESTED_PREFIX = /^(?:taxon-occurrence:\d+:)+/
export const idPrefix = id => NESTED_PREFIX.exec(String(id || ''))?.[0] || ''
export const localId = id => String(id || '').slice(idPrefix(id).length)
export const isColumnId = id => localId(id).startsWith('column:')

const flatConditions = conditions => (conditions || []).flatMap(condition =>
  ['any', 'all'].includes(condition.type) ? flatConditions(condition.conditions) : [condition])
const conditionIds = item => flatConditions((item.default_when || []).flatMap(branch => branch.when || []))
  .filter(condition => condition.type === 'decision_in').map(condition => condition.id)
// Columns whose target a default follows (recordedByID follows where recordedBy goes).
const conditionColumns = item => flatConditions((item.default_when || []).flatMap(branch => branch.when || []))
  .filter(condition => ['target_in', 'target_not_in'].includes(condition.type)).map(condition => condition.column)

// A decision or column as the server last computed it, without unsaved local changes.
const plainDefault = (state, id) => (state?.plan?.automatic_choices || []).find(choice => choice.id === id)?.default ??
  (state?.plan?.columns || []).find(column => column.id === id)?.default
const savedValue = (state, id) => state?.decisions?.[id] ?? state?.conditional_defaults?.[id]?.value ?? plainDefault(state, id)

export function automaticSummary(state, selected, hidden = new Set()) {
  const plan = state?.plan || {}
  const automatic = plan.automatic_choices || []
  const issues = plan.issues || []
  const columns = plan.columns || []
  const entries = [...issues, ...automatic]
  const warningIds = new Set((plan.warnings || []).map(warning => warning.id).filter(Boolean))
  const lines = []
  for (const item of automatic) {
    // A choice that needs an answer is shown once, as a question.
    if (hidden.has(item.id)) continue
    const prefix = idPrefix(item.id)
    const local = localId(item.id)
    const tablePreserved = item.table !== undefined && !plan.tables?.[item.table]?.core && selected(`table:${item.table}`) === 'preserve'
    if (tablePreserved && item.id !== `table:${item.table}`) continue
    const sourceColumn = prefix ? `${prefix}column:0:${item.source_column}` : `column:${item.table}:${item.source_column}`
    if (item.source_column != null && selected(sourceColumn) === 'preserve') continue
    if (local.startsWith('agent-share:') && selected(`${prefix}agent-names`, 'shared') === 'text') continue
    const isColumn = isColumnId(item.id)
    if (isColumn && item.nonempty === 0) continue
    const value = selected(item.id, item.default)
    const changed = Object.hasOwn(state?.decisions || {}, item.id)
    const family = item.family || KIND_FAMILY[item.kind] || 'other'
    const heading = plan.glossary?.families?.[family] || FAMILY_HEADINGS[family] || FAMILY_HEADINGS.other
    const target = glossaryEntry(state, value)
    const decided = option => glossaryEntry(state, option)?.decided ?? optionLabel(item, option)
    let title = isColumn ? `${shortTerm(item.term)} → ${target?.decided ?? optionLabel(item, value)}` : item.title
    let text = isColumn
      ? changed ? `You chose this: ${optionLabel(item, value)}.` : (state?.conditional_defaults?.[item.id]?.reason ?? item.reason)
      : changed ? `You chose: ${optionLabel(item, value)}.` : `${optionLabel(item, value)}. ${item.reason || ''}`.trim()
    // A default that depends on a question still open says what each answer does, rather than explaining today's fallback.
    const withPrefix = id => prefix && !idPrefix(id) ? prefix + id : id
    const referenced = changed ? [] : [...conditionIds(item), ...conditionColumns(item)].map(withPrefix)
    const unsaved = referenced.some(id => selected(id, plainDefault(state, id)) !== savedValue(state, id))
    const waitingFor = unsaved ? null : conditionIds(item).map(withPrefix).filter(id => (state?.unresolved || []).includes(id))
      .map(id => entries.find(entry => entry.id === id)).find(Boolean)
    // A specimen field only receives values when specimen records are created; otherwise they stay in the originals.
    const materialId = prefix ? `${prefix}material:0` : `material:${item.table}`
    const noSpecimens = isColumn && String(value).startsWith('material.') &&
      entries.some(entry => entry.id === materialId) && !['per_row', 'by_id'].includes(selected(materialId))
    if (noSpecimens) {
      title = `${shortTerm(item.term)} → kept in your original files`
      text = `${changed ? `You chose “${optionLabel(item, value)}”, but` : 'This goes to the specimen record, but'} no specimen records are created, so the values stay in your original files. Choose another option, or create specimen records.`
    } else if (!changed && unsaved) {
      // The server recomputes this default from an answer that is still being saved.
      title = `${shortTerm(item.term)} → updating to follow your answer`
      text = 'This follows the answer you just changed; it updates once your answer is saved.'
    } else if (waitingFor) {
      const outcomes = waitingFor.options.filter(option => option.value !== 'preserve').map(option => {
        const branch = item.default_when.find(entry => (entry.when || []).some(condition =>
          condition.type === 'decision_in' && localId(condition.id) === localId(waitingFor.id) && condition.values.includes(option.value)))
        return `“${option.label}”: ${decided(branch ? branch.value : item.default)}`
      })
      title = `${shortTerm(item.term)} → waiting for your answer`
      text = `Depends on your answer to “${waitingFor.title}”. ${outcomes.join('; ')}; otherwise ${decided(item.default)}.`
    }
    lines.push({ line: { id: item.id, item, family, heading, title, text, value, changed },
      glance: Boolean(item.glance || item.convention || warningIds.has(item.id)) })
  }
  const rank = family => {
    const index = FAMILY_ORDER.indexOf(family)
    return index < 0 ? FAMILY_ORDER.length : index
  }
  const glanceLines = lines.filter(entry => entry.glance)
    .map((entry, index) => ({ entry, index })).sort((a, b) => rank(a.entry.line.family) - rank(b.entry.line.family) || a.index - b.index).map(entry => entry.entry.line)
  const silent = lines.filter(entry => !entry.glance).map(entry => entry.line)
  const specimen = []
  const materialEntries = new Map(entries.filter(item => localId(item.id).startsWith('material:')).map(item => [item.id, item]))
  const details = plan.glossary?.specimen_details || {}
  for (const [id, item] of materialEntries) {
    const followers = columns.filter(column => column.follows === id && column.nonempty > 0)
    if (!followers.length) continue
    const value = selected(id)
    const specimenState = ['per_row', 'by_id'].includes(value) ? 'stored' : value == null || value === '' ? 'pending' : 'kept'
    const name = column => glossaryEntry(state, column.default)?.field_label || shortTerm(column.term)
    // A detail the user moved elsewhere (or kept in the originals) is listed apart, not counted as stored.
    const following = followers.filter(column => selected(column.id, column.default) === column.default)
    const overridden = followers.filter(column => !following.includes(column)).map(column => `${name(column)} (${optionLabel(column, selected(column.id, column.default))})`)
    const count = following.length
    if (!count && !overridden.length) continue
    const title = formatTemplate(details[specimenState], count) || {
      stored: `${count} specimen ${count === 1 ? 'detail' : 'details'} stored on specimen records`,
      kept: `${count} specimen ${count === 1 ? 'detail' : 'details'} kept in your original files`,
      pending: `${count} specimen ${count === 1 ? 'detail' : 'details'} waiting for your choice`,
    }[specimenState]
    const why = formatTemplate(details[`${specimenState}_why`], count) || {
      stored: 'You chose to create specimen records.', kept: 'You chose not to create specimen records.',
      pending: 'Your choice about specimen records is still open.',
    }[specimenState]
    // While the specimen question is open below, it is answered there rather than from this line.
    specimen.push({ id, item, count, names: following.map(name), overridden, state: specimenState, title, why, editable: !hidden.has(id) })
  }
  return { glance: glanceLines, specimen, silent, ids: new Set([...lines.map(entry => entry.line.id), ...specimen.map(line => line.id)]) }
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
// When the table's own verbatimIdentification is converted, only rows without one receive the copy, so it is not counted.
const partialCopy = (state, column, selected) => {
  const source = column.verbatim_source && (state?.plan?.columns || []).find(item => item.id === column.verbatim_source)
  // Only the file's own text in the same destination field takes precedence over the copy.
  return Boolean(source) && !retainedTable(state, selected, source.table) && selected(source.id, source.default) === column.verbatim_copy
}
const writtenTarget = (state, column, value, selected) =>
  value === 'preserve' && column.verbatim_copy && !partialCopy(state, column, selected) ? column.verbatim_copy : value

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
    const value = writtenTarget(state, column, selected(column.id, column.default), selected)
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
    else if (value === 'preserve' && column.verbatim_role === 'qualifier') outcome = partialCopy(state, column, selected)
      ? `Added after the name text in ${column.verbatim_copy.split('.').pop()} where your file leaves it empty`
      : `Added after the name text in ${column.verbatim_copy.split('.').pop()}`
    else if (value === 'preserve' && column.verbatim_copy) outcome = partialCopy(state, column, selected)
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

export const AGENT_NAMES_ID = 'agent-names'

// Warnings the backend raises for automatic keep-in-originals choices repeat what the summary already says.
// The agent-names notice describes linking names to agents, so it goes once every name is kept as text only.
export function dedupeNotices(state, notices, selected, panelIds = new Set()) {
  const automatic = new Set((state?.plan?.automatic_choices || []).filter(choice => selected(choice.id, choice.default) === 'preserve').map(choice => choice.id))
  const seen = new Set()
  return (notices || []).filter(notice => {
    if (notice.id && panelIds.has(notice.id)) return false
    if (notice.id && automatic.has(notice.id)) return false
    if (notice.id === AGENT_NAMES_ID && selected(AGENT_NAMES_ID, 'shared') === 'text') return false
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
    const value = writtenTarget(state, column, selected(column.id, column.default), selected)
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
