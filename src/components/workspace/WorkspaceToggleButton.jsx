/**
 * WorkspaceToggleButton — small icon button in ChatPanel header.
 * Opens/closes the DatasetWorkspace panel.
 * Shows a dot indicator when evidence is active.
 * Only visible when a dataset is loaded.
 */
import React from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Grid3x3 } from 'lucide-react';
import { useDatasetWorkspace } from '../../hooks/useDatasetWorkspace.js';

export default function WorkspaceToggleButton({ datasetId, sessionId, className = '', onClick }) {
  const { isOpen, toggle, openForDataset, totalRows } = useDatasetWorkspace();

  if (!datasetId) return null;

  const handleClick = (e) => {
    if (onClick) {
      onClick(e);
      return;
    }
    if (datasetId) {
      openForDataset(datasetId, sessionId);
    } else {
      toggle();
    }
  };

  return (
    <motion.button
      type="button"
      id="workspace-toggle-btn"
      className={`ws-toggle-btn ${isOpen ? 'ws-toggle-btn--active' : ''} ${className}`}
      onClick={handleClick}
      whileHover={{ scale: 1.08 }}
      whileTap={{ scale: 0.92 }}
      title={isOpen ? 'Close data table' : `Open data table${totalRows ? ` · ${totalRows.toLocaleString()} rows` : ''}`}
    >
      <Grid3x3 size={15} strokeWidth={1.8} />
      <AnimatePresence>
        {isOpen && (
          <motion.span
            className="ws-toggle-btn__indicator"
            initial={{ scale: 0 }}
            animate={{ scale: 1 }}
            exit={{ scale: 0 }}
          />
        )}
      </AnimatePresence>
    </motion.button>
  );
}
