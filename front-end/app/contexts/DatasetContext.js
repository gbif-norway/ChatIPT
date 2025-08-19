'use client'

import React, { createContext, useContext, useState, useCallback, useEffect } from 'react';
import config from '../config.js';
import { getCsrfToken } from '../utils/csrf.js';

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

  // Helper function to fetch data with timeout and retry logic
  const fetchData = async (url, options = {}) => {
    const { timeout = 90000, retries = 3 } = options; // 90 second timeout, 3 retries
    
    for (let attempt = 0; attempt <= retries; attempt++) {
      try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), timeout);
        
        const response = await fetch(url, {
          credentials: 'include',
          signal: controller.signal,
          ...options
        });
        
        clearTimeout(timeoutId);
        
        if (!response.ok) {
          throw new Error(`HTTP error! status: ${response.status}`);
        }
        return response.json();
      } catch (error) {
        console.log(`Fetch attempt ${attempt + 1} failed:`, error.message);
        
        // If this is the last attempt, or if it's not a network error, throw
        if (attempt === retries || (!error.name?.includes('Abort') && !error.message?.includes('fetch'))) {
          throw error;
        }
        
        // Wait with exponential backoff before retrying
        const delay = Math.min(1000 * Math.pow(2, attempt), 10000); // Max 10 seconds
        console.log(`Retrying in ${delay}ms...`);
        await new Promise(resolve => setTimeout(resolve, delay));
      }
    }
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
    const timestamp = new Date().toISOString();
    console.log(`[${timestamp}] 🔄 refreshDataset called for dataset ${datasetId}, currentDatasetId is ${currentDatasetId}`);
    
    if (!datasetId || datasetId !== currentDatasetId) {
      console.log(`[${timestamp}] ⏭️ Skipping refresh for dataset ${datasetId} - not the current dataset`);
      return;
    }

    try {
      console.log(`[${timestamp}] 📡 Refreshing dataset ${datasetId} - making API call...`);
      const refreshedDataset = await fetchData(`${config.baseUrl}/api/datasets/${datasetId}/refresh`);
      
      console.log(`[${timestamp}] ✅ Got refreshed dataset:`, {
        id: refreshedDataset.id,
        published_at: refreshedDataset.published_at,
        rejected_at: refreshedDataset.rejected_at,
        agent_count: refreshedDataset.visible_agent_set?.length || 0,
        last_agent_completed: refreshedDataset.visible_agent_set?.at(-1)?.completed_at,
        last_message_role: refreshedDataset.visible_agent_set?.at(-1)?.message_set?.at(-1)?.role,
        message_count: refreshedDataset.visible_agent_set?.at(-1)?.message_set?.length || 0
      });
      
      // Only update if this is still the current dataset
      if (datasetId === currentDatasetId) {
        console.log(`[${timestamp}] 💾 Updating dataset state in context`);
        setDatasets(prev => new Map(prev).set(datasetId, refreshedDataset));
        
        // Continue the refresh loop only if this is still the current dataset
        if (datasetId === currentDatasetId) {
          // If the dataset is published, don't do any more
          if (refreshedDataset.published_at != null && 
              refreshedDataset.visible_agent_set && 
              refreshedDataset.visible_agent_set.length > 0 && 
              refreshedDataset.visible_agent_set.at(-1).completed_at != null) {
            console.log(`[${timestamp}] 🎉 Dataset is published and complete - stopping refresh loop`);
            return;
          }
          
          // If the dataset is not suitable for publication, don't do any more
          if (refreshedDataset.rejected_at != null) {
            console.log(`[${timestamp}] ❌ Dataset was rejected - stopping refresh loop`);
            return;
          }
          
          // Check if we need to continue refreshing
          const hasAgents = refreshedDataset.visible_agent_set && refreshedDataset.visible_agent_set.length > 0;
          const lastAgent = hasAgents ? refreshedDataset.visible_agent_set.at(-1) : null;
          const hasMessages = lastAgent?.message_set && lastAgent.message_set.length > 0;
          const lastMessage = hasMessages ? lastAgent.message_set.at(-1) : null;
          
          console.log(`[${timestamp}] 🔍 Checking refresh conditions:`, {
            hasAgents,
            lastAgentCompleted: lastAgent?.completed_at,
            hasMessages,
            lastMessageRole: lastMessage?.role,
            shouldContinue: hasAgents && hasMessages && lastMessage?.role !== 'assistant'
          });
          
          // If the latest agent message is not an assistant message, we need to refresh again
          if (hasAgents && hasMessages && lastMessage.role !== 'assistant') {
            console.log(`[${timestamp}] 🔄 Need to continue refreshing - last message role: ${lastMessage.role}`);
            // Increased delay to reduce server load and give processing more time
            await new Promise(resolve => setTimeout(resolve, 2000));
            console.log(`[${timestamp}] ⏰ Finished waiting, scheduling next refresh...`);
            if (datasetId === currentDatasetId) {
              refreshDataset(datasetId);
            } else {
              console.log(`[${timestamp}] 🛑 Dataset changed while waiting, aborting refresh`);
            }
          } else {
            console.log(`[${timestamp}] ✅ Refresh cycle complete - last message is assistant or no messages`);
          }
        } else {
          console.log(`[${timestamp}] 🛑 Dataset changed during refresh, not continuing loop`);
        }
      } else {
        console.log(`[${timestamp}] 🛑 Dataset changed during API call, discarding result`);
      }
    } catch (error) {
      console.error(`[${timestamp}] ❌ Error refreshing dataset:`, error);
      // Only set error if it's not a temporary network issue
      if (!error.message?.includes('fetch') && !error.message?.includes('network')) {
        console.log(`[${timestamp}] 🚨 Setting error state: ${error.message}`);
        setError(error.message);
      } else {
        console.log(`[${timestamp}] 🌐 Network error detected, will retry...`);
      }
      
      // If it's a network error and we're still processing, retry after delay
      const currentDataset = datasets.get(datasetId);
      if (currentDataset?.visible_agent_set?.length > 0) {
        const lastAgent = currentDataset.visible_agent_set.at(-1);
        if (lastAgent?.completed_at === null) {
          console.log(`[${timestamp}] 🔄 Network error during processing, will retry in 5 seconds...`);
          setTimeout(() => {
            if (datasetId === currentDatasetId) {
              console.log(`[${timestamp}] 🔄 Retrying after network error...`);
              refreshDataset(datasetId);
            }
          }, 5000);
        }
      }
    }
  }, [currentDatasetId, datasets]);

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

  // Helper to re-fetch datasets list for dashboard
  const refetchDatasetsList = useCallback(async () => {
    try {
      const response = await fetch(`${config.baseUrl}/api/my-datasets/`, {
        credentials: 'include'
      });
      if (response.ok) {
        const datasetsList = await response.json();
        // This could be used by components that need to refresh the dashboard
        return datasetsList;
      } else {
        throw new Error('Failed to fetch datasets list');
      }
    } catch (error) {
      console.error('Error fetching datasets list:', error);
      throw error;
    }
  }, []);

  // Delete a dataset
  const deleteDataset = useCallback(async (datasetId) => {
    try {
      const csrfToken = await getCsrfToken();
      const headers = {};
      
      if (csrfToken) {
        headers['X-CSRFToken'] = csrfToken;
      }

      const response = await fetch(`${config.baseUrl}/api/datasets/${datasetId}/`, {
        method: 'DELETE',
        headers,
        credentials: 'include'
      });
      
      if (!response.ok) {
        throw new Error(`Failed to delete dataset: ${response.status}`);
      }
      
      // Remove from local state
      clearDataset(datasetId);
      
      return true;
    } catch (error) {
      console.error('Error deleting dataset:', error);
      throw error;
    }
  }, [clearDataset]);

  const value = {
    currentDataset,
    currentDatasetId,
    loading,
    error,
    loadDataset,
    refreshDataset,
    clearDataset,
    clearAllDatasets,
    refetchDatasetsList,
    deleteDataset,
    setCurrentDatasetId
  };

  return (
    <DatasetContext.Provider value={value}>
      {children}
    </DatasetContext.Provider>
  );
}; 