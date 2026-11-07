import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { X } from 'lucide-react';
import { triggerGoogleSignIn } from '../oauth/GoogleSignInButton';
import './GuestModals.css';

export default function GuestNotification({ transformationsUsed = 0, transformationsMax = 2 }) {
  const [dismissed, setDismissed] = useState(() => {
    return sessionStorage.getItem('plexis_guest_toast_dismissed') === 'true';
  });

  const handleDismiss = () => {
    setDismissed(true);
    sessionStorage.setItem('plexis_guest_toast_dismissed', 'true');
  };

  if (dismissed) return null;

  return (
    <AnimatePresence>
      {!dismissed && (
        <motion.div
          className="v2-guest-mode-toast"
          initial={{ opacity: 0, y: 18, scale: 0.94 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 12, scale: 0.94 }}
          transition={{
            type: 'spring',
            stiffness: 380,
            damping: 24,
            mass: 0.8,
          }}
          role="status"
          aria-live="polite"
        >
          <div className="v2-guest-toast-content">
            <span className="v2-guest-toast-title">Guest Mode</span>
            <span className="v2-guest-toast-divider">·</span>
            <span className="v2-guest-toast-desc">
              {transformationsMax} transformations/month
              {transformationsUsed > 0 ? ` (${transformationsUsed}/${transformationsMax} used)` : ''}
            </span>
          </div>

          <button
            type="button"
            className="v2-guest-toast-signin"
            onClick={triggerGoogleSignIn}
            title="Sign in with Google"
          >
            Sign in
          </button>

          <button
            type="button"
            className="v2-guest-toast-close"
            onClick={handleDismiss}
            aria-label="Dismiss guest notification"
          >
            <X size={13} />
          </button>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
