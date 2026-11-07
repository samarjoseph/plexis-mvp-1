import React from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Table, BarChart2, UploadCloud, History, Sparkles, X, ShieldAlert } from 'lucide-react';
import GoogleSignInButton from '../oauth/GoogleSignInButton';
import './GuestModals.css';

const FEATURE_CONFIGS = {
  spreadsheet: {
    icon: Table,
    iconColor: '#38bdf8',
    title: 'Unlock Live Spreadsheet',
    description: 'The interactive spreadsheet panel is unavailable in Guest Mode. Sign in with Google to view, sort, filter, and inspect your datasets directly in the real-time grid.',
    highlight: 'Full spreadsheet view with virtual scrolling up to 40,000+ rows.',
  },
  analytics: {
    icon: BarChart2,
    iconColor: '#a855f7',
    title: 'Unlock Deep Analytics',
    description: 'Automated statistical profiling and deep analysis are disabled in Guest Mode. Sign in with Google to explore anomaly detection, column correlations, and intelligence summaries.',
    highlight: 'Automated insights, KPI rankings, and deep analytical distributions.',
  },
  upload: {
    icon: UploadCloud,
    iconColor: '#3b82f6',
    title: 'Upload Your Own Datasets',
    description: 'Custom dataset uploads are disabled in Guest Mode. Sign in with Google to upload your own CSV and Excel files and run end-to-end AI investigations.',
    highlight: 'Upload datasets up to 50 MB with persistent storage and analysis.',
  },
  history: {
    icon: History,
    iconColor: '#f59e0b',
    title: 'Access Saved Conversations',
    description: 'Guest conversations reset when you end your session or refresh the page. Sign in with Google to automatically save, organize, and resume previous chats across devices.',
    highlight: 'Permanent chat persistence, context memory, and conversation history.',
  },
  quota: {
    icon: Sparkles,
    iconColor: '#ec4899',
    title: 'Monthly Transformation Quota Reached',
    description: "You've used all 2 free transformations for this month in Guest Mode. Sign in with Google to unlock daily transformation allowances and keep your data clean.",
    highlight: 'Sign in to get higher transformation limits and permanent dataset versioning.',
  },
};

export default function GuestConversionModal({ isOpen, feature = 'spreadsheet', onClose }) {
  if (!isOpen) return null;

  const config = FEATURE_CONFIGS[feature] || FEATURE_CONFIGS.spreadsheet;
  const IconComponent = config.icon;

  return (
    <AnimatePresence>
      <div className="guest-modal-backdrop" onClick={onClose} role="dialog" aria-modal="true">
        <motion.div
          className="guest-modal-container glass-panel"
          onClick={(e) => e.stopPropagation()}
          initial={{ opacity: 0, scale: 0.92, y: 20 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.92, y: 20 }}
          transition={{ type: 'spring', damping: 25, stiffness: 350 }}
        >
          {/* Close button */}
          <button
            type="button"
            className="guest-modal-close"
            onClick={onClose}
            aria-label="Close modal"
          >
            <X size={18} />
          </button>

          {/* Header Icon */}
          <div className="guest-modal-icon-wrap" style={{ backgroundColor: `${config.iconColor}22` }}>
            <IconComponent size={32} style={{ color: config.iconColor }} />
          </div>

          {/* Titles */}
          <div className="guest-modal-tag">Plexis AI · Guest Mode Limit</div>
          <h2 className="guest-modal-title">{config.title}</h2>
          <p className="guest-modal-desc">{config.description}</p>

          {/* Highlight pill */}
          <div className="guest-modal-feature-pill">
            <span className="guest-modal-feature-dot" style={{ backgroundColor: config.iconColor }} />
            <span>{config.highlight}</span>
          </div>

          {/* Action buttons */}
          <div className="guest-modal-actions">
            <GoogleSignInButton variant="primary" label="Sign in with Google" />
            <button type="button" className="guest-modal-secondary-btn" onClick={onClose}>
              Continue in Guest Mode
            </button>
          </div>
        </motion.div>
      </div>
    </AnimatePresence>
  );
}
