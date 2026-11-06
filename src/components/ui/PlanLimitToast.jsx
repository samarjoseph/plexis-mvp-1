import React, { useState, useEffect, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useNavigate } from 'react-router-dom';
import { X, Sparkles } from 'lucide-react';
import { EventBus } from '../../events/EventBus.js';
import { Events } from '../../events/Events.js';
import { getPlexisUserPlan } from '../../api.js';

export default function PlanLimitToast({ userPlan = 'free' }) {
  const navigate = useNavigate();
  const [toast, setToast] = useState(null);
  const mousePosRef = useRef({ x: null, y: null });

  // Track cursor position dynamically across the window
  useEffect(() => {
    const handleMouseMove = (e) => {
      mousePosRef.current = { x: e.clientX, y: e.clientY };
    };

    window.addEventListener('mousemove', handleMouseMove, { passive: true });
    return () => window.removeEventListener('mousemove', handleMouseMove);
  }, []);

  useEffect(() => {
    const unsub = EventBus.on(Events.PLAN_LIMIT_NOTICE, (payload) => {
      // Only display for free tier users
      const currentPlan = getPlexisUserPlan() || userPlan;
      if (currentPlan === 'plus') return;

      const id = Date.now();
      const message = payload?.message || 'Want to meet more data? Upgrade to';
      const linkText = payload?.linkText || 'Plus';
      const duration = payload?.duration || 10000;

      // Anchor notification bubble to cursor position
      const cursor =
        payload?.x != null && payload?.y != null
          ? { x: payload.x, y: payload.y }
          : mousePosRef.current.x != null && mousePosRef.current.y != null
            ? { x: mousePosRef.current.x, y: mousePosRef.current.y }
            : null;

      setToast({ id, message, linkText, duration, cursor });
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

  // Compute position relative to cursor with viewport boundary clamping
  let positionStyle = {};
  let placeAbove = true;
  let tailLeft = 20;

  if (toast?.cursor) {
    const vw = typeof window !== 'undefined' ? window.innerWidth : 1200;
    const vh = typeof window !== 'undefined' ? window.innerHeight : 800;
    const bubbleWidth = 330;

    if (toast.cursor.y >= 65) {
      placeAbove = true;
      positionStyle = {
        top: `${Math.max(10, toast.cursor.y - 52)}px`,
      };
    } else {
      placeAbove = false;
      positionStyle = {
        top: `${Math.min(vh - 60, toast.cursor.y + 22)}px`,
      };
    }

    const idealLeft = toast.cursor.x - 140;
    const left = Math.max(16, Math.min(idealLeft, vw - bubbleWidth - 16));
    positionStyle.left = `${left}px`;
    positionStyle.bottom = 'auto';
    positionStyle.right = 'auto';

    tailLeft = Math.max(16, Math.min(toast.cursor.x - left, bubbleWidth - 28));
  } else {
    positionStyle = {
      bottom: '28px',
      right: '28px',
      top: 'auto',
      left: 'auto',
    };
  }

  return (
    <AnimatePresence>
      {toast && (
        <motion.div
          key={toast.id}
          className="v2-plan-limit-toast"
          style={positionStyle}
          initial={{ opacity: 0, y: placeAbove ? 6 : -6, scale: 0.95 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: placeAbove ? 4 : -4, scale: 0.95 }}
          transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
          role="alert"
        >
          {/* Blue logo icon */}
          <div className="v2-plan-toast-logo" aria-hidden="true">
            <Sparkles size={13} color="#3b82f6" />
          </div>

          <span className="v2-plan-toast-text">
            {toast.message}{' '}
            <button
              type="button"
              className="v2-plan-toast-plus-btn"
              onClick={() => {
                setToast(null);
                navigate('/billing');
              }}
              title={`View ${toast.linkText} plan`}
            >
              {toast.linkText}
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

          {/* Dynamic tail pointing at cursor */}
          <span
            className={`v2-plan-toast-tail ${
              toast.cursor
                ? placeAbove
                  ? 'v2-plan-toast-tail--bottom'
                  : 'v2-plan-toast-tail--top'
                : 'v2-plan-toast-tail--fallback'
            }`}
            style={toast.cursor ? { left: `${tailLeft}px` } : undefined}
          />
        </motion.div>
      )}
    </AnimatePresence>
  );
}
