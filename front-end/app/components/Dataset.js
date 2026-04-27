'use client'

import React, { useState, useCallback, useEffect } from 'react';
import { useDataset } from '../contexts/DatasetContext';
import { useAuth } from '../contexts/AuthContext';
import Agent from './Agent';
import TreeVisualization from './TreeVisualization';

import Accordion from 'react-bootstrap/Accordion';
import DataTable from 'react-data-table-component';
import Tabs from 'react-bootstrap/Tabs';
import Tab from 'react-bootstrap/Tab';
import config from '../config.js';
import { getCsrfToken } from '../utils/csrf.js';

const Dataset = ({ onNewDataset, onBackToDashboard }) => {
  const { currentDataset, currentDatasetId, loading, error, refreshDataset, queueNextUserMessagePrefix } = useDataset();
  const { user } = useAuth();
  const [tables, setTables] = useState([]);
  const [tablesLoading, setTablesLoading] = useState(true);
  const [activeTableId, setActiveTableId] = useState(null);
  const [activeAgentKey, setActiveAgentKey] = useState(null);
  const [isEditingMetadata, setIsEditingMetadata] = useState(false);
  const [isSavingMetadata, setIsSavingMetadata] = useState(false);
  const [metadataSaveError, setMetadataSaveError] = useState('');
  const [metadataSaveSuccess, setMetadataSaveSuccess] = useState('');
  const [metadataForm, setMetadataForm] = useState({
    title: '',
    description: '',
    temporal_scope: '',
    geographic_scope: '',
    taxonomic_scope: '',
    methodology: '',
    project_title: '',
    dataset_citation: '',
    manuscript_doi: '',
    manuscript_title: '',
    journal: '',
    publication_year: '',
    abstract_source: '',
    methods_source: '',
    creators_source: '',
  });
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
    setTablesLoading(true);
    try {
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
    } catch (error) {
      console.error('Error loading tables:', error);
    } finally {
      setTablesLoading(false);
    }
  }, []);

  const refreshTables = useCallback(async () => {
    console.log('refreshing tables');
    setTablesLoading(true);
    try {
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
    } catch (error) {
      console.error('Error refreshing tables:', error);
    } finally {
      setTablesLoading(false);
    }
  }, [currentDatasetId]);

  const handleVisualizeTreeClick = () => {
    // Bootstrap modal will be shown via data-bs-toggle
    // Tree data will be loaded by TreeVisualization component
  };



  // Load tables when dataset changes
  useEffect(() => {
    if (currentDatasetId) {
      loadTablesForDataset(currentDatasetId);
    } else {
      // Reset tables state when dataset is cleared
      setTables([]);
      setTablesLoading(false);
      setActiveTableId(null);
    }
  }, [currentDatasetId, loadTablesForDataset]);

  // Set active agent when dataset changes
  useEffect(() => {
    if (currentDataset && currentDataset.visible_agent_set && currentDataset.visible_agent_set.length > 0) {
      setActiveAgentKey(currentDataset.visible_agent_set.at(-1).id);
    }
  }, [currentDataset]);

  useEffect(() => {
    if (!currentDataset) {
      return;
    }
    const eml = currentDataset.eml || {};
    setMetadataForm({
      title: currentDataset.title || '',
      description: currentDataset.description || '',
      temporal_scope: eml.temporal_scope || '',
      geographic_scope: eml.geographic_scope || '',
      taxonomic_scope: eml.taxonomic_scope || '',
      methodology: eml.methodology || '',
      project_title: eml.project_title || '',
      dataset_citation: eml.dataset_citation || '',
      manuscript_doi: eml.manuscript_doi || '',
      manuscript_title: eml.manuscript_title || '',
      journal: eml.journal || '',
      publication_year: eml.publication_year ? String(eml.publication_year) : '',
      abstract_source: eml.abstract_source || '',
      methods_source: eml.methods_source || '',
      creators_source: eml.creators_source || '',
    });
    setIsEditingMetadata(false);
    setMetadataSaveError('');
    setMetadataSaveSuccess('');
  }, [currentDatasetId, currentDataset]);

  const handleMetadataInputChange = (event) => {
    const { name, value } = event.target;
    setMetadataForm((prev) => ({ ...prev, [name]: value }));
    if (metadataSaveError) {
      setMetadataSaveError('');
    }
    if (metadataSaveSuccess) {
      setMetadataSaveSuccess('');
    }
  };

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

  const fallbackFileNameRaw = currentDataset?.user_files?.[0]?.filename || '';
  const fallbackFileName = fallbackFileNameRaw.replace(/\([^)]*\)/g, '').trim();

  // Prebuild mailto link for publishing to GBIF production (shown after sandbox publish)
  const productionPublishMailto = (() => {
    if (!currentDataset) return null;
    const subject = '(ChatIPT) Request to publish dataset to GBIF production';
    const mailtoTitle = currentDataset.title || fallbackFileName;
    const body = `Hello GBIF Norway Helpdesk,\n\nI’m pleased to confirm that my dataset has been successfully published to the GBIF Sandbox for validation. I would like to request its publication to GBIF production.\n\n- Dataset title: ${mailtoTitle}\n- Darwin Core Archive (DwC-A): ${currentDataset.dwca_url}\n- GBIF Sandbox dataset page: ${currentDataset.gbif_url}\n\nPlease let me know if you need any further information or changes before proceeding.\n\nThank you for your assistance.\n\nBest regards,`;
    return `mailto:helpdesk@gbif.no?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  })();

  const title = currentDataset.title || fallbackFileName;
  const createdAtDisplay = currentDataset.created_at
    ? new Date(currentDataset.created_at).toLocaleString()
    : 'Unknown';
  const datasetOwner = currentDataset.user_info || user;
  const datasetOwnerName = [datasetOwner?.first_name, datasetOwner?.last_name]
    .filter(Boolean)
    .join(' ')
    .trim() || datasetOwner?.email || datasetOwner?.orcid_id || 'Unknown';
  const eml = currentDataset.eml || {};
  const metadataRows = [
    { label: 'Temporal Coverage', value: eml.temporal_scope },
    { label: 'Geographic Coverage', value: eml.geographic_scope },
    { label: 'Taxonomic Coverage', value: eml.taxonomic_scope },
    { label: 'Methodology', value: eml.methodology },
    { label: 'Project Title', value: eml.project_title },
    { label: 'Dataset Citation', value: eml.dataset_citation },
    { label: 'Manuscript DOI', value: eml.manuscript_doi },
    { label: 'Manuscript Title', value: eml.manuscript_title },
    { label: 'Journal', value: eml.journal },
    { label: 'Publication Year', value: eml.publication_year },
  ];
  const sourceRows = [
    { label: 'Abstract Source', value: eml.abstract_source },
    { label: 'Methods Source', value: eml.methods_source },
    { label: 'Creators Source', value: eml.creators_source },
  ];
  const hasMetadataRows = metadataRows.some(({ value }) => value !== undefined && value !== null && String(value).trim() !== '');
  const hasSourceRows = sourceRows.some(({ value }) => value !== undefined && value !== null && String(value).trim() !== '');
  const emlUsers = Array.isArray(eml.users) ? eml.users : [];
  const taxonomicKeywords = Array.isArray(eml.taxonomic_keywords) ? eml.taxonomic_keywords : [];
  const hasGeographicBounds = eml.geographic_bounds && typeof eml.geographic_bounds === 'object';
  const knownEmlKeys = new Set([
    'temporal_scope',
    'geographic_scope',
    'taxonomic_scope',
    'methodology',
    'manuscript_doi',
    'manuscript_title',
    'journal',
    'publication_year',
    'dataset_citation',
    'project_title',
    'abstract_source',
    'methods_source',
    'creators_source',
    'users',
    'taxonomic_keywords',
    'geographic_bounds',
  ]);
  const additionalEmlEntries = Object.entries(eml).filter(
    ([key, value]) => !knownEmlKeys.has(key) && value !== undefined && value !== null && String(value).trim() !== ''
  );
  const latestAgent = Array.isArray(currentDataset.visible_agent_set) && currentDataset.visible_agent_set.length > 0
    ? currentDataset.visible_agent_set.at(-1)
    : null;
  const latestAgentLastMessage = latestAgent?.message_set?.length > 0 ? latestAgent.message_set.at(-1) : null;
  const latestAssistantHasToolCalls = latestAgentLastMessage?.role === 'assistant'
    && Array.isArray(latestAgentLastMessage?.openai_obj?.tool_calls)
    && latestAgentLastMessage.openai_obj.tool_calls.length > 0;
  const isAgentCurrentlyWorking = Boolean(
    latestAgent
    && latestAgent.completed_at === null
    && (
      latestAgent.busy_thinking
      || !latestAgentLastMessage
      || latestAgentLastMessage.role !== 'assistant'
      || latestAssistantHasToolCalls
    )
  );

  const resetMetadataFormToCurrentDataset = () => {
    setMetadataForm({
      title: currentDataset.title || '',
      description: currentDataset.description || '',
      temporal_scope: eml.temporal_scope || '',
      geographic_scope: eml.geographic_scope || '',
      taxonomic_scope: eml.taxonomic_scope || '',
      methodology: eml.methodology || '',
      project_title: eml.project_title || '',
      dataset_citation: eml.dataset_citation || '',
      manuscript_doi: eml.manuscript_doi || '',
      manuscript_title: eml.manuscript_title || '',
      journal: eml.journal || '',
      publication_year: eml.publication_year ? String(eml.publication_year) : '',
      abstract_source: eml.abstract_source || '',
      methods_source: eml.methods_source || '',
      creators_source: eml.creators_source || '',
    });
  };

  const handleStartMetadataEdit = () => {
    if (isAgentCurrentlyWorking) {
      setMetadataSaveError('Metadata editing is temporarily disabled while the agent is actively working.');
      return;
    }
    resetMetadataFormToCurrentDataset();
    setMetadataSaveError('');
    setMetadataSaveSuccess('');
    setIsEditingMetadata(true);
  };

  const handleCancelMetadataEdit = () => {
    resetMetadataFormToCurrentDataset();
    setMetadataSaveError('');
    setMetadataSaveSuccess('');
    setIsEditingMetadata(false);
  };

  const handleSaveMetadata = async () => {
    if (isAgentCurrentlyWorking) {
      setMetadataSaveError('Metadata editing is temporarily disabled while the agent is actively working.');
      return;
    }

    const normalizedYear = String(metadataForm.publication_year || '').trim();
    let parsedPublicationYear = null;
    if (normalizedYear.length > 0) {
      parsedPublicationYear = Number.parseInt(normalizedYear, 10);
      if (Number.isNaN(parsedPublicationYear)) {
        setMetadataSaveError('Publication Year must be a valid number.');
        return;
      }
    }

    setIsSavingMetadata(true);
    setMetadataSaveError('');
    setMetadataSaveSuccess('');

    try {
      const csrfToken = await getCsrfToken();
      const headers = { 'Content-Type': 'application/json' };
      if (csrfToken) {
        headers['X-CSRFToken'] = csrfToken;
      }

      const updatedEml = {
        ...eml,
        temporal_scope: metadataForm.temporal_scope.trim(),
        geographic_scope: metadataForm.geographic_scope.trim(),
        taxonomic_scope: metadataForm.taxonomic_scope.trim(),
        methodology: metadataForm.methodology.trim(),
        project_title: metadataForm.project_title.trim(),
        dataset_citation: metadataForm.dataset_citation.trim(),
        manuscript_doi: metadataForm.manuscript_doi.trim(),
        manuscript_title: metadataForm.manuscript_title.trim(),
        journal: metadataForm.journal.trim(),
        publication_year: parsedPublicationYear,
        abstract_source: metadataForm.abstract_source.trim(),
        methods_source: metadataForm.methods_source.trim(),
        creators_source: metadataForm.creators_source.trim(),
      };

      const response = await fetch(`${config.baseUrl}/api/datasets/${currentDatasetId}/`, {
        method: 'PATCH',
        headers,
        credentials: 'include',
        body: JSON.stringify({
          title: metadataForm.title.trim(),
          description: metadataForm.description.trim(),
          eml: updatedEml,
        }),
      });

      if (!response.ok) {
        let errorMessage = 'Failed to save dataset metadata.';
        try {
          const errorData = await response.json();
          const errorValues = Object.values(errorData || {}).flat().filter(Boolean);
          if (errorValues.length > 0) {
            errorMessage = errorValues.join(' ');
          }
        } catch (_) {
          // keep default error message
        }
        throw new Error(errorMessage);
      }

      await refreshDataset(currentDatasetId);
      queueNextUserMessagePrefix(currentDatasetId, '[user updated metadata]');
      setMetadataSaveSuccess('Metadata updated. The next message to the agent will include: [user updated metadata].');
      setIsEditingMetadata(false);
    } catch (saveError) {
      setMetadataSaveError(saveError.message || 'Failed to save dataset metadata.');
    } finally {
      setIsSavingMetadata(false);
    }
  };
  const pdfFiles = Array.isArray(currentDataset?.user_files)
    ? currentDataset.user_files.filter((file) => {
        const fileType = String(file?.file_type || '').toLowerCase();
        return fileType.includes('pdf');
      })
    : [];

  const getPdfStatusBadge = () => {
    return { label: 'Available to model', className: 'text-bg-success' };
  };

  return (
    <div className="container">
      <div className="row mx-auto p-4 no-bottom-margin no-bottom-padding no-left-padding">
        <div className="col-12 alerts-div">
          <div className="mb-3">
            <div className="d-flex flex-wrap align-items-center gap-2">
              <h2 className="mb-0 me-2">{title || 'Untitled Dataset'}</h2>
              {currentDataset.structure_notes && (
                <button 
                  className="btn btn-outline-secondary btn-sm" 
                  data-bs-toggle="modal" 
                  data-bs-target="#structureNotesModal"
                  title="View structure notes"
                >
                  <i className="bi bi-info-circle me-1"></i>
                  Structure Notes
                </button>
              )}
              <button
                className="btn btn-primary btn-sm"
                data-bs-toggle="modal"
                data-bs-target="#datasetMetadataModal"
                title="View dataset metadata"
              >
                <i className="bi bi-file-earmark-text me-1"></i>
                Dataset Metadata
              </button>
              {currentDataset.can_visualize_tree && (
                <button 
                  className="btn btn-outline-primary btn-sm" 
                  data-bs-toggle="modal" 
                  data-bs-target="#treeVisualizationModal"
                  onClick={handleVisualizeTreeClick}
                  title="Visualize phylogenetic tree"
                >
                  <i className="bi bi-diagram-3 me-1"></i>
                  Visualise Tree
                </button>
              )}
            </div>
            <div className="mt-2">
              <small className="text-muted d-flex flex-wrap align-items-center gap-2">
                <span className="d-inline-flex align-items-center">
                  <i className="bi bi-person-circle me-1"></i>
                  Dataset owner: {datasetOwnerName}
                </span>
                {datasetOwner?.orcid_id && (
                  <a
                    href={`https://orcid.org/${datasetOwner.orcid_id}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-decoration-none text-muted"
                  >
                    {datasetOwner.orcid_id}
                  </a>
                )}
                {datasetOwner?.institution && (
                  <span>{datasetOwner.institution}</span>
                )}
                <span className="badge text-bg-secondary">Started dataset at {createdAtDisplay}</span>
              </small>
            </div>
          </div>
          {currentDataset.description && (
            <p className="mb-3">{currentDataset.description}</p>
          )}
          {currentDataset.rejected_at && (<div className="alert alert-warning" role="alert">This dataset cannot be published on GBIF as it does not contain valid occurrence or checklist data with all the required fields. Please try uploading a new dataset</div>)}
          {pdfFiles.length > 0 && (
            <div className="mb-3">
              <div className="d-flex flex-wrap gap-2">
                {pdfFiles.map((pdfFile) => {
                  const badge = getPdfStatusBadge(pdfFile);
                  return (
                    <span key={pdfFile.id} className={`badge ${badge.className} d-inline-flex align-items-center gap-1`}>
                      <i className="bi bi-file-earmark-pdf" aria-hidden="true"></i>
                      <span>{pdfFile.filename}</span>
                      <span>•</span>
                      <span>{badge.label}</span>
                    </span>
                  );
                })}
              </div>
            </div>
          )}
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
                {currentDataset.rejected_at ? (
                  <>
                    <strong>Dataset requires new source data.</strong><br />
                    The uploaded files did not provide enough publishable biodiversity data.
                  </>
                ) : (
                  <>
                    <strong>Initializing dataset...</strong><br />
                    This dataset is being set up for processing. Please wait while the system prepares your data.
                  </>
                )}
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
                <a href={currentDataset.gbif_url} className="btn btn-outline-primary" role="button" aria-pressed="true" target="_blank" rel="noopener noreferrer">🌐 View on GBIF (sandbox)</a>
                <a href={productionPublishMailto} className="btn btn-success" role="button" aria-pressed="true">🚀 Request publication to GBIF (production) 🚀</a>
                <a href={currentDataset.dwca_url} className="btn btn-outline-secondary" role="button" aria-pressed="true">⬇️ Download your Darwin Core Archive file</a>
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
            {tablesLoading ? (
              <div className="d-flex justify-content-center align-items-center" style={{ minHeight: '200px' }}>
                <div className="spinner-border" role="status">
                  <span className="visually-hidden">Loading tables...</span>
                </div>
              </div>
            ) : tables.length > 0 ? (
              <Tabs activeKey={activeTableId} onSelect={(k) => setActiveTableId(k)} className="mb-3">
                {tables.map((table) => (
                  <Tab
                    eventKey={table.id}
                    title={<CustomTabTitle>{`${table.title} <small>(ID ${table.id})</small>`}</CustomTabTitle>}
                    key={table.id}
                  >
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
            ) : (
              <div className="alert alert-info">
                <strong>No tables to display yet.</strong>
                <div className="small">
                  If you uploaded only PDFs, review extraction status above and upload raw biodiversity tables when needed.
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="row">
        <div className="footer"><hr />Something not working? Please send me an email with feedback: <a href="mailto:rukayasj@uio.no" target="_blank">rukayasj@uio.no</a></div>
      </div>

      {/* Dataset Metadata Modal */}
      <div className="modal fade" id="datasetMetadataModal" tabIndex="-1" aria-labelledby="datasetMetadataModalLabel" aria-hidden="true">
        <div className="modal-dialog modal-lg">
          <div className="modal-content">
            <div className="modal-header">
              <h5 className="modal-title" id="datasetMetadataModalLabel">
                <i className="bi bi-file-earmark-text me-2"></i>
                Dataset Metadata
              </h5>
              <button type="button" className="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
            </div>
            <div className="modal-body">
              {isAgentCurrentlyWorking && (
                <div className="alert alert-warning mb-3" role="alert">
                  Metadata editing is disabled while the agent is actively working.
                </div>
              )}
              {metadataSaveError && (
                <div className="alert alert-danger mb-3" role="alert">
                  {metadataSaveError}
                </div>
              )}
              {metadataSaveSuccess && (
                <div className="alert alert-success mb-3" role="alert">
                  {metadataSaveSuccess}
                </div>
              )}

              <h6 className="mb-3">Dataset Details</h6>
              <div className="row g-3 mb-2">
                <div className="col-12">
                  <label className="form-label">Title</label>
                  {isEditingMetadata ? (
                    <input
                      type="text"
                      className="form-control"
                      name="title"
                      value={metadataForm.title}
                      onChange={handleMetadataInputChange}
                      disabled={isSavingMetadata || isAgentCurrentlyWorking}
                    />
                  ) : (
                    <div>{title || 'Untitled Dataset'}</div>
                  )}
                </div>
                <div className="col-12">
                  <label className="form-label">Description</label>
                  {isEditingMetadata ? (
                    <textarea
                      rows={3}
                      className="form-control"
                      name="description"
                      value={metadataForm.description}
                      onChange={handleMetadataInputChange}
                      disabled={isSavingMetadata || isAgentCurrentlyWorking}
                    />
                  ) : (
                    <div>{currentDataset.description || 'No description provided'}</div>
                  )}
                </div>
                <div className="col-md-4">
                  <label className="form-label">Owner</label>
                  <div>{datasetOwnerName}</div>
                </div>
                <div className="col-md-4">
                  <label className="form-label">Started</label>
                  <div>{createdAtDisplay}</div>
                </div>
                <div className="col-md-4">
                  <label className="form-label">Published</label>
                  <div>{currentDataset.published_at ? new Date(currentDataset.published_at).toLocaleString() : 'Not published'}</div>
                </div>
                <div className="col-md-6">
                  <label className="form-label">Source Mode</label>
                  <div>{currentDataset.source_mode || 'Unknown'}</div>
                </div>
                <div className="col-md-6">
                  <label className="form-label">DwC Core</label>
                  <div>{currentDataset.dwc_core || 'Not set'}</div>
                </div>
              </div>

              <hr />
              <h6 className="mb-3">EML Metadata</h6>

              {isEditingMetadata ? (
                <div className="row g-3 mb-3">
                  <div className="col-12">
                    <label className="form-label">Temporal Coverage</label>
                    <input
                      type="text"
                      className="form-control"
                      name="temporal_scope"
                      value={metadataForm.temporal_scope}
                      onChange={handleMetadataInputChange}
                      disabled={isSavingMetadata || isAgentCurrentlyWorking}
                    />
                  </div>
                  <div className="col-12">
                    <label className="form-label">Geographic Coverage</label>
                    <textarea
                      rows={2}
                      className="form-control"
                      name="geographic_scope"
                      value={metadataForm.geographic_scope}
                      onChange={handleMetadataInputChange}
                      disabled={isSavingMetadata || isAgentCurrentlyWorking}
                    />
                  </div>
                  <div className="col-12">
                    <label className="form-label">Taxonomic Coverage</label>
                    <textarea
                      rows={2}
                      className="form-control"
                      name="taxonomic_scope"
                      value={metadataForm.taxonomic_scope}
                      onChange={handleMetadataInputChange}
                      disabled={isSavingMetadata || isAgentCurrentlyWorking}
                    />
                  </div>
                  <div className="col-12">
                    <label className="form-label">Methodology</label>
                    <textarea
                      rows={3}
                      className="form-control"
                      name="methodology"
                      value={metadataForm.methodology}
                      onChange={handleMetadataInputChange}
                      disabled={isSavingMetadata || isAgentCurrentlyWorking}
                    />
                  </div>
                  <div className="col-md-6">
                    <label className="form-label">Project Title</label>
                    <input
                      type="text"
                      className="form-control"
                      name="project_title"
                      value={metadataForm.project_title}
                      onChange={handleMetadataInputChange}
                      disabled={isSavingMetadata || isAgentCurrentlyWorking}
                    />
                  </div>
                  <div className="col-md-6">
                    <label className="form-label">Dataset Citation</label>
                    <input
                      type="text"
                      className="form-control"
                      name="dataset_citation"
                      value={metadataForm.dataset_citation}
                      onChange={handleMetadataInputChange}
                      disabled={isSavingMetadata || isAgentCurrentlyWorking}
                    />
                  </div>
                  <div className="col-md-6">
                    <label className="form-label">Manuscript DOI</label>
                    <input
                      type="text"
                      className="form-control"
                      name="manuscript_doi"
                      value={metadataForm.manuscript_doi}
                      onChange={handleMetadataInputChange}
                      disabled={isSavingMetadata || isAgentCurrentlyWorking}
                    />
                  </div>
                  <div className="col-md-6">
                    <label className="form-label">Manuscript Title</label>
                    <input
                      type="text"
                      className="form-control"
                      name="manuscript_title"
                      value={metadataForm.manuscript_title}
                      onChange={handleMetadataInputChange}
                      disabled={isSavingMetadata || isAgentCurrentlyWorking}
                    />
                  </div>
                  <div className="col-md-4">
                    <label className="form-label">Journal</label>
                    <input
                      type="text"
                      className="form-control"
                      name="journal"
                      value={metadataForm.journal}
                      onChange={handleMetadataInputChange}
                      disabled={isSavingMetadata || isAgentCurrentlyWorking}
                    />
                  </div>
                  <div className="col-md-4">
                    <label className="form-label">Publication Year</label>
                    <input
                      type="number"
                      className="form-control"
                      name="publication_year"
                      value={metadataForm.publication_year}
                      onChange={handleMetadataInputChange}
                      disabled={isSavingMetadata || isAgentCurrentlyWorking}
                    />
                  </div>
                  <div className="col-md-4">
                    <label className="form-label">Abstract Source</label>
                    <input
                      type="text"
                      className="form-control"
                      name="abstract_source"
                      value={metadataForm.abstract_source}
                      onChange={handleMetadataInputChange}
                      disabled={isSavingMetadata || isAgentCurrentlyWorking}
                    />
                  </div>
                  <div className="col-md-6">
                    <label className="form-label">Methods Source</label>
                    <input
                      type="text"
                      className="form-control"
                      name="methods_source"
                      value={metadataForm.methods_source}
                      onChange={handleMetadataInputChange}
                      disabled={isSavingMetadata || isAgentCurrentlyWorking}
                    />
                  </div>
                  <div className="col-md-6">
                    <label className="form-label">Creators Source</label>
                    <input
                      type="text"
                      className="form-control"
                      name="creators_source"
                      value={metadataForm.creators_source}
                      onChange={handleMetadataInputChange}
                      disabled={isSavingMetadata || isAgentCurrentlyWorking}
                    />
                  </div>
                </div>
              ) : (
                <>
                  {hasMetadataRows ? (
                    <dl className="row mb-3">
                      {metadataRows
                        .filter(({ value }) => value !== undefined && value !== null && String(value).trim() !== '')
                        .map(({ label, value }) => (
                          <React.Fragment key={label}>
                            <dt className="col-sm-4">{label}</dt>
                            <dd className="col-sm-8">{String(value)}</dd>
                          </React.Fragment>
                        ))}
                    </dl>
                  ) : (
                    <p className="text-muted mb-3">No descriptive EML fields have been set yet.</p>
                  )}

                  {hasSourceRows && (
                    <>
                      <h6 className="mb-2">Provenance</h6>
                      <dl className="row mb-3">
                        {sourceRows
                          .filter(({ value }) => value !== undefined && value !== null && String(value).trim() !== '')
                          .map(({ label, value }) => (
                            <React.Fragment key={label}>
                              <dt className="col-sm-4">{label}</dt>
                              <dd className="col-sm-8">{String(value)}</dd>
                            </React.Fragment>
                          ))}
                      </dl>
                    </>
                  )}
                </>
              )}

              {hasGeographicBounds && (
                <>
                  <h6 className="mb-2">Geographic Bounds</h6>
                  <div className="d-flex flex-wrap gap-2 mb-3">
                    {Object.entries(eml.geographic_bounds).map(([key, value]) => (
                      <span className="badge text-bg-light border" key={key}>
                        {key}: {String(value)}
                      </span>
                    ))}
                  </div>
                </>
              )}

              {taxonomicKeywords.length > 0 && (
                <>
                  <h6 className="mb-2">Taxonomic Keywords</h6>
                  <div className="table-responsive mb-3">
                    <table className="table table-sm">
                      <thead>
                        <tr>
                          <th scope="col">Rank</th>
                          <th scope="col">Scientific Name</th>
                        </tr>
                      </thead>
                      <tbody>
                        {taxonomicKeywords.map((item, index) => (
                          <tr key={`${item.rank || 'rank'}-${item.scientificName || 'name'}-${index}`}>
                            <td>{item.rank || 'Unknown'}</td>
                            <td>{item.scientificName || 'Unknown'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}

              {emlUsers.length > 0 && (
                <>
                  <h6 className="mb-2">People</h6>
                  <ul className="list-group mb-3">
                    {emlUsers.map((person, index) => {
                      const personName = [person.first_name, person.last_name].filter(Boolean).join(' ').trim() || 'Unnamed';
                      return (
                        <li className="list-group-item" key={`${personName}-${index}`}>
                          <div className="fw-semibold">{personName}</div>
                          {person.email && (
                            <div>
                              <small className="text-muted">{person.email}</small>
                            </div>
                          )}
                          {person.orcid && (
                            <div>
                              <a
                                href={`https://orcid.org/${String(person.orcid).replace(/^https?:\/\/orcid\.org\//, '')}`}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="text-decoration-none"
                              >
                                {person.orcid}
                              </a>
                            </div>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                </>
              )}

              {additionalEmlEntries.length > 0 && (
                <>
                  <h6 className="mb-2">Additional Metadata</h6>
                  <dl className="row mb-0">
                    {additionalEmlEntries.map(([key, value]) => (
                      <React.Fragment key={key}>
                        <dt className="col-sm-4">{key.replace(/_/g, ' ')}</dt>
                        <dd className="col-sm-8">{typeof value === 'object' ? JSON.stringify(value) : String(value)}</dd>
                      </React.Fragment>
                    ))}
                  </dl>
                </>
              )}
            </div>
            <div className="modal-footer">
              {isEditingMetadata ? (
                <>
                  <button
                    type="button"
                    className="btn btn-outline-secondary"
                    onClick={handleCancelMetadataEdit}
                    disabled={isSavingMetadata}
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={handleSaveMetadata}
                    disabled={isSavingMetadata || isAgentCurrentlyWorking}
                  >
                    {isSavingMetadata ? 'Saving...' : 'Save Metadata'}
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={handleStartMetadataEdit}
                  disabled={isSavingMetadata || isAgentCurrentlyWorking}
                >
                  Edit Metadata
                </button>
              )}
              <button type="button" className="btn btn-secondary" data-bs-dismiss="modal">Close</button>
            </div>
          </div>
        </div>
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

      {/* Tree Visualization Modal */}
      {currentDataset.can_visualize_tree && (
        <div className="modal fade" id="treeVisualizationModal" tabIndex="-1" aria-labelledby="treeVisualizationModalLabel" aria-hidden="true">
          <div className="modal-dialog modal-fullscreen-lg-down" style={{ maxWidth: '95vw', height: '90vh', margin: 'auto' }}>
            <div className="modal-content" style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
              <div className="modal-header">
                <h5 className="modal-title" id="treeVisualizationModalLabel">
                  <i className="bi bi-diagram-3 me-2"></i>
                  Phylogenetic Tree Visualization
                </h5>
                <button type="button" className="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
              </div>
              <div className="modal-body" style={{ flex: 1, overflow: 'hidden', padding: 0, display: 'flex', flexDirection: 'column' }}>
                <TreeVisualization datasetId={currentDatasetId} />
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default Dataset;
