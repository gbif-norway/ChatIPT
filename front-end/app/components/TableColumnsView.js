'use client'

import { memo, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import ColumnValueBreakdown, { ColumnHistogram } from './ColumnValueBreakdown'
import '../table-columns.css'
import {
  WIDE_LAYOUT_MIN_WIDTH,
  barPercent,
  dateHistogramText,
  dateRangeText,
  displayValue,
  fieldBadges,
  fieldsByName,
  filterColumns,
  formatCount,
  formatPercent,
  inlineValues,
  isEmptyColumn,
  lineageText,
  linkSummary,
  nextSort,
  numericBinLabel,
  numericHistogramText,
  numericSummary,
  pluralize,
  sortColumns,
  uniqueSummary,
  valueTitle,
  variantFlagText,
} from '../utils/columnStats.mjs'

const SORT_LABELS = {
  name: { asc: 'A to Z', desc: 'Z to A' },
  populated: { asc: 'least filled first', desc: 'most filled first' },
  unique: { asc: 'fewest values first', desc: 'most values first' },
}

// Layout effects do not run on the server; the client measures before the first paint.
const useIsomorphicLayoutEffect = typeof window === 'undefined' ? useEffect : useLayoutEffect

// Width of the element, measured before the first paint so a wide container does not render narrow first.
// A hidden container (Rows view showing) measures 0; the last real width is kept then.
function useContainerWidth(ref) {
  const [width, setWidth] = useState(0)
  useIsomorphicLayoutEffect(() => {
    const element = ref.current
    if (!element) return undefined
    const update = (next) => {
      const rounded = Math.round(next || 0)
      if (rounded > 0) setWidth((current) => (current === rounded ? current : rounded))
    }
    update(element.getBoundingClientRect().width)
    if (typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver((entries) => update(entries[0]?.contentRect?.width))
    observer.observe(element)
    return () => observer.disconnect()
  }, [ref])
  return width
}

// Clicks that should not open or close a column: on a control, or the end of a text selection drag.
const ignoreRowClick = (event) => {
  if (event.target.closest('button, a, input, select, textarea, label, summary, details')) return true
  const selection = typeof window !== 'undefined' ? window.getSelection?.() : null
  return Boolean(selection && !selection.isCollapsed && selection.toString().trim())
}

function SortButton({ label, sortKey, sort, onSort, className = '' }) {
  const active = sort.key === sortKey
  const icon = !active ? 'bi-arrow-down-up' : sort.direction === 'asc' ? 'bi-sort-up' : 'bi-sort-down'
  const next = nextSort(sort, sortKey)
  return (
    <button
      type="button"
      className={`tcv-sort ${active ? 'is-active' : ''} ${className}`}
      onClick={() => onSort(sortKey)}
      aria-label={`Sort columns by ${label.toLowerCase()}, ${SORT_LABELS[sortKey][next.direction]}`}
      aria-pressed={active}
    >
      {label}
      <i className={`bi ${icon} ms-1`} aria-hidden="true"></i>
      {active && <span className="visually-hidden">, sorted {SORT_LABELS[sortKey][sort.direction]}</span>}
    </button>
  )
}

function FieldDescription({ field, columnName }) {
  const [open, setOpen] = useState(false)
  const id = useId()
  if (!field?.description) return null
  return (
    <div className={`tcv-description ${open ? 'is-open' : ''}`}>
      <button
        type="button"
        className="tcv-description-toggle"
        aria-expanded={open}
        aria-controls={id}
        aria-label={open ? `Hide the description of ${columnName}` : `Show the full description of ${columnName}`}
        onClick={(event) => {
          event.stopPropagation()
          setOpen((value) => !value)
        }}
      >
        <i className={`bi ${open ? 'bi-chevron-up' : 'bi-info-circle'}`} aria-hidden="true"></i>
      </button>
      <span id={id} className="tcv-description-text">{field.description}</span>
    </div>
  )
}

const ColumnRow = memo(function ColumnRow({
  column,
  field,
  link,
  lineage,
  rowCount,
  selected,
  onSelect,
  onFilter,
  children,
}) {
  const empty = isEmptyColumn(column)
  const populated = Number(column.populated) || 0
  const blank = Number(column.blank) || 0
  const badges = fieldBadges(field)
  const unique = uniqueSummary(column, field)
  const chips = inlineValues(column)
  const variants = variantFlagText(column)
  const health = linkSummary(link)
  const source = lineageText(lineage)
  const numbers = column.kind === 'number' ? numericSummary(column.numeric) : null
  const date = column.kind === 'date' && column.date ? column.date : null

  const filter = (event, value) => {
    event.stopPropagation()
    onFilter?.(value)
  }

  // Only the summary part toggles the column; the inline breakdown (children) sits outside it, so clicking,
  // selecting text or opening "Show as a table" inside the breakdown never collapses it.
  return (
    <li className={`tcv-row ${selected ? 'is-selected' : ''} ${empty ? 'is-empty' : ''}`}>
      <div
        className="tcv-row-summary"
        onClick={(event) => {
          if (ignoreRowClick(event)) return
          onSelect(column.name)
        }}
      >
        <div className="tcv-row-grid">
          <div className="tcv-cell-name">
            <button
              type="button"
              className="tcv-name"
              onClick={() => onSelect(column.name)}
              aria-expanded={selected}
              title={selected ? `Hide the values of ${column.name}` : `Show the values of ${column.name}`}
            >
              <span className="tcv-name-text">{column.name}</span>
              {empty && <span className="tcv-empty-tag">empty</span>}
            </button>
            {(badges.length > 0 || (field?.title && field.title !== column.name)) && (
              <div className="tcv-badges">
                {field?.title && field.title !== column.name && <span className="tcv-field-title">{field.title}</span>}
                {badges.map((badge) => (
                  <span key={badge.key} className={`tcv-badge tcv-badge-${badge.key}`} title={badge.title}>
                    {badge.key === 'primary' && <i className="bi bi-key me-1" aria-hidden="true"></i>}
                    {badge.label}
                  </span>
                ))}
              </div>
            )}
            <FieldDescription field={field} columnName={column.name} />
          </div>

          <div className="tcv-cell-filled">
            <div className="tcv-bar" aria-hidden="true">
              <span style={{ width: `${barPercent(populated, rowCount)}%` }}></span>
            </div>
            <div className="tcv-filled-text">
              <span>
                <span className="visually-hidden">Filled in </span>
                {formatPercent(populated, rowCount)}
              </span>
              {blank > 0 && !empty && (
                <button
                  type="button"
                  className="tcv-blank-link"
                  onClick={(event) => filter(event, { column: column.name, op: 'blank' })}
                  aria-label={`Show the ${pluralize(blank, 'row')} where ${column.name} is blank`}
                >
                  {formatCount(blank)} blank
                </button>
              )}
            </div>
          </div>

          <div className="tcv-cell-unique">
            <span className="tcv-unique-count">
              {formatCount(column.unique)}
              <span className="tcv-unique-suffix"> different {Number(column.unique) === 1 ? 'value' : 'values'}</span>
            </span>
            {unique.filter ? (
              <button
                type="button"
                className="tcv-warning-link"
                onClick={(event) => filter(event, unique.filter)}
                title={unique.detail}
                aria-label={`${unique.text} in ${column.name}. Show these rows`}
              >
                <i className="bi bi-exclamation-triangle me-1" aria-hidden="true"></i>{unique.text}
              </button>
            ) : (
              unique.text && <span className={`tcv-unique-text tcv-tone-${unique.tone}`}>{unique.text}</span>
            )}
          </div>
        </div>

        {!empty && (chips.length > 0 || (unique.value !== undefined && unique.value !== null) || numbers || date || variants || health || source) && (
          <div className="tcv-row-extra">
            {chips.length > 0 && (
              <ul className="tcv-chips" aria-label={`Values in ${column.name}`}>
                {chips.map((item, chipIndex) => (
                  <li key={item.truncated ? `cut-${chipIndex}` : String(item.value)}>
                    <button
                      type="button"
                      className="tcv-chip"
                      disabled={item.truncated}
                      title={valueTitle(item.value, item.truncated)}
                      onClick={(event) => filter(event, { column: column.name, op: 'is', value: String(item.value) })}
                      aria-label={`Show the ${pluralize(item.count, 'row')} where ${column.name} is "${item.value}"`}
                    >
                      <span className="tcv-chip-value">{displayValue(item.value)}</span>
                      <span className="tcv-chip-count">{formatCount(item.count)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {chips.length === 0 && unique.value !== undefined && unique.value !== null && (
              <ul className="tcv-chips" aria-label={`The only value in ${column.name}`}>
                <li>
                  <button
                    type="button"
                    className="tcv-chip"
                    disabled={unique.valueTruncated}
                    title={valueTitle(unique.value, unique.valueTruncated)}
                    onClick={(event) => filter(event, { column: column.name, op: 'is', value: String(unique.value) })}
                    aria-label={`Show the rows where ${column.name} is "${unique.value}"`}
                  >
                    <span className="tcv-chip-value">{displayValue(unique.value)}</span>
                    <span className="tcv-chip-count">{formatCount(populated)}</span>
                  </button>
                </li>
              </ul>
            )}
            {numbers && (
              <div className="tcv-type-summary">
                <ColumnHistogram
                  size="mini"
                  bins={(column.numeric.histogram || []).map((bin) => ({ label: numericBinLabel(bin), count: bin.count }))}
                  text={numericHistogramText(column.numeric)}
                />
                <span>
                  {numbers.min === numbers.max ? numbers.min : (
                    <>{numbers.min} <span className="tcv-muted">to</span> {numbers.max}<span className="tcv-muted">, median</span> {numbers.median}</>
                  )}
                </span>
              </div>
            )}
            {date && (
              <div className="tcv-type-summary">
                <ColumnHistogram
                  size="mini"
                  bins={(date.histogram || []).map((bin) => ({ label: String(bin.label), count: bin.count }))}
                  text={dateHistogramText(date)}
                />
                <span>{dateRangeText(date)}</span>
              </div>
            )}
            {variants && (
              <span className="tcv-flag">
                <i className="bi bi-shuffle me-1" aria-hidden="true"></i>{variants}
              </span>
            )}
            {health && (
              <span className={`tcv-link-health tcv-link-${health.tone}`}>
                <span aria-hidden="true">→</span>
                <span className="visually-hidden">Links to</span> {health.target}
                <span className="tcv-muted"> · </span>
                {health.tone === 'warning' && <i className="bi bi-exclamation-triangle me-1" aria-hidden="true"></i>}
                {health.text}
                {health.blankText && <span className="tcv-muted">, {health.blankText}</span>}
              </span>
            )}
            {source && (
              <span className="tcv-lineage">
                <i className="bi bi-arrow-return-right me-1" aria-hidden="true"></i>{source}
              </span>
            )}
          </div>
        )}
      </div>
      {children}
    </li>
  )
})

function SkeletonRows({ names }) {
  return (
    <ul className="tcv-list" aria-hidden="true">
      {names.map((name, index) => (
        <li key={`${name}-${index}`} className="tcv-row tcv-row-skeleton placeholder-glow">
          <div className="tcv-row-grid">
            <div className="tcv-cell-name">
              {name ? <span className="tcv-name-text tcv-muted">{name}</span> : <span className="placeholder col-7"></span>}
            </div>
            <div className="tcv-cell-filled"><span className="placeholder col-10"></span></div>
            <div className="tcv-cell-unique"><span className="placeholder col-6"></span></div>
          </div>
        </li>
      ))}
    </ul>
  )
}

/**
 * Columns view of the table explorer: one row per column with how filled it is and how many
 * different values it holds, and a value breakdown for the selected column (beside the list
 * when the container is wide, under the selected row when narrow).
 */
export default function TableColumnsView({
  tableId,
  tableTitle = '',
  stats = null,
  statsLoading = false,
  statsError = '',
  onRetryStats,
  fields = null,
  links = null,
  lineage = null,
  selectedColumn = null,
  onSelectColumn,
  onFilter,
  onAsk,
}) {
  const rootRef = useRef(null)
  const width = useContainerWidth(rootRef)
  const wide = width >= WIDE_LAYOUT_MIN_WIDTH
  const headingId = useId()
  const [sort, setSort] = useState({ key: 'position', direction: 'asc' })
  const [query, setQuery] = useState('')
  const [hideEmpty, setHideEmpty] = useState(false)

  // Search, order and loaded pages of each column's breakdown, kept when the layout switches between
  // side-by-side and inline (which mounts the breakdown somewhere else).
  const [breakdownMemory] = useState(() => new Map())

  const fieldMap = useMemo(() => fieldsByName(fields), [fields])
  const allColumns = useMemo(() => (Array.isArray(stats?.columns) ? stats.columns : []), [stats])
  const emptyCount = useMemo(() => allColumns.filter(isEmptyColumn).length, [allColumns])
  const visibleColumns = useMemo(
    () => sortColumns(filterColumns(allColumns, query, { hideEmpty, fields: fieldMap }), sort.key, sort.direction),
    [allColumns, query, hideEmpty, fieldMap, sort],
  )
  const rowCount = Number(stats?.row_count) || 0
  const selected = allColumns.find((column) => column.name === selectedColumn) || null

  const select = useCallback(
    (name) => onSelectColumn?.(name === selectedColumn ? null : name),
    [onSelectColumn, selectedColumn],
  )
  const onSort = (key) => setSort((current) => nextSort(current, key))

  const breakdown = selected ? (
    <ColumnValueBreakdown
      key={`${tableId}-${selected.name}`}
      tableId={tableId}
      tableTitle={tableTitle}
      column={selected}
      rowCount={rowCount}
      field={fieldMap.get(selected.name) || null}
      link={links?.[selected.name] || null}
      lineage={lineage?.[selected.name] || null}
      onFilter={onFilter}
      onAsk={onAsk}
      onClose={() => onSelectColumn?.(null)}
      headingId={headingId}
      memory={breakdownMemory}
    />
  ) : null

  let body
  if (statsError && !stats) {
    body = (
      <div className="alert alert-danger d-flex align-items-center gap-2 m-0" role="alert">
        <span>{statsError || 'The column summary could not be loaded.'}</span>
        {onRetryStats && (
          <button type="button" className="btn btn-sm btn-outline-danger ms-auto" onClick={onRetryStats}>Retry</button>
        )}
      </div>
    )
  } else if (statsLoading && !stats) {
    const names = Array.isArray(fields) && fields.length ? fields.slice(0, 12).map((field) => field.name) : Array(6).fill('')
    body = (
      <div role="status" aria-live="polite">
        <span className="visually-hidden">Summarising columns…</span>
        <SkeletonRows names={names} />
      </div>
    )
  } else if (!stats) {
    body = null
  } else if (!allColumns.length) {
    body = <p className="tcv-empty-state">This table has no columns.</p>
  } else {
    body = (
      <>
        {statsError && (
          <div className="alert alert-warning d-flex align-items-center gap-2 py-2 small" role="alert">
            <span>{statsError} Showing the last summary.</span>
            {onRetryStats && (
              <button type="button" className="btn btn-sm btn-outline-secondary ms-auto" onClick={onRetryStats}>Retry</button>
            )}
          </div>
        )}
        {rowCount === 0 && (
          <p className="tcv-empty-state">This table has no rows yet, so every column is empty.</p>
        )}
        <div className="tcv-toolbar">
          <div className="tcv-find">
            <i className="bi bi-search" aria-hidden="true"></i>
            <input
              type="search"
              className="form-control form-control-sm"
              placeholder="Find a column…"
              aria-label="Find a column"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>
          {emptyCount > 0 && (
            <div className="form-check form-switch tcv-hide-empty">
              <input
                className="form-check-input"
                type="checkbox"
                role="switch"
                id={`${headingId}-hide-empty`}
                checked={hideEmpty}
                onChange={(event) => setHideEmpty(event.target.checked)}
              />
              <label className="form-check-label" htmlFor={`${headingId}-hide-empty`}>
                Hide {pluralize(emptyCount, 'empty column')}
              </label>
            </div>
          )}
          <small className="tcv-muted tcv-count" aria-live="polite">
            {visibleColumns.length === allColumns.length
              ? `${pluralize(allColumns.length, 'column')} · ${pluralize(rowCount, 'row')}`
              : `${formatCount(visibleColumns.length)} of ${pluralize(allColumns.length, 'column')}`}
          </small>
        </div>

        <div className="tcv-list-header">
          <div className="tcv-cell-name">
            <SortButton label="Column" sortKey="name" sort={sort} onSort={onSort} />
            {sort.key !== 'position' && (
              <button
                type="button"
                className="tcv-sort tcv-sort-reset"
                onClick={() => setSort({ key: 'position', direction: 'asc' })}
              >
                table order
              </button>
            )}
          </div>
          <div className="tcv-cell-filled"><SortButton label="Filled" sortKey="populated" sort={sort} onSort={onSort} /></div>
          <div className="tcv-cell-unique"><SortButton label="Values" sortKey="unique" sort={sort} onSort={onSort} /></div>
        </div>

        {visibleColumns.length === 0 ? (
          <p className="tcv-empty-state">No columns match “{query}”.</p>
        ) : (
          <ul className="tcv-list" aria-label={`Columns of ${tableTitle || 'this table'}`}>
            {visibleColumns.map((column) => (
              <ColumnRow
                key={column.name}
                column={column}
                field={fieldMap.get(column.name)}
                link={links?.[column.name]}
                lineage={lineage?.[column.name]}
                rowCount={rowCount}
                selected={column.name === selectedColumn}
                onSelect={select}
                onFilter={onFilter}
              >
                {!wide && column.name === selectedColumn && breakdown && (
                  <div className="tcv-inline-breakdown">{breakdown}</div>
                )}
              </ColumnRow>
            ))}
          </ul>
        )}
      </>
    )
  }

  const showSide = wide && stats && allColumns.length > 0
  return (
    <div ref={rootRef} className={`tcv ${wide ? 'is-wide' : 'is-narrow'}`}>
      <div className={showSide ? 'tcv-layout' : ''}>
        <div className="tcv-main">{body}</div>
        {showSide && (
          <aside className="tcv-side">
            {breakdown || (
              <div className="tcv-side-placeholder">
                <i className="bi bi-bar-chart-steps" aria-hidden="true"></i>
                <p className="mb-0">Choose a column to see its values, how often each appears, and which rows are blank.</p>
              </div>
            )}
          </aside>
        )}
      </div>
    </div>
  )
}
