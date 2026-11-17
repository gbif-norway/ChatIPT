'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import config from '../config.js'
import { apiErrorMessage } from '../utils/tableApi.mjs'
import {
  VALUE_PAGE_SIZE,
  askBlankText,
  askMergeText,
  askValueText,
  barPercent,
  canPageValues,
  columnValuesUrl,
  dateHistogramText,
  dateRangeText,
  displayValue,
  fieldBadges,
  filterAndSortValues,
  formatCount,
  formatPercent,
  lineageText,
  linkSummary,
  mergeValuePages,
  nextValueOffset,
  variantGroupFilterValue,
  numericBinLabel,
  numericHistogramText,
  numericSummary,
  pluralize,
  scaleHistogram,
  uniqueSummary,
  valueTitle,
  variantFlagText,
  variantGroupHiddenCount,
} from '../utils/columnStats.mjs'

const SEARCH_DELAY_MS = 300

/**
 * Small single-series histogram drawn as inline SVG. `bins` are `{label, count}`;
 * `text` is the accessible description. Hovering a bar shows its label and count.
 */
export function ColumnHistogram({ bins, text, size = 'large', startLabel = '', endLabel = '' }) {
  const scaled = scaleHistogram(bins)
  if (!scaled.length) return null
  const width = 100
  const height = size === 'mini' ? 18 : 56
  const step = width / scaled.length
  const gap = Math.min(step * 0.18, size === 'mini' ? 0.8 : 1.2)
  return (
    <figure className={`tcv-histogram tcv-histogram-${size}`}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={text}
        focusable="false"
      >
        <line className="tcv-histogram-base" x1="0" x2={width} y1={height - 0.25} y2={height - 0.25} />
        {scaled.map((bin, index) => {
          const barHeight = (bin.height / 100) * (height - 1)
          return (
            <rect
              key={`${bin.label}-${index}`}
              className="tcv-histogram-bar"
              x={index * step + gap / 2}
              y={height - barHeight}
              width={Math.max(step - gap, 0.3)}
              height={barHeight}
            >
              <title>{`${bin.label}: ${pluralize(bin.count, 'row')}`}</title>
            </rect>
          )
        })}
      </svg>
      {size !== 'mini' && (startLabel || endLabel) && (
        <div className="tcv-histogram-axis" aria-hidden="true">
          <span>{startLabel}</span>
          <span>{endLabel}</span>
        </div>
      )}
    </figure>
  )
}

const numericBins = (numeric) => (numeric?.histogram || []).map((bin) => ({ label: numericBinLabel(bin), count: bin.count }))
const dateBins = (date) => (date?.histogram || []).map((bin) => ({ label: String(bin.label), count: bin.count }))

function HistogramTable({ caption, bins }) {
  if (!bins.length) return null
  return (
    <details className="tcv-histogram-table">
      <summary>Show as a table</summary>
      <table className="table table-sm mb-0">
        <caption className="visually-hidden">{caption}</caption>
        <thead>
          <tr><th scope="col">Range</th><th scope="col" className="text-end">Rows</th></tr>
        </thead>
        <tbody>
          {bins.map((bin, index) => (
            <tr key={`${bin.label}-${index}`}>
              <td>{bin.label}</td>
              <td className="text-end">{formatCount(bin.count)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  )
}

function ValueRow({ item, rowCount, columnName, tableTitle, onFilter, onAsk }) {
  const shown = displayValue(item.value)
  const title = valueTitle(item.value, item.truncated)
  return (
    <li className="tcv-value-row">
      <div className="tcv-value-main">
        <span className="tcv-value-text" title={title}>{shown}</span>
        {item.truncated && (
          <span className="tcv-value-note">cut short, too long to filter exactly</span>
        )}
      </div>
      <div className="tcv-value-bar" aria-hidden="true">
        <span style={{ width: `${barPercent(item.count, rowCount)}%` }}></span>
      </div>
      <div className="tcv-value-count">
        <span>{formatCount(item.count)}</span>
        <small>{formatPercent(item.count, rowCount)}</small>
      </div>
      <div className="tcv-value-actions">
        <button
          type="button"
          className="btn btn-sm tcv-icon-btn"
          onClick={() => onFilter?.({ column: columnName, op: 'is', value: String(item.value) })}
          disabled={item.truncated}
          aria-label={item.truncated
            ? `Cannot filter on this value: it was cut short because it is longer than 300 characters`
            : `Show the ${pluralize(item.count, 'row')} where ${columnName} is "${item.value}"`}
          title={item.truncated ? 'This value was cut short, so rows cannot be filtered on it exactly' : 'Show these rows'}
        >
          <i className="bi bi-funnel" aria-hidden="true"></i>
        </button>
        {onAsk && (
          <button
            type="button"
            className="btn btn-sm tcv-icon-btn"
            onClick={() => onAsk(askValueText(tableTitle, columnName, item.value, item.count))}
            aria-label={`Ask ChatIPT about the value "${item.value}" in ${columnName}`}
            title="Ask ChatIPT about this value"
          >
            <i className="bi bi-chat-left-text" aria-hidden="true"></i>
          </button>
        )}
      </div>
    </li>
  )
}

function BlankRow({ blank, rowCount, columnName, tableTitle, onFilter, onAsk }) {
  return (
    <li className="tcv-value-row tcv-value-row-blank">
      <div className="tcv-value-main">
        <span className="tcv-value-text"><i className="bi bi-dash-circle-dotted me-1" aria-hidden="true"></i>blank</span>
        <span className="tcv-value-note">no value in these rows</span>
      </div>
      <div className="tcv-value-bar tcv-value-bar-blank" aria-hidden="true">
        <span style={{ width: `${barPercent(blank, rowCount)}%` }}></span>
      </div>
      <div className="tcv-value-count">
        <span>{formatCount(blank)}</span>
        <small>{formatPercent(blank, rowCount)}</small>
      </div>
      <div className="tcv-value-actions">
        <button
          type="button"
          className="btn btn-sm tcv-icon-btn"
          onClick={() => onFilter?.({ column: columnName, op: 'blank' })}
          aria-label={`Show the ${pluralize(blank, 'row')} where ${columnName} is blank`}
          title="Show these rows"
        >
          <i className="bi bi-funnel" aria-hidden="true"></i>
        </button>
        {onAsk && (
          <button
            type="button"
            className="btn btn-sm tcv-icon-btn"
            onClick={() => onAsk(askBlankText(tableTitle, columnName, blank))}
            aria-label={`Ask ChatIPT about the blank rows in ${columnName}`}
            title="Ask ChatIPT about these blanks"
          >
            <i className="bi bi-chat-left-text" aria-hidden="true"></i>
          </button>
        )}
      </div>
    </li>
  )
}

function VariantGroups({ column, tableTitle, onFilter, onAsk }) {
  const groups = column.variant_groups || []
  if (!column.variant_group_count) return null
  return (
    <section className="tcv-section tcv-variants" aria-label="Possible spelling variants">
      <h4 className="tcv-section-title">
        <i className="bi bi-shuffle me-1" aria-hidden="true"></i>
        {variantFlagText(column)}
      </h4>
      <p className="tcv-muted small mb-2">
        These values differ only in capitals, spaces or punctuation. They may be the same thing written differently.
      </p>
      <ul className="tcv-variant-list">
        {groups.map((group, index) => {
          const first = group.values?.[0]?.value
          const filterValue = variantGroupFilterValue(group)
          return (
            <li key={`${first}-${index}`} className="tcv-variant-group">
              <ul className="tcv-variant-values">
                {(group.values || []).map((item, position) => (
                  <li key={`${position}-${item.value}`} title={valueTitle(item.value, item.truncated)}>
                    <span className="tcv-variant-value">{displayValue(item.value)}</span>
                    <span className="tcv-muted">{formatCount(item.count)}</span>
                  </li>
                ))}
                {variantGroupHiddenCount(group) > 0 && (
                  <li className="tcv-variant-more">
                    <span className="tcv-muted">+{pluralize(variantGroupHiddenCount(group), 'more value')}</span>
                  </li>
                )}
              </ul>
              <div className="tcv-variant-actions">
                <button
                  type="button"
                  className="btn btn-sm btn-outline-secondary"
                  disabled={filterValue === null}
                  title={filterValue === null ? 'These values were cut short, so rows cannot be filtered on them exactly' : undefined}
                  onClick={() => filterValue !== null && onFilter?.({ column: column.name, op: 'variant_of', value: filterValue })}
                >
                  <i className="bi bi-funnel me-1" aria-hidden="true"></i>Show these rows
                </button>
                {onAsk && (
                  <button
                    type="button"
                    className="btn btn-sm btn-outline-primary"
                    onClick={() => onAsk(askMergeText(tableTitle, column.name, group))}
                  >
                    <i className="bi bi-chat-left-text me-1" aria-hidden="true"></i>Ask ChatIPT to merge these
                  </button>
                )}
              </div>
            </li>
          )
        })}
      </ul>
      {column.variant_group_count > groups.length && (
        <p className="tcv-muted small mb-0">
          Showing the {formatCount(groups.length)} biggest of {formatCount(column.variant_group_count)} groups.
        </p>
      )}
    </section>
  )
}

/**
 * Value breakdown for one column: summary, type-aware chart, spelling variants and every
 * distinct value (paged from the column-values API when there are more than the top 10).
 */
export default function ColumnValueBreakdown({
  tableId,
  tableTitle = '',
  column,
  rowCount = 0,
  field = null,
  link = null,
  lineage = null,
  onFilter,
  onAsk,
  onClose,
  headingId,
  memory = null,
}) {
  const topValues = useMemo(() => column?.top_values || [], [column])
  const remote = canPageValues(column)
  // `memory` (a Map owned by the parent) keeps search, order and loaded values when this breakdown is
  // mounted again elsewhere (the Columns view switching between wide and narrow layout).
  const memoryKey = `${tableId}|${column?.name ?? ''}`
  const [restored] = useState(() => {
    const entry = memory?.get(memoryKey)
    if (!entry) return null
    // Loaded values are reused only for the same summary and when they finished loading.
    const pageUsable = entry.page?.basis === column && !entry.page.loading && !entry.page.error
    return { searchText: entry.searchText, search: entry.search, sort: entry.sort, page: pageUsable ? entry.page : null }
  })
  const [searchText, setSearchText] = useState(restored?.searchText ?? '')
  const [search, setSearch] = useState(restored?.search ?? '')
  const [sort, setSort] = useState(restored?.sort ?? 'count')
  const [page, setPage] = useState(() => restored?.page ?? {
    items: topValues, count: Number(column?.unique) || 0, loading: false, error: '', query: '', order: 'count', basis: column,
  })
  const controllerRef = useRef(null)
  const lastRequestRef = useRef(null)
  const pageRef = useRef(page)

  useEffect(() => {
    pageRef.current = page
    memory?.set(memoryKey, { searchText, search, sort, page })
  }, [memory, memoryKey, searchText, search, sort, page])

  const load = useCallback(async ({ query, order, offset, base }) => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    lastRequestRef.current = { query, order, offset, base }
    setPage((current) => ({ ...current, items: base, loading: true, error: '' }))
    try {
      const response = await fetch(
        columnValuesUrl(config.baseUrl, tableId, {
          column: column.name, search: query, offset, limit: VALUE_PAGE_SIZE, sort: order,
        }),
        { credentials: 'include', signal: controller.signal },
      )
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(apiErrorMessage(payload, response.status))
      const results = Array.isArray(payload.results) ? payload.results : []
      setPage({
        items: mergeValuePages(base, results),
        nextOffset: nextValueOffset(offset, results),
        exhausted: results.length === 0,
        count: Number(payload.count) || 0,
        loading: false,
        error: '',
        query,
        order,
        basis: column,
      })
    } catch (loadError) {
      if (loadError.name === 'AbortError') return
      console.error('Error loading column values:', loadError)
      setPage((current) => ({ ...current, loading: false, error: 'These values could not be loaded.' }))
    }
  }, [tableId, column])

  useEffect(() => () => controllerRef.current?.abort(), [])

  // Debounced search; a new search or sort starts again from the first page.
  useEffect(() => {
    const timeout = window.setTimeout(() => setSearch(searchText.trim()), SEARCH_DELAY_MS)
    return () => window.clearTimeout(timeout)
  }, [searchText])

  useEffect(() => {
    if (!remote) return undefined
    const timeout = window.setTimeout(() => {
      // Already showing these values (restored after the layout switched): keep them and the pages loaded so far.
      const current = pageRef.current
      if (current.query === search && current.order === sort && current.basis === column && !current.loading && !current.error) return
      if (!search && sort === 'count') {
        controllerRef.current?.abort()
        setPage({ items: topValues, count: Number(column?.unique) || 0, loading: false, error: '', query: '', order: 'count', basis: column })
        return
      }
      load({ query: search, order: sort, offset: 0, base: [] })
    }, 0)
    return () => window.clearTimeout(timeout)
  }, [remote, search, sort, load, topValues, column])

  const showMore = () => {
    load({ query: search, order: sort, offset: page.nextOffset ?? page.items.length, base: page.items })
  }
  const retry = () => {
    if (lastRequestRef.current) load(lastRequestRef.current)
  }

  if (!column) return null

  const values = remote ? page.items : filterAndSortValues(topValues, { search, sort })
  const matchCount = remote ? page.count : values.length
  const totalDistinct = Number(column.unique) || 0
  const hasMore = remote && !page.exhausted && (page.nextOffset ?? values.length) < matchCount
  const blank = Number(column.blank) || 0
  const populated = Number(column.populated) || 0
  const unique = uniqueSummary(column, field)
  const health = linkSummary(link)
  const source = lineageText(lineage)
  const badges = fieldBadges(field)
  const isEmpty = populated === 0
  const numeric = column.kind === 'number' ? column.numeric : null
  const date = column.kind === 'date' ? column.date : null
  const numbers = numericSummary(numeric)
  const showSearch = totalDistinct > 8

  return (
    <section className="tcv-breakdown" aria-labelledby={headingId}>
      <header className="tcv-breakdown-header">
        <div className="tcv-breakdown-heading">
          <h3 id={headingId} className="tcv-breakdown-title">{column.name}</h3>
          {field?.title && field.title !== column.name && <div className="tcv-muted small">{field.title}</div>}
          {badges.length > 0 && (
            <div className="tcv-badges">
              {badges.map((badge) => (
                <span key={badge.key} className={`tcv-badge tcv-badge-${badge.key}`} title={badge.title}>{badge.label}</span>
              ))}
            </div>
          )}
        </div>
        {onClose && (
          <button type="button" className="btn btn-sm tcv-icon-btn" onClick={onClose} aria-label={`Close the breakdown of ${column.name}`}>
            <i className="bi bi-x-lg" aria-hidden="true"></i>
          </button>
        )}
      </header>

      {field?.description && <p className="tcv-breakdown-description">{field.description}</p>}
      {source && (
        <p className="tcv-lineage">
          <i className="bi bi-arrow-return-right me-1" aria-hidden="true"></i>{source}
        </p>
      )}

      <dl className="tcv-facts">
        <div>
          <dt>Filled</dt>
          <dd>{formatCount(populated)} <small>({formatPercent(populated, rowCount)})</small></dd>
        </div>
        <div>
          <dt>Blank</dt>
          <dd>
            {blank > 0 ? (
              <button
                type="button"
                className="btn btn-link tcv-inline-link"
                onClick={() => onFilter?.({ column: column.name, op: 'blank' })}
                aria-label={`Show the ${pluralize(blank, 'row')} where ${column.name} is blank`}
              >
                {formatCount(blank)}
              </button>
            ) : '0'}
            {' '}<small>({formatPercent(blank, rowCount)})</small>
          </dd>
        </div>
        <div>
          <dt>Different values</dt>
          <dd>
            {formatCount(totalDistinct)}{' '}
            {unique.filter ? (
              <button
                type="button"
                className="btn btn-link tcv-inline-link tcv-warning-text"
                onClick={() => onFilter?.(unique.filter)}
                title={unique.detail}
              >
                <i className="bi bi-exclamation-triangle me-1" aria-hidden="true"></i>{unique.text}
              </button>
            ) : (
              unique.text && <small className="tcv-muted">({unique.text})</small>
            )}
          </dd>
        </div>
      </dl>
      {unique.filter && <p className="tcv-warning-note small">{unique.detail}</p>}

      {health && (
        <p className={`tcv-link-health tcv-link-${health.tone}`}>
          <i className={`bi ${health.tone === 'warning' ? 'bi-exclamation-triangle' : 'bi-link-45deg'} me-1`} aria-hidden="true"></i>
          <span>Links to {health.target}{health.predicate ? ` (${health.predicate})` : ''}: {health.text}</span>
          {health.blankText && <span className="tcv-muted">, {health.blankText}</span>}
        </p>
      )}

      {numbers && (
        <section className="tcv-section" aria-label="Number summary">
          <dl className="tcv-facts tcv-facts-compact">
            <div><dt>Lowest</dt><dd>{numbers.min}</dd></div>
            <div><dt>Median</dt><dd>{numbers.median}</dd></div>
            <div><dt>Highest</dt><dd>{numbers.max}</dd></div>
            {numbers.mean && <div><dt>Mean</dt><dd>{numbers.mean}</dd></div>}
          </dl>
          <ColumnHistogram
            bins={numericBins(numeric)}
            text={numericHistogramText(numeric)}
            startLabel={numbers.min}
            endLabel={numbers.max}
          />
          <p className="visually-hidden">{numericHistogramText(numeric)}</p>
          <HistogramTable caption={`How values of ${column.name} are spread`} bins={numericBins(numeric)} />
        </section>
      )}

      {date && (
        <section className="tcv-section" aria-label="Date summary">
          <p className="mb-1"><i className="bi bi-calendar3 me-1 tcv-muted" aria-hidden="true"></i>{dateRangeText(date)}</p>
          <ColumnHistogram
            bins={dateBins(date)}
            text={dateHistogramText(date)}
            startLabel={date.histogram?.[0]?.label}
            endLabel={date.histogram?.at(-1)?.label}
          />
          <p className="visually-hidden">{dateHistogramText(date)}</p>
          <HistogramTable caption={`How dates in ${column.name} are spread`} bins={dateBins(date)} />
        </section>
      )}

      <VariantGroups column={column} tableTitle={tableTitle} onFilter={onFilter} onAsk={onAsk} />

      <section className="tcv-section" aria-label={`Values in ${column.name}`}>
        <div className="tcv-values-toolbar">
          <h4 className="tcv-section-title mb-0">Values</h4>
          {!isEmpty && totalDistinct > 1 && (
            <div className="tcv-values-controls">
              {showSearch && (
                <input
                  type="search"
                  className="form-control form-control-sm"
                  placeholder="Search values…"
                  aria-label={`Search values in ${column.name}`}
                  value={searchText}
                  onChange={(event) => setSearchText(event.target.value)}
                />
              )}
              <select
                className="form-select form-select-sm"
                aria-label="Order values"
                value={sort}
                onChange={(event) => setSort(event.target.value)}
              >
                <option value="count">Most common first</option>
                <option value="value">A to Z</option>
              </select>
            </div>
          )}
        </div>

        {isEmpty ? (
          <p className="tcv-muted small mb-0">Every row is blank in this column.</p>
        ) : (
          <>
            <ul className="tcv-value-list" aria-busy={page.loading}>
              {!search && blank > 0 && (
                <BlankRow
                  blank={blank}
                  rowCount={rowCount}
                  columnName={column.name}
                  tableTitle={tableTitle}
                  onFilter={onFilter}
                  onAsk={onAsk}
                />
              )}
              {values.map((item, index) => (
                <ValueRow
                  key={item.truncated ? `cut-${index}` : String(item.value)}
                  item={item}
                  rowCount={rowCount}
                  columnName={column.name}
                  tableTitle={tableTitle}
                  onFilter={onFilter}
                  onAsk={onAsk}
                />
              ))}
            </ul>
            {!page.loading && !page.error && values.length === 0 && (
              <p className="tcv-muted small my-2">No values match “{search}”.</p>
            )}
            {page.error && (
              <div className="alert alert-danger py-2 px-3 my-2 small d-flex align-items-center gap-2" role="alert">
                <span>{page.error}</span>
                <button type="button" className="btn btn-sm btn-outline-danger ms-auto" onClick={retry}>Retry</button>
              </div>
            )}
            <div className="tcv-values-footer">
              <small className="tcv-muted" aria-live="polite">
                {page.error ? '' : search
                  ? `${pluralize(matchCount, 'value')} match “${search}”${values.length < matchCount ? `, showing ${formatCount(values.length)}` : ''}`
                  : `Showing ${formatCount(values.length)} of ${pluralize(totalDistinct, 'different value')}`}
              </small>
              {page.loading && (
                <span className="spinner-border spinner-border-sm tcv-muted" role="status">
                  <span className="visually-hidden">Loading values…</span>
                </span>
              )}
              {hasMore && !page.loading && !page.error && (
                <button type="button" className="btn btn-sm btn-outline-secondary" onClick={showMore}>
                  Show {formatCount(Math.min(VALUE_PAGE_SIZE, matchCount - values.length))} more
                </button>
              )}
            </div>
          </>
        )}
      </section>
    </section>
  )
}
