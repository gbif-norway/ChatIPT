'use client'

import Modal from 'react-bootstrap/Modal'

// Newest first. One line per change a user would notice.
const CHANGES = [
  {
    month: 'November 2026',
    items: [
      'See each table’s columns, values and links to other tables.',
      'DNA sequence data is published with the DNA Derived Data extension.',
    ],
  },
  {
    month: 'October 2026',
    items: [
      'Convert a Darwin Core Archive to a Darwin Core Data Package.',
      'Processing pauses while GBIF is down and carries on when it is back.',
    ],
  },
  {
    month: 'September 2026',
    items: ['Scientific names are checked against the Catalogue of Life, and you review the matches.'],
  },
  {
    month: 'July 2026',
    items: ['Datasets are built as a Darwin Core Data Package, with a Darwin Core Archive made from it for GBIF.'],
  },
  {
    month: 'April 2026',
    items: ['Upload a manuscript PDF to fill in the dataset description and pull out its data tables.'],
  },
]

export default function WhatsNew({ show, onHide }) {
  return <Modal show={show} onHide={onHide} centered scrollable aria-labelledby="whats-new-title">
    <Modal.Header closeButton>
      <Modal.Title as="h2" className="h5" id="whats-new-title">What&apos;s new</Modal.Title>
    </Modal.Header>
    <Modal.Body>
      {CHANGES.map(({ month, items }) => <section key={month} className="whats-new-month">
        <h3 className="h6">{month}</h3>
        <ul>{items.map(item => <li key={item}>{item}</li>)}</ul>
      </section>)}
      <p className="small text-body-secondary mb-0">Questions or problems: <a href="mailto:rukayasj@uio.no">rukayasj@uio.no</a></p>
    </Modal.Body>
  </Modal>
}
