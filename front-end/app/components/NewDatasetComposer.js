'use client';

import { useMemo, useRef, useState } from 'react';
import config from '../config.js';
import { getCsrfToken } from '../utils/csrf.js';
import { ACCEPT_INPUT_EXTENSIONS, ALLOWED_FILE_EXTENSIONS, isExtensionAllowed } from '../utils/uploadConstraints.js';
import { useAuth } from '../contexts/AuthContext.js';
import { useTheme } from '../contexts/ThemeContext.js';

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
  const [userInput, setUserInput] = useState('');
  const [selectedFiles, setSelectedFiles] = useState([]);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const fileInputRef = useRef(null);

  const displayName = useMemo(() => buildDisplayName(user), [user]);

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

  const sendInitialMessage = async ({ agentId, messageContent, headers }) => {
    if (!agentId || !messageContent) {
      return;
    }

    const messageHeaders = {
      'Content-Type': 'application/json',
      ...headers
    };

    await fetch(`${config.baseUrl}/api/messages/`, {
      method: 'POST',
      headers: messageHeaders,
      credentials: 'include',
      body: JSON.stringify({
        agent: agentId,
        openai_obj: { role: 'user', content: messageContent }
      })
    });
  };

  const handleSubmit = async (event) => {
    event.preventDefault();

    if (isSubmitting) {
      return;
    }

    if (!selectedFiles.length) {
      setError('Attach at least one data file to start your dataset.');
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

      const response = await fetch(`${config.baseUrl}/api/datasets/`, {
        method: 'POST',
        body: formData,
        headers,
        credentials: 'include'
      });

      if (!response.ok) {
        if (response.status === 413) {
          throw new Error('Your file is too large for the server to accept. Please upload a smaller file or contact support.');
        }

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

      if (trimmedInput.length > 0) {
        const activeAgent = Array.isArray(dataset.visible_agent_set)
          ? dataset.visible_agent_set[dataset.visible_agent_set.length - 1]
          : null;

        try {
          await sendInitialMessage({
            agentId: activeAgent?.id,
            messageContent: trimmedInput,
            headers
          });
        } catch (messageError) {
          console.error('Failed to send initial message after dataset creation:', messageError);
          // Do not block navigation; allow dataset creation to succeed.
        }
      }

      setUserInput('');
      setSelectedFiles([]);
      resetFileInput();

      if (typeof onDatasetCreated === 'function') {
        onDatasetCreated(datasetId);
      }
    } catch (err) {
      console.error('Error creating dataset:', err);
      setError(err.message || 'Something went wrong while creating your dataset. Please try again.');
    } finally {
      setIsSubmitting(false);
    }
  };

  const composerClassName = [
    'chat-composer border rounded p-3',
    isDark ? 'bg-dark border-secondary text-light' : 'bg-light'
  ].join(' ');

  return (
    <div className="container p-4">
      <div className="row justify-content-center">
        <div className="col-lg-9">
          <div className="message assistant-message mb-4">
            <div className="inner-message">
              <strong>Hello {displayName}.</strong><br />
              Upload a data file and let's get started with a new dataset!
            </div>
          </div>

          <form className={composerClassName} onSubmit={handleSubmit}>
            <div className="d-flex flex-wrap align-items-center gap-2">
              <button
                type="button"
                className="btn btn-outline-secondary"
                onClick={triggerFileDialog}
                title="Add data files or phylogenetic tree files"
                disabled={isSubmitting}
              >
                <i className="bi bi-paperclip" aria-hidden="true"></i>
                <span className="visually-hidden">Attach files</span>
              </button>
              <div className="flex-grow-1">
                <input
                  type="text"
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
              <button type="submit" className="btn btn-primary d-flex align-items-center gap-1" disabled={isSubmitting}>
                {isSubmitting ? (
                  <>
                    <span className="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>
                    <span>Uploading…</span>
                  </>
                ) : (
                  <>
                    <i className="bi bi-send-fill" aria-hidden="true"></i>
                    <span className="d-none d-md-inline">Send</span>
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

            {error && (
              <div className="text-danger small mt-3" role="alert">
                {error}
              </div>
            )}

            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={ACCEPT_INPUT_EXTENSIONS}
              onChange={handleFilesSelected}
              className="d-none"
            />
            <div className="text-muted small mt-3">
              Tip: use the paperclip to add data files or phylogenetic tree files.
            </div>
          </form>
        </div>
      </div>
    </div>
  );
};

export default NewDatasetComposer;
