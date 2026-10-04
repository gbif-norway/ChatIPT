'use client'

import Modal from 'react-bootstrap/Modal'

const CHOICES = [
  {
    key: 'new',
    icon: 'bi-file-earmark-spreadsheet',
    title: 'Start from your own data files',
    text: 'Upload spreadsheets, CSV files or documents in any layout. ChatIPT helps you structure, clean and describe them, and prepares them for publication on GBIF.',
    action: 'Upload your files',
  },
  {
    key: 'converted',
    icon: 'bi-arrow-repeat',
    title: 'Convert a Darwin Core Archive',
    text: 'Already have a Darwin Core Archive, for example from an IPT or downloaded from GBIF? Convert it to a Darwin Core Data Package. Supported mappings are automatic; you review only what is ambiguous.',
    action: 'Convert an archive',
  },
]

// Replaces the two separate "new dataset" buttons. Choosing an option closes the chooser and starts that flow.
export default function NewDatasetChooser({ show, onHide, onStartOwnData, onConvertArchive }) {
  const choose = (key) => {
    onHide()
    if (key === 'new') onStartOwnData()
    else onConvertArchive()
  }
  return <Modal show={show} onHide={onHide} centered size="lg" aria-labelledby="new-dataset-chooser-title">
    <Modal.Header closeButton>
      <Modal.Title as="h2" className="h5" id="new-dataset-chooser-title">Add a new dataset</Modal.Title>
    </Modal.Header>
    <Modal.Body>
      <p className="text-body-secondary">What are you starting from?</p>
      <div className="row g-3">
        {CHOICES.map(choice => <div key={choice.key} className="col-12 col-md-6">
          <button type="button" className={`chooser-card chooser-card-${choice.key} card h-100 w-100 text-start`} onClick={() => choose(choice.key)}>
            <span className="card-body d-flex flex-column gap-2">
              <i className={`bi ${choice.icon} chooser-card-icon`} aria-hidden="true" />
              <span className="h6 mb-0">{choice.title}</span>
              <span className="small text-body-secondary">{choice.text}</span>
              <span className="mt-auto pt-2 fw-semibold small">{choice.action} <i className="bi bi-arrow-right" aria-hidden="true" /></span>
            </span>
          </button>
        </div>)}
      </div>
    </Modal.Body>
  </Modal>
}
