import Message from './Message';
import { useState, useEffect } from 'react';
import Accordion from 'react-bootstrap/Accordion';
import Badge from 'react-bootstrap/Badge';
import config from '../config.js';
import { getCsrfToken } from '../utils/csrf.js';

const Agent = ({ agent, refreshDataset, currentDatasetId }) => {
  const [userInput, setUserInput] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [loadingMessage, setLoadingMessage] = useState("Working...");

  useEffect(() => {
      const runAsyncEffect = async () => {
        console.log('running this for every agent I think?')
        console.log(agent);
        var last_message_role = null;
        if (agent.message_set && agent.message_set.length > 0) { last_message_role = agent.message_set.at(-1).role }
        if (agent.completed_at === null && last_message_role != 'user') { 
          setIsLoading(true);
          console.log('running this only once when component is loaded if completed_at is null for agent ' + agent.id);
          console.log(agent.completed_at);
          await refreshDataset();
          setIsLoading(false);
        } else {
          setIsLoading(false);
        }
    };
    runAsyncEffect();
  }, []); // Only run once when component mounts

  const formatTableIDs = (ids) => {
    if (!ids || !ids.length) return "[Deleted table(s)]";
    const prefix = ids.length === 1 ? "(Table ID " : "(Table IDs ";
    return prefix + ids.join(", ") + ")";
  }

  const handleUserInput = async (event) => {
    if (event.key === 'Enter') {
      console.log(agent.busy_thinking);
      event.preventDefault();
      setIsLoading(true);
      setLoadingMessage("Working...");

      try {
        const csrfToken = await getCsrfToken();
        const headers = { 'Content-Type': 'application/json' };
        
        if (csrfToken) {
          headers['X-CSRFToken'] = csrfToken;
        }

        await fetch(`${config.baseUrl}/api/messages/`, {
          method: 'POST',
          headers,
          body: JSON.stringify({ openai_obj: { content: userInput, role: 'user' }, agent: agent.id }),
          credentials: 'include' // Include credentials for authenticated requests
        });
        setUserInput("");
        await refreshDataset();
        setIsLoading(false);
      } catch (error) {
        console.error("Error:", error);
        setIsLoading(false);
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

          {/* Show spinner while loading, thinking, or when the assistant hasn't yet asked for a reply */}
          {(isLoading || agent.busy_thinking || (!assistantWaitingForReply && !agent.completed_at)) && (
            <div className="message user-input-loading">
              <div className="d-flex align-items-center">
                <strong>{loadingMessage}</strong>
                <div className="spinner-border ms-auto" role="status" aria-hidden="true"></div>
              </div>
            </div>
          )}

          {/* Only show the chat input when the assistant is explicitly waiting for a user reply */}
          {!agent.completed_at && !isLoading && !agent.busy_thinking && assistantWaitingForReply && (
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
