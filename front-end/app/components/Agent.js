import Message from './Message';
import { useState, useEffect } from 'react';
import Accordion from 'react-bootstrap/Accordion';
import Badge from 'react-bootstrap/Badge';
import config from '../config.js';

const Agent = ({ agent, refreshDataset }) => {
  const [userInput, setUserInput] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [loadingMessage, setLoadingMessage] = useState("Working...");

  useEffect(() => {
      const runAsyncEffect = async () => {
        console.log('running this for every agent I think?')
        console.log(agent);
        var last_message_role = null;
        if (agent.message_set.length > 0) { last_message_role = agent.message_set.at(-1).role }
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
  }, []);

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
        await fetch(`${config.baseUrl}/api/messages/`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ openai_obj: { content: userInput, role: 'user' }, agent: agent.id })
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
          {agent.message_set.map((message) => ( <Message key={message.id} message={message} /> ))}
          {(isLoading || agent.busy_thinking) && (
            <div className="message user-input-loading">
              <div className="d-flex align-items-center">
                <strong>{loadingMessage}</strong>
                <div className="spinner-border ms-auto" role="status" aria-hidden="true"></div>
              </div>
            </div>
          )}
          {!agent.completed_at && !isLoading && !agent.busy_thinking && (
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
