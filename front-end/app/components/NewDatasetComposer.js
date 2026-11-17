'use client';

import { useMemo, useState } from 'react';
import config from '../config.js';
import { getCsrfToken } from '../utils/csrf.js';
import { ALLOWED_FILE_EXTENSIONS, TABLE_FILE_EXTENSIONS, TREE_FILE_EXTENSIONS } from '../utils/uploadConstraints.js';
import { sendUpload, uploadSizeError } from '../utils/uploadSize.mjs';
import { useAuth } from '../contexts/AuthContext.js';
import { useGbifStatus } from '../contexts/GbifStatusContext.js';
import FileDropZone from './FileDropZone.js';

const buildDisplayName = (user) => {
  if (!user) {
    return 'there';
  }

  const parts = [user.first_name, user.last_name].filter(Boolean);

  if (parts.length > 0) {
    return parts.join(' ');
  }

  if (user.name) {
    return user.name;
  }

  if (user.orcid_name) {
    return user.orcid_name;
  }

  return 'there';
};

const NewDatasetComposer = ({ onDatasetCreated }) => {
  const { user } = useAuth();
  const { paused: gbifPaused } = useGbifStatus();
  const [userInput, setUserInput] = useState('');
  const [selectedFiles, setSelectedFiles] = useState([]);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState(null);

  const displayName = useMemo(() => buildDisplayName(user), [user]);
  // All files go in one request, so their total counts against the upload limit.
  const sizeError = useMemo(
    () => uploadSizeError(selectedFiles, config.maxUploadMB),
    [selectedFiles]
  );

  const handleInputKeyDown = (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  const handleSubmit = async (event) => {
    event.preventDefault();

    if (isSubmitting || sizeError || gbifPaused) {
      return;
    }

    if (!selectedFiles.length) {
      setError('Attach at least one data file, tree file, or manuscript PDF to start your dataset.');
      return;
    }

    const trimmedInput = userInput.trim();

    setIsSubmitting(true);
    setError(null);

    try {
      const csrfToken = await getCsrfToken();
      const headers = {};

      if (csrfToken) {
        headers['X-CSRFToken'] = csrfToken;
      }

      const formData = new FormData();
      selectedFiles.forEach((file) => {
        formData.append('files', file);
      });
      if (trimmedInput.length > 0) {
        formData.append('upload_context_message', trimmedInput);
      }

      const response = await sendUpload(`${config.baseUrl}/api/datasets/`, {
        method: 'POST',
        body: formData,
        headers,
        credentials: 'include'
      }, config.maxUploadMB);

      if (!response.ok) {
        let errorMessage = 'Failed to create dataset.';

        try {
          const errorData = await response.json();
          const errorValues = Object.values(errorData).flat();
          if (errorValues.length > 0) {
            errorMessage = errorValues.join(' ');
          }
        } catch (parseError) {
          if (response.statusText) {
            errorMessage = response.statusText;
          }
        }

        throw new Error(errorMessage);
      }

      const dataset = await response.json();
      const datasetId = dataset?.id;

      if (!datasetId) {
        throw new Error('Dataset created without an ID. Please try again.');
      }

      setUserInput('');
      setSelectedFiles([]);

      if (typeof onDatasetCreated === 'function') {
        onDatasetCreated(datasetId);
      }
    } catch (err) {
      console.error('Error creating dataset:', err);
      const defaultMessage = 'Something went wrong while creating your dataset. Please try again.';
      const message =
        typeof err?.message === 'string' && err.message.length > 0
          ? err.message
          : defaultMessage;
      setError(message);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="container p-4 upload-page">
      <header className="upload-heading"><h1>Start from your own data files</h1><p>Hello {displayName}. Add your data files or a manuscript, and ChatIPT will guide you from here.</p></header>

      <form className="card card-body gap-3" onSubmit={handleSubmit}>
        <FileDropZone
          files={selectedFiles}
          onChange={(files) => { setSelectedFiles(files); setError(null); }}
          onRejected={(message) => message && setError(message)}
          extensions={ALLOWED_FILE_EXTENSIONS}
          disabled={isSubmitting}
          limitMB={config.maxUploadMB}
          hint={`Data files, tree files or manuscript PDFs, up to ${config.maxUploadMB} MB in total`}
          details={<>
            <p className="mb-1">Spreadsheets and tables in any layout: {TABLE_FILE_EXTENSIONS.join(', ')}.</p>
            <p className="mb-1">Phylogenetic trees: {TREE_FILE_EXTENSIONS.join(', ')}.</p>
            <p className="mb-0">A manuscript PDF, to fill in the dataset description and pull out any data tables.</p>
          </>}
        />
        <label className="form-label mb-0">Note for ChatIPT (optional)
          <textarea
            rows={2}
            className="form-control mt-1"
            value={userInput}
            onChange={(e) => {
              setUserInput(e.target.value);
              if (error) {
                setError(null);
              }
            }}
            onKeyDown={handleInputKeyDown}
            placeholder="For example, what the data is or how it was collected"
            disabled={isSubmitting}
          />
        </label>

        {[error, sizeError].filter(Boolean).map((message) => (
          <div key={message} className="alert alert-danger mb-0" role="alert">
            {message}
          </div>
        ))}

        <button type="submit" className="btn btn-primary align-self-start" disabled={isSubmitting || !selectedFiles.length || Boolean(sizeError) || gbifPaused}>
          {isSubmitting ? (
            <>
              <span className="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>
              Uploading…
            </>
          ) : 'Start dataset'}
        </button>
      </form>
    </div>
  );
};

export default NewDatasetComposer;
