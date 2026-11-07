import React, { useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { ShieldAlert, X } from 'lucide-react';
import GoogleSignInButton from '../oauth/GoogleSignInButton';
import './GuestModals.css';

export default function CustomUploadGuestModal({ isOpen, onClose }) {
  const [secondsLeft, setSecondsLeft] = useState(10);

  useEffect(() => {
    if (!isOpen) return;

    setSecondsLeft(10);
    const timer = setInterval(() => {
      setSecondsLeft((prev) => {
        if (prev <= 1) {
          clearInterval(timer);
          onClose();
          return 0;
        }
        return prev - 1;
      });
    }, 1000);

    return () => clearInterval(timer);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  return (
    <AnimatePresence>
      <div className="guest-modal-backdrop" onClick={onClose} role="dialog" aria-modal="true">
        <motion.div
          className="guest-modal-container glass-panel"
          onClick={(e) => e.stopPropagation()}
          initial={{ opacity: 0, scale: 0.92, y: 16 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.92, y: 16 }}
          transition={{ type: 'spring', damping: 25, stiffness: 350 }}
        >
          {/* Close button with Cross */}
          <button
            type="button"
            className="guest-modal-close"
            onClick={onClose}
            aria-label="Close modal"
          >
            <X size={18} />
          </button>

          {/* Header Icon */}
          <div className="guest-modal-icon-wrap" style={{ backgroundColor: 'rgba(56, 189, 248, 0.14)' }}>
            <ShieldAlert size={30} style={{ color: '#38bdf8' }} />
          </div>

          <div className="guest-modal-tag">Plexis · Guest Mode</div>

          <p
            className="guest-modal-desc"
            style={{
              fontSize: '0.92rem',
              color: '#f8fafc',
              fontWeight: 500,
              margin: '14px 0 20px',
              lineHeight: 1.55,
            }}
          >
            Custom uploads are available only to sign in user. Please sign in to get advanced features.
          </p>

          <div className="guest-modal-actions" style={{ marginTop: '8px' }}>
            <GoogleSignInButton variant="primary" label="Sign in with Google" />
            <button
              type="button"
              className="guest-modal-secondary-btn"
              onClick={onClose}
            >
              Close ({secondsLeft}s)
            </button>
          </div>
        </motion.div>
      </div>
    </AnimatePresence>
  );
}
