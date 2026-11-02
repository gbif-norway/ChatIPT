// Presentation only: saved plans, option values and conversion rules stay unchanged.
// Existing plans get these explanations without another inspection or model call.
const localId = id => String(id || '').replace(/^(?:taxon-occurrence:\d+:)+/, '')
const prefixOf = id => String(id || '').slice(0, -localId(id).length)
const quoted = value => `“${value}”`

export function helpTermParts(text, term) {
  if (!CONVERSION_TERMS[term]) return null
  const start = String(text).includes(': ') ? String(text).indexOf(': ') + 2 : 0
  const stem = term.replace(/s$/, '')
  const match = new RegExp(`\\b${stem}s?\\b`, 'i').exec(String(text).slice(start))
  if (!match) return null
  const index = start + match.index
  return [text.slice(0, index), match[0], text.slice(index + match[0].length)]
}

export const CONVERSION_TERMS = {
  identifiers: 'A code or link for a particular person or organization, such as an ORCID or Wikidata ID. It distinguishes people who share a name. Supplied identifiers take priority over names.',
  events: 'An event records where and when something happened, such as a visit or sampling activity. Several observations can belong to the same event.',
  observation: 'A record of an organism being seen, collected, or looked for but not found.',
  specimen: 'A physical object or sample kept for study, such as a herbarium sheet, a preserved animal, or a tissue sample.',
}

export function choicePresentation(item) {
  const id = localId(item.id)
  if (id === 'agent-names' && Number.isFinite(item.count) && Number.isFinite(item.names)) {
    return { ...item, title: 'Names without identifiers', help: 'identifiers',
      reason: `Up to ${item.count.toLocaleString()} ${item.count === 1 ? 'mention' : 'mentions'} of ${item.names.toLocaleString()} ${item.names === 1 ? 'name' : 'names'} ${item.count === 1 ? 'has' : 'have'} no identifier. We link mentions with exactly the same name. Keep names as text only if they may refer to different people or organizations.`,
      summary: true,
    }
  }
  if (id.startsWith('agent-share:') && item.source_value && Number.isFinite(item.count)) {
    return { ...item, title: `Does ${quoted(item.source_value)} always mean the same person or organization?`,
      reason: `We link its ${item.count.toLocaleString()} mentions to one person or organization. Keep it as text only if those mentions refer to different people or organizations.`,
      summary: true,
    }
  }
  if (id === 'event-category') {
    return { ...item, title: 'What do these events describe?', help: 'events', details: item.reason,
      reason: 'Only events without a category are affected. Choose planned sampling or monitoring, or just where and when observations were made.' +
        (/Humboldt/.test(item.reason || '') ? ' Survey descriptions are included only for survey events; otherwise they stay only in your original files.' : ''),
    }
  }
  if (id.startsWith('occurrence-events:')) return { ...item, title: 'Where should observation details go?', help: 'observation' }
  if (id.startsWith('material:')) return { ...item, help: 'specimen' }
  if (item.default === 'derive' && String(item.term || '').endsWith('/samplingProtocol')) {
    return { ...item, summaryTitle: 'Sampling methods', details: item.reason,
      reason: 'Each method is saved once and linked to the events that use it. Lists separated by “ | ” (with spaces) are split into separate methods.',
    }
  }
  if (item.default === 'derive' && /\/sampleSize(Value|Unit)$/.test(item.term || '')) {
    return { ...item, summaryTitle: item.term.endsWith('Unit') ? 'Sample size units' : 'Sample sizes', details: item.reason,
      reason: 'Sample sizes stay with their units. If either is missing, that pair stays only in your original files.',
    }
  }
  return item
}

export function choiceExamples(state, item) {
  const id = localId(item.id)
  if (id === 'agent-names') {
    const prefix = item.id.slice(0, -id.length)
    const repeated = [...(state?.plan?.automatic_choices || []), ...(state?.plan?.issues || [])]
      .filter(entry => entry.id.startsWith(`${prefix}agent-share:`) && entry.source_value && entry.count > 1)
      .sort((a, b) => b.count - a.count || a.source_value.localeCompare(b.source_value))[0]
    return repeated ? [`${quoted(repeated.source_value)} appears ${repeated.count.toLocaleString()} times. Those mentions can link to one person or organization, or stay as unlinked text.`] : []
  }
  if (id.startsWith('occurrence-events:')) {
    const target = column => state?.decisions?.[column.id] ?? state?.conditional_defaults?.[column.id]?.value ?? column.default
    return (state?.plan?.columns || []).filter(column => prefixOf(column.id) === prefixOf(item.id) && column.table === item.table && column.nonempty > 0 &&
      String(target(column) || '').startsWith('event.') && !String(column.term).endsWith('/eventID') && column.samples?.length)
      .slice(0, 2).map(column => {
        const field = state.plan.glossary?.targets?.[target(column)]?.field_label || String(column.term).split(/[/#]/).pop()
          .replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase()
        return `${field}: ${column.samples.slice(0, 3).map(quoted).join(' · ')}`
      })
  }
  // Column samples come from the upload. Samples from different columns are not
  // paired: their order does not establish that they came from the same row.
  const column = state?.plan?.columns?.find(column => column.id === item.id) || item
  const samples = [...new Set((column.samples || []).filter(value => typeof value === 'string' && value.trim()))].slice(0, 3)
  return samples.length ? [samples.map(quoted).join(' · ')] : []
}
