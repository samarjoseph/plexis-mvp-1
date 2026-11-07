import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Sparkles, X } from 'lucide-react';
import { triggerGoogleSignIn } from '../oauth/GoogleSignInButton';

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
      <motion.div
        className="v2-plan-limit-toast"
        style={{
          bottom: '28px',
          right: '28px',
          top: 'auto',
          left: 'auto',
        }}
        initial={{ opacity: 0, y: 6, scale: 0.95 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 4, scale: 0.95 }}
        transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
        role="alert"
      >
        <div className="v2-plan-toast-logo" aria-hidden="true">
          <Sparkles size={13} color="#3b82f6" />
        </div>

        <span className="v2-plan-toast-text">
          Guest Mode: {transformationsMax} transformations/month
          {transformationsUsed > 0 ? ` (${transformationsUsed}/${transformationsMax})` : ''}.{' '}
          <button
            type="button"
            className="v2-plan-toast-plus-btn"
            onClick={triggerGoogleSignIn}
            title="Sign in with Google"
          >
            Sign in
          </button>
        </span>

        <button
          type="button"
          className="v2-plan-toast-close"
          onClick={handleDismiss}
          aria-label="Dismiss guest notification"
        >
          <X size={13} />
        </button>

        <span className="v2-plan-toast-tail v2-plan-toast-tail--fallback" />
      </motion.div>
    </AnimatePresence>
  );
}
