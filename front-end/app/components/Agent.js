import Message from './Message';
import { useState, useEffect, useRef } from 'react';
import Accordion from 'react-bootstrap/Accordion';
import Badge from 'react-bootstrap/Badge';
import OverlayTrigger from 'react-bootstrap/OverlayTrigger';
import Tooltip from 'react-bootstrap/Tooltip';
import config from '../config.js';
import { getCsrfToken } from '../utils/csrf.js';
import { getLoadingText } from '../utils/loading.js';
import {
  ALLOWED_FILE_EXTENSIONS,
  ACCEPT_INPUT_EXTENSIONS,
  isExtensionAllowed
} from '../utils/uploadConstraints.js';
import { useTheme } from '../contexts/ThemeContext.js';

const normalizeMessageContent = (content) => {
  if (!content) {
    return '';
  }

  if (Array.isArray(content)) {
    return content
      .map((entry) => {
        if (typeof entry === 'string') {
          return entry;
        }
        if (entry && typeof entry === 'object') {
          if (typeof entry.text === 'string') {
            return entry.text;
          }
          return JSON.stringify(entry);
        }
        return '';
      })
      .join('\n')
      .trim();
  }

  if (typeof content === 'object') {
    if (typeof content.text === 'string') {
      return content.text.trim();
    }
    return JSON.stringify(content);
  }

  return String(content).trim();
};

const getComparableMessageText = (content) => {
  const normalized = normalizeMessageContent(content);
  const [main] = normalized.split('\n\n[NOTE:');
  return main.trim();
};

const Agent = ({ agent, refreshDataset, currentDatasetId, refreshTables }) => {
  const [userInput, setUserInput] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [loadingMessage, setLoadingMessage] = useState(getLoadingText({ phase: 'working' }));
  const [isUserSending, setIsUserSending] = useState(false);
  const [optimisticMessage, setOptimisticMessage] = useState(null);
  const [selectedFiles, setSelectedFiles] = useState([]);
  const [uploadError, setUploadError] = useState(null);
  const fileInputRef = useRef(null);
  const { isDark } = useTheme();

  useEffect(() => {
    const runAsyncEffect = async () => {
      const timestamp = new Date().toISOString();
      console.log(`[${timestamp}] 🤖 Agent ${agent.id} mount refresh`);

      const last_message_role = agent.message_set?.length > 0 ? agent.message_set.at(-1).role : null;
      const hasToolCalls = agent.message_set?.length > 0 ? Array.isArray(agent.message_set.at(-1)?.openai_obj?.tool_calls) && agent.message_set.at(-1).openai_obj.tool_calls.length > 0 : false;

      // Kick the dataset refresh loop when the agent is in progress or tool calls are pending
      if (agent.completed_at === null && (agent.busy_thinking || hasToolCalls || last_message_role !== 'assistant')) {
        setIsLoading(true);
        try {
          await refreshDataset();
          if (typeof refreshTables === 'function') {
            await refreshTables();
          }
        } catch (error) {
          console.error(`[${timestamp}] ❌ Error in agent refresh:`, error);
        } finally {
          setIsLoading(false);
        }
      } else {
        setIsLoading(false);
      }
    };
    runAsyncEffect();
  }, []); // run once when component mounts

  // Track the count of Python tool result messages to detect new ones
  const lastPythonToolResultCountRef = useRef(0);

  // Log when agent data changes (this will help us see if updates are coming through)
  useEffect(() => {
    const timestamp = new Date().toISOString();
    console.log(`[${timestamp}] 🔄 Agent ${agent.id} data updated:`, {
      completed_at: agent.completed_at,
      message_count: agent.message_set?.length || 0,
      last_message_role: agent.message_set?.at(-1)?.role || 'none',
      busy_thinking: agent.busy_thinking,
      isLoading: isLoading
    });
    
    // Clear optimistic message if the server data now contains a user message with the same content
    if (optimisticMessage && agent.message_set?.length > 0) {
      const optimisticComparable = getComparableMessageText(optimisticMessage.openai_obj?.content);

      const hasMatchingUserMessage = agent.message_set.some((message) => {
        if (message.role !== 'user') {
          return false;
        }
        const comparable = getComparableMessageText(message.openai_obj?.content);
        return comparable === optimisticComparable && comparable.length > 0;
      });
      
      if (hasMatchingUserMessage) {
        console.log(`[${timestamp}] ✅ Found matching user message in server data - clearing optimistic message`);
        setOptimisticMessage(null);
      }
    }
    
    // If agent is completed or has new assistant messages, clear loading
    // BUT don't interfere if user is currently sending a message
    if (!isUserSending && (agent.completed_at !== null || 
        (agent.message_set?.length > 0 && agent.message_set.at(-1).role === 'assistant' && !agent.busy_thinking))) {
      if (isLoading) {
        console.log(`[${timestamp}] ✅ Agent ${agent.id} appears complete - clearing loading state`);
        setIsLoading(false);
      }
    }
  }, [agent.completed_at, agent.message_set, agent.busy_thinking, isLoading, isUserSending, optimisticMessage]);

  // Watch for new Python tool results and refresh tables when they arrive
  useEffect(() => {
    if (!agent.message_set || typeof refreshTables !== 'function') {
      return;
    }

    // Count tool messages that are results of Python calls
    // We identify Python tool results by checking if there's a preceding assistant message 
    // with a tool_call that matches this tool result's tool_call_id
    const pythonToolCallIds = new Set();
    agent.message_set.forEach((message) => {
      if (message.role === 'assistant' && Array.isArray(message.openai_obj?.tool_calls)) {
        message.openai_obj.tool_calls.forEach((tc) => {
          if (tc.function?.name === 'Python') {
            pythonToolCallIds.add(tc.id);
          }
        });
      }
    });

    const pythonToolResultCount = agent.message_set.filter((message) => {
      return message.role === 'tool' && pythonToolCallIds.has(message.openai_obj?.tool_call_id);
    }).length;

    const timestamp = new Date().toISOString();
    
    // If we have more Python tool results than before, refresh the tables
    if (pythonToolResultCount > lastPythonToolResultCountRef.current) {
      console.log(`[${timestamp}] 🐍 New Python tool result detected (${lastPythonToolResultCountRef.current} -> ${pythonToolResultCount}) - refreshing tables`);
      lastPythonToolResultCountRef.current = pythonToolResultCount;
      refreshTables();
    } else if (pythonToolResultCount !== lastPythonToolResultCountRef.current) {
      // Update ref if count changed (e.g., reset)
      lastPythonToolResultCountRef.current = pythonToolResultCount;
    }
  }, [agent.message_set, refreshTables]);

  // Handle loading message timeout - change message after 4 seconds
  useEffect(() => {
    let timeoutId;
    
    // Determine if we're showing the loading spinner (same logic as in the render)
    const lastMessage = (agent.message_set && agent.message_set.length > 0) ? agent.message_set[agent.message_set.length - 1] : null;
    const assistantWaitingForReply = lastMessage && lastMessage.role === 'assistant' && (!lastMessage.openai_obj.tool_calls || lastMessage.openai_obj.tool_calls.length === 0);
    const lastAssistantHasGbifValidation = lastMessage && lastMessage.role === 'assistant' && Array.isArray(lastMessage.openai_obj?.tool_calls) && lastMessage.openai_obj.tool_calls.some(tc => tc.function?.name === 'ValidateDwCA');
    const showingLoader = isLoading || isUserSending || agent.busy_thinking || (!assistantWaitingForReply && !agent.completed_at);
    
    if (showingLoader) {
      // Reset to initial message when loading starts
      setLoadingMessage(getLoadingText({ phase: 'working' }));
      
      // Set timeout to change message after 4 seconds
      timeoutId = setTimeout(() => {
        const ctx = lastAssistantHasGbifValidation ? 'waiting for the GBIF validator' : null;
        setLoadingMessage(getLoadingText({ phase: 'still', context: ctx, long: !!ctx }));
      }, 4000);
    } else {
      // Reset to initial message when not loading
      setLoadingMessage(getLoadingText({ phase: 'working' }));
    }
    
    // Cleanup timeout on dependency change or unmount
    return () => {
      if (timeoutId) {
        clearTimeout(timeoutId);
      }
    };
  }, [isLoading, isUserSending, agent.busy_thinking, agent.message_set, agent.completed_at]);

  const formatTableIDs = (ids) => {
    if (!ids || !ids.length) return "[Deleted table(s)]";
    const prefix = ids.length === 1 ? "(Table ID " : "(Table IDs ";
    return prefix + ids.join(", ") + ")";
  }

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
      setUploadError(
        `Unsupported file type: ${rejectedNames.join(', ')}. Allowed types: ${ALLOWED_FILE_EXTENSIONS.join(', ')}`
      );
    } else {
      setUploadError(null);
    }
  };

  const removeSelectedFile = (id) => {
    setSelectedFiles((prev) => prev.filter((file) => file.id !== id));
  };

  const handleInputKeyDown = (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  const triggerFileDialog = () => {
    if (fileInputRef.current) {
      fileInputRef.current.click();
    }
  };

  const handleSubmit = async (event) => {
    event.preventDefault();
    if (isUserSending) {
      return;
    }

    const trimmedInput = userInput.trim();
    const filesToUpload = [...selectedFiles];

    if (trimmedInput.length === 0 && filesToUpload.length === 0) {
      setUploadError('Add a message or attach at least one file before sending.');
      return;
    }

    setIsUserSending(true);
    setIsLoading(true);
    setUploadError(null);

    const uploadedFileNames = [];

    try {
      if (filesToUpload.length > 0) {
        const csrfToken = await getCsrfToken();
        const headers = {};
        if (csrfToken) {
          headers['X-CSRFToken'] = csrfToken;
        }

        for (const { file } of filesToUpload) {
          const formData = new FormData();
          formData.append('dataset', currentDatasetId);
          formData.append('file', file);

          const response = await fetch(`${config.baseUrl}/api/user-files/`, {
            method: 'POST',
            body: formData,
            headers,
            credentials: 'include'
          });

          if (!response.ok) {
            if (response.status === 413) {
              throw new Error('The file is too large for the server to accept. Please reduce your data file size and try again.');
            }
            let errorMessage = 'Failed to upload file.';
            try {
              const errorData = await response.json();
              const errorValues = Object.values(errorData).flat();
              if (errorValues.length > 0) {
                errorMessage = errorValues.join(' ');
              }
            } catch (_) {
              // ignore JSON parse errors and use default message
            }
            throw new Error(errorMessage);
          }

          const uploaded = await response.json();
          uploadedFileNames.push(uploaded.filename || file.name);
        }

        await refreshDataset();
        if (typeof refreshTables === 'function') {
          await refreshTables();
        }
      }

      const messageContent =
        trimmedInput.length > 0
          ? trimmedInput
          : (uploadedFileNames.length > 0 ? `Uploaded files: ${uploadedFileNames.join(', ')}` : '');

      if (messageContent) {
        setOptimisticMessage({
          id: `optimistic-${Date.now()}`,
          role: 'user',
          openai_obj: { content: messageContent }
        });

        const csrfToken = await getCsrfToken();
        const headers = { 'Content-Type': 'application/json' };

        if (csrfToken) {
          headers['X-CSRFToken'] = csrfToken;
        }

        const response = await fetch(`${config.baseUrl}/api/messages/`, {
          method: 'POST',
          headers,
          body: JSON.stringify({ openai_obj: { content: messageContent, role: 'user' }, agent: agent.id }),
          credentials: 'include'
        });

        if (!response.ok) {
          throw new Error('Failed to send message.');
        }

        await refreshDataset();
        if (typeof refreshTables === 'function') {
          await refreshTables();
        }
      }

      setUserInput('');
      setSelectedFiles([]);
      resetFileInput();

      setIsLoading(false);
      setIsUserSending(false);
    } catch (error) {
      console.error("Error submitting user input:", error);
      setOptimisticMessage(null);
      setIsLoading(false);
      setIsUserSending(false);

      const defaultMessage = 'Something went wrong while sending your message. Please try again.';
      const message =
        typeof error?.message === 'string' && error.message.length > 0
          ? error.message
          : defaultMessage;

      const lowerMessage = message.toLowerCase();
      if (
        filesToUpload.length > 0 &&
        (lowerMessage === 'failed to fetch' || lowerMessage.includes('networkerror'))
      ) {
        setUploadError('Upload failed. Please reduce your data file size and try again.');
      } else {
        setUploadError(message || defaultMessage);
      }
    }
  };

  const renderGroupedMessages = (messages) => {
    const groupedComponents = [];
    let i = 0;
    
    while (i < messages.length) {
      const message = messages[i];
      
      // Handle assistant messages with python tool calls
      if (message.role === 'assistant' && message.openai_obj.tool_calls) {
        const python_calls = message.openai_obj.tool_calls.filter(
          tool_call => tool_call.function.name === 'Python'
        );
        
        if (python_calls.length > 0) {
          // Look ahead for corresponding tool result messages
          const toolResults = [];
          let j = i + 1;
          
          // Collect consecutive tool messages that correspond to the python calls
          while (j < messages.length && messages[j].role === 'tool') {
            toolResults.push(messages[j]);
            j++;
          }
          
          // Render python calls with their results
          python_calls.forEach((python_call, callIndex) => {
            const correspondingResult = toolResults[callIndex];
            
            groupedComponents.push(
              <Message 
                key={`grouped-${message.id}-${python_call.id}`} 
                message={{
                  ...message,
                  id: `grouped-${message.id}-${python_call.id}`,
                  openai_obj: {
                    ...message.openai_obj,
                    tool_calls: [python_call]
                  }
                }}
                toolResult={correspondingResult}
              />
            );
          });
          
          // Skip the processed tool result messages
          i = j;
          continue;
        }
      }
      
      // Handle regular messages (including standalone tool results)
      groupedComponents.push(<Message key={message.id} message={message} />);
      i++;
    }
    
    return groupedComponents;
  };

  // Determine if the assistant is waiting for a reply from the user
  const lastMessage = (agent.message_set && agent.message_set.length > 0) ? agent.message_set[agent.message_set.length - 1] : null;
  const assistantWaitingForReply = lastMessage && lastMessage.role === 'assistant' && (!lastMessage.openai_obj.tool_calls || lastMessage.openai_obj.tool_calls.length === 0);
  const composerClassName = [
    'chat-composer border rounded p-3 mt-3',
    isDark ? 'bg-dark border-secondary text-light' : 'bg-light'
  ].join(' ');
  const showSendTooltip = selectedFiles.length > 0 && !isUserSending;

  return (
    <>
      <Accordion.Item eventKey={agent.id}>
        <Accordion.Header>
          Task: {agent.task.name.replace(/^[-_]*(.)/, (_, c) => c.toUpperCase()).replace(/[-_]+(.)/g, (_, c) => ' ' + c.toUpperCase())}
          &nbsp;-&nbsp;<small>{formatTableIDs(agent.tables)}</small>
          {agent.completed_at != null && (
            <span className={`agent-id-${agent.id}-message`}>&nbsp;<Badge bg="secondary">complete <i className="bi-check-square"></i></Badge></span>
          )}
          &nbsp;
        </Accordion.Header>
        <Accordion.Body>
          {renderGroupedMessages(agent.message_set)}
          
          {/* Show optimistic user message immediately */}
          {optimisticMessage && (
            <Message key={optimisticMessage.id} message={optimisticMessage} />
          )}

          {/* Show spinner while loading, thinking, or when the assistant hasn't yet asked for a reply */}
          {(isLoading || isUserSending || agent.busy_thinking || (!assistantWaitingForReply && !agent.completed_at)) && (
            <div className="message user-input-loading">
              <div className="d-flex align-items-center">
                <strong>{loadingMessage}</strong>
                <div className="spinner-border ms-auto" role="status" aria-hidden="true"></div>
              </div>
            </div>
          )}

          {/* Only show the chat input when the assistant is explicitly waiting for a user reply */}
          {!agent.completed_at && !isLoading && !isUserSending && !agent.busy_thinking && assistantWaitingForReply && (
            <form className={composerClassName} onSubmit={handleSubmit}>
              <div className="d-flex flex-wrap align-items-center gap-2">
                <button
                  type="button"
                  className="btn btn-outline-secondary"
                  onClick={triggerFileDialog}
                  title="Add data files or phylogenetic tree files"
                  disabled={isUserSending}
                >
                  <i className="bi bi-paperclip" aria-hidden="true"></i>
                  <span className="visually-hidden">Attach files</span>
                </button>
                <div className="flex-grow-1">
                  <input
                    type="text"
                    className="form-control user-input"
                    value={userInput}
                    onKeyDown={handleInputKeyDown}
                    onChange={(e) => {
                      setUserInput(e.target.value);
                      if (uploadError) {
                        setUploadError(null);
                      }
                    }}
                    placeholder="Message ChatIPT"
                    disabled={isUserSending}
                    aria-label="Message ChatIPT"
                  />
                </div>
                <OverlayTrigger
                  placement="top"
                  overlay={
                    <Tooltip id={`send-tooltip-${agent.id}`}>
                      Click send to upload your selected files.
                    </Tooltip>
                  }
                  show={showSendTooltip}
                  trigger={[]}
                >
                  <button type="submit" className="btn btn-primary d-flex align-items-center gap-1" disabled={isUserSending}>
                    <i className="bi bi-send-fill" aria-hidden="true"></i>
                    <span className="d-none d-md-inline">Send</span>
                  </button>
                </OverlayTrigger>
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
                      >
                        <i className="bi bi-x-lg" aria-hidden="true"></i>
                      </button>
                    </span>
                  ))}
                </div>
              )}

              {uploadError && (
                <div className="text-danger small mt-3" role="alert">
                  {uploadError}
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
          )}
        </Accordion.Body>
      </Accordion.Item>
    </>
  );
};

export default Agent;
