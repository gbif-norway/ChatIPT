'use client'

import { openQuestions, optionState, shownRecommendation } from '../utils/conversionReview.mjs'

// Open questions and the AI reviewer's recommendations for them. Each names a choice; clicking it shows that
// choice card. "Use this recommendation" saves through the same handler as the choice card's own button.
export default function ConversionOpenQuestions({ state, disabled, onChoose, onFocusDecision }) {
  const questions = openQuestions(state)
  if (!questions.length) return null
  return <section className="card card-body mb-3" aria-label="Open questions">
    <h3 className="h6">Needs your answer ({questions.length})</h3>
    <ul className="list-unstyled mb-0">
      {questions.map(question => {
        const recommendation = shownRecommendation(state, question.id)
        return <li key={question.id} className={`conversion-question ${recommendation ? 'conversion-question-recommended' : ''}`}>
          <button type="button" className="btn btn-link p-0 text-start fw-semibold" onClick={() => onFocusDecision(question.id)}>
            {question.title}<i className="bi bi-arrow-right-short" aria-hidden="true" /><span className="visually-hidden"> (show this choice)</span>
          </button>
          {recommendation && <div className="small mt-1">
            Recommended: <strong>{recommendation.option_label}</strong>{recommendation.rationale ? `. ${recommendation.rationale}` : ''}
            <div className="mt-1">
              <button type="button" className="btn btn-sm btn-outline-primary me-2" disabled={disabled || !optionState(state, question.id, recommendation.option).available}
                onClick={() => onChoose(question.id, recommendation.option, { accepted_recommendations: [question.id] })}>Use this recommendation</button>
              <button type="button" className="btn btn-sm btn-link" onClick={() => onFocusDecision(question.id)}>Show choice</button>
            </div>
          </div>}
        </li>
      })}
    </ul>
  </section>
}
