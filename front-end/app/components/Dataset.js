'use client'

import React, { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import { usePathname, useSearchParams } from 'next/navigation';
import { useDataset } from '../contexts/DatasetContext';
import { useAuth } from '../contexts/AuthContext';
import { useGbifStatus } from '../contexts/GbifStatusContext';
import Agent from './Agent';
import DatasetPanel from './DatasetPanel';
import PackageExplorer, { openPackageExplorer } from './PackageExplorer';
import { rememberedView } from './TableExplorer';
import TreeVisualization from './TreeVisualization';
import { getDatasetStatus, getStatusMeta, pluralize } from '../utils/datasetPresentation';
import { datasetIsActivelyProcessing } from '../utils/workflowState.mjs';

import Accordion from 'react-bootstrap/Accordion';
import config from '../config.js';
import { getCsrfToken } from '../utils/csrf.js';
import { publishableEmail } from '../utils/creatorEmail.mjs';
import { apiErrorMessage, normalizeTableList } from '../utils/tableApi.mjs';
import {
  decodeExplorerState,
  explorerSearchString,
  publishedTablesKey,
  resolveExplorerState,
  sameId,
} from '../utils/explorerState.mjs';

// The package model computes link coverage, so it is refetched only after the tables settle,
// and later still while the agent is working (it saves tables many times in a row).
const PACKAGE_MODEL_REFRESH_DELAY_MS = 1500;
const PACKAGE_MODEL_BUSY_DELAY_MS = 8000;
const URL_UPDATE_DELAY_MS = 250;

const emptyCreator = () => ({ first_name: '', last_name: '', email: '', orcid: '' });

const METADATA_EDIT_TASKS = new Set([
  'Publication package preparation',
  'Pre-publication quality gate',
  'Final Review & Publication',
  'Data maintenance',
]);

const creatorsFromDataset = (dataset, fallbackUser) => {
  const savedCreators = Array.isArray(dataset?.eml?.users) ? dataset.eml.users : [];
  if (savedCreators.length > 0) {
    return savedCreators.map((person) => ({
      first_name: person?.first_name || '',
      last_name: person?.last_name || '',
      email: publishableEmail(person?.email),
      orcid: person?.orcid || '',
    }));
  }
  // The account holder did not create the data, so do not suggest them.
  if (dataset?.eml?.creators_unknown) return [emptyCreator()];

  const profile = dataset?.user_info || fallbackUser;
  if (!profile) return [emptyCreator()];
  return [{
    first_name: profile.first_name || '',
    last_name: profile.last_name || '',
    email: publishableEmail(profile.email),
    orcid: profile.orcid_id || '',
  }];
};

const metadataFormFromDataset = (dataset, fallbackUser) => {
  const eml = dataset?.eml || {};
  const bounds = eml.geographic_bounds || {};
  return {
    title: dataset?.title || '',
    description: dataset?.description || '',
    license: eml.license || 'CC BY 4.0',
    temporal_scope: eml.temporal_scope || '',
    geographic_scope: eml.geographic_scope || '',
    geographic_west: bounds.west ?? '',
    geographic_east: bounds.east ?? '',
    geographic_north: bounds.north ?? '',
    geographic_south: bounds.south ?? '',
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
    creators_source: eml.creators_source || (eml.creators_unknown ? 'unknown' : 'user_profile'),
    users: creatorsFromDataset(dataset, fallbackUser),
  };
};

// The latest agent is still thinking or running tools.
const agentIsWorking = (dataset) => {
  const agents = Array.isArray(dataset?.visible_agent_set) ? dataset.visible_agent_set : [];
  const latestAgent = agents.length > 0 ? agents.at(-1) : null;
  const lastMessage = latestAgent?.message_set?.length > 0 ? latestAgent.message_set.at(-1) : null;
  const hasToolCalls = lastMessage?.role === 'assistant'
    && Array.isArray(lastMessage?.openai_obj?.tool_calls)
    && lastMessage.openai_obj.tool_calls.length > 0;
  return Boolean(
    latestAgent
    && latestAgent.completed_at === null
    && (
      latestAgent.busy_thinking
      || !lastMessage
      || lastMessage.role !== 'assistant'
      || hasToolCalls
    )
  );
};

const Dataset = ({ onNewDataset, onBackToDashboard }) => {
  const { currentDataset, currentDatasetId, loading, error, refreshDataset, queueNextUserMessagePrefix } = useDataset();
  const { paused: gbifPaused } = useGbifStatus();
  const { user } = useAuth();
  const [tables, setTables] = useState([]);
  const [tablesLoading, setTablesLoading] = useState(true);
  const [tablesError, setTablesError] = useState('');
  // The table, view, column and filters open in the package explorer modal (null while it is closed),
  // mirrored in the address so a view can be shared or reloaded.
  const [addressState, setAddressState] = useState(null);
  // The address is only written once it has been read: until the tables load, it may name a table to open.
  const [addressRead, setAddressRead] = useState(false);
  // The dataset whose tables this page shows; a table request for any other dataset is stale.
  const shownDatasetRef = useRef(null);
  const pathname = usePathname();
  const searchParams = useSearchParams();
  // Read when the tables load; otherwise the address only follows the modal, never the other way round.
  const searchParamsRef = useRef(searchParams);
  // One package model for the whole page, used by the package explorer modal.
  const [packageModelState, setPackageModelState] = useState({ datasetId: null, model: null, loading: false, error: '' });
  const modelRequestRef = useRef({ datasetId: null, inFlightKey: null, pendingKey: null, loadedKey: null, controller: null });
  const agentBusyRef = useRef(false);
  const [composerAgentIds, setComposerAgentIds] = useState([]);
  const [askRequest, setAskRequest] = useState(null);
  const processingRefreshStartedRef = useRef(null);
  const [activeAgentKey, setActiveAgentKey] = useState(null);
  const [isEditingMetadata, setIsEditingMetadata] = useState(false);
  const [isSavingMetadata, setIsSavingMetadata] = useState(false);
  const [metadataSaveError, setMetadataSaveError] = useState('');
  const [metadataSaveSuccess, setMetadataSaveSuccess] = useState('');
  const [metadataForm, setMetadataForm] = useState({
    title: '',
    description: '',
    license: 'CC BY 4.0',
    temporal_scope: '',
    geographic_scope: '',
    geographic_west: '',
    geographic_east: '',
    geographic_north: '',
    geographic_south: '',
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
    users: [emptyCreator()],
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

  // Tables that arrive after the page has moved on to another dataset (or left) are dropped.
  const loadTablesForDataset = useCallback(async (datasetId) => {
    console.log('loading tables for dataset', datasetId);
    setTablesLoading(true);
    setTablesError('');
    setAddressRead(false);
    let nextTables = null;
    try {
      nextTables = normalizeTableList(await fetchData(`${config.baseUrl}/api/tables?dataset=${datasetId}`));
    } catch (error) {
      console.error('Error loading tables:', error);
    }
    if (!sameId(shownDatasetRef.current, datasetId)) return;
    if (nextTables) {
      setTables(nextTables);
      // A shared or reloaded address opens the explorer on its table, view, column and filters when they still fit.
      const restored = resolveExplorerState(
        decodeExplorerState(searchParamsRef.current),
        nextTables,
        rememberedView(),
      );
      if (restored) {
        // The address already says this; keeping it while the modal opens means it is never cleared meanwhile.
        setAddressState(restored);
        openPackageExplorer({
          tableId: restored.tableId,
          state: restored,
          isCurrent: () => sameId(shownDatasetRef.current, datasetId),
        });
      }
      setAddressRead(true);
    } else {
      // The address keeps any table it names, for the retry below or a reload.
      setTables([]);
      setTablesError('Tables could not be loaded. Please retry.');
    }
    setTablesLoading(false);
  }, []);

  const refreshTables = useCallback(async () => {
    console.log('refreshing tables');
    const datasetId = currentDatasetId;
    setTablesLoading(true);
    setTablesError('');
    let nextTables = null;
    try {
      nextTables = normalizeTableList(await fetchData(`${config.baseUrl}/api/tables?dataset=${datasetId}`));
    } catch (error) {
      console.error('Error refreshing tables:', error);
    }
    if (!sameId(shownDatasetRef.current, datasetId)) return;
    if (nextTables) setTables(nextTables);
    else setTablesError('Tables could not be refreshed. Please retry.');
    setTablesLoading(false);
  }, [currentDatasetId]);

  // Reloads the tables (after agent changes, or Retry). While the first load has not succeeded it loads again
  // from scratch, so a table named in the address still opens.
  const reloadTables = useCallback(() => (
    addressRead ? refreshTables() : loadTablesForDataset(currentDatasetId)
  ), [addressRead, refreshTables, loadTablesForDataset, currentDatasetId]);


  const handleVisualizeTreeClick = () => {
    // Bootstrap modal will be shown via data-bs-toggle
    // Tree data will be loaded by TreeVisualization component
  };



  // Load tables when dataset changes
  useEffect(() => {
    import('bootstrap/dist/js/bootstrap.bundle.min.js');
  }, []);

  useEffect(() => {
    shownDatasetRef.current = currentDatasetId ?? null;
    if (currentDatasetId) {
      loadTablesForDataset(currentDatasetId);
    } else {
      // Reset tables state when dataset is cleared
      setTables([]);
      setTablesLoading(false);
      setTablesError('');
    }
    return () => {
      shownDatasetRef.current = null;
    };
  }, [currentDatasetId, loadTablesForDataset]);

  useEffect(() => {
    searchParamsRef.current = searchParams;
  }, [searchParams]);

  // The address follows the explorer modal (replace, never push), so a view can be shared or reloaded;
  // closing the modal takes the table out of it again.
  // history.replaceState rather than router.replace: no request to the Next server (whose logs would
  // otherwise hold filter values), and Next keeps useSearchParams in step with it.
  useEffect(() => {
    if (!addressRead) return undefined;
    const timeout = window.setTimeout(() => {
      const current = window.location.search.replace(/^\?/, '');
      const next = explorerSearchString(current, addressState);
      if (next === current) return;
      window.history.replaceState(null, '', `${pathname}${next ? `?${next}` : ''}${window.location.hash}`);
    }, URL_UPDATE_DELAY_MS);
    return () => window.clearTimeout(timeout);
  }, [addressState, addressRead, pathname]);

  // Field descriptions, key badges, link health and linked records for the DwC-DP tables come from
  // the package model. Loaded only when such tables exist, and refetched (debounced) when they change.
  // Building it is heavy on the server, so only one request runs at a time: changes while it runs are
  // fetched once, after it settles, and a request in flight is never cancelled for a newer one.
  const publishedKey = useMemo(() => publishedTablesKey(tables), [tables]);
  const agentBusy = agentIsWorking(currentDataset);
  useEffect(() => {
    agentBusyRef.current = agentBusy;
  }, [agentBusy]);

  const fetchPackageModel = useCallback(function requestModel(datasetId, key, { force = false } = {}) {
    const request = modelRequestRef.current;
    if (!sameId(request.datasetId, datasetId)) {
      request.controller?.abort();
      Object.assign(request, { datasetId, inFlightKey: null, pendingKey: null, loadedKey: null, controller: null });
    }
    if (request.inFlightKey !== null) {
      if (request.inFlightKey !== key) request.pendingKey = key;
      return;
    }
    if (!force && request.loadedKey === key) return;
    const controller = new AbortController();
    Object.assign(request, { inFlightKey: key, pendingKey: null, controller });
    setPackageModelState((current) => (sameId(current.datasetId, datasetId)
      ? { ...current, loading: true, error: '' }
      : { datasetId, model: null, loading: true, error: '' }));
    fetch(`${config.baseUrl}/api/datasets/${datasetId}/package-explorer/`, {
      credentials: 'include',
      signal: controller.signal,
    })
      .then(async (response) => {
        const data = await response.json().catch(() => null);
        if (!response.ok || !data) {
          throw new Error(response.ok ? 'The package model could not be loaded.' : apiErrorMessage(data, response.status));
        }
        if (request.controller !== controller) return;
        request.loadedKey = key;
        setPackageModelState({ datasetId, model: data, loading: false, error: '' });
      })
      .catch((modelError) => {
        if (modelError.name === 'AbortError' || request.controller !== controller) return;
        // Without the model the explorer still works, only without schema information.
        console.warn('Package model could not be loaded:', modelError);
        setPackageModelState((current) => (sameId(current.datasetId, datasetId)
          ? { ...current, loading: false, error: modelError.message || 'The package model could not be loaded.' }
          : current));
      })
      .finally(() => {
        if (request.controller !== controller) return;
        const pending = request.pendingKey;
        Object.assign(request, { inFlightKey: null, pendingKey: null, controller: null });
        if (pending !== null && pending !== key) requestModel(datasetId, pending);
      });
  }, []);

  useEffect(() => {
    if (!currentDatasetId || !publishedKey) return undefined;
    const request = modelRequestRef.current;
    const loadedBefore = sameId(request.datasetId, currentDatasetId) && request.loadedKey !== null;
    const delay = !loadedBefore ? 0 : agentBusyRef.current ? PACKAGE_MODEL_BUSY_DELAY_MS : PACKAGE_MODEL_REFRESH_DELAY_MS;
    const timeout = window.setTimeout(() => fetchPackageModel(currentDatasetId, publishedKey), delay);
    return () => window.clearTimeout(timeout);
  }, [currentDatasetId, publishedKey, fetchPackageModel]);

  // Another dataset (or leaving the page) drops the request in flight.
  useEffect(() => () => {
    const request = modelRequestRef.current;
    request.controller?.abort();
    Object.assign(request, { datasetId: null, inFlightKey: null, pendingKey: null, loadedKey: null, controller: null });
  }, [currentDatasetId]);

  const reloadPackageModel = useCallback(() => {
    if (currentDatasetId) fetchPackageModel(currentDatasetId, publishedKey, { force: true });
  }, [currentDatasetId, publishedKey, fetchPackageModel]);
  const modelIsCurrent = sameId(packageModelState.datasetId, currentDatasetId);
  const sharedPackageModel = modelIsCurrent ? packageModelState.model : null;
  // "Ask ChatIPT" puts text into the chat box of the agent whose box is shown right now.
  const handleComposerAvailability = useCallback((agentId, available) => {
    setComposerAgentIds((ids) => {
      if (available) return ids.includes(agentId) ? ids : [...ids, agentId];
      return ids.includes(agentId) ? ids.filter((id) => id !== agentId) : ids;
    });
  }, []);
  const visibleAgents = currentDataset?.visible_agent_set;
  const askAgentId = useMemo(() => {
    const agents = Array.isArray(visibleAgents) ? visibleAgents : [];
    return [...agents].reverse().find((agent) => composerAgentIds.includes(agent.id))?.id ?? null;
  }, [visibleAgents, composerAgentIds]);
  const handleAsk = useCallback((text) => {
    if (askAgentId === null) return;
    setActiveAgentKey(askAgentId);
    setAskRequest({ id: `${Date.now()}-${Math.random()}`, agentId: askAgentId, text });
  }, [askAgentId]);
  const onAsk = askAgentId !== null ? handleAsk : undefined;

  useEffect(() => {
    if (currentDatasetId && datasetIsActivelyProcessing(currentDataset)
      && processingRefreshStartedRef.current !== currentDatasetId) {
      processingRefreshStartedRef.current = currentDatasetId;
      refreshDataset(currentDatasetId);
    }
  }, [currentDataset, currentDatasetId, refreshDataset]);

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
    setMetadataForm(metadataFormFromDataset(currentDataset, user));
    setIsEditingMetadata(false);
    setMetadataSaveError('');
    setMetadataSaveSuccess('');
  }, [currentDatasetId, currentDataset, user]);

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

  const handleCreatorChange = (index, field, value) => {
    setMetadataForm((prev) => ({
      ...prev,
      users: prev.users.map((person, personIndex) => (
        personIndex === index ? { ...person, [field]: value } : person
      )),
    }));
    setMetadataSaveError('');
    setMetadataSaveSuccess('');
  };

  const addCreator = () => {
    setMetadataForm((prev) => ({ ...prev, users: [...prev.users, emptyCreator()] }));
  };

  const removeCreator = (index) => {
    setMetadataForm((prev) => ({
      ...prev,
      users: prev.users.filter((_, personIndex) => personIndex !== index),
    }));
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

  const fallbackFileNameRaw = currentDataset?.user_files?.[0]?.filename || '';
  const fallbackFileName = fallbackFileNameRaw.replace(/\([^)]*\)/g, '').trim();

  // Prebuild mailto link for publishing to GBIF production (shown after sandbox publish)
  const productionPublishMailto = (() => {
    if (!currentDataset) return null;
    const subject = '(ChatIPT) Request to publish dataset to GBIF production';
    const mailtoTitle = currentDataset.title || fallbackFileName;
    const dwcDpLine = currentDataset.dwc_dp_url ? `\n- Darwin Core Data Package (DwC-DP): ${currentDataset.dwc_dp_url}` : '';
    const body = `Hello GBIF Norway Helpdesk,\n\nMy dataset is published to the GBIF sandbox. Please publish it to GBIF.\n\n- Dataset title: ${mailtoTitle}${dwcDpLine}\n- Darwin Core Archive (DwC-A): ${currentDataset.dwca_url}\n- GBIF sandbox dataset page: ${currentDataset.gbif_url}\n\nBest regards,`;
    return `mailto:helpdesk@gbif.no?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  })();

  const title = currentDataset.title || fallbackFileName;
  const createdAtDisplay = currentDataset.created_at
    ? new Date(currentDataset.created_at).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
    : 'date unknown';
  const datasetOwner = currentDataset.user_info || user;
  const datasetOwnerName = [datasetOwner?.first_name, datasetOwner?.last_name]
    .filter(Boolean)
    .join(' ')
    .trim() || datasetOwner?.email || datasetOwner?.orcid_id || 'Unknown';
  const eml = currentDataset.eml || {};
  const metadataRows = [
    { label: 'Licence', value: eml.license || 'CC BY 4.0' },
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
  const emlPeople = (people) => (Array.isArray(people) ? people : [])
    .map((person) => ({ ...person, email: publishableEmail(person?.email) }));
  const emlUsers = emlPeople(eml.users);
  const emlContacts = emlPeople(eml.contacts);
  const emlMetadataProviders = emlPeople(eml.metadata_providers);
  const taxonomicKeywords = Array.isArray(eml.taxonomic_keywords) ? eml.taxonomic_keywords : [];
  const hasGeographicBounds = eml.geographic_bounds && typeof eml.geographic_bounds === 'object';
  const knownEmlKeys = new Set([
    'license',
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
    'contacts',
    'metadata_providers',
    'taxonomic_keywords',
    'geographic_bounds',
  ]);
  const additionalEmlEntries = Object.entries(eml).filter(
    ([key, value]) => !knownEmlKeys.has(key) && value !== undefined && value !== null && String(value).trim() !== ''
  );
  const isAgentCurrentlyWorking = agentBusy;

  const resetMetadataFormToCurrentDataset = () => {
    setMetadataForm(metadataFormFromDataset(currentDataset, user));
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

    const coordinateFields = [
      ['geographic_west', 'West longitude', -180, 180],
      ['geographic_east', 'East longitude', -180, 180],
      ['geographic_north', 'North latitude', -90, 90],
      ['geographic_south', 'South latitude', -90, 90],
    ];
    const enteredCoordinateCount = coordinateFields.filter(([field]) => String(metadataForm[field]).trim() !== '').length;
    if (enteredCoordinateCount !== 0 && enteredCoordinateCount !== coordinateFields.length) {
      setMetadataSaveError('Geographic bounds must include west, east, north, and south values.');
      return;
    }
    const geographicBounds = {};
    for (const [field, label, minimum, maximum] of coordinateFields) {
      if (enteredCoordinateCount === 0) break;
      const value = Number(metadataForm[field]);
      if (!Number.isFinite(value) || value < minimum || value > maximum) {
        setMetadataSaveError(`${label} must be a number from ${minimum} to ${maximum}.`);
        return;
      }
      geographicBounds[field.replace('geographic_', '')] = value;
    }

    const creators = metadataForm.users
      .map((person) => Object.fromEntries(
        Object.entries(person).map(([key, value]) => [key, String(value || '').trim()])
      ))
      .filter((person) => Object.values(person).some(Boolean));
    const incompleteCreator = creators.find((person) => !person.first_name || !person.last_name);
    if (incompleteCreator) {
      setMetadataSaveError('Each dataset creator needs a first name and last name.');
      return;
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
        license: metadataForm.license,
        temporal_scope: metadataForm.temporal_scope.trim(),
        geographic_scope: metadataForm.geographic_scope.trim(),
        geographic_bounds: enteredCoordinateCount === 0 ? null : geographicBounds,
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
        users: creators,
      };
      if (creators.length > 0) {
        delete updatedEml.creators_unknown;
      }
      // A saved empty scope tells the agent it was cleared on purpose, so a blank
      // scope the dataset never had stays absent and can still be inferred.
      for (const key of ['temporal_scope', 'geographic_scope', 'taxonomic_scope', 'methodology']) {
        if (!updatedEml[key] && !(key in eml)) delete updatedEml[key];
      }

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
  const uploadedFiles = Array.isArray(currentDataset?.user_files) ? currentDataset.user_files : [];
  const datasetStatus = getDatasetStatus(currentDataset, gbifPaused);
  const statusMeta = getStatusMeta(datasetStatus);
  const normalizedStructureNotes = String(currentDataset.structure_notes || '').replace(/\\n/g, '\n');

  // Secondary actions live in one menu so the header keeps a single main button. The GBIF link, the
  // uploaded files and their notes are in the panel beside the chat.
  const moreActions = [
    currentDataset.can_visualize_tree && {
      label: 'View phylogenetic tree',
      item: <button type="button" className="dropdown-item" data-bs-toggle="modal" data-bs-target="#treeVisualizationModal" onClick={handleVisualizeTreeClick}><i className="bi bi-diagram-3 me-2" aria-hidden="true"></i>View phylogenetic tree</button>
    },
  ].filter(Boolean);

  return (
    <div className="container publication-workspace">
      <div className="row mx-auto p-4 no-bottom-margin no-bottom-padding no-left-padding">
        <div className="col-12 alerts-div">
          <div className="dataset-heading mb-3">
            <div className="d-flex flex-wrap align-items-center gap-2">
              <h1 className="h3 mb-0 me-1">{title || 'Untitled dataset'}</h1>
              <span className={`badge ${statusMeta.badgeClass}`}>
                <i className={`bi ${statusMeta.icon} me-1`} aria-hidden="true"></i>
                {statusMeta.label}
              </span>
              <div className="d-flex gap-2 ms-auto">
                <button
                  className="btn btn-primary btn-sm"
                  data-bs-toggle="modal"
                  data-bs-target="#datasetMetadataModal"
                  onClick={handleStartMetadataEdit}
                >
                  <i className="bi bi-pencil me-1" aria-hidden="true"></i>
                  Edit metadata
                </button>
                {moreActions.length > 0 && (
                  <div className="dropdown">
                    <button className="btn btn-outline-secondary btn-sm" data-bs-toggle="dropdown" aria-expanded="false" aria-label="More actions" title="More actions">
                      <i className="bi bi-three-dots" aria-hidden="true"></i>
                    </button>
                    <ul className="dropdown-menu dropdown-menu-end">
                      {moreActions.map((action) => <li key={action.label}>{action.item}</li>)}
                    </ul>
                  </div>
                )}
              </div>
            </div>
            <p className="small text-muted mb-0 mt-1">
              Started {createdAtDisplay}
              {currentDataset.user_info && !sameId(currentDataset.user_info.id, user?.id) && (
                <>
                  {' · Owner: '}{datasetOwnerName}
                  {datasetOwner?.orcid_id && (
                    <>{' · '}<a href={`https://orcid.org/${datasetOwner.orcid_id}`} target="_blank" rel="noopener noreferrer" className="text-muted">{datasetOwner.orcid_id}</a></>
                  )}
                  {datasetOwner?.institution && ` · ${datasetOwner.institution}`}
                </>
              )}
            </p>
          </div>
          {currentDataset.description && (
            <p className="mb-3">{currentDataset.description}</p>
          )}
        </div>
      </div>
      <div className="row mx-auto px-4 pb-4 pt-2 no-left-padding">
        <div className="col-12 col-lg-8">
          {Array.isArray(currentDataset.visible_agent_set) && currentDataset.visible_agent_set.length > 0 ? (
            <Accordion activeKey={activeAgentKey} onSelect={(key) => setActiveAgentKey(key)}>
              {currentDataset.visible_agent_set.map(agent => (
                <Agent
                  key={agent.id}
                  agent={agent}
                  refreshDataset={() => refreshDataset(currentDatasetId)}
                  currentDatasetId={currentDatasetId}
                  refreshTables={reloadTables}
                  showMetadataEditLink={METADATA_EDIT_TASKS.has(agent.task.name)}
                  onEditMetadata={handleStartMetadataEdit}
                  askRequest={askRequest?.agentId === agent.id ? askRequest : null}
                  onComposerAvailabilityChange={handleComposerAvailability}
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
          {datasetIsActivelyProcessing(currentDataset) && (
            <div className="message user-input-loading">
              {gbifPaused ? (
                <div className="d-flex align-items-center gap-2">
                  <i className="bi bi-pause-circle" aria-hidden="true"></i>
                  <strong>Paused until GBIF is working again</strong>
                </div>
              ) : (
                <div className="d-flex align-items-center">
                  <strong>Working...</strong>
                  <div className="spinner-border ms-auto" role="status" aria-hidden="true"></div>
                </div>
              )}
            </div>
          )}
        </div>
        <div className="col-12 col-lg-4 mt-4 mt-lg-0">
          <DatasetPanel
            dataset={currentDataset}
            status={datasetStatus}
            tables={tables}
            tablesLoading={tablesLoading}
            tablesError={tablesError}
            onRetryTables={reloadTables}
            productionPublishMailto={currentDataset.published_at ? productionPublishMailto : ''}
          />
        </div>
      </div>

      <div className="row">
        <div className="footer"><hr />Something not working? Please send me an email with feedback: <a href="mailto:rukayasj@uio.no" target="_blank">rukayasj@uio.no</a></div>
      </div>

      {/* Dataset Metadata Modal */}
      <PackageExplorer
        datasetId={currentDatasetId}
        publishedUrl={currentDataset?.published_at ? currentDataset.gbif_url || null : null}
        onAsk={onAsk}
        model={sharedPackageModel}
        modelLoading={modelIsCurrent && packageModelState.loading}
        modelError={modelIsCurrent ? packageModelState.error : ''}
        onReloadModel={reloadPackageModel}
        sourceTables={tables}
        onExplorerStateChange={setAddressState}
      />

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
                  <label className="form-label">Main table of the GBIF archive</label>
                  <div>{currentDataset.dwc_core || 'Not set'}</div>
                </div>
                {currentDataset.dwc_dp_url && (
                  <div className="col-md-12">
                    <label className="form-label">Darwin Core Data Package</label>
                    <div><a href={currentDataset.dwc_dp_url}>Download DwC-DP</a></div>
                  </div>
                )}
              </div>

              <hr />
              <h6 className="mb-3">EML Metadata</h6>

              {isEditingMetadata ? (
                <div className="row g-3 mb-3">
                  <div className="col-md-6">
                    <label className="form-label">Licence</label>
                    <select
                      className="form-select"
                      name="license"
                      value={metadataForm.license}
                      onChange={handleMetadataInputChange}
                      disabled={isSavingMetadata || isAgentCurrentlyWorking}
                    >
                      <option value="CC BY 4.0">CC BY 4.0</option>
                      <option value="CC0 1.0">CC0 1.0</option>
                      <option value="CC BY-NC 4.0">CC BY-NC 4.0</option>
                    </select>
                    <div className="form-text">The default is CC BY 4.0.</div>
                  </div>
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
                    <label className="form-label">Geographic Bounds</label>
                    <div className="row g-2">
                      {[
                        ['geographic_west', 'West longitude'],
                        ['geographic_east', 'East longitude'],
                        ['geographic_north', 'North latitude'],
                        ['geographic_south', 'South latitude'],
                      ].map(([name, label]) => (
                        <div className="col-md-3" key={name}>
                          <input
                            type="number"
                            step="any"
                            className="form-control"
                            aria-label={label}
                            placeholder={label}
                            name={name}
                            value={metadataForm[name]}
                            onChange={handleMetadataInputChange}
                            disabled={isSavingMetadata || isAgentCurrentlyWorking}
                          />
                        </div>
                      ))}
                    </div>
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
                  <div className="col-12">
                    <div className="d-flex align-items-center justify-content-between mb-2">
                      <div>
                        <label className="form-label mb-0">Dataset Creators</label>
                        <div className="form-text mt-0">
                          {eml.creators_unknown && !(eml.users || []).length
                            ? 'Creators are recorded as unknown. Add creators only if they are known.'
                            : 'Initially filled from your ORCID profile when no creator list has been saved.'}
                        </div>
                      </div>
                      <button
                        type="button"
                        className="btn btn-outline-primary btn-sm"
                        onClick={addCreator}
                        disabled={isSavingMetadata || isAgentCurrentlyWorking}
                      >
                        Add creator
                      </button>
                    </div>
                    {metadataForm.users.map((person, index) => (
                      <div className="border rounded p-3 mb-2" key={`creator-${index}`}>
                        <div className="row g-2">
                          <div className="col-md-6">
                            <input
                              type="text"
                              className="form-control"
                              aria-label={`Creator ${index + 1} first name`}
                              placeholder="First name"
                              value={person.first_name}
                              onChange={(event) => handleCreatorChange(index, 'first_name', event.target.value)}
                              disabled={isSavingMetadata || isAgentCurrentlyWorking}
                            />
                          </div>
                          <div className="col-md-6">
                            <input
                              type="text"
                              className="form-control"
                              aria-label={`Creator ${index + 1} last name`}
                              placeholder="Last name"
                              value={person.last_name}
                              onChange={(event) => handleCreatorChange(index, 'last_name', event.target.value)}
                              disabled={isSavingMetadata || isAgentCurrentlyWorking}
                            />
                          </div>
                          <div className="col-md-6">
                            <input
                              type="email"
                              className="form-control"
                              aria-label={`Creator ${index + 1} email`}
                              placeholder="Email (optional)"
                              value={person.email}
                              onChange={(event) => handleCreatorChange(index, 'email', event.target.value)}
                              disabled={isSavingMetadata || isAgentCurrentlyWorking}
                            />
                          </div>
                          <div className="col-md-5">
                            <input
                              type="text"
                              className="form-control"
                              aria-label={`Creator ${index + 1} ORCID`}
                              placeholder="ORCID"
                              value={person.orcid}
                              onChange={(event) => handleCreatorChange(index, 'orcid', event.target.value)}
                              disabled={isSavingMetadata || isAgentCurrentlyWorking}
                            />
                          </div>
                          <div className="col-md-1 d-flex justify-content-end">
                            <button
                              type="button"
                              className="btn btn-outline-danger"
                              aria-label={`Remove creator ${index + 1}`}
                              onClick={() => removeCreator(index)}
                              disabled={isSavingMetadata || isAgentCurrentlyWorking}
                            >
                              <i className="bi bi-trash" aria-hidden="true"></i>
                            </button>
                          </div>
                        </div>
                      </div>
                    ))}
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

              {[
                ['Source Contacts', emlContacts],
                ['Metadata Providers', emlMetadataProviders],
              ].map(([heading, people]) => people.length > 0 && (
                <section className="mb-3" key={heading}>
                  <h6 className="mb-2">{heading}</h6>
                  <ul className="list-group">
                    {people.map((person, index) => {
                      const personName = [person.first_name, person.last_name].filter(Boolean).join(' ').trim() || 'Unnamed';
                      return (
                        <li className="list-group-item" key={`${personName}-${index}`}>
                          <div className="fw-semibold">{personName}</div>
                          {person.email && <small className="text-muted d-block">{person.email}</small>}
                          {person.orcid && (
                            <a
                              href={`https://orcid.org/${String(person.orcid).replace(/^https?:\/\/orcid\.org\//, '')}`}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-decoration-none"
                            >
                              {person.orcid}
                            </a>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                </section>
              ))}

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
      {(currentDataset.structure_notes || uploadedFiles.length > 0) && (
        <div className="modal fade" id="structureNotesModal" tabIndex="-1" aria-labelledby="structureNotesModalLabel" aria-hidden="true">
          <div className="modal-dialog modal-lg">
            <div className="modal-content">
              <div className="modal-header">
                <h5 className="modal-title" id="structureNotesModalLabel">
                  <i className="bi bi-info-circle me-2"></i>
                  How ChatIPT organised your data
                </h5>
                <button type="button" className="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
              </div>
              <div className="modal-body">
                <p className="text-muted">
                  Source files, transformation details, and data provenance for this dataset.
                </p>

                <h6 className="mb-2">Originally uploaded files</h6>
                {uploadedFiles.length > 0 ? (
                  <ul className="mb-3">
                    {uploadedFiles.map((file, index) => (
                      <li key={file.id || `${file.filename || 'file'}-${index}`}>
                        {file.file_url ? (
                          <a href={file.file_url} target="_blank" rel="noopener noreferrer">
                            {file.filename || 'Unnamed file'}
                          </a>
                        ) : (file.filename || 'Unnamed file')}
                        {file.file_type && <span className="text-muted ms-2">({file.file_type})</span>}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-muted">No uploaded files found for this dataset.</p>
                )}

                {currentDataset.package_ready && (
                  <div className="card bg-body-tertiary mb-3">
                    <div className="card-body">
                      <h6 className="card-title">Summary</h6>
                      <dl className="row small mb-0">
                        <dt className="col-sm-5">Validation</dt>
                        <dd className="col-sm-7 mb-0">
                          Passed
                          {Array.isArray(currentDataset.dwc_dp_validation?.warnings)
                            && currentDataset.dwc_dp_validation.warnings.length > 0
                            ? ` with ${pluralize(currentDataset.dwc_dp_validation.warnings.length, 'advisory warning')}`
                            : ''}
                        </dd>
                      </dl>
                    </div>
                  </div>
                )}

                {Array.isArray(currentDataset.dwc_dp_validation?.warnings)
                  && currentDataset.dwc_dp_validation.warnings.length > 0 && (
                  <div className="alert alert-warning">
                    <h6>Warnings and assumptions</h6>
                    <ul className="mb-0">
                      {currentDataset.dwc_dp_validation.warnings.map((warning, index) => (
                        <li key={`${warning}-${index}`}>{warning}</li>
                      ))}
                    </ul>
                  </div>
                )}

                {normalizedStructureNotes && (
                  <Accordion>
                    <Accordion.Item eventKey="structure-notes">
                      <Accordion.Header>Detailed structure notes</Accordion.Header>
                      <Accordion.Body>
                        <div style={{ whiteSpace: 'pre-wrap' }}>
                          {normalizedStructureNotes}
                        </div>
                      </Accordion.Body>
                    </Accordion.Item>
                  </Accordion>
                )}
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
