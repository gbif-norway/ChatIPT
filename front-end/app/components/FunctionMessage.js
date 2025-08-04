import { useState } from 'react';
import Button from 'react-bootstrap/Button';
import Collapse from 'react-bootstrap/Collapse';
import { CodeBlock, dracula } from "react-code-blocks";


function FunctionMessage({ message_content, message_id, is_python, result_content = null, result_id = null }) {
  const [open, setOpen] = useState(false);
  
  let content = message_content
  if(typeof message_content === 'string') {
    if(message_content.replace(/[ \t\n\r]/gm,'').startsWith('{"code":"')) { 
      console.log('starts with code ' + message_id);
      content = JSON.parse(message_content);
      content = content['code']
    }
  }
  else {
    content = JSON.stringify(content)
  }

  let resultContent = null;
  if (result_content) {
    resultContent = result_content;
    if(typeof result_content === 'string') {
      if(result_content.replace(/[ \t\n\r]/gm,'').startsWith('{"code":"')) { 
        resultContent = JSON.parse(result_content);
        resultContent = resultContent['code']
      }
    }
    else {
      resultContent = JSON.stringify(result_content)
    }
  }

  // Determine button text and class
  let buttonText, buttonClass;
  if (is_python && result_content) {
    buttonText = "Show code + results";
    buttonClass = "code-and-results";
  } else if (is_python) {
    buttonText = "Show generated code";
    buttonClass = "code";
  } else {
    buttonText = "Show code results";
    buttonClass = "results results-only";
  }

  return (
    <>
      <div className='inner-message python'>
        <Button onClick={() => setOpen(!open)} aria-controls={`collapseFor${message_id}`} aria-expanded={open} className={buttonClass}>
          {buttonText}
        </Button>
      </div>
      <div className="inner-function-message">
        <Collapse in={open}>
          <div id={`collapseFor${message_id}`}>
            {is_python && (
              <div className="code-section">
                <h6 className="code-section-title">Generated Code:</h6>
                <CodeBlock text={content} language="python" theme={dracula} />
              </div>
            )}
            {result_content && (
              <div className="results-section">
                <h6 className="code-section-title">Results:</h6>
                <CodeBlock text={resultContent} language="python" theme={dracula} />
              </div>
            )}
            {!is_python && !result_content && (
              <CodeBlock text={content} language="python" theme={dracula} />
            )}
          </div>
        </Collapse>
      </div>
    </>
  );
}

export default FunctionMessage;
