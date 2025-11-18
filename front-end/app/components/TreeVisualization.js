'use client'

import React, { useState, useEffect, useRef, useCallback } from 'react';
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

const TreeVisualization = ({ datasetId, onClose }) => {
  const [treeData, setTreeData] = useState(null);
  const [nodeIdMap, setNodeIdMap] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [highlighted, setHighlighted] = useState({});
  const [highlightedLeaf, setHighlightedLeaf] = useState(null);
  const [unmatchedScientificNames, setUnmatchedScientificNames] = useState([]);
  const [totalUniqueScientificNames, setTotalUniqueScientificNames] = useState(0);
  const mapRef = useRef(null);
  const mapInstanceRef = useRef(null);
  const mapContainerRef = useRef(null);
  const layerByNode = useRef(new Map());
  const [leafletLoaded, setLeafletLoaded] = useState(false);

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

  // Fetch tree data
  useEffect(() => {
    if (!datasetId) return;
    
    const fetchTreeData = async () => {
      setLoading(true);
      setError(null);
      try {
        const response = await fetch(`${config.baseUrl}/api/datasets/${datasetId}/tree_files/`, {
          credentials: 'include'
        });
        if (!response.ok) {
          throw new Error(`HTTP error! status: ${response.status}`);
        }
        const data = await response.json();
        
        if (data.error) {
          throw new Error(data.error);
        }
        
        const map = {};
        if (data.tree_data) {
          buildNodeIdMap(data.tree_data, map);
        }
        
        setTreeData(data.tree_data);
        setNodeIdMap(map);
        setUnmatchedScientificNames(data.unmatched_scientific_names || []);
        setTotalUniqueScientificNames(data.total_unique_scientific_names || 0);
      } catch (err) {
        console.error('Error loading tree data:', err);
        setError(err.message);
      } finally {
        setLoading(false);
      }
    };
    
    fetchTreeData();
  }, [datasetId]);

  // Decorate tree with positions and sizes - MUST be before early returns
  const decoratedTree = React.useMemo(() => {
    if (!treeData) return null;
    const tree = JSON.parse(JSON.stringify(treeData));
    decorateTree(tree, null);
    return tree;
  }, [treeData]);

  // Initialize map - exactly like ui.html
  useEffect(() => {
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
  }, [leafletLoaded, decoratedTree]);

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
  const collectDescendantTips = (node, tips = []) => {
    if (!node) return tips;
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
    if (!nodeKey || !mapRef.current || !treeData) return;

    const findNodeByKey = (node, key) => {
      if (!node) return null;
      if (node.key === key) return node;
      if (node.children) {
        for (const child of node.children) {
          const found = findNodeByKey(child, key);
          if (found) return found;
        }
      }
      return null;
    };

    const node = findNodeByKey(treeData, nodeKey);
    if (!node) return;

    const tipLabels = collectDescendantTips(node);
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
          <div 
            ref={mapContainerRef} 
            className="leaflet-container"
            style={{ 
              height: '100%', 
              width: '100%'
            }} 
          />
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

// Decorate tree with positions and sizes
const decorateTree = (node, parent) => {
  let position = 0;
  let index = 0;
  let leafIndex = 0;

  function recursive(node, parent) {
    const children = node.children || [];
    node.position = position;
    node.parent = parent;
    node.id = index;
    index++;
    
    if (children.length === 0) {
      if (node.leafIndex === undefined) {
        node.leafIndex = leafIndex++;
      }
      position++;
      node.size = 1;
      node.childrenLength = 0;
      return {
        size: node.size,
        childrenLength: node.branch_length || 0
      };
    }
    const sizes = children.map(x => recursive(x, node));
    const sum = sizes.reduce((partial_sum, a) => partial_sum + a.size, 0);
    const childrenLength = sizes.reduce((maxLength, a) => Math.max(maxLength, a.childrenLength), 0);

    node.size = sum;
    node.childrenLength = childrenLength;
    return {
      size: node.size,
      childrenLength: (node.childrenLength || 0) + (node.branch_length || 0)
    };
  }
  return recursive(node, parent);
};

// Tree Node Component matching ui.html structure
const TreeNode = ({ 
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
            <TreeNode
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

// Simplified Tree View Component
const SimpleTreeView = ({ tree, nodeIdMap, highlighted, highlightedLeaf, onToggle }) => {
  const treeRef = useRef(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [fontSize, setFontSize] = useState(12);
  const [q, setQ] = useState('');
  const [hoveredNode, setHoveredNode] = useState(null);
  const [leafSuggestions, setLeafSuggestions] = useState([]);
  const [elementHeight, setElementHeight] = useState(20);
  const multiplier = 5;

  useEffect(() => {
    const baseHeight = Math.max(10, Math.floor(fontSize * 1.15) + 2);
    const verticalGap = Math.max(2, Math.round(fontSize * 0.35));
    setElementHeight(baseHeight + verticalGap);
  }, [fontSize]);

  useEffect(() => {
    if (!nodeIdMap) return;
    const suggestions = Object.keys(nodeIdMap)
      .filter(key => nodeIdMap[key].title && nodeIdMap[key].leafIndex !== null)
      .map(key => ({
        key,
        label: nodeIdMap[key].title
      }));
    setLeafSuggestions(suggestions);
  }, [nodeIdMap]);

  const handleNodeEnter = useCallback(({ node }) => {
    setHoveredNode(node);
  }, []);

  const handleNodeLeave = useCallback(() => {
    setHoveredNode(null);
  }, []);

  const filteredSuggestions = leafSuggestions.filter(s => 
    s.label.toLowerCase().includes(q.toLowerCase())
  );

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
