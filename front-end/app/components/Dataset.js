'use client'

import React, { useState, useCallback, useEffect } from 'react';
import { useDataset } from '../contexts/DatasetContext';
import Agent from './Agent';

import Accordion from 'react-bootstrap/Accordion';
import DataTable from 'react-data-table-component';
import Tabs from 'react-bootstrap/Tabs';
import Tab from 'react-bootstrap/Tab';
import config from '../config.js';

const Dataset = ({ onNewDataset, onBackToDashboard }) => {
  const { currentDataset, currentDatasetId, loading, error, refreshDataset } = useDataset();
  const [tables, setTables] = useState([]);
  const [activeTableId, setActiveTableId] = useState(null);
  const [activeAgentKey, setActiveAgentKey] = useState(null);

  // Helper function to fetch data with timeout
  const fetchData = async (url, options = {}) => {
    const { timeout = 30000, retries = 2 } = options; // 30 second timeout for table requests
    
    for (let attempt = 0; attempt <= retries; attempt++) {
      try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), timeout);
        
        const response = await fetch(url, {
          credentials: 'include',
          signal: controller.signal,
          ...options
        });
        
        clearTimeout(timeoutId);
        
        if (!response.ok) {
          throw new Error(`HTTP error! status: ${response.status}`);
        }
        return response.json();
      } catch (error) {
        console.log(`Fetch attempt ${attempt + 1} failed for ${url}:`, error.message);
        
        // If this is the last attempt, or if it's not a network error, throw
        if (attempt === retries || (!error.name?.includes('Abort') && !error.message?.includes('fetch'))) {
          throw error;
        }
        
        // Wait before retrying
        const delay = 1000 * (attempt + 1);
        console.log(`Retrying in ${delay}ms...`);
        await new Promise(resolve => setTimeout(resolve, delay));
      }
    }
  };

  const loadTablesForDataset = useCallback(async (datasetId) => {
    console.log('loading tables for dataset', datasetId);
    const tables = await fetchData(`${config.baseUrl}/api/tables?dataset=${datasetId}`);
    const updatedTables = tables.map(item => {
      const df = JSON.parse(item.df_json);
      delete item.df_json;
      return { ...item, df };
    });
    console.log(updatedTables);
    setTables(updatedTables);
    const sortedTables = tables.sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at));
    setActiveTableId(sortedTables[0]?.id);
  }, []);

  const refreshTables = useCallback(async () => {
    console.log('refreshing tables');
    const tables = await fetchData(`${config.baseUrl}/api/tables?dataset=${currentDatasetId}`);
    const updatedTables = tables.map(item => {
      const df = JSON.parse(item.df_json);
      delete item.df_json;
      return { ...item, df };
    });
    console.log(updatedTables);
    setTables(updatedTables);
    const sortedTables = tables.sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at));
    setActiveTableId(sortedTables[0]?.id);
  }, [currentDatasetId]);



  // Load tables when dataset changes
  useEffect(() => {
    if (currentDatasetId) {
      loadTablesForDataset(currentDatasetId);
    }
  }, [currentDatasetId, loadTablesForDataset]);

  // Set active agent when dataset changes
  useEffect(() => {
    if (currentDataset && currentDataset.visible_agent_set && currentDataset.visible_agent_set.length > 0) {
      setActiveAgentKey(currentDataset.visible_agent_set.at(-1).id);
    }
  }, [currentDataset]);

  // Dataset.js should only be shown when there's a currentDatasetId
  // The upload flow is now handled in page.js
  if (!currentDatasetId) {
    return (
      <div className="container">
        <div className="col-lg-9 mx-auto">
          <div className="message assistant-message">
            <div className="inner-message">
              <strong>No dataset selected</strong><br />
              Please return to the dashboard to select a dataset.
            </div>
          </div>
        </div>
      </div>
    );
  }

  // If loading, show loading state
  if (loading) {
    return (
      <div className="container">
        <div className="col-lg-9 mx-auto">
          <div className="spinner"></div>
        </div>
      </div>
    );
  }

  // If error, show error state
  if (error) {
    return (
      <div className="container">
        <div className="col-lg-9 mx-auto">
          <div className="message assistant-message assistant-message-error">
            <div className="inner-message">
              <strong>Connection Error</strong><br />
              {error.includes('fetch') || error.includes('network') ? 
                'There was a temporary network issue. The processing is continuing in the background. Please refresh the page to see the latest updates.' : 
                error
              }
            </div>
          </div>
        </div>
      </div>
    );
  }

  // If no dataset data, show empty state
  if (!currentDataset) {
    return (
      <div className="container">
        <div className="col-lg-9 mx-auto">
          <div className="message assistant-message">
            <div className="inner-message">
              <strong>No dataset found</strong><br />
              The selected dataset could not be loaded.
            </div>
          </div>
        </div>
      </div>
    );
  }

  const CustomTabTitle = ({ children }) => <span dangerouslySetInnerHTML={{ __html: children }} />;

  // Prebuild mailto link for publishing to GBIF production (shown after sandbox publish)
  const productionPublishMailto = (() => {
    if (!currentDataset) return null;
    const subject = '(ChatIPT) Request to publish dataset to GBIF production';
    const title = currentDataset.title || (currentDataset.file ? currentDataset.file.split(/\//).pop().replace(/\([^)]*\)/g, '').trim() : '');
    const body = `Hello GBIF Norway Helpdesk,\n\nI’m pleased to confirm that my dataset has been successfully published to the GBIF Sandbox for validation. I would like to request its publication to GBIF production.\n\n- Dataset title: ${title}\n- Darwin Core Archive (DwC-A): ${currentDataset.dwca_url}\n- GBIF Sandbox dataset page: ${currentDataset.gbif_url}\n\nPlease let me know if you need any further information or changes before proceeding.\n\nThank you for your assistance.\n\nBest regards,`;
    return `mailto:helpdesk@gbif.no?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  })();

  return (
    <div className="container">
      <div className="row mx-auto p-4 no-bottom-margin no-bottom-padding no-left-padding">
        <div className="col-12 alerts-div">
          <div className="mb-3">
            <div className="d-flex align-items-center gap-2">
              <h2>{currentDataset.title || currentDataset.file.split(/\//).pop().replace(/\([^)]*\)/g, '').trim()}</h2>
              <span className="badge text-bg-secondary">Started {new Date(currentDataset.created_at).toLocaleString()}</span>
              {currentDataset.structure_notes && (
                <button 
                  className="btn btn-info btn-sm" 
                  data-bs-toggle="modal" 
                  data-bs-target="#structureNotesModal"
                  title="View structure notes"
                >
                  <i className="bi bi-info-circle me-1"></i>
                  Structure Notes
                </button>
              )}
            </div>
          </div>
          {currentDataset.description && (
            <p className="mb-3">{currentDataset.description}</p>
          )}
          {currentDataset.rejected_at && (<div className="alert alert-warning" role="alert">This dataset cannot be published on GBIF as it does not contain valid occurrence or checklist data with all the required fields. Please try uploading a new dataset</div>)}
        </div>
      </div>
      <div className="row mx-auto p-4 no-left-padding">
        <div className="col-6">
          {Array.isArray(currentDataset.visible_agent_set) && currentDataset.visible_agent_set.length > 0 ? (
            <Accordion activeKey={activeAgentKey} onSelect={(key) => setActiveAgentKey(key)}>
              {currentDataset.visible_agent_set.map(agent => (
                <Agent
                  key={agent.id}
                  agent={agent}
                  refreshDataset={() => refreshDataset(currentDatasetId)}
                  currentDatasetId={currentDatasetId}
                  refreshTables={refreshTables}
                />
              ))}
            </Accordion>
          ) : (
            <div className="message assistant-message">
              <div className="inner-message">
                <strong>Initializing dataset...</strong><br />
                This dataset is being set up for processing. Please wait while the system prepares your data.
              </div>
            </div>
          )}
          {(currentDataset.visible_agent_set && currentDataset.visible_agent_set.length > 0 && currentDataset.visible_agent_set.at(-1).completed_at != null && currentDataset.published_at == null) && (
            <div className="message user-input-loading">
              <div className="d-flex align-items-center">
                <strong>Working... loading next task</strong>
                <div className="spinner-border ms-auto" role="status" aria-hidden="true"></div>
              </div>
            </div>
          )}
          {currentDataset.published_at != null && (
            <div className="message final-message">
              <div className="alert alert-success" role="alert">
                <strong>🎉 Your dataset has now been published! 🎉</strong>
                <hr />
                <a href={currentDataset.gbif_url} className="btn btn-outline-primary" role="button" aria-pressed="true" target="_blank" rel="noopener noreferrer">View on GBIF (sandbox)</a>
                &nbsp;
                <a href={productionPublishMailto} className="btn btn-primary" role="button" aria-pressed="true">Publish to GBIF (production)</a>
                &nbsp;
                <a href={currentDataset.dwca_url} className="btn btn-outline-secondary" role="button" aria-pressed="true">Download your Darwin Core Archive file</a>

              </div>
            </div>
          )}

          {currentDataset.rejected_at && (
            <div className="alert alert-warning" role="alert">
              This dataset cannot be published on GBIF as it does not contain valid occurrence or checklist data with all the required fields.

            </div>
          )}
        </div>
        <div className="col-6">
          <div className="sticky-top">
            {tables.length > 0 && (
              <Tabs activeKey={activeTableId} onSelect={(k) => setActiveTableId(k)} className="mb-3">
                {tables.map((table) => (
                  <Tab eventKey={table.id}
                    title={<CustomTabTitle>{`${table.title} <small>(ID ${table.id})</small>`}</CustomTabTitle>}
                    key={table.id}>
                    <DataTable
                      columns={table.df[0] ? Object.keys(table.df[0]).map(column => ({
                        name: column,
                        selector: row => row[column],
                        sortable: true,
                      })) : []}
                      data={table.df}
                      theme="dark"
                      pagination
                      dense
                    />
                  </Tab>
                ))}
              </Tabs>
            )}
          </div>
        </div>
      </div>

      <div className="row">
        <div className="footer"><hr />Something not working? Please send me an email with feedback: <a href="mailto:rukayasj@uio.no" target="_blank">rukayasj@uio.no</a></div>
      </div>

      {/* Structure Notes Modal */}
      {currentDataset.structure_notes && (
        <div className="modal fade" id="structureNotesModal" tabIndex="-1" aria-labelledby="structureNotesModalLabel" aria-hidden="true">
          <div className="modal-dialog modal-lg">
            <div className="modal-content">
              <div className="modal-header">
                <h5 className="modal-title" id="structureNotesModalLabel">
                  <i className="bi bi-info-circle me-2"></i>
                  Structure Notes
                </h5>
                <button type="button" className="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
              </div>
              <div className="modal-body">
                <div style={{ whiteSpace: 'pre-wrap' }}>
                  {currentDataset.structure_notes}
                </div>
              </div>
              <div className="modal-footer">
                <button type="button" className="btn btn-secondary" data-bs-dismiss="modal">Close</button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default Dataset;
