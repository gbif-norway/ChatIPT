import Message from './Message';
import { useState, useEffect } from 'react';
import Accordion from 'react-bootstrap/Accordion';
import Badge from 'react-bootstrap/Badge';
import config from '../config.js';
import { getCsrfToken } from '../utils/csrf.js';

const Agent = ({ agent, refreshDataset, currentDatasetId, refreshTables }) => {
  const [userInput, setUserInput] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [loadingMessage, setLoadingMessage] = useState("Working...");
  const [isUserSending, setIsUserSending] = useState(false);
  const [optimisticMessage, setOptimisticMessage] = useState(null);

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
      const hasMatchingUserMessage = agent.message_set.some(message => 
        message.role === 'user' && 
        message.openai_obj.content === optimisticMessage.openai_obj.content
      );
      
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
      setLoadingMessage("Working...");
      
      // Set timeout to change message after 4 seconds
      timeoutId = setTimeout(() => {
        if (lastAssistantHasGbifValidation) {
          setLoadingMessage("Still working... waiting for the GBIF validator (can take a long time)");
        } else {
          setLoadingMessage("Still working...");
        }
      }, 4000);
    } else {
      // Reset to initial message when not loading
      setLoadingMessage("Working...");
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

  const handleUserInput = async (event) => {
    if (event.key === 'Enter') {
      console.log(agent.busy_thinking);
      event.preventDefault();
      
      // Clear input immediately for instant feedback and prevent useEffect interference
      const messageContent = userInput;
      setUserInput("");
      setIsUserSending(true);
      setIsLoading(true);

      // Add optimistic user message immediately
      setOptimisticMessage({
        id: `optimistic-${Date.now()}`,
        role: 'user',
        openai_obj: { content: messageContent }
      });

      try {
        const csrfToken = await getCsrfToken();
        const headers = { 'Content-Type': 'application/json' };
        
        if (csrfToken) {
          headers['X-CSRFToken'] = csrfToken;
        }

        await fetch(`${config.baseUrl}/api/messages/`, {
          method: 'POST',
          headers,
          body: JSON.stringify({ openai_obj: { content: messageContent, role: 'user' }, agent: agent.id }),
          credentials: 'include' // Include credentials for authenticated requests
        });
        await refreshDataset();
        if (typeof refreshTables === 'function') {
          await refreshTables();
        }
        
        // Note: optimisticMessage will be cleared automatically by useEffect when server data arrives
        setIsLoading(false);
        setIsUserSending(false);
      } catch (error) {
        console.error("Error:", error);
        // On error, clear optimistic message since server call failed
        setOptimisticMessage(null);
        setIsLoading(false);
        setIsUserSending(false);
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
            <div className="input-group">
              <input type="text" className="form-control user-input" value={userInput} onKeyPress={handleUserInput} onChange={e => setUserInput(e.target.value)} placeholder="Message ChatIPT" />
              <div className="input-group-append"><span className="input-group-text"><i className="bi bi-arrow-up-circle"></i></span></div>
            </div>
          )}
        </Accordion.Body>
      </Accordion.Item>
    </>
  );
};

export default Agent;
