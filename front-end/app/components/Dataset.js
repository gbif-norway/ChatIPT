'use client'

import React, { useState, useCallback, useEffect } from 'react';
import { useDataset } from '../contexts/DatasetContext';
import Agent from './Agent';
import FileDrop from './FileDrop';
import Accordion from 'react-bootstrap/Accordion';
import config from '../config.js';

const Dataset = ({ onNewDataset }) => {
  const { currentDataset, currentDatasetId, loading, error, refreshDataset } = useDataset();
  const [tables, setTables] = useState([]);
  const [activeTableId, setActiveTableId] = useState(null);
  const [activeAgentKey, setActiveAgentKey] = useState(null);

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

  // If no dataset is selected, show the file drop interface
  if (!currentDatasetId) {
    return <FileDrop onNewDataset={onNewDataset} />;
  }

  // If loading, show loading state
  if (loading) {
    return (
      <div className="container-fluid">
        <div className="row">
          <div className="col-12">
            <div className="d-flex justify-content-center align-items-center" style={{ height: '50vh' }}>
              <div className="spinner-border" role="status">
                <span className="visually-hidden">Loading...</span>
              </div>
              <span className="ms-3">Loading dataset...</span>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // If error, show error state
  if (error) {
    return (
      <div className="container-fluid">
        <div className="row">
          <div className="col-12">
            <div className="alert alert-danger" role="alert">
              <h4 className="alert-heading">Error loading dataset</h4>
              <p>{error}</p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // If no dataset data, show empty state
  if (!currentDataset) {
    return (
      <div className="container-fluid">
        <div className="row">
          <div className="col-12">
            <div className="alert alert-warning" role="alert">
              <h4 className="alert-heading">No dataset found</h4>
              <p>The selected dataset could not be loaded.</p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="container-fluid">
      <div className="row">
        <div className="col-6">
          {Array.isArray(currentDataset.visible_agent_set) && currentDataset.visible_agent_set.length > 0 ? (
            <Accordion activeKey={activeAgentKey} onSelect={(key) => setActiveAgentKey(key)}>
              {currentDataset.visible_agent_set.map(agent => (
                <Agent key={agent.id} agent={agent} refreshDataset={() => refreshDataset(currentDatasetId)} currentDatasetId={currentDatasetId} />
              ))}
            </Accordion>
          ) : (
            <div className="alert alert-info" role="alert">
              <h4 className="alert-heading">No agents found</h4>
              <p>This dataset doesn't have any processing agents yet. Please wait for the system to initialize.</p>
            </div>
          )}
        </div>
        <div className="col-6">
          {tables.length > 0 ? (
            <div>
              <h4>Data Tables</h4>
              <div className="table-responsive">
                <table className="table table-sm table-striped">
                  <thead>
                    <tr>
                      {Object.keys(tables[0].df[0] || {}).map((key) => (
                        <th key={key}>{key}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {tables[0].df.slice(0, 10).map((row, index) => (
                      <tr key={index}>
                        {Object.values(row).map((value, cellIndex) => (
                          <td key={cellIndex}>{String(value)}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
                {tables[0].df.length > 10 && (
                  <p className="text-muted">Showing first 10 rows of {tables[0].df.length} total rows</p>
                )}
              </div>
            </div>
          ) : (
            <div className="alert alert-info" role="alert">
              <h4 className="alert-heading">No tables found</h4>
              <p>This dataset doesn't have any data tables yet.</p>
            </div>
          )}
        </div>
      </div>
      
      {/* Show loading next task message */}
      {(currentDataset.visible_agent_set && currentDataset.visible_agent_set.length > 0 && currentDataset.visible_agent_set.at(-1).completed_at != null && currentDataset.published_at == null) && (
        <div className="message user-input-loading">
          <div className="d-flex align-items-center">
            <strong>Loading next task</strong>
            <div className="spinner-border ms-auto" role="status" aria-hidden="true"></div>
          </div>
        </div>
      )}
    </div>
  );
};

export default Dataset;
