import React from 'react';
import { useState } from 'react';
import { useDropzone } from 'react-dropzone';
import config from '../config.js';
import { getCsrfToken } from '../utils/csrf.js';

const FileDrop = ({ onFileAccepted, onError }) => {
  const [loading, setLoading] = useState(false);  

  const onDrop = async (acceptedFiles) => {
    console.log('file dropped');
    setLoading(true);
    const formData = new FormData();
    formData.append('file', acceptedFiles[0]);

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
      if(err.message == "Failed to fetch") { 
        onError("The app is undergoing maintenance, please try again later. If you'd like to be informed when it's back up, send an email to rukayasj@uio.no.");
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
