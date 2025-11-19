'use client'

import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import config from '../config.js';
import { getCsrfToken } from '../utils/csrf.js';

// Tree visualization CSS styles
const treeStyles = `
  .tree-visualization {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  .treeArea {
    display: flex;
    flex-direction: column;
    height: 100%;
    position: relative;
  }
  .tree-controls {
    border-bottom: 1px solid #ddd;
    flex: 0 0 auto;
    padding: 12px;
    background: white;
  }
  .tree-tree {
    padding: 24px 0;
    flex: 1 1 auto;
    overflow: auto;
    overscroll-behavior-x: none;
    width: 100%;
    max-width: 100%;
  }
  .gb-snack-bar {
    position: absolute;
    background: #333;
    color: white;
    padding: 8px 12px;
    border-radius: 4px;
    bottom: 0;
    left: 0;
    pointer-events: none;
    z-index: 2000;
    margin: 24px;
  }
  .tree-tree .gb-tree-list {
    list-style: none;
    margin: 0;
    padding: 0;
    position: relative;
    line-height: 1.15;
    padding-left: .25em;
  }
  .tree-tree .gb-tree-list::before {
    width: .25em;
    content: '';
    position: absolute;
    left: 0;
    top: 50%;
    border-top: 1px solid #333;
  }
  .gb-tree-node {
    display: flex;
    flex-direction: row;
    align-items: center;
    white-space: nowrap;
    position: relative;
  }
  .gb-tree-node .gb-tree-pipe {
    position: absolute;
    left: 0;
    border-top: 1px solid #333;
  }
  .gb-tree-node::after {
    content: '';
    position: absolute;
    left: 0;
    border-left: 1px solid #333;
  }
  .gb-tree-node:last-of-type::after {
    height: 50%;
    top: 0;
  }
  .gb-tree-node:first-of-type::after {
    height: 50%;
    bottom: 0;
  }
  .gb-tree-node:only-of-type::after {
    display: none;
  }
  .gb-tree-node:not(:first-of-type):not(:last-of-type)::after {
    height: 100%;
  }
  .gb-tree-color {
    width: .8em;
    height: .8em;
    padding: 0;
    border: 1px solid #333;
    border-radius: 50%;
    display: inline-block;
    background: white;
    cursor: pointer;
    z-index: 10;
    position: relative;
  }
  .gb-tree-color-click-area {
    width: 10px;
    height: 10px;
    display: block;
    left: -5px;
    top: -5px;
    position: relative;
  }
  .gb-tree-leaf-color {
    margin-right: -4px;
    background: white;
  }
  .gb-tree-content-node {
    margin: .5em 0;
    position: relative;
    cursor: pointer;
  }
  .gb-tree-content-node.gb-tree-leaf {
    background: #fafafa;
    border: 1px solid #333;
    padding: 0 .25em 0 .5em;
  }
  .gb-tree-content-node.gb-tree-leaf .gb-tree-color {
    margin-left: .5em;
  }
  .gb-tree-node-name {
    color: #333;
  }
  .gb-tree-color:hover + ol .gb-tree-leaf,
  .gb-tree-color:hover + .gb-tree-leaf {
    background: #cad2d3 !important;
    color: white;
  }
  .gb-tree-hover-title:hover {
    box-shadow: 0 0 2px 2px #0089ffab;
    z-index: 1000;
  }
  .gb-tree-subtree-placeholder {
    border-top: 2px solid #333;
  }
  .gb-tree-highlighted .gb-tree-color {
    background-color: currentColor;
  }
  /* Leaflet map container styles */
  .leaflet-container {
    height: 100%;
    width: 100%;
    z-index: 1;
  }
`;

// Clone tree structure (more efficient than JSON.parse/stringify)
const cloneTree = (node) => {
  const cloned = { ...node };
  if (node.children && node.children.length > 0) {
    cloned.children = node.children.map(child => cloneTree(child));
  }
  return cloned;
};

const TreeVisualization = ({ datasetId, onClose }) => {
  const [treeData, setTreeData] = useState(null);
  const [nodeIdMap, setNodeIdMap] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [highlighted, setHighlighted] = useState({});
  const [highlightedLeaf, setHighlightedLeaf] = useState(null);
  const [unmatchedScientificNames, setUnmatchedScientificNames] = useState([]);
  const [totalUniqueScientificNames, setTotalUniqueScientificNames] = useState(0);
  const [hasCoordinates, setHasCoordinates] = useState(false);
  const [hasScientificName, setHasScientificName] = useState(false);
  const mapRef = useRef(null);
  const mapInstanceRef = useRef(null);
  const mapContainerRef = useRef(null);
  const layerByNode = useRef(new Map());
  const nodeByKeyMap = useRef(new Map());
  const [leafletLoaded, setLeafletLoaded] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  // Inject CSS styles
  useEffect(() => {
    if (typeof document !== 'undefined') {
      const styleId = 'tree-visualization-styles';
      if (!document.getElementById(styleId)) {
        const style = document.createElement('style');
        style.id = styleId;
        style.textContent = treeStyles;
        document.head.appendChild(style);
      }
    }
  }, []);

  // Load Leaflet from CDN
  useEffect(() => {
    if (typeof window === 'undefined') return;
    
    if (window.L) {
      setLeafletLoaded(true);
      return;
    }

    const existingScript = document.querySelector('script[src*="leaflet"]');
    if (existingScript) {
      existingScript.addEventListener('load', () => {
        if (window.L) setLeafletLoaded(true);
      });
      return;
    }

    if (!document.querySelector('link[href*="leaflet.css"]')) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
      link.integrity = 'sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=';
      link.crossOrigin = '';
      document.head.appendChild(link);
    }

    const script = document.createElement('script');
    script.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
    script.crossOrigin = 'anonymous';
    script.onload = () => {
      const checkL = setInterval(() => {
        if (window.L) {
          clearInterval(checkL);
          setLeafletLoaded(true);
        }
      }, 50);
      
      setTimeout(() => {
        clearInterval(checkL);
        if (window.L) {
          setLeafletLoaded(true);
        }
      }, 5000);
    };
    document.body.appendChild(script);
  }, []);

  const clearMapLayers = useCallback(() => {
    if (!mapRef.current || layerByNode.current.size === 0) return;
    
    const clearStart = performance.now();
    layerByNode.current.forEach((layer) => {
      if (mapRef.current && mapRef.current.hasLayer(layer)) {
        mapRef.current.removeLayer(layer);
      }
    });
    layerByNode.current.clear();
    console.log(`[TreeViz] Cleared map layers in ${(performance.now() - clearStart).toFixed(2)}ms`);
  }, []);

  // Fetch tree data function
  const fetchTreeData = useCallback(async ({ keepExistingData = false } = {}) => {
    if (!datasetId) return;
    
    const perfStart = performance.now();
    console.log('[TreeViz] Starting tree data fetch...', { keepExistingData });
    
    if (keepExistingData) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    setError(null);
    
    if (!keepExistingData) {
      clearMapLayers();
    }

    try {
      const fetchStart = performance.now();
      const response = await fetch(`${config.baseUrl}/api/datasets/${datasetId}/tree_files/`, {
        credentials: 'include',
        cache: 'no-cache' // Prevent caching
      });
      const fetchTime = performance.now() - fetchStart;
      console.log(`[TreeViz] API fetch completed in ${fetchTime.toFixed(2)}ms (${(fetchTime / 1000).toFixed(2)}s)`);
      
      const jsonStart = performance.now();
      let data;
      try {
        data = await response.json();
        const jsonTime = performance.now() - jsonStart;
        const dataSize = JSON.stringify(data).length;
        console.log(`[TreeViz] JSON parsing completed in ${jsonTime.toFixed(2)}ms (${(jsonTime / 1000).toFixed(2)}s), data size: ${(dataSize / 1024).toFixed(2)}KB`);
      } catch (jsonError) {
        // If response is not JSON, use status text
        throw new Error(`HTTP error! status: ${response.status} ${response.statusText}`);
      }
      
      if (!response.ok) {
        throw new Error(data.error || `HTTP error! status: ${response.status}`);
      }
      
      if (data.error) {
        throw new Error(data.error);
      }
      
      const processStart = performance.now();
      const map = {};
      if (data.tree_data) {
        const buildMapStart = performance.now();
        buildNodeIdMap(data.tree_data, map);
        console.log(`[TreeViz] buildNodeIdMap completed in ${(performance.now() - buildMapStart).toFixed(2)}ms`);
      }
      
      if (keepExistingData) {
        clearMapLayers();
      }

      const stateUpdateStart = performance.now();
      setTreeData(data.tree_data);
      setNodeIdMap(map);
      setUnmatchedScientificNames(data.unmatched_scientific_names || []);
      setTotalUniqueScientificNames(data.total_unique_scientific_names || 0);
      setHighlighted({});
      setHighlightedLeaf(null);
      console.log(`[TreeViz] State updates completed in ${(performance.now() - stateUpdateStart).toFixed(2)}ms`);
      
      const totalTime = performance.now() - perfStart;
      console.log('[TreeViz] Tree data response:', {
        has_coordinates: data.has_coordinates,
        has_scientific_name: data.has_scientific_name,
        tree_data: data.tree_data ? 'present' : 'missing',
        all_keys: Object.keys(data),
        total_time_ms: totalTime.toFixed(2),
        total_time_s: (totalTime / 1000).toFixed(2)
      });
      setHasCoordinates(data.has_coordinates || false);
      setHasScientificName(data.has_scientific_name || false);
    } catch (err) {
      console.error('[TreeViz] Error loading tree data:', err);
      setError(err.message);
    } finally {
      if (keepExistingData) {
        setRefreshing(false);
      } else {
        setLoading(false);
      }
      const totalTime = performance.now() - perfStart;
      console.log(`[TreeViz] Total fetchTreeData time: ${totalTime.toFixed(2)}ms (${(totalTime / 1000).toFixed(2)}s)`);
    }
  }, [datasetId, clearMapLayers]);

  // Fetch tree data on mount and when datasetId changes
  useEffect(() => {
    fetchTreeData();
  }, [fetchTreeData]);

  const handleRefreshClick = useCallback(() => {
    if (refreshing) return;
    fetchTreeData({ keepExistingData: true });
  }, [fetchTreeData, refreshing]);

  // Decorate tree with positions and sizes - MUST be before early returns
  const decoratedTree = React.useMemo(() => {
    if (!treeData) return null;
    const decorateStart = performance.now();
    console.log('[TreeViz] Starting tree decoration...');
    // Shallow clone to avoid expensive deep clone, then mutate during decoration
    const cloneStart = performance.now();
    const tree = cloneTree(treeData);
    console.log(`[TreeViz] Tree cloned in ${(performance.now() - cloneStart).toFixed(2)}ms`);
    
    nodeByKeyMap.current.clear();
    const decorateTreeStart = performance.now();
    decorateTree(tree, null, nodeByKeyMap.current);
    const decorateTime = performance.now() - decorateTreeStart;
    const totalDecorateTime = performance.now() - decorateStart;
    console.log(`[TreeViz] decorateTree completed in ${decorateTime.toFixed(2)}ms (${(decorateTime / 1000).toFixed(2)}s)`);
    console.log(`[TreeViz] Total decoration time: ${totalDecorateTime.toFixed(2)}ms (${(totalDecorateTime / 1000).toFixed(2)}s)`);
    return tree;
  }, [treeData]);

  // Initialize map - only when coordinates are available
  useEffect(() => {
    if (!hasCoordinates) return;
    if (!mapContainerRef.current || mapInstanceRef.current) return;
    if (!window.L) return;

    const map = window.L.map(mapContainerRef.current, {
      center: [40, -95],
      zoom: 3,
      zoomControl: true
    });

    window.L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '© OpenStreetMap contributors',
      maxZoom: 19
    }).addTo(map);

    mapInstanceRef.current = map;
    mapRef.current = map;

    return () => {
      if (mapInstanceRef.current) {
        mapInstanceRef.current.remove();
        mapInstanceRef.current = null;
        mapRef.current = null;
      }
    };
  }, [leafletLoaded, decoratedTree, hasCoordinates]);

  // Build nodeIdMap helper
  const buildNodeIdMap = (node, map = {}, parentKey = 'root', index = 0, leafIndex = { current: 0 }, nodeIndex = { current: 0 }) => {
    const key = `${parentKey}-${index}`;
    const isLeaf = !node.children || node.children.length === 0;
    
    node.key = key;
    node.nodeIndex = nodeIndex.current++;
    node.title = node.name || null;
    
    if (isLeaf) {
      node.leafIndex = leafIndex.current++;
      map[key] = {
        key: key,
        title: node.name || 'Unknown',
        leafIndex: node.leafIndex,
        nodeIndex: node.nodeIndex
      };
    } else {
      map[key] = {
        key: key,
        title: null,
        leafIndex: null,
        nodeIndex: node.nodeIndex
      };
    }

    if (node.children) {
      node.children.forEach((child, i) => {
        buildNodeIdMap(child, map, key, i, leafIndex, nodeIndex);
      });
    }

    return map;
  };

  // Collect descendant tip labels (using original_tip_label if available, else name)
  // This is now only used as a fallback - descendant tips are precomputed during decoration
  const collectDescendantTips = (node, tips = []) => {
    if (!node) return tips;
    // Use precomputed descendant tips if available
    if (node.descendantTips) {
      tips.push(...node.descendantTips);
      return tips;
    }
    // Fallback to recursive collection
    if (!node.children || node.children.length === 0) {
      const tipLabel = node.original_tip_label || node.name;
      if (tipLabel) tips.push(tipLabel);
      return tips;
    }
    node.children.forEach(child => {
      collectDescendantTips(child, tips);
    });
    return tips;
  };

  // Fetch occurrences for a node
  const fetchOccurrencesForNode = async (tipLabels) => {
    if (!tipLabels || tipLabels.length === 0 || !mapRef.current) return;

    try {
      const csrfToken = await getCsrfToken();
      const headers = {
        'Content-Type': 'application/json',
      };
      
      if (csrfToken) {
        headers['X-CSRFToken'] = csrfToken;
      }

      const response = await fetch(`${config.baseUrl}/api/datasets/${datasetId}/tree_node_occurrences/`, {
        method: 'POST',
        headers,
        credentials: 'include',
        body: JSON.stringify({ tip_labels: tipLabels })
      });
      
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      
      const data = await response.json();
      return data.occurrences || [];
    } catch (err) {
      console.error('Error fetching occurrences:', err);
      return [];
    }
  };

  // Handle node selection
  const handleNodeSelect = async (nodeKey, color) => {
    if (!nodeKey || !hasCoordinates || !mapRef.current || !decoratedTree) return;

    // Use lookup map instead of tree traversal
    const node = nodeByKeyMap.current.get(nodeKey);
    if (!node) return;

    // Use precomputed descendant tips
    const tipLabels = node.descendantTips || collectDescendantTips(node);
    const occurrences = await fetchOccurrencesForNode(tipLabels);
    
    const existingLayer = layerByNode.current.get(nodeKey);
    if (existingLayer && mapRef.current.hasLayer(existingLayer)) {
      mapRef.current.removeLayer(existingLayer);
    }

    const layer = window.L.layerGroup();
    let hasCoords = false;
    
    for (const occ of occurrences) {
      const lat = occ.decimalLatitude || occ.lat;
      const lon = occ.decimalLongitude || occ.lon;
      if (lat != null && lon != null && isFinite(lat) && isFinite(lon)) {
        hasCoords = true;
        window.L.circleMarker([lat, lon], {
          radius: 4,
          color: color,
          weight: 1,
          fillOpacity: 0.6
        })
          .bindTooltip(`${occ.scientificName || ""}`.trim() || occ.catalogNumber || occ.occurrenceID || "")
          .addTo(layer);
      }
    }

    if (hasCoords) {
      layer.addTo(mapRef.current);
      layerByNode.current.set(nodeKey, layer);
      
      if (layerByNode.current.size === 1) {
        try {
          mapRef.current.fitBounds(layer.getBounds(), { maxZoom: 6, padding: [20, 20] });
        } catch (e) {
          // Ignore fitBounds errors
        }
      }
    } else {
      layerByNode.current.set(nodeKey, layer);
    }
  };

  // Handle node deselection
  const handleNodeDeselect = (nodeKey) => {
    if (!nodeKey || !mapRef.current) return;
    const layer = layerByNode.current.get(nodeKey);
    if (layer && mapRef.current.hasLayer(layer)) {
      mapRef.current.removeLayer(layer);
      layerByNode.current.delete(nodeKey);
    }
  };

  // Handle toggle
  const handleToggle = ({ selected }) => {
    setHighlighted(prev => {
      const next = { ...prev };
      if (next[selected]) {
        delete next[selected];
        handleNodeDeselect(selected);
      } else {
        const colors = ['#ff4d4f', '#1890ff', '#52c41a', '#faad14', '#722ed1', '#eb2f96'];
        const color = colors[Object.keys(next).length % colors.length];
        next[selected] = { color };
        handleNodeSelect(selected, color);
      }
      return next;
    });
  };

  if (loading) {
    return (
      <div className="d-flex justify-content-center align-items-center" style={{ minHeight: '400px' }}>
        <div className="spinner-border" role="status">
          <span className="visually-hidden">Loading tree...</span>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="alert alert-danger">
        <strong>Error loading tree:</strong> {error}
      </div>
    );
  }

  if (!treeData || !nodeIdMap) {
    return (
      <div className="alert alert-info">
        No tree data available.
      </div>
    );
  }

  return (
    <div className="tree-visualization" style={{ display: 'flex', flexDirection: 'column', height: '100%', width: '100%' }}>
      <div
        className="tree-refresh-banner"
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: '12px',
          padding: '8px 16px',
          background: '#fff7e6',
          border: '1px solid #ffe58f',
          borderRadius: '6px',
          marginBottom: '12px'
        }}
      >
        <div style={{ fontWeight: 500, color: '#ad6800', display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span>Showing cached tree.</span>
          {refreshing && (
            <span className="text-muted small" aria-live="polite">
              Refreshing...
            </span>
          )}
        </div>
        <button
          type="button"
          className="btn btn-outline-primary btn-sm d-flex align-items-center gap-2"
          onClick={handleRefreshClick}
          disabled={refreshing}
        >
          {refreshing && (
            <span className="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>
          )}
          Refresh
        </button>
      </div>
      <div style={{ display: 'flex', flex: 1, minHeight: 0, width: '100%' }}>
        <div style={{ width: '50%', display: 'flex', flexDirection: 'column', borderRight: '1px solid #ddd', overflow: 'hidden' }}>
          {decoratedTree && (
            <SimpleTreeView
              tree={decoratedTree}
              nodeIdMap={nodeIdMap}
              highlighted={highlighted}
              highlightedLeaf={highlightedLeaf}
              onToggle={handleToggle}
            />
          )}
        </div>
        <div style={{ width: '50%', position: 'relative', minHeight: 0, overflow: 'hidden' }}>
          {hasCoordinates ? (
            <div 
              ref={mapContainerRef} 
              className="leaflet-container"
              style={{ 
                height: '100%', 
                width: '100%'
              }} 
            />
          ) : (
            <div style={{
              height: '100%',
              width: '100%',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              padding: '40px',
              backgroundColor: '#f8f9fa',
              textAlign: 'center'
            }}>
              <div style={{
                maxWidth: '500px',
                color: '#6c757d'
              }}>
                <div style={{
                  fontSize: '48px',
                  marginBottom: '20px',
                  opacity: 0.5
                }}>
                  🗺️
                </div>
                <div style={{
                  fontSize: '18px',
                  fontWeight: '500',
                  marginBottom: '12px',
                  color: '#495057'
                }}>
                  Waiting for data
                </div>
                <div style={{
                  fontSize: '14px',
                  lineHeight: '1.6'
                }}>
                  Waiting for scientificName and decimalLatitude/decimalLongitude values in your dataset - ChatIPT may need to format these.
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
      {unmatchedScientificNames.length > 0 && (
        <UnmatchedNamesPanel 
          names={unmatchedScientificNames} 
          total={totalUniqueScientificNames}
        />
      )}
    </div>
  );
};

// Decorate tree with positions and sizes, build lookup map, and precompute descendant tips
const decorateTree = (node, parent, nodeByKeyMap) => {
  let position = 0;
  let index = 0;
  let leafIndex = 0;

  function recursive(node, parent) {
    const children = node.children || [];
    node.position = position;
    node.parent = parent;
    node.id = index;
    index++;
    
    // Add to lookup map
    if (node.key && nodeByKeyMap) {
      nodeByKeyMap.set(node.key, node);
    }
    
    if (children.length === 0) {
      // Leaf node - precompute descendant tips (just this node's tip label)
      if (node.leafIndex === undefined) {
        node.leafIndex = leafIndex++;
      }
      position++;
      node.size = 1;
      node.childrenLength = 0;
      const tipLabel = node.original_tip_label || node.name;
      node.descendantTips = tipLabel ? [tipLabel] : [];
      return {
        size: node.size,
        childrenLength: node.branch_length || 0
      };
    }
    
    // Internal node - recursively process children and collect their descendant tips
    const sizes = children.map(x => recursive(x, node));
    const sum = sizes.reduce((partial_sum, a) => partial_sum + a.size, 0);
    const childrenLength = sizes.reduce((maxLength, a) => Math.max(maxLength, a.childrenLength), 0);

    node.size = sum;
    node.childrenLength = childrenLength;
    
    // Precompute descendant tips by collecting from all children
    const descendantTips = [];
    children.forEach(child => {
      if (child.descendantTips) {
        descendantTips.push(...child.descendantTips);
      }
    });
    node.descendantTips = descendantTips;
    
    return {
      size: node.size,
      childrenLength: (node.childrenLength || 0) + (node.branch_length || 0)
    };
  }
  return recursive(node, parent);
};

// Tree Node Component matching ui.html structure
const TreeNodeComponent = ({ 
  node, 
  highlighted, 
  highlightedLeaf, 
  onToggle, 
  onNodeEnter, 
  onNodeLeave, 
  elementHeight = 20,
  multiplier = 5,
  hasVisibleParent = true
}) => {
  if (!node || !node.size) return null;
  
  const isHighlighted = highlighted && highlighted[node.key];
  const isHighlightedLeaf = highlightedLeaf === node.leafIndex;
  const visibleNames = elementHeight >= 10;
  const children = node.children || [];
  const childrenLength = children.length;
  const depth = (node.branch_length || 0) * multiplier;

  if (node.size * elementHeight < 10 && childrenLength > 0) {
    const totalDepth = (node.childrenLength || 0) * multiplier;
    return (
      <li
        className="gb-tree-node"
        style={{
          height: node.size * elementHeight,
          paddingLeft: depth,
          background: !visibleNames && isHighlighted ? (isHighlighted.color + 'cc') : null
        }}
      >
        <span className="gb-tree-pipe" style={{ width: depth }} />
        <span
          onClick={() => onToggle && onToggle({ selected: node.key })}
          className={`gb-tree-hover-title gb-tree-color ${childrenLength === 0 ? 'gb-tree-leaf-color' : ''}`}
          style={{
            backgroundColor: isHighlighted ? isHighlighted.color : null,
            boxShadow: isHighlightedLeaf ? '0 0 0 2px #ff6868' : null
          }}
          onMouseEnter={() => onNodeEnter && onNodeEnter({ node })}
          onMouseLeave={() => onNodeLeave && onNodeLeave({ node })}
          id={childrenLength === 0 ? `gb-tree-node-${node.leafIndex}` : null}
        >
          {!visibleNames && <span className="gb-tree-color-click-area" />}
        </span>
        <div className="gb-tree-content-node">
          {node.name && (
            <span className="gb-tree-hover-title">
              {node.name}
              {node.occurrence_count > 0 && ` (${node.occurrence_count})`}
            </span>
          )}
        </div>
        <div className="gb-tree-subtree-placeholder" style={{ width: totalDepth }} />
      </li>
    );
  }

  return (
    <li
      className={`gb-tree-node ${isHighlighted ? 'gb-tree-highlighted' : ''}`}
      style={{
        height: node.size * elementHeight,
        paddingLeft: depth,
        borderRight: visibleNames && isHighlighted ? `8px solid ${isHighlighted.color}` : null,
        background: !visibleNames && isHighlighted ? (isHighlighted.color + 'cc') : null
      }}
    >
      <span className="gb-tree-pipe" style={{ width: depth }} />
      <span
        onClick={() => onToggle && onToggle({ selected: node.key })}
        className={`gb-tree-hover-title gb-tree-color ${childrenLength === 0 ? 'gb-tree-leaf-color' : ''}`}
        style={{
          backgroundColor: isHighlighted ? isHighlighted.color : null,
          boxShadow: isHighlightedLeaf ? '0 0 0 2px #ff6868' : null
        }}
        onMouseEnter={() => onNodeEnter && onNodeEnter({ node })}
        onMouseLeave={() => onNodeLeave && onNodeLeave({ node })}
        id={childrenLength === 0 ? `gb-tree-node-${node.leafIndex}` : null}
      >
        {!visibleNames && <span className="gb-tree-color-click-area" />}
      </span>
      {visibleNames && childrenLength === 0 && (
        <div
          className={`gb-tree-content-node gb-tree-leaf`}
          onClick={() => onToggle && onToggle({ selected: node.key })}
          style={{
            boxShadow: isHighlightedLeaf ? '0 0 0 2px #ff6868' : null
          }}
        >
          {node.name && (
            <span>
              <span>
                <span className="gb-tree-node-name">
                  {node.name || node.title || 'Unknown'}
                  {node.occurrence_count > 0 && ` (${node.occurrence_count})`}
                </span>
              </span>
            </span>
          )}
        </div>
      )}
      {childrenLength > 0 && (
        <ol
          className="gb-tree-list"
          style={{
            color: isHighlighted ? isHighlighted.color : null
          }}
        >
          {children.map((child) => (
            <TreeNodeComponent
              key={child.nodeIndex}
              node={child}
              highlighted={highlighted}
              highlightedLeaf={highlightedLeaf}
              onToggle={onToggle}
              onNodeEnter={onNodeEnter}
              onNodeLeave={onNodeLeave}
              elementHeight={elementHeight}
              multiplier={multiplier}
            />
          ))}
        </ol>
      )}
    </li>
  );
};

// Memoized TreeNode with custom comparison
const TreeNode = React.memo(TreeNodeComponent, (prevProps, nextProps) => {
  // Return true if props are equal (skip re-render), false if different (re-render)
  const nodeKey = prevProps.node?.key;
  const isHighlightedEqual = prevProps.highlighted?.[nodeKey]?.color === nextProps.highlighted?.[nodeKey]?.color;
  const hasHighlighted = !!(prevProps.highlighted?.[nodeKey]) === !!(nextProps.highlighted?.[nodeKey]);
  
  return (
    nodeKey === nextProps.node?.key &&
    hasHighlighted &&
    isHighlightedEqual &&
    prevProps.highlightedLeaf === nextProps.highlightedLeaf &&
    prevProps.elementHeight === nextProps.elementHeight &&
    prevProps.multiplier === nextProps.multiplier &&
    prevProps.onToggle === nextProps.onToggle &&
    prevProps.onNodeEnter === nextProps.onNodeEnter &&
    prevProps.onNodeLeave === nextProps.onNodeLeave
  );
});

// Simplified Tree View Component
const SimpleTreeView = ({ tree, nodeIdMap, highlighted, highlightedLeaf, onToggle }) => {
  const treeRef = useRef(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [fontSize, setFontSize] = useState(12);
  const [q, setQ] = useState('');
  const [hoveredNode, setHoveredNode] = useState(null);
  const [elementHeight, setElementHeight] = useState(20);
  const multiplier = 5;

  useEffect(() => {
    const baseHeight = Math.max(10, Math.floor(fontSize * 1.15) + 2);
    const verticalGap = Math.max(2, Math.round(fontSize * 0.35));
    setElementHeight(baseHeight + verticalGap);
  }, [fontSize]);

  // Memoize leaf suggestions
  const leafSuggestions = useMemo(() => {
    if (!nodeIdMap) return [];
    return Object.keys(nodeIdMap)
      .filter(key => nodeIdMap[key].title && nodeIdMap[key].leafIndex !== null)
      .map(key => ({
        key,
        label: nodeIdMap[key].title
      }));
  }, [nodeIdMap]);

  const handleNodeEnter = useCallback(({ node }) => {
    setHoveredNode(node);
  }, []);

  const handleNodeLeave = useCallback(() => {
    setHoveredNode(null);
  }, []);

  // Memoize filtered suggestions
  const filteredSuggestions = useMemo(() => {
    if (!q) return [];
    const lowerQ = q.toLowerCase();
    return leafSuggestions.filter(s => 
      s.label.toLowerCase().includes(lowerQ)
    );
  }, [q, leafSuggestions]);

  const containerHeight = tree ? tree.size * elementHeight : 0;

  return (
    <div className="treeArea" style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="tree-controls">
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <div style={{ position: 'relative', flex: 1 }}>
            <input
              type="text"
              placeholder="Search tree..."
              value={q}
              onChange={(e) => setQ(e.target.value)}
              style={{ width: '100%', padding: '4px 8px', border: '1px solid #ddd', borderRadius: '4px' }}
            />
            {q && filteredSuggestions.length > 0 && (
              <div style={{
                position: 'absolute',
                top: '100%',
                left: 0,
                right: 0,
                backgroundColor: 'white',
                border: '1px solid #ddd',
                borderTop: 'none',
                borderRadius: '0 0 4px 4px',
                maxHeight: '200px',
                overflowY: 'auto',
                zIndex: 1000,
                boxShadow: '0 2px 8px rgba(0,0,0,0.15)',
              }}>
                {filteredSuggestions.slice(0, 10).map(s => (
                  <div
                    key={s.key}
                    onClick={() => {
                      setQ('');
                      if (nodeIdMap[s.key]) {
                        onToggle({ selected: s.key });
                      }
                    }}
                    style={{ padding: '8px', cursor: 'pointer', borderBottom: '1px solid #eee' }}
                    onMouseEnter={(e) => e.target.style.backgroundColor = '#f5f5f5'}
                    onMouseLeave={(e) => e.target.style.backgroundColor = 'white'}
                  >
                    {s.label}
                  </div>
                ))}
              </div>
            )}
          </div>
          <div style={{ display: 'flex', gap: '4px', alignItems: 'center' }}>
            <button
              onClick={() => setFontSize(Math.max(8, fontSize - 2))}
              className="btn btn-sm btn-outline-secondary"
            >
              -
            </button>
            <span style={{ padding: '4px 8px', fontSize: '12px' }}>{fontSize}px</span>
            <button
              onClick={() => setFontSize(Math.min(20, fontSize + 2))}
              className="btn btn-sm btn-outline-secondary"
            >
              +
            </button>
          </div>
        </div>
      </div>
      {hoveredNode && (
        <div className="gb-snack-bar">
          {hoveredNode.name || hoveredNode.title || `Node ${hoveredNode.nodeIndex}`}
          {hoveredNode.occurrence_count > 0 && ` (${hoveredNode.occurrence_count} occurrences)`}
        </div>
      )}
      <div
        ref={treeRef}
        className="tree-tree"
        onScroll={(e) => setScrollTop(e.target.scrollTop)}
      >
        {tree && (
          <ol
            className="gb-tree-list"
            style={{
              fontSize: `${fontSize}px`,
              willChange: "transform",
              position: "relative",
              height: containerHeight,
            }}
          >
            <TreeNode
              node={tree}
              highlighted={highlighted}
              highlightedLeaf={highlightedLeaf}
              onToggle={onToggle}
              onNodeEnter={handleNodeEnter}
              onNodeLeave={handleNodeLeave}
              elementHeight={elementHeight}
              multiplier={multiplier}
            />
          </ol>
        )}
      </div>
    </div>
  );
};

// Unmatched Names Panel Component
const UnmatchedNamesPanel = ({ names, total }) => {
  const [isExpanded, setIsExpanded] = useState(false);

  return (
    <div style={{ 
      borderTop: '1px solid #ddd', 
      backgroundColor: '#f8f9fa',
      maxHeight: isExpanded ? '200px' : '40px',
      overflow: 'auto',
      transition: 'max-height 0.3s ease'
    }}>
      <div
        style={{
          padding: '8px 12px',
          cursor: 'pointer',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          backgroundColor: '#e9ecef',
          fontWeight: '500',
          userSelect: 'none'
        }}
        onClick={() => setIsExpanded(!isExpanded)}
      >
        <span>
          Scientific names not found in tree {names.length}/{total}
        </span>
        <span style={{ fontSize: '12px' }}>
          {isExpanded ? '▼' : '▶'}
        </span>
      </div>
      {isExpanded && (
        <div style={{ padding: '8px 12px' }}>
          <div style={{ 
            display: 'flex', 
            flexWrap: 'wrap', 
            gap: '4px',
            fontSize: '13px'
          }}>
            {names.map((name, index) => (
              <span
                key={index}
                style={{
                  padding: '2px 6px',
                  backgroundColor: 'white',
                  border: '1px solid #ddd',
                  borderRadius: '3px',
                  display: 'inline-block'
                }}
              >
                {name}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

export default TreeVisualization;
