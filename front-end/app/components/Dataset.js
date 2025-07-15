import { useEffect, useState, useCallback } from 'react';
import Agent from './Agent';
import FileDrop from './FileDrop';
import Accordion from 'react-bootstrap/Accordion';
import DataTable from 'react-data-table-component';
import Tabs from 'react-bootstrap/Tabs';
import Tab from 'react-bootstrap/Tab';
import config from '../config.js';

const fetchData = async (url, options = {}) => {
  const response = await fetch(url, {
    ...options,
    credentials: 'include' // Include credentials for authenticated requests
  });
  if (!response.ok) throw new Error('Network response was not ok');
  return response.json();
};

const wait = (n) => new Promise((resolve) => setTimeout(resolve, n));

const Dataset = ({ initialDatasetId, onNewDataset }) => {
  const [error, setError] = useState(null);
  const [dataset, setDataset] = useState(null);
  const [activeDatasetID, setActiveDatasetID] = useState(null);
  const [activeAgentKey, setActiveAgentKey] = useState(null);
  const [tables, setTables] = useState([]);
  const [activeTableId, setActiveTableId] = useState(null);
  const [loading, setLoading] = useState(false);  

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
    const tables = await fetchData(`${config.baseUrl}/api/tables?dataset=${activeDatasetID}`);
    const updatedTables = tables.map(item => {
      const df = JSON.parse(item.df_json);
      delete item.df_json;
      return { ...item, df };
    });
    console.log(updatedTables);
    setTables(updatedTables);
    const sortedTables = tables.sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at));
    setActiveTableId(sortedTables[0]?.id);
  }, [activeDatasetID]);

  const refreshDataset = useCallback(async () => {
    console.log(dataset);
    try {
      console.log('refreshing dataset');
      const refreshedDataset = await fetchData(`${config.baseUrl}/api/datasets/${activeDatasetID}/refresh`);
      setDataset(refreshedDataset);
      if (refreshedDataset.visible_agent_set && refreshedDataset.visible_agent_set.length > 0) {
        setActiveAgentKey(refreshedDataset.visible_agent_set.at(-1).id);
      }
      await refreshTables();
      console.log('finished refreshing tables');

      // If the dataset is published, don't do any more
      if(refreshedDataset.published_at != null && refreshedDataset.visible_agent_set && refreshedDataset.visible_agent_set.length > 0 && refreshedDataset.visible_agent_set.at(-1).completed_at != null) { return }
      console.log('dataset is not yet published')

      // If the dataset is not suitable for publication, don't do any more
      console.log('suitable for publication on gbif:')
      console.log(refreshedDataset.rejected_at);
      if(refreshedDataset.rejected_at != null) { return }

      // If the latest agent message is not an assistant message, we need to refresh again
      if(refreshedDataset.visible_agent_set && refreshedDataset.visible_agent_set.length > 0 && 
         refreshedDataset.visible_agent_set.at(-1).message_set && refreshedDataset.visible_agent_set.at(-1).message_set.length > 0 &&
         refreshedDataset.visible_agent_set.at(-1).message_set.at(-1).role != 'assistant') {
        console.log('about to start looping')
        console.log(refreshedDataset.visible_agent_set.at(-1).message_set.at(-1));
        console.log(refreshedDataset.visible_agent_set.at(-1).message_set.at(-1).role);
        // If the latest agent is not complete we should mark it as busy thinking
        if(refreshedDataset.visible_agent_set.at(-1).completed_at == null) { 
          refreshedDataset.visible_agent_set.at(-1).busy_thinking = true;
        }
        console.log(refreshedDataset.visible_agent_set.at(-1).busy_thinking);
        await wait(500);
        console.log('finished waiting, refreshing again');
        refreshDataset();
      }
      console.log('end of refreshing dataset');
    } catch (err) {
      console.log(err.message);
    } finally {
      setLoading(false); // Stop loading when dataset is fully refreshed
    }
  }, [dataset, activeDatasetID, refreshTables]);

  useEffect(() => {if (initialDatasetId) { initialLoadDataset(initialDatasetId); }}, [initialDatasetId]);

  const initialLoadDataset = async (datasetId) => {
    try {
      setLoading(true);
      console.log(`loading dataset with ${config.baseUrl}/api/datasets/${datasetId}/`);
      const refreshedDataset = await fetchData(`${config.baseUrl}/api/datasets/${datasetId}/`);
      setActiveDatasetID(datasetId); 
      setDataset(refreshedDataset);
      
      // Check if the dataset has any agents - if not, we need to use the refresh endpoint
      // to ensure agents are created (this handles incomplete datasets)
      if (!refreshedDataset.visible_agent_set || refreshedDataset.visible_agent_set.length === 0) {
        console.log('No agents found, using refresh endpoint to initialize dataset');
        const refreshedDatasetWithAgents = await fetchData(`${config.baseUrl}/api/datasets/${datasetId}/refresh`);
        setDataset(refreshedDatasetWithAgents);
        
        // Set the active agent key to the last agent
        if (refreshedDatasetWithAgents.visible_agent_set && refreshedDatasetWithAgents.visible_agent_set.length > 0) {
          setActiveAgentKey(refreshedDatasetWithAgents.visible_agent_set.at(-1).id);
        }
      } else {
        // Set the active agent key to the last agent
        if (refreshedDataset.visible_agent_set && refreshedDataset.visible_agent_set.length > 0) {
          setActiveAgentKey(refreshedDataset.visible_agent_set.at(-1).id);
        }
      }
      
      // Load tables for this dataset
      await loadTablesForDataset(datasetId);
    } catch (error) {
      setError(error.message);
    } finally {
      setLoading(false);
    }
  }
  const CustomTabTitle = ({ children }) => <span dangerouslySetInnerHTML={{ __html: children }} />;

  return (
    <div className="container-fluid">
      {!dataset ? (
        <div className="col-lg-9 mx-auto">
          {onNewDataset && (
            <div className="text-end mb-3">
              <button 
                className="btn btn-outline-primary btn-sm"
                onClick={onNewDataset}
              >
                <i className="bi bi-plus-circle me-1"></i>
                New Dataset
              </button>
            </div>
          )}
          <div className="agent-task initialise">
            <div className="messages">
              <div className="message assistant-message d-flex">
                <div className='flex-shrink-0'>
                  <svg version="1.2" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 1080" width="70" height="70"><path id="Path 0" className="s0" d="m527 0.7c-5.2 0.1-16.9 0.7-26 1.3-9.1 0.6-23 2-31 3-8 1-21.5 3.2-30 4.9-8.5 1.7-23.6 5.1-33.5 7.6-9.9 2.5-25 6.8-33.5 9.6-8.5 2.8-21.1 7.3-28 9.9-6.9 2.6-18.6 7.5-26 10.8-7.4 3.3-19.8 9.3-27.5 13.4-7.7 4.1-17.6 9.5-22 12-4.4 2.5-12.5 7.4-18 10.9-5.5 3.5-14.5 9.5-20 13.3-5.5 3.9-14.7 10.6-20.5 15-5.8 4.5-15.4 12.3-21.5 17.4-6 5.1-18.4 16.6-27.4 25.5-9 8.9-21.8 22.5-28.5 30.2-6.7 7.7-15.3 18.1-19.2 23-3.8 4.9-9.7 12.8-13.1 17.5-3.3 4.7-9.8 14.3-14.5 21.5-4.6 7.2-11.4 18.2-15 24.5-3.6 6.3-9.8 18-13.8 26-4 8-9.3 19-11.7 24.5-2.5 5.5-7.1 16.8-10.3 25-3.1 8.3-7.6 20.9-9.8 28-2.2 7.1-5.3 17.5-6.8 23-1.4 5.5-3.9 15.4-5.4 22-1.6 6.6-3.9 18.5-5.3 26.5-1.4 8-3.3 20.1-4.1 27-0.9 6.9-2 18.6-2.6 26-0.6 7.4-1.1 25.4-1.1 40 0 14.6 0.4 32.4 1 39.5 0.6 7.1 1.8 18.9 2.7 26 0.8 7.1 2.7 19.5 4.1 27.5 1.4 8 3.7 19.9 5.3 26.5 1.5 6.6 4 16.5 5.4 22 1.5 5.5 4.9 16.8 7.6 25 2.6 8.3 6.7 20 9 26 2.3 6 6.4 16.2 9.2 22.5 2.8 6.3 8.4 18.3 12.6 26.5 4.1 8.3 10.4 20.2 14 26.5 3.6 6.3 12 19.8 18.7 30 6.8 10.2 16.5 23.9 21.6 30.5 5.1 6.6 14.6 18.1 21 25.5 6.4 7.4 18.8 20.5 27.5 29.2 8.8 8.6 20.4 19.5 25.9 24.3 5.5 4.8 14.7 12.4 20.5 17 5.8 4.5 15.9 12 22.5 16.6 6.6 4.6 16.7 11.4 22.5 15 5.8 3.7 15.4 9.5 21.5 13 6.1 3.4 20.4 10.9 32 16.5 11.6 5.6 28.4 13.1 37.5 16.7 9.1 3.6 23 8.5 31 11 8 2.5 18.6 5.7 23.5 7.1 4.9 1.4 15.1 3.8 22.5 5.5 7.4 1.6 20 4.1 28 5.5 8 1.4 18.1 3 22.5 3.6 4.4 0.6 12.1 1.5 17 2.1 4.9 0.5 18.7 1.4 30.5 1.8 11.8 0.5 31.4 0.5 43.5 0 12.1-0.4 29.6-1.7 39-2.9 9.4-1.1 23.5-3.2 31.5-4.6 8-1.4 19.7-3.7 26-5.1 6.3-1.4 16.5-3.8 22.5-5.5 6-1.6 16.9-4.8 24-7 7.1-2.2 18.9-6.2 26-9 7.1-2.7 18.2-7.2 24.5-10 6.3-2.8 18.3-8.4 26.5-12.6 8.3-4.1 19.7-10.1 25.5-13.4 5.8-3.4 15.2-9 21-12.7 5.8-3.6 15.9-10.4 22.5-15 6.6-4.6 16.7-12.1 22.5-16.6 5.8-4.6 15-12.2 20.5-17 5.5-4.8 17.1-15.7 25.9-24.3 8.7-8.7 21.1-21.8 27.5-29.2 6.4-7.4 15.9-18.9 21-25.5 5.1-6.6 14.8-20.3 21.6-30.5 6.7-10.2 15.1-23.7 18.7-30 3.6-6.3 9.8-18 13.8-26 4-8 9.3-19 11.7-24.5 2.4-5.5 6.7-15.9 9.5-23 2.7-7.1 6.9-19.1 9.3-26.5 2.4-7.4 5.9-19.1 7.9-26 1.9-6.9 4.8-18.8 6.5-26.5 1.7-7.7 3.9-19.2 5-25.5 1.1-6.3 2.7-17.4 3.5-24.5 0.9-7.1 2.1-18.9 2.7-26 0.6-7.1 1-24.9 1-39.5 0-14.6-0.5-32.6-1.1-40-0.6-7.4-1.8-18.9-2.6-25.5-0.8-6.6-2.3-17.4-3.4-24-1.2-6.6-3.4-18.3-5.1-26-1.7-7.7-4.4-18.9-6.1-25-1.6-6.1-4.8-16.9-7-24-2.3-7.1-6.7-19.8-9.9-28-3.1-8.3-7.7-19.5-10.2-25-2.4-5.5-7.8-16.8-12-25-4.1-8.3-10.7-20.6-14.7-27.5-4-6.9-10.7-17.7-14.8-24-4.1-6.3-10.2-15.3-13.5-20-3.4-4.7-10.3-13.8-15.4-20.3-5.1-6.4-14-17-19.7-23.4-5.7-6.5-17.5-18.8-26.2-27.5-8.8-8.6-20.6-19.6-26.4-24.5-5.8-4.9-14.5-12.1-19.5-15.9-5-3.9-14.4-10.8-21-15.5-6.6-4.6-16.7-11.4-22.5-15.1-5.8-3.6-13.4-8.3-17-10.3-3.6-2.1-13.5-7.4-22-12-8.5-4.5-21.4-10.8-28.5-14-7.1-3.1-17-7.3-22-9.2-5-1.9-16.9-6.2-26.5-9.5-9.6-3.2-26-8.1-36.5-10.8-10.5-2.6-26.2-6.2-35-8-8.8-1.7-22.5-4-30.5-5-8-1-21.9-2.3-31-2.9-9.1-0.6-22.4-1.2-29.5-1.3-7.1-0.2-17.3-0.2-22.5-0.1z"/><path id="Path 1" className="s1" d="m312 198.1c-9.1 0.4-34.6 1.2-56.8 1.8-22.1 0.6-50.5 1.1-63.2 1.1-19.8 0-23 0.2-23 1.5 0 0.8 3.6 8.7 8 17.5 4.4 8.8 11.9 22.6 16.6 30.8 4.8 8.1 11.4 18.8 14.7 23.7 3.3 4.9 9.7 13.9 14.2 20 4.5 6.1 12.4 15.9 17.7 22 5.2 6.1 13.3 14.6 17.9 19 4.6 4.4 11.5 10.5 15.4 13.6 3.9 3 10.4 7.8 14.5 10.6 4.1 2.7 10.9 6.8 15 9.1 4.1 2.2 12 5.6 17.5 7.5 5.5 1.9 16.8 4.9 25 6.6 8.3 1.6 21.8 3.8 30 4.8 12.8 1.5 21.1 1.7 56.5 1.4 22.8-0.2 53.6-0.4 68.5-0.5 24.4-0.1 27.5 0.1 32.5 1.9 3 1 6.6 2.9 8 4 1.4 1.2 3.8 4.4 5.4 7.1 1.6 2.7 4.6 10.1 6.7 16.4 2 6.3 4.7 16.1 5.8 21.8l2.1 10.2c-14.4 16.7-23 27.1-28.4 34-5.3 6.9-14.3 19-20 27-5.7 8-14.1 20.6-18.8 28-4.7 7.4-12 19.6-16.3 27-4.3 7.4-11.3 20.6-15.6 29.3-4.4 8.6-10.6 21.9-13.8 29.5-3.2 7.5-8.4 20.7-11.4 29.2-3 8.5-7.4 22.3-9.6 30.5-2.3 8.3-5.5 21.5-7.1 29.5-1.6 8-3.7 19.9-4.6 26.5-0.9 7-1.8 22.4-2.1 37-0.4 19.2-0.2 28.5 1.1 40 0.9 8.3 2.5 19.7 3.7 25.5 1.1 5.8 3.1 15 4.5 20.5 1.4 5.5 3.4 11.5 4.4 13.3 1.1 1.7 3.6 3.9 5.5 4.7 1.9 0.8 4.7 1.5 6.3 1.5 1.5 0 4.2-0.9 6-2 1.7-1.1 3.9-3.5 4.8-5.2 1-2.1 1.5-4.8 1.2-7.3-0.3-2.2-1.9-8.5-3.5-14-1.7-5.5-4-14-5.1-19-1.2-5-2.8-13.3-3.6-18.5-0.8-5.2-2-15.4-2.7-22.5-0.7-8.1-1-20.6-0.6-33 0.3-11 1.3-25.6 2.2-32.5 0.8-6.9 2.5-17.5 3.6-23.5 1.1-6 3.6-17.3 5.5-25 2-7.7 5.6-20.3 8.1-28 2.5-7.7 7.4-21.4 11-30.5 3.6-9.1 9.8-23.5 13.7-32 4-8.5 11.4-23.1 16.6-32.5 5.1-9.4 12.9-22.9 17.3-30 4.3-7.1 11.8-18.6 16.4-25.5 4.7-6.9 13.1-18.6 18.6-26 5.6-7.4 14-17.6 18.8-22.5 4.8-4.9 12.1-11.9 16.3-15.5 4.2-3.6 10.4-8.2 13.9-10.3 4.3-2.6 7.4-3.7 9.9-3.7 2.1 0 5.5 0.7 7.5 1.6 2.1 0.8 6.5 3.5 9.8 5.8 3.3 2.4 19 17 35 32.6 16 15.5 35.5 34.1 43.5 41.2 8 7.1 20.6 17.6 28 23.2 7.4 5.7 17.8 13.1 23 16.6 5.2 3.5 14 8.7 19.5 11.6 5.5 2.9 14 6.6 19 8.2 5 1.7 13.3 3.7 18.5 4.5 5.2 0.9 13.1 1.9 17.5 2.2 4.4 0.3 13.9 0.2 21-0.4 7.1-0.6 19.1-2.2 26.5-3.6 7.4-1.5 18.2-4.2 24-6 5.8-1.8 15-5 20.5-7 5.5-2 14.7-5.9 20.5-8.6 5.8-2.6 14.9-7.2 20.3-10.1l9.7-5.3c-46.9-44.1-76.6-72.4-96.3-91.4-19.7-19-40.2-37.9-45.5-41.9-5.3-4-14.7-9.7-20.7-12.6-6-3-14.6-6.6-19-8-4.4-1.4-12.3-3.6-17.5-4.7-6.3-1.3-15.1-2.2-26-2.6-10.1-0.3-20.6-0.1-27 0.6-5.8 0.7-15.7 2.4-22 3.8-6.3 1.4-16 3.9-21.5 5.5-5.5 1.6-16.1 5.4-23.5 8.4-7.4 3.1-19.4 8.6-26.5 12.3-7.1 3.7-17.3 9.5-22.5 13-5.2 3.5-12.6 8.9-16.5 11.9-3.9 3.1-9.7 8.2-13 11.4l-6 5.7c-5.9-22-9.9-34.5-12.7-41.9-2.7-7.4-9.1-22.1-14.2-32.5-5.1-10.4-12.7-25.1-17-32.5-4.3-7.4-12.3-20-17.7-28-5.4-8-13.8-19.4-18.6-25.5-4.8-6-13.7-16.1-19.8-22.4-6.1-6.2-15.3-15-20.5-19.3-5.2-4.4-14.4-11.3-20.5-15.3-6.1-4.1-15.7-9.6-21.5-12.4-5.8-2.8-15.2-6.6-21-8.4-5.8-1.9-15.8-4.2-22.3-5.1-6.4-0.9-16.5-1.5-22.5-1.4-5.9 0-18.1 0.4-27.2 0.9z"/></svg>
                </div>
                <div className="flex-grow-1 ms-3">
                  <div className="inner-message">
                    So, you want to publish some biodiversity data to <a href="https://gbif.org" target="_blank" rel="noreferrer">gbif.org</a>? I can help you with that! Let's start by taking a look at your data file <small>(<a href="https://storage.gbif-no.sigma2.no/misc/static/TestDatasetChatIPT.xlsx">test data file also available</a>)</small>. 
                    <hr /><p><small>By the way, I'm going to start our conversation in English, but feel free to talk to me in your preferred language.</small></p>
                  </div>
                </div>
              </div>
              {loading ? (
                <div className="spinner"></div>
              ) : (
                <FileDrop
                  onFileAccepted={(data) => { 
                    initialLoadDataset(data);
                  }}
                  onError={(errorMessage) => setError(errorMessage)}
                />
              )}
              {error && <div className="message assistant-message assistant-message-error">{error}</div>}
            </div>
          </div>
        </div>
      ) : (
        <div>
          <div className="row mx-auto p-4 no-bottom-margin no-bottom-padding no-top-padding">
            <div className="col-12 alerts-div">
              <div className="d-flex justify-content-between align-items-center mb-3">
                <div className="publishing-heading">Publishing {dataset.file.split(/\//).pop()} (original file name) <span className="badge text-bg-secondary">Started {new Date(dataset.created_at).toLocaleString()}</span></div>
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
              {dataset.title && (<div className="alert alert-info" role="alert"><strong>Title</strong>: {dataset.title}<br /><strong>Description</strong>: {dataset.description}</div>)}
              {dataset.structure_notes && (
                <Accordion>
                  <Accordion.Item eventKey="0">
                    <Accordion.Header>Notes about the structure</Accordion.Header>
                    <Accordion.Body>
                      <small>{dataset.structure_notes}</small>
                    </Accordion.Body>
                  </Accordion.Item>
                </Accordion>
              )}
              {dataset.rejected_at && (<div className="alert alert-warning" role="alert">This dataset cannot be published on GBIF as it does not contain valid occurrence or checklist data with all the required fields. Please try uploading a new dataset</div>)}
            </div>
          </div>
          <div className="row mx-auto p-4">
            <div className="col-6">
              {Array.isArray(dataset.visible_agent_set) && dataset.visible_agent_set.length > 0 ? (
                <Accordion activeKey={activeAgentKey} onSelect={(key) => setActiveAgentKey(key)}>
                  {dataset.visible_agent_set.map(agent => (
                    <Agent key={agent.id} agent={agent} refreshDataset={refreshDataset} />
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
              {(dataset.visible_agent_set && dataset.visible_agent_set.length > 0 && dataset.visible_agent_set.at(-1).completed_at != null && dataset.published_at == null) && (
                <div className="message user-input-loading">
                  <div className="d-flex align-items-center">
                    <strong>Loading next task</strong>
                    <div className="spinner-border ms-auto" role="status" aria-hidden="true"></div>
                  </div>
                </div>
              )}
              {dataset.published_at != null && (
                <div className="message user-message">
                  <div className="d-flex align-items-center">
                  <div class="alert alert-success" role="alert">
                    <strong>🎉 Your dataset has now been published! 🎉</strong>
                    <hr />
                    <a href={dataset.gbif_url} className="btn btn-outline-primary" role="button" aria-pressed="true">View on GBIF</a> 
                    &nbsp;
                    <a href={dataset.dwca_url} className="btn btn-outline-secondary" role="button" aria-pressed="true">Download your Darwin Core Archive file</a>
                    <br /><br />
                    Editing and updating options will be available in the future. <strong>But for now, thanks for trying out ChatIPT.</strong></div>
                  </div>
                </div>
              )}
              {dataset.rejected_at && (<div className="alert alert-warning" role="alert">This dataset cannot be published on GBIF as it does not contain valid occurrence or checklist data with all the required fields. Please try uploading a new dataset</div>)}
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
        </div>
      )}

      <div className="row">
        <div className="col-lg-9 mx-auto">
          <div className="footer"><hr />Something not working? Please send me an email with feedback: <a href="mailto:rukayasj@uio.no" target="_blank">rukayasj@uio.no</a></div>
        </div>
      </div>
    </div>
  );
};

export default Dataset;
