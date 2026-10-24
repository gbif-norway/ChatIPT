'use client';

import { useMemo, useRef, useState } from 'react';
import config from '../config.js';
import { getCsrfToken } from '../utils/csrf.js';
import { ACCEPT_INPUT_EXTENSIONS, ALLOWED_FILE_EXTENSIONS, isExtensionAllowed } from '../utils/uploadConstraints.js';
import { formatFileSize, sendUpload, uploadSizeError } from '../utils/uploadSize.mjs';
import { useAuth } from '../contexts/AuthContext.js';
import { useTheme } from '../contexts/ThemeContext.js';
import { useGbifStatus } from '../contexts/GbifStatusContext.js';

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
  const { isDark } = useTheme();
  const { paused: gbifPaused } = useGbifStatus();
  const [userInput, setUserInput] = useState('');
  const [selectedFiles, setSelectedFiles] = useState([]);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const fileInputRef = useRef(null);

  const displayName = useMemo(() => buildDisplayName(user), [user]);
  // All files go in one request, so their total counts against the upload limit.
  const sizeError = useMemo(
    () => uploadSizeError(selectedFiles.map(({ file }) => file), config.maxUploadMB),
    [selectedFiles]
  );

  const resetFileInput = () => {
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  };

  const handleFilesSelected = (event) => {
    const files = Array.from(event.target.files || []);
    if (!files.length) {
      return;
    }

    const rejectedNames = [];
    const nextFiles = [...selectedFiles];

    files.forEach((file) => {
      if (!isExtensionAllowed(file.name)) {
        rejectedNames.push(file.name);
        return;
      }

      const alreadySelected = nextFiles.some(
        (item) => item.file.name === file.name && item.file.size === file.size
      );

      if (!alreadySelected) {
        nextFiles.push({
          id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
          file
        });
      }
    });

    setSelectedFiles(nextFiles);
    resetFileInput();

    if (rejectedNames.length > 0) {
      setError(
        `Unsupported file type: ${rejectedNames.join(', ')}. Allowed types: ${ALLOWED_FILE_EXTENSIONS.join(', ')}`
      );
    } else {
      setError(null);
    }
  };

  const removeSelectedFile = (id) => {
    setSelectedFiles((prev) => prev.filter((file) => file.id !== id));
  };

  const triggerFileDialog = () => {
    if (fileInputRef.current) {
      fileInputRef.current.click();
    }
  };

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
      selectedFiles.forEach(({ file }) => {
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
      resetFileInput();

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

  const composerClassName = [
    'chat-composer border rounded p-3',
    isDark ? 'bg-dark border-secondary text-light' : 'bg-light'
  ].join(' ');

  return (
    <div className="container p-4 upload-page">
      <div className="row justify-content-center">
        <div className="col-lg-9">
          <header className="upload-heading"><span className="eyebrow">A new beginning</span><h1>Let’s bring your data to life</h1><p>Hello {displayName}. Add your data files or a manuscript, and ChatIPT will guide you from here.</p></header>

          <form className={composerClassName} onSubmit={handleSubmit}>
            <div className="d-flex flex-wrap align-items-start gap-2">
              <button
                type="button"
                className="btn btn-outline-secondary"
                onClick={triggerFileDialog}
                title="Add data files, phylogenetic tree files, or manuscript PDFs"
                disabled={isSubmitting}
              >
                <i className="bi bi-paperclip" aria-hidden="true"></i>
                <span>Add files</span>
              </button>
              <div className="flex-grow-1">
                <textarea
                  rows={2}
                  className="form-control user-input"
                  value={userInput}
                  onChange={(e) => {
                    setUserInput(e.target.value);
                    if (error) {
                      setError(null);
                    }
                  }}
                  onKeyDown={handleInputKeyDown}
                  placeholder="Add a note for ChatIPT (optional)"
                  disabled={isSubmitting}
                  aria-label="Message ChatIPT"
                />
              </div>
              <button type="submit" className="btn btn-primary d-flex align-items-center gap-1" disabled={isSubmitting || Boolean(sizeError) || gbifPaused}>
                {isSubmitting ? (
                  <>
                    <span className="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>
                    <span>Uploading…</span>
                  </>
                ) : (
                  <>
                    <i className="bi bi-send-fill" aria-hidden="true"></i>
                    <span>Start dataset</span>
                  </>
                )}
              </button>
            </div>

            {selectedFiles.length > 0 && (
              <div className="d-flex flex-wrap gap-2 mt-3">
                {selectedFiles.map(({ id, file }) => (
                  <span
                    key={id}
                    className="badge rounded-pill text-bg-secondary d-flex align-items-center gap-2 py-2 px-3"
                  >
                    <i className="bi bi-file-earmark-arrow-up" aria-hidden="true"></i>
                    <span className="text-truncate" style={{ maxWidth: '200px' }}>{file.name}</span>
                    <span className="opacity-75">{formatFileSize(file.size)}</span>
                    <button
                      type="button"
                      className="btn btn-sm btn-outline-light border-0 text-white px-2 py-0"
                      onClick={() => removeSelectedFile(id)}
                      aria-label={`Remove ${file.name}`}
                      disabled={isSubmitting}
                    >
                      <i className="bi bi-x-lg" aria-hidden="true"></i>
                    </button>
                  </span>
                ))}
              </div>
            )}

            {[error, sizeError].filter(Boolean).map((message) => (
              <div key={message} className="text-danger small mt-3" role="alert">
                {message}
              </div>
            ))}

            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={ACCEPT_INPUT_EXTENSIONS}
              onChange={handleFilesSelected}
              className="d-none"
            />
            <div className="text-muted small mt-3">
              Tip: use the paperclip to add data files, tree files, or manuscript PDFs, up to {config.maxUploadMB} MB in total.
            </div>
          </form>
        </div>
      </div>
    </div>
  );
};

export default NewDatasetComposer;
