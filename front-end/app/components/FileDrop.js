import React from 'react';
import { useState } from 'react';
import { useDropzone } from 'react-dropzone';
import config from '../config.js';
import { getCsrfToken } from '../utils/csrf.js';

const FileDrop = ({ onFileAccepted, onError }) => {
  const [loading, setLoading] = useState(false);  

  const onDrop = async (acceptedFiles) => {
    console.log('file dropped');
    const file = acceptedFiles[0];
    setLoading(true);
    const formData = new FormData();
    formData.append('file', file);

    try {
      const csrfToken = await getCsrfToken();
      const headers = {};
      
      if (csrfToken) {
        headers['X-CSRFToken'] = csrfToken;
      }

      const response = await fetch(`${config.baseUrl}/api/datasets/`, {
        method: 'POST',
        body: formData,
        headers,
        credentials: 'include', // Include credentials for authenticated requests
      });
      if (!response.ok) {
        // Explicit handling for payload too large responses from proxy/server
        if (response.status === 413) {
          setLoading(false);
          onError(`Your file is too large for the server to accept${config.maxUploadMB ? ` (limit ≈ ${config.maxUploadMB} MB)` : ''}. Please upload a smaller file or contact support.`);
          return;
        }
        let errorMessage = 'Upload failed';
    
        try {
          const errorData = await response.json();
    
          // Check for non-field errors first
          if (errorData.non_field_errors && errorData.non_field_errors.length > 0) {
            errorMessage = errorData.non_field_errors.join(' ');
          } else {
            // Iterate through all errors and concatenate messages
            const fieldErrors = Object.values(errorData).flat();
            if (fieldErrors.length > 0) {
              errorMessage = fieldErrors.join(' ');
            }
          }
        } catch (parseError) {
          // If response is not JSON, use the status text
          errorMessage = response.statusText || errorMessage;
        }
    
        // Throw an error with the detailed message
        setLoading(false);
        throw new Error(errorMessage);
      }
      const data = await response.json();
      console.log('file accepted');
      console.log(data)
      onFileAccepted(data.id);
      setLoading(false);
    } catch (err) {
      console.log(err);
      setLoading(false);
      if(err.message === "Failed to fetch") { 
        // Network/CORS-level failure (common when proxies return 413 without CORS headers)
        onError(`Upload failed due to a network error. This may be due to a file size limit${config.maxUploadMB ? ` (≈ ${config.maxUploadMB} MB)` : ''}. If the problem persists, please try again later or contact support.`);
      } else { 
        onError(err.message);
      }
    }
  };

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    multiple: false, // Ensure only one file is processed at a time
  });

  return (
    <div>
      {loading ? (
        <div className="spinner"></div>
      ) : (
      <div {...getRootProps()} className="file-drop">
        <input {...getInputProps()} />
        {isDragActive ? (
          <p>Drop the file here ...</p>
        ) : (
          <p>Drag and drop a file here, or click to select a file</p>
        )}
      </div>
      )}
    </div>
  );
};

export default FileDrop;
