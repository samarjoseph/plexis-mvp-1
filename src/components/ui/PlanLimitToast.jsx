import React, { useState, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useNavigate } from 'react-router-dom';
import { Sparkles, X, ArrowUpRight } from 'lucide-react';
import { EventBus } from '../../events/EventBus.js';
import { Events } from '../../events/Events.js';
import { getPlexisUserPlan } from '../../api.js';

export default function PlanLimitToast({ userPlan = 'free' }) {
  const navigate = useNavigate();
  const [toast, setToast] = useState(null);

  useEffect(() => {
    const unsub = EventBus.on(Events.PLAN_LIMIT_NOTICE, (payload) => {
      // Only display for free plan users
      const currentPlan = getPlexisUserPlan() || userPlan;
      if (currentPlan === 'plus') return;

      const id = Date.now();
      const message = payload?.message || 'Want to meet more data? Upgrade to';
      const linkText = payload?.linkText || 'Plus';
      const duration = payload?.duration || 5500; // 5.5 seconds (within 5-6s specification)

      setToast({ id, message, linkText, duration });
    });

    return () => {
      unsub();
    };
  }, [userPlan]);

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => {
      setToast(null);
    }, toast.duration);

    return () => clearTimeout(timer);
  }, [toast]);

  if (!toast) return null;

  return (
    <AnimatePresence>
      {toast && (
        <motion.div
          key={toast.id}
          className="v2-plan-limit-toast"
          initial={{ opacity: 0, y: 24, scale: 0.94 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 16, scale: 0.94 }}
          transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
          role="alert"
        >
          <div className="v2-plan-toast-icon">
            <Sparkles size={16} />
          </div>

          <div className="v2-plan-toast-body">
            <span className="v2-plan-toast-text">{toast.message}</span>
            <button
              type="button"
              className="v2-plan-toast-link"
              onClick={() => {
                setToast(null);
                navigate('/billing');
              }}
              title="View Plexis Plus plan"
            >
              <span>{toast.linkText}</span>
              <ArrowUpRight size={13} />
            </button>
          </div>

          <button
            type="button"
            className="v2-plan-toast-close"
            onClick={() => setToast(null)}
            aria-label="Close notification"
            title="Dismiss notification"
          >
            <X size={15} />
          </button>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
