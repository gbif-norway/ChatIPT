'use client'

import React, { createContext, useContext, useState, useCallback, useEffect } from 'react';
import config from '../config.js';

const DatasetContext = createContext();

export const useDataset = () => {
  const context = useContext(DatasetContext);
  if (!context) {
    throw new Error('useDataset must be used within a DatasetProvider');
  }
  return context;
};

export const DatasetProvider = ({ children }) => {
  const [datasets, setDatasets] = useState(new Map()); // Map of datasetId -> dataset state
  const [currentDatasetId, setCurrentDatasetId] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Helper function to fetch data
  const fetchData = async (url) => {
    const response = await fetch(url, {
      credentials: 'include'
    });
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }
    return response.json();
  };

  // Load a dataset and store it in the context
  const loadDataset = useCallback(async (datasetId) => {
    console.log(`loadDataset called for dataset ${datasetId}`);
    if (!datasetId) return;
    
    try {
      setLoading(true);
      setError(null);
      
      console.log(`Loading dataset ${datasetId} with ${config.baseUrl}/api/datasets/${datasetId}/`);
      const dataset = await fetchData(`${config.baseUrl}/api/datasets/${datasetId}/`);
      
      // Check if the dataset has any agents - if not, use the refresh endpoint to initialize
      if (!dataset.visible_agent_set || dataset.visible_agent_set.length === 0) {
        console.log('No agents found, using refresh endpoint to initialize dataset');
        const refreshedDataset = await fetchData(`${config.baseUrl}/api/datasets/${datasetId}/refresh`);
        setDatasets(prev => new Map(prev).set(datasetId, refreshedDataset));
      } else {
        setDatasets(prev => new Map(prev).set(datasetId, dataset));
      }
      
      setCurrentDatasetId(datasetId);
    } catch (error) {
      console.error('Error loading dataset:', error);
      setError(error.message);
    } finally {
      setLoading(false);
    }
  }, []);

  // Refresh a specific dataset (only if it's the current one)
  const refreshDataset = useCallback(async (datasetId) => {
    console.log(`refreshDataset called for dataset ${datasetId}, currentDatasetId is ${currentDatasetId}`);
    if (!datasetId || datasetId !== currentDatasetId) {
      console.log(`Skipping refresh for dataset ${datasetId} - not the current dataset`);
      return;
    }

    try {
      console.log(`Refreshing dataset ${datasetId}`);
      const refreshedDataset = await fetchData(`${config.baseUrl}/api/datasets/${datasetId}/refresh`);
      
      // Only update if this is still the current dataset
      if (datasetId === currentDatasetId) {
        setDatasets(prev => new Map(prev).set(datasetId, refreshedDataset));
        
        // Continue the refresh loop only if this is still the current dataset
        if (datasetId === currentDatasetId) {
          // If the dataset is published, don't do any more
          if (refreshedDataset.published_at != null && 
              refreshedDataset.visible_agent_set && 
              refreshedDataset.visible_agent_set.length > 0 && 
              refreshedDataset.visible_agent_set.at(-1).completed_at != null) {
            return;
          }
          
          // If the dataset is not suitable for publication, don't do any more
          if (refreshedDataset.rejected_at != null) {
            return;
          }
          
          // If the latest agent message is not an assistant message, we need to refresh again
          if (refreshedDataset.visible_agent_set && 
              refreshedDataset.visible_agent_set.length > 0 && 
              refreshedDataset.visible_agent_set.at(-1).message_set && 
              refreshedDataset.visible_agent_set.at(-1).message_set.length > 0 && 
              refreshedDataset.visible_agent_set.at(-1).message_set.at(-1).role !== 'assistant') {
            console.log('about to start looping');
            console.log(refreshedDataset.visible_agent_set.at(-1).message_set.at(-1).role);
            await new Promise(resolve => setTimeout(resolve, 500));
            console.log('finished waiting, refreshing again');
            if (datasetId === currentDatasetId) {
              refreshDataset(datasetId);
            }
          }
        }
      }
    } catch (error) {
      console.error('Error refreshing dataset:', error);
      setError(error.message);
    }
  }, [currentDatasetId]);

  // Get the current dataset
  const currentDataset = datasets.get(currentDatasetId) || null;

  // Clear a dataset from the context
  const clearDataset = useCallback((datasetId) => {
    setDatasets(prev => {
      const newMap = new Map(prev);
      newMap.delete(datasetId);
      return newMap;
    });
    if (datasetId === currentDatasetId) {
      setCurrentDatasetId(null);
    }
  }, [currentDatasetId]);

  // Clear all datasets
  const clearAllDatasets = useCallback(() => {
    setDatasets(new Map());
    setCurrentDatasetId(null);
  }, []);

  const value = {
    currentDataset,
    currentDatasetId,
    loading,
    error,
    loadDataset,
    refreshDataset,
    clearDataset,
    clearAllDatasets,
    setCurrentDatasetId
  };

  return (
    <DatasetContext.Provider value={value}>
      {children}
    </DatasetContext.Provider>
  );
}; 