'use client'

import styles from './app.css'
import Dataset from './components/Dataset'
import Sidebar from './components/Sidebar'
import SidebarToggle from './components/SidebarToggle'
import ProtectedRoute from './components/ProtectedRoute'
import { useAuth } from './contexts/AuthContext'
import { useEffect, useState } from 'react'

const Home = () => {
  const { authenticated } = useAuth()
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [currentDatasetId, setCurrentDatasetId] = useState(null)

  useEffect(() => {
    // Only show welcome modal if user is authenticated
    if (authenticated) {
      // Dynamically import Bootstrap JavaScript to ensure it's available
      import('bootstrap/dist/js/bootstrap.bundle.min.js').then((bootstrap) => {
        const myModal = new bootstrap.Modal(document.getElementById('myModal'));

        // Show the modal on page load
        myModal.show();

        // Clean up modal and backdrop when it is hidden
        const modalElement = document.getElementById('myModal');
        modalElement.addEventListener('hidden.bs.modal', () => {
          myModal.dispose();
          const backdrops = document.querySelectorAll('.modal-backdrop');
          backdrops.forEach((backdrop) => backdrop.remove());
        });
      });
    }
  }, [authenticated]);

  const handleDatasetSelect = (datasetId) => {
    setCurrentDatasetId(datasetId)
    setSidebarOpen(false) // Close sidebar on mobile after selection
  }

  const handleNewDataset = () => {
    setCurrentDatasetId(null)
    setSidebarOpen(false)
  }

  return (
    <ProtectedRoute>
      <main style={{ marginLeft: sidebarOpen ? '320px' : '0', transition: 'margin-left 0.3s ease-in-out' }}>
        <SidebarToggle 
          isOpen={sidebarOpen} 
          onToggle={() => setSidebarOpen(!sidebarOpen)} 
        />
        
        <Sidebar 
          isOpen={sidebarOpen}
          onToggle={() => setSidebarOpen(!sidebarOpen)}
          onDatasetSelect={handleDatasetSelect}
          currentDatasetId={currentDatasetId}
        />

        <Dataset 
          initialDatasetId={currentDatasetId}
          onNewDataset={handleNewDataset}
        />

        <div className="modal modal-lg fade" id="myModal" tabIndex="-1" aria-labelledby="exampleModalLabel" aria-hidden="true">
          <div className="modal-dialog">
            <div className="modal-content">
              <div className="modal-header">
                <h5 className="modal-title" id="exampleModalLabel">Welcome to ChatIPT</h5>
                <button type="button" className="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
              </div>
              <div className="modal-body">
                <div className="alert alert-warning" role="alert">
                <p>ChatIPT is a chatbot for students and researchers who are new to data publication or only occasionally publish data.</p>
                <p className="no-bottom-margin">It cleans and standardises spreadsheets, creates basic metadata, asking the user for guidance where necessary through natural conversation. Finally, it publishes the data on gbif.org as a Darwin Core Archive. </p>
                </div>
                <hr />
                <p><strong>Why is it necessary?</strong></p>
                <p>At a conservative estimate, there are 300 - 400 PhDs and MScs in Europe alone at the moment generating biodiversity data, as well as countless small biodiversity research studies.</p>
                <p>Publishing piecemeal but high quality data such as this is difficult to do at scale:</p>
                  <ul>
                    <li><i className="bi bi-bar-chart-steps"></i> Data standardisation is hard and requires specialist knowledge of:
                      <ul>
                        <li>data standards and the the domain of standardisation (e.g. ontologies, etc)</li>
                        <li>programming languages (e.g. R, Python, SQL)</li>
                        <li>data management techniques (e.g. normalisation, wide vs long format, etc)</li>
                        <li>familiarity with specialised software (e.g. OpenRefine and the IPT, etc)</li>
                      </ul>
                    </li>
                    <li><i className="bi bi-person-lock"></i> No open access to publishing facilities - users have to know who to email and have to wait to get added to IPTs manually</li>
                    <li><i className="bi bi-alarm"></i> GBIF node staff time and resources are limited</li>
                    <li><i className="bi bi-person-raised-hand"></i> Training workshops can help, but:
                      <ul>
                        <li>are costly and time consuming</li>
                        <li>teach generic techniques which users find difficult to put into practice in the real world</li>
                        <li>have logistical and language barriers</li>
                        <li>have to be done regularly: users who only publish data once or twice a year forget how to do it and need the same help every time</li>
                      </ul>
                    </li>
                  </ul> 
                <p>ChatIPT solves these problems: a non-technical user without training or specialist knowledge only needs a web browser and verified ORCID account to go from an unformatted, raw spreadsheet to standardised, published data on GBIF.</p>
                <hr />
                <div className="alert alert-light" role="alert">
                  <p><strong>Future plans</strong></p>
                  <ol>
                    <li>Restrict access with an ORCID login</li>
                    <li>Build in strict safety rails to ensure the bot is only used for legitimate data publication</li>
                    <li>Create a front page dashboard listing a logged-in user's datasets, along with some stats for each dataset from the GBIF API</li>
                    <li>Provide edit access for already published or work-in-progress datasets</li>
                    <li>Currently publishing using the GBIF Norway publishing institution - this would need to be opened up to more countries. National nodes would sign up for it (agreeing that ad-hoc users can publish to a generic national institution), and publicise it at their higher education institutions. </li>
                    <li>Only works well at the moment for occurrence data - expand to sampling event, checklist and others.</li>
                    <li>Add support for frictionless data & the new data models</li>
                    <li>Test chatbot thoroughly in other languages</li>
                    <li>Parse PDF uploads (e.g. drafts of journal papers) to create better metadata for each dataset</li>
                    <li>Use the details from the user's ORCID login to give chatbot context so it can provide more tailored help. For example, it could read biographies to discover a user's area of expertise and make inferences about the data from that, automatically get current institution name + address for metadata, work out likely level of experience with data publication and tailor language accordingly, and more. The chatbot could also be more personalised and human-like, addressing the user by name, commenting on the new dataset compared to the old work done previously, etc.</li>
                    <li>Currently using OpenAI's gpt4o model - experiment with open source models to reduce costs, depending on uptake</li>
                  </ol>
                  <p>Note: Not suitable for publishing data from a database, or for large data sources, and there are no plans to support this. A chatbot is not the right format as it needs to be done by a technician who understands the database, and as there are far fewer databases than ad-hoc spreadsheets it is (in many ways) a different problem, which we already have a great tool for: the IPT. The IPT is less good for those new to data publication who only need to publish a small, single datase once or twice every few years.</p>
                </div>
              </div>
              <div className="modal-footer">
                <button type="button" className="btn btn-secondary" data-bs-dismiss="modal">Close</button>
              </div>
            </div>
          </div>
        </div>
      </main>
    </ProtectedRoute>
  )
}

export default Home
