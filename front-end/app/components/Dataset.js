'use client'

import React, { useState, useCallback, useEffect } from 'react';
import { useDataset } from '../contexts/DatasetContext';
import Agent from './Agent';

import Accordion from 'react-bootstrap/Accordion';
import { getCsrfToken } from '../utils/csrf';
import DataTable from 'react-data-table-component';
import Tabs from 'react-bootstrap/Tabs';
import Tab from 'react-bootstrap/Tab';
import config from '../config.js';

const Dataset = ({ onNewDataset, onBackToDashboard }) => {
  const { currentDataset, currentDatasetId, loading, error, refreshDataset } = useDataset();
  const [tables, setTables] = useState([]);
  const [activeTableId, setActiveTableId] = useState(null);
  const [activeAgentKey, setActiveAgentKey] = useState(null);

  // Helper function to fetch data

  const handleEditDataset = useCallback(async () => {
    try {
      // Get the "Data maintenance" task
      const tasks = await fetchData(`${config.baseUrl}/api/tasks/`);
      const maintenanceTask = tasks.find(task => task.name === 'Data maintenance');

      if (!maintenanceTask) {
        console.error('Data maintenance task not found');
        return;
      }

      // CSRF token for POST
      const csrfToken = await getCsrfToken();
      const headers = { 'Content-Type': 'application/json' };
      if (csrfToken) {
        headers['X-CSRFToken'] = csrfToken;
      }

      const response = await fetch(`${config.baseUrl}/api/agents/`, {
        method: 'POST',
        headers,
        credentials: 'include',
        body: JSON.stringify({ dataset: currentDatasetId, task: maintenanceTask.id })
      });

      if (response.ok) {
        await refreshDataset();
      } else {
        console.error('Failed to create edit agent');
      }
    } catch (error) {
      console.error('Error creating edit agent:', error);
    }
  }, [currentDatasetId, refreshDataset]);

  // Helper function to fetch data
  const fetchData = async (url) => {
    const response = await fetch(url, {
      credentials: 'include'
    });
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }
    return response.json();
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
          <div className="message assistant-message assistant-message-error">{error}</div>
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

  return (
    <div className="container">
      <div className="row mx-auto p-4 no-bottom-margin no-bottom-padding no-left-padding">
        <div className="col-12 alerts-div">
          <div className="d-flex justify-content-between align-items-center mb-3">
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
            <div className="d-flex gap-2">
              {onBackToDashboard && (
                <button 
                  className="btn btn-outline-secondary btn-sm"
                  onClick={onBackToDashboard}
                >
                  <i className="bi bi-arrow-left me-1"></i>
                  Dashboard
                </button>
              )}
              {onNewDataset && (
                <button 
                  className="btn btn-outline-primary btn-sm"
                  onClick={onNewDataset}
                >
                  <i className="bi bi-plus-circle me-1"></i>
                  New Dataset
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
                <strong>Loading next task</strong>
                <div className="spinner-border ms-auto" role="status" aria-hidden="true"></div>
              </div>
            </div>
          )}
          {currentDataset.published_at != null && (
            <div className="message final-message">
              <div className="alert alert-success" role="alert">
                <strong>🎉 Your dataset has now been published! 🎉</strong>
                <hr />
                <a href={currentDataset.gbif_url} className="btn btn-outline-primary" role="button" aria-pressed="true" target="_blank" rel="noopener noreferrer">View on GBIF</a> 
                &nbsp;
                <a href={currentDataset.dwca_url} className="btn btn-outline-secondary" role="button" aria-pressed="true">Download your Darwin Core Archive file</a>
                <br /><br />
                <button className="btn btn-success" onClick={handleEditDataset}>
                  <i className="bi bi-pencil-square me-1"></i>
                  Edit with ChatIPT
                </button>
                <br /><small className="text-muted">Make further changes to your published dataset</small>

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
