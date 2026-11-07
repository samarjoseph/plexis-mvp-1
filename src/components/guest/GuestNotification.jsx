import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Sparkles, X, LogIn } from 'lucide-react';
import GoogleSignInButton from '../oauth/GoogleSignInButton';
import './GuestModals.css';

export default function GuestNotification({ transformationsUsed = 0, transformationsMax = 2 }) {
  const [isDismissed, setIsDismissed] = useState(() => {
    return sessionStorage.getItem('plexis_guest_banner_dismissed') === 'true';
  });

  const handleDismiss = () => {
    setIsDismissed(true);
    sessionStorage.setItem('plexis_guest_banner_dismissed', 'true');
  };

  if (isDismissed) return null;

  return (
    <AnimatePresence>
      <motion.div
        className="guest-notification-bar glass-panel"
        initial={{ opacity: 0, y: -16 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, y: -16 }}
        transition={{ duration: 0.35, ease: 'easeOut' }}
      >
        <div className="guest-notif-left">
          <div className="guest-notif-badge">
            <Sparkles size={14} className="guest-notif-sparkle" />
            <span>Guest Mode</span>
          </div>

          <div className="guest-notif-body">
            <span className="guest-notif-title">
              You're exploring Plexis in Guest Mode.
            </span>
            <span className="guest-notif-text">
              You can use our sample dataset and perform {transformationsMax} transformations per month
              {transformationsUsed > 0 ? ` (${transformationsUsed}/${transformationsMax} used)` : ''}.
              Sign in to upload your own datasets, access the live spreadsheet, analytics and saved conversations.
            </span>
          </div>
        </div>

        <div className="guest-notif-right">
          <GoogleSignInButton variant="nav" label="Sign in with Google" />
          <button
            type="button"
            className="guest-notif-close"
            onClick={handleDismiss}
            aria-label="Dismiss guest notification"
          >
            <X size={16} />
          </button>
        </div>
      </motion.div>
    </AnimatePresence>
  );
}
