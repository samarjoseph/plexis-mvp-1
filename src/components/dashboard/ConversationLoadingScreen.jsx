import React, { useState, useEffect } from 'react';
import { motion } from 'framer-motion';
import PlexisLogo from '../brand/PlexisLogo';

const LOADING_STEPS = [
  'Restoring conversation session…',
  'Synchronizing analytical context…',
  'Loading chat history…',
];

const STEP_INTERVAL_MS = 750;

export default function ConversationLoadingScreen() {
  const [stepIdx, setStepIdx] = useState(0);

  useEffect(() => {
    const interval = setInterval(() => {
      setStepIdx((i) => (i + 1) % LOADING_STEPS.length);
    }, STEP_INTERVAL_MS);
    return () => clearInterval(interval);
  }, []);

  return (
    <motion.div
      className="v2-conv-loading-screen"
      initial={{ opacity: 0, scale: 0.98 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.98 }}
      transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
    >
      {/* Background ambient aura */}
      <div className="v2-conv-loading-glow" />

      {/* Futuristic AI orb with Plexis Logo */}
      <div className="v2-conv-loading-orb-container">
        {/* Orbital pulsing rings */}
        <motion.div
          className="v2-conv-loading-ring v2-conv-loading-ring--outer"
          animate={{ rotate: 360, scale: [1, 1.05, 1] }}
          transition={{
            rotate: { duration: 12, repeat: Infinity, ease: 'linear' },
            scale: { duration: 3, repeat: Infinity, ease: 'easeInOut' },
          }}
        />
        <motion.div
          className="v2-conv-loading-ring v2-conv-loading-ring--inner"
          animate={{ rotate: -360, opacity: [0.4, 0.8, 0.4] }}
          transition={{
            rotate: { duration: 8, repeat: Infinity, ease: 'linear' },
            opacity: { duration: 2.2, repeat: Infinity, ease: 'easeInOut' },
          }}
        />

        <motion.div
          className="v2-conv-loading-logo"
          animate={{ scale: [1, 1.08, 1], filter: ['drop-shadow(0 0 12px rgba(99,102,241,0.4))', 'drop-shadow(0 0 24px rgba(99,102,241,0.85))', 'drop-shadow(0 0 12px rgba(99,102,241,0.4))'] }}
          transition={{ duration: 2.5, repeat: Infinity, ease: 'easeInOut' }}
        >
          <PlexisLogo width={36} height={36} />
        </motion.div>
      </div>

      {/* Dynamic AI Status Heading */}
      <div className="v2-conv-loading-status-box">
        <motion.div
          key={stepIdx}
          className="v2-conv-loading-status"
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -6 }}
          transition={{ duration: 0.28, ease: 'easeOut' }}
        >
          {LOADING_STEPS[stepIdx]}
        </motion.div>

        {/* Shimmer gradient line */}
        <div className="v2-conv-loading-bar-track">
          <motion.div
            className="v2-conv-loading-bar-shimmer"
            animate={{ x: ['-100%', '100%'] }}
            transition={{ duration: 1.4, repeat: Infinity, ease: 'easeInOut' }}
          />
        </div>
      </div>
    </motion.div>
  );
}
