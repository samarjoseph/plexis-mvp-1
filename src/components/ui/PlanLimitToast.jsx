import React, { useState, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useNavigate } from 'react-router-dom';
import { X } from 'lucide-react';
import { EventBus } from '../../events/EventBus.js';
import { Events } from '../../events/Events.js';
import { getPlexisUserPlan } from '../../api.js';

export default function PlanLimitToast({ userPlan = 'free' }) {
  const navigate = useNavigate();
  const [toast, setToast] = useState(null);

  useEffect(() => {
    const unsub = EventBus.on(Events.PLAN_LIMIT_NOTICE, (payload) => {
      // Only display for free tier users
      const currentPlan = getPlexisUserPlan() || userPlan;
      if (currentPlan === 'plus') return;

      const id = Date.now();
      const message = payload?.message || 'Want to meet more data? Upgrade to';
      const duration = payload?.duration || 5500;

      setToast({ id, message, duration });
    });

    return () => unsub();
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
          initial={{ opacity: 0, y: 16, scale: 0.96 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 12, scale: 0.96 }}
          transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
          role="alert"
        >
          <span className="v2-plan-toast-text">
            {toast.message}{' '}
            <button
              type="button"
              className="v2-plan-toast-plus-btn"
              onClick={() => {
                setToast(null);
                navigate('/billing');
              }}
              title="View Plus plan"
            >
              Plus
            </button>
          </span>

          <button
            type="button"
            className="v2-plan-toast-close"
            onClick={() => setToast(null)}
            aria-label="Dismiss notification"
          >
            <X size={13} />
          </button>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
