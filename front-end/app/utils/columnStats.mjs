// Pure helpers for the table explorer's Columns view: sorting, labels, percentages, bar scaling
// and the plain-language sentences shown to biologists checking their converted data.

export const INLINE_VALUE_MIN = 2
export const INLINE_VALUE_MAX = 6
export const VALUE_PAGE_SIZE = 50
export const WIDE_LAYOUT_MIN_WIDTH = 900

const toNumber = (value) => {
  const number = Number(value)
  return Number.isFinite(number) ? number : 0
}

export const formatCount = (value) => toNumber(value).toLocaleString()

export const pluralize = (count, singular, plural = `${singular}s`) => (
  `${formatCount(count)} ${toNumber(count) === 1 ? singular : plural}`
)

// "41%", "0.4%", "<0.1%", "99.8%", ">99.9%". Never shows 0% for something present
// or 100% for something that is not complete.
export const formatPercent = (part, total) => {
  const whole = toNumber(total)
  const piece = toNumber(part)
  if (whole <= 0 || piece <= 0) return '0%'
  if (piece >= whole) return '100%'
  const percent = (piece / whole) * 100
  if (percent < 0.1) return '<0.1%'
  if (percent > 99.9) return '>99.9%'
  if (percent < 1 || percent >= 99) return `${percent.toFixed(1).replace(/\.0$/, '')}%`
  return `${Math.round(percent)}%`
}

// Width of a share bar as a CSS percentage. Anything present stays visible as a sliver.
export const barPercent = (part, total, minVisible = 0.75) => {
  const whole = toNumber(total)
  const piece = toNumber(part)
  if (whole <= 0 || piece <= 0) return 0
  return Math.min(100, Math.max(minVisible, (piece / whole) * 100))
}

// Bars of a histogram scaled to the tallest bin (0-100). Non-empty bins stay visible.
export const scaleHistogram = (bins, minVisible = 4) => {
  const list = Array.isArray(bins) ? bins : []
  const max = list.reduce((highest, bin) => Math.max(highest, toNumber(bin?.count)), 0)
  return list.map((bin) => {
    const count = toNumber(bin?.count)
    const height = max > 0 && count > 0 ? Math.max(minVisible, (count / max) * 100) : 0
    return { ...bin, count, height }
  })
}

// `extraDigits` adds precision, used to keep different numbers from looking the same.
export const formatNumber = (value, extraDigits = 0) => {
  if (value === null || value === undefined || value === '') return ''
  const number = Number(value)
  if (!Number.isFinite(number)) return String(value)
  const abs = Math.abs(number)
  // Small numbers keep their significant digits, so 0.00003 is not shown as 0.
  if (abs !== 0 && abs < 1) {
    return number.toLocaleString(undefined, { maximumSignificantDigits: Math.min(4 + extraDigits, 21) })
  }
  const maximumFractionDigits = Math.min((abs >= 1000 ? 0 : 2) + extraDigits, 20)
  return number.toLocaleString(undefined, { maximumFractionDigits })
}

// Formats numbers that belong together (min, median, max) with just enough precision that
// different values never read the same: 1500.1 and 1500.2 stay apart.
export const formatNumbersApart = (values) => {
  const numbers = values.map((value) => (value === null || value === undefined || value === '' ? NaN : Number(value)))
  for (let extra = 0; extra <= 12; extra += 1) {
    const texts = values.map((value) => formatNumber(value, extra))
    const clash = numbers.some((a, i) => numbers.some((b, j) => (
      j > i && Number.isFinite(a) && Number.isFinite(b) && a !== b && texts[i] === texts[j]
    )))
    if (!clash) return texts
  }
  return values.map((value) => (value === null || value === undefined ? '' : String(value)))
}

export const fieldsByName = (fields) => {
  const map = new Map()
  for (const field of Array.isArray(fields) ? fields : []) {
    if (field?.name) map.set(field.name, field)
  }
  return map
}

export const isKeyField = (field) => Boolean(field?.primary || field?.weakPrimary)

export const fieldBadges = (field) => {
  if (!field) return []
  const badges = []
  if (field.primary) {
    badges.push({ key: 'primary', label: 'primary key', title: 'Each row should have its own value here; other tables link to it.' })
  } else if (field.weakPrimary) {
    badges.push({ key: 'identifier', label: 'public identifier', title: 'Each row should have its own value here; it identifies the record publicly.' })
  }
  if (field.type) {
    badges.push({ key: 'type', label: String(field.type), title: `Expected type in the Data Package schema: ${field.type}` })
  }
  return badges
}

const SORTERS = {
  position: (column) => toNumber(column.position),
  name: (column) => String(column.name || '').toLocaleLowerCase(),
  populated: (column) => toNumber(column.populated),
  unique: (column) => toNumber(column.unique),
}

export const DEFAULT_SORT_DIRECTION = { position: 'asc', name: 'asc', populated: 'desc', unique: 'desc' }

// Stable sort; ties keep table order.
export const sortColumns = (columns, key = 'position', direction) => {
  const read = SORTERS[key] || SORTERS.position
  const dir = (direction || DEFAULT_SORT_DIRECTION[key] || 'asc') === 'desc' ? -1 : 1
  return [...(Array.isArray(columns) ? columns : [])]
    .map((column, index) => ({ column, index }))
    .sort((a, b) => {
      const left = read(a.column)
      const right = read(b.column)
      if (left < right) return -1 * dir
      if (left > right) return 1 * dir
      const byPosition = SORTERS.position(a.column) - SORTERS.position(b.column)
      return byPosition || a.index - b.index
    })
    .map(({ column }) => column)
}

export const nextSort = (current, key) => {
  if (current?.key === key) {
    return { key, direction: current.direction === 'asc' ? 'desc' : 'asc' }
  }
  return { key, direction: DEFAULT_SORT_DIRECTION[key] || 'asc' }
}

export const isEmptyColumn = (column) => column?.distinctness === 'empty' || column?.kind === 'empty' || toNumber(column?.populated) === 0

// "find a column" matches the column name and the schema title.
export const filterColumns = (columns, query = '', { hideEmpty = false, fields = null } = {}) => {
  const needle = String(query || '').trim().toLocaleLowerCase()
  const byName = fields instanceof Map ? fields : fieldsByName(fields)
  return (Array.isArray(columns) ? columns : []).filter((column) => {
    if (hideEmpty && isEmptyColumn(column)) return false
    if (!needle) return true
    const title = byName.get(column.name)?.title || ''
    return `${column.name} ${title}`.toLocaleLowerCase().includes(needle)
  })
}

// Leading/trailing spaces matter for spelling variants, so show them.
export const displayValue = (value) => {
  const text = String(value ?? '')
  if (!text) return '(empty text)'
  const leading = text.length - text.trimStart().length
  const trailing = text.length - text.trimEnd().length
  if (!leading && !trailing) return text
  if (!text.trim()) return '␣'.repeat(Math.min(text.length, 5))
  return `${'␣'.repeat(Math.min(leading, 5))}${text.trim()}${'␣'.repeat(Math.min(trailing, 5))}`
}

export const valueTitle = (value, truncated = false) => {
  const text = `"${String(value ?? '')}"`
  return truncated ? `${text}… (cut short: longer than 300 characters)` : text
}

// The values to show inline when a column has only a handful.
export const inlineValues = (column) => {
  const unique = toNumber(column?.unique)
  const values = Array.isArray(column?.top_values) ? column.top_values : []
  if (unique < INLINE_VALUE_MIN || unique > INLINE_VALUE_MAX) return []
  if (values.length < unique) return []
  return values.slice(0, unique)
}

// What the unique count means. `filter` is set when the sentence is clickable.
export const uniqueSummary = (column, field = null) => {
  const populated = toNumber(column?.populated)
  const blank = toNumber(column?.blank)
  const unique = toNumber(column?.unique)
  const duplicatedValues = toNumber(column?.duplicated_values)
  const duplicatedRows = toNumber(column?.duplicated_rows)
  const distinctness = column?.distinctness || (populated === 0 ? 'empty' : 'repeats')
  const name = column?.name || ''

  if (distinctness === 'empty' || populated === 0) {
    return { tone: 'muted', text: 'no values', detail: 'Every row is blank.' }
  }
  if (isKeyField(field) && populated > 1 && distinctness !== 'all_distinct') {
    const values = duplicatedValues || (distinctness === 'constant' ? 1 : Math.max(populated - unique, 1))
    return {
      tone: 'warning',
      text: pluralize(values, 'duplicated value'),
      detail: `${name} should be different in every row, but ${pluralize(values, 'value is', 'values are')} used more than once${duplicatedRows ? ` (${pluralize(duplicatedRows, 'row')})` : ''}.`,
      filter: { column: name, op: 'duplicated' },
    }
  }
  if (distinctness === 'constant') {
    const top = column?.top_values?.[0]
    const value = top?.value
    // With a single filled row "the same value in every row" says nothing; the value itself is shown instead.
    let text = blank > 0 ? 'same value in every filled row' : 'same value in every row'
    if (populated <= 1) text = ''
    return {
      tone: 'neutral',
      text,
      value: value === undefined ? null : value,
      valueTruncated: Boolean(top?.truncated),
    }
  }
  if (distinctness === 'all_distinct') {
    return { tone: 'neutral', text: 'all distinct', detail: 'Every filled row has a different value.' }
  }
  const repeating = duplicatedValues || Math.max(populated - unique, 1)
  return {
    tone: 'neutral',
    text: `${pluralize(repeating, 'value repeats', 'values repeat')}`,
    detail: duplicatedRows
      ? `${pluralize(repeating, 'value appears', 'values appear')} more than once, across ${pluralize(duplicatedRows, 'row')}.`
      : '',
  }
}

export const numericSummary = (numeric) => {
  if (!numeric) return null
  const [min, median, max] = formatNumbersApart([numeric.min, numeric.median, numeric.max])
  return { min, median, max, mean: formatNumber(numeric.mean) }
}

export const numericBinLabel = (bin) => {
  const [start, end] = formatNumbersApart([bin?.start, bin?.end])
  return start === end ? start : `${start} to ${end}`
}

const busiestBin = (bins) => (Array.isArray(bins) ? bins : []).reduce(
  (best, bin) => (toNumber(bin?.count) > toNumber(best?.count) ? bin : best),
  null,
)

// Text alternative for the numeric histogram.
export const numericHistogramText = (numeric) => {
  if (!numeric) return ''
  const summary = numericSummary(numeric)
  // Constant is decided on the numbers themselves, not on how they are rounded for display.
  const constant = Number(numeric.min) === Number(numeric.max)
  const range = constant ? `Every value is ${summary.min}.` : `Values run from ${summary.min} to ${summary.max}, median ${summary.median}.`
  const busiest = busiestBin(numeric.histogram)
  if (!busiest || constant) return range
  return `${range} The most common range is ${numericBinLabel(busiest)} (${pluralize(busiest.count, 'row')}).`
}

export const dateRangeText = (date) => {
  if (!date?.min && !date?.max) return ''
  if (!date.max || date.min === date.max) return String(date.min || date.max)
  return `${date.min} to ${date.max}`
}

export const dateHistogramText = (date) => {
  const range = dateRangeText(date)
  if (!range) return ''
  const busiest = busiestBin(date.histogram)
  const sentence = date.min === date.max || !date.max ? `Every date is ${range}.` : `Dates run from ${range}.`
  if (!busiest) return sentence
  return `${sentence} The busiest period is ${busiest.label} (${pluralize(busiest.count, 'row')}).`
}

// A variant group lists at most 10 of its values; `value_count` and `count` describe the whole group.
export const variantGroupValueCount = (group) => {
  const listed = Array.isArray(group?.values) ? group.values.length : 0
  const total = toNumber(group?.value_count)
  return Math.max(total, listed)
}

export const variantGroupRowCount = (group) => {
  const listed = (Array.isArray(group?.values) ? group.values : []).reduce((sum, item) => sum + toNumber(item?.count), 0)
  const total = toNumber(group?.count)
  return Math.max(total, listed)
}

// How many values of the group are not listed.
export const variantGroupHiddenCount = (group) => (
  variantGroupValueCount(group) - (Array.isArray(group?.values) ? group.values.length : 0)
)

export const variantFlagText = (column) => {
  const count = toNumber(column?.variant_group_count)
  if (count <= 0) return ''
  const groups = Array.isArray(column?.variant_groups) ? column.variant_groups : []
  if (count === 1) {
    const size = variantGroupValueCount(groups[0]) || 2
    return `${formatCount(size)} values look like spelling variants`
  }
  return `${formatCount(count)} groups of values look like spelling variants`
}

const quoteList = (items) => {
  if (items.length <= 1) return items.join('')
  if (items.length === 2) return `${items[0]} and ${items[1]}`
  return `${items.slice(0, -1).join(', ')} and ${items.at(-1)}`
}

const tableName = (tableTitle) => (tableTitle ? `the ${tableTitle} table` : 'this table')

export const askMergeText = (tableTitle, column, group) => {
  const values = (group?.values || []).map((item) => `"${item.value}" (${pluralize(item.count, 'row')})`)
  const hidden = variantGroupHiddenCount(group)
  const intro = `In ${tableName(tableTitle)}, the ${column} column has values that look like spelling variants of each other`
  const ask = 'Please check whether they mean the same thing and, if so, merge them into one consistent value.'
  if (hidden <= 0) return `${intro}: ${quoteList(values)}. ${ask}`
  const total = variantGroupValueCount(group)
  return `${intro}. The group has ${formatCount(total)} values in total across ${pluralize(variantGroupRowCount(group), 'row')}, including ${values.join(', ')} and ${pluralize(hidden, 'more value')}. ${ask.replace('they mean', 'all of them mean')}`
}

export const askValueText = (tableTitle, column, value, count) => (
  `In ${tableName(tableTitle)}, the ${column} column has the value "${value}" in ${pluralize(count, 'row')}. `
)

export const askBlankText = (tableTitle, column, count) => (
  `In ${tableName(tableTitle)}, the ${column} column is blank in ${pluralize(count, 'row')}. `
)

// Link health for a foreign-key-like column.
export const linkSummary = (link) => {
  if (!link) return null
  const target = link.targetTitle || 'the linked table'
  if (link.coverage === 'skipped') {
    return {
      target,
      predicate: link.predicate || '',
      tone: 'muted',
      text: `links to ${target} not checked (the package is too large)`,
      blankText: '',
    }
  }
  const linked = toNumber(link.linkedRows)
  const unmatched = toNumber(link.unmatchedRows)
  const blank = toNumber(link.blankRows)
  const populated = toNumber(link.populatedRows) || linked + unmatched
  let status
  if (unmatched > 0) {
    status = { tone: 'warning', text: `${formatCount(unmatched)} ${unmatched === 1 ? 'does' : 'do'} not match any ${target}` }
  } else if (populated > 0) {
    status = { tone: 'good', text: populated === 1 ? `the 1 value links to ${target}` : `all ${formatCount(populated)} link to ${target}` }
  } else {
    status = { tone: 'muted', text: `nothing to link to ${target} yet` }
  }
  return {
    target,
    predicate: link.predicate || '',
    ...status,
    blankText: blank > 0 ? `${formatCount(blank)} blank` : '',
  }
}

// "from Species in wasps.xlsx", with the server's qualifications ("derived from this column",
// "39 of 40 distinct values found", sampling) kept: without them a derived or partial match reads
// like a straight copy. Value matches are called a likely source.
export const lineageText = (entries) => {
  const list = Array.isArray(entries) ? entries.filter(Boolean) : []
  if (!list.length) return ''
  return list.map((entry) => {
    const column = String(entry.column ?? '').trim()
    const table = String(entry.table ?? '').trim() || 'an uploaded file'
    const source = column ? `from ${column} in ${table}` : `from ${table}`
    const note = String(entry.note ?? '').trim()
    const qualifiers = [entry.basis === 'matched' ? 'likely, matched by values' : '', note].filter(Boolean)
    return qualifiers.length ? `${source} (${qualifiers.join('; ')})` : source
  }).join('; ')
}

export const columnValuesUrl = (baseUrl, tableId, { column, search = '', offset = 0, limit = VALUE_PAGE_SIZE, sort = 'count' } = {}) => {
  const params = new URLSearchParams({
    column: String(column ?? ''),
    offset: String(Math.max(toNumber(offset), 0)),
    limit: String(Math.min(Math.max(toNumber(limit) || VALUE_PAGE_SIZE, 1), 200)),
    sort: sort === 'value' ? 'value' : 'count',
  })
  const needle = String(search || '').trim()
  if (needle) params.set('search', needle)
  return `${baseUrl}/api/tables/${tableId}/column-values/?${params}`
}

export const columnStatsUrl = (baseUrl, tableId) => `${baseUrl}/api/tables/${tableId}/column-stats/`

// Local search/sort when every distinct value is already in hand.
export const filterAndSortValues = (values, { search = '', sort = 'count' } = {}) => {
  const needle = String(search || '').trim().toLocaleLowerCase()
  const list = (Array.isArray(values) ? values : []).filter((item) => (
    !needle || String(item.value ?? '').toLocaleLowerCase().includes(needle)
  ))
  return list.sort((a, b) => {
    if (sort === 'value') return String(a.value).localeCompare(String(b.value))
    return toNumber(b.count) - toNumber(a.count) || String(a.value).localeCompare(String(b.value))
  })
}

// Append a fetched page, dropping values already shown. Values cut short to 300 characters can
// look identical while being different values, so they are always kept.
export const mergeValuePages = (current, incoming) => {
  const seen = new Set((current || []).filter((item) => !item.truncated).map((item) => String(item.value)))
  const merged = [...(current || [])]
  for (const item of incoming || []) {
    if (item?.truncated) {
      merged.push(item)
      continue
    }
    const key = String(item.value)
    if (!seen.has(key)) {
      seen.add(key)
      merged.push(item)
    }
  }
  return merged
}

// Where the next page starts on the server: counted in server results, not in the values shown.
export const nextValueOffset = (offset, results) => Math.max(toNumber(offset), 0) + (Array.isArray(results) ? results.length : 0)

// The value a variant group's "Show these rows" can filter on: a member not cut short, or null.
export const variantGroupFilterValue = (group) => {
  const usable = (group?.values || []).find((item) => item && !item.truncated && item.value !== undefined && item.value !== null)
  return usable ? String(usable.value) : null
}

export const canPageValues = (column) => toNumber(column?.unique) > (column?.top_values?.length || 0)
