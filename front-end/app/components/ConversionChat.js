'use client'

import { useState } from 'react'
import { decisionTitle, openProposals } from '../utils/conversionReview.mjs'

// Escalated questions in plain language. Assertions are applied only by the Confirm buttons,
// which show the plan's own option labels (docs/dwca-conversion/ai-review-and-chat.md §9.4).
export default function ConversionChat({ state, send, disabled, onFocusDecision }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const chat = state.chat
  const proposals = openProposals(state)
  const canDecide = chat.can_decide
  const submit = async (body) => {
    setBusy(true); setError('')
    try { await send({ action: 'chat', plan_id: state.plan?.id, ...body }); if (body.message) setText('') } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  const messages = chat.messages || []
  const firstCurrent = messages.findIndex(message => message.current_plan)
  return <section className="card card-body mb-3" aria-label="Conversation about your data">
    <h3 className="h6">Conversation</h3>
    <p className="small text-muted mb-2">Answer here in your own words, or choose directly in the list. Both change the same saved choices. The conversation is kept with this conversion; the downloaded report records which choices were answered here, but not the conversation itself.</p>
    <div className="d-flex flex-column gap-2 mb-2" style={{ maxHeight: 480, overflowY: 'auto' }}>
      {messages.map((message, index) => <div key={message.id}>
        {index === firstCurrent && index > 0 && <div className="small text-muted text-center my-1">Files inspected again; earlier messages refer to the previous inspection.</div>}
        <div className={`p-2 rounded ${message.role === 'user' ? 'bg-primary-subtle align-self-end ms-5' : 'bg-body-tertiary me-5'} ${message.current_plan ? '' : 'opacity-50'}`}
          style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
          {message.content}
          {message.current_plan && message.proposals?.length > 0 && <div className="small mt-1">
            {message.proposals.map(proposal => <button key={`${proposal.id}:${proposal.value}`} type="button" className="btn btn-link btn-sm p-0 me-3 text-start"
              onClick={() => onFocusDecision?.(proposal.id)}>Show choice: {decisionTitle(state, proposal.id)}</button>)}
          </div>}
          {message.actions?.length > 0 && <div className="small text-muted mt-1">Recorded {message.actions.length} {message.actions.length === 1 ? 'answer' : 'answers'}.</div>}
        </div>
      </div>)}
      {chat.pending && <div className="small text-muted" role="status"><span className="spinner-border spinner-border-sm me-2" />Preparing a reply…</div>}
    </div>
    {canDecide && proposals.length > 0 && <div className="border rounded p-2 mb-2">
      <p className="small fw-semibold mb-1">Please confirm these answers. They add information that is not in your files.</p>
      {proposals.map(proposal => <div key={`${proposal.id}:${proposal.value}`} className="d-flex flex-wrap align-items-center gap-2 my-1">
        <span className="small me-auto"><button type="button" className="btn btn-link btn-sm p-0 align-baseline text-start" onClick={() => onFocusDecision?.(proposal.id)}>{proposal.title}</button>: <strong>{proposal.label}</strong></span>
        <button className="btn btn-sm btn-primary" disabled={disabled || busy} onClick={() => submit({ confirm: [{ message_id: proposal.message_id, id: proposal.id, value: proposal.value }] })}>Confirm</button>
      </div>)}
      <p className="small text-muted mb-0">If an answer is wrong, say so below or pick another option in the list.</p>
    </div>}
    {error && <div className="alert alert-danger py-1 small" role="alert">{error}</div>}
    {chat.available && <form className="d-flex gap-2" onSubmit={event => { event.preventDefault(); if (text.trim()) submit({ message: text }) }}>
      <textarea className="form-control" rows={2} maxLength={4000} value={text} disabled={disabled || busy} aria-label="Your answer"
        placeholder={canDecide ? 'Answer in your own words…' : 'Ask about the problem…'} onChange={event => setText(event.target.value)} />
      <button className="btn btn-outline-primary align-self-end" disabled={disabled || busy || !text.trim()}>Send</button>
    </form>}
  </section>
}
