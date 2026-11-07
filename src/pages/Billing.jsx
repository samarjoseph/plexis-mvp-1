import React, { useState, useEffect, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useNavigate } from 'react-router-dom';
import {
  ArrowLeft,
  Check,
  Sparkles,
  Zap,
  Shield,
  Layers,
  Database,
  Cpu,
  MessageSquare,
  Clock,
  HardDrive,
  CheckCircle2,
  AlertCircle,
  X,
} from 'lucide-react';
import { PlexisAPI, getPlexisUserPlan } from '../api.js';
import PlexisLogo from '../components/brand/PlexisLogo.jsx';
import '../styles/billing.css';

export default function Billing() {
  const navigate = useNavigate();
  const [currentPlan, setCurrentPlan] = useState(() => getPlexisUserPlan());
  const [planData, setPlanData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [updating, setUpdating] = useState(false);
  const [toastMessage, setToastMessage] = useState(null);
  const mousePosRef = useRef({ x: null, y: null });

  // Track live mouse position for cursor-anchored notification bubbles
  useEffect(() => {
    const handleMouseMove = (e) => {
      mousePosRef.current = { x: e.clientX, y: e.clientY };
    };
    window.addEventListener('mousemove', handleMouseMove, { passive: true });
    return () => window.removeEventListener('mousemove', handleMouseMove);
  }, []);

  const showToast = (val) => {
    if (!val) {
      setToastMessage(null);
      return;
    }
    const text = typeof val === 'string' ? val : val?.text || '';
    const cursor =
      mousePosRef.current.x != null && mousePosRef.current.y != null
        ? { ...mousePosRef.current }
        : null;
    setToastMessage({ text, cursor, id: Date.now() });
  };

  useEffect(() => {
    if (!toastMessage) return;
    const timer = setTimeout(() => {
      setToastMessage(null);
    }, 10000);
    return () => clearTimeout(timer);
  }, [toastMessage]);

  useEffect(() => {
    let mounted = true;
    PlexisAPI.getUserPlan()
      .then((data) => {
        if (!mounted) return;
        setPlanData(data);
        if (data?.plan) {
          setCurrentPlan(data.plan);
        }
      })
      .catch((err) => {
        console.warn('[Billing] Failed to load plan data:', err);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, []);

  useEffect(() => {
    // Check for Stripe Checkout return
    const params = new URLSearchParams(window.location.search);
    const sessionId = params.get('session_id');
    const isSuccess = params.get('success');
    const isCanceled = params.get('canceled');

    if (sessionId && isSuccess === 'true') {
      showToast('Verifying Stripe payment...');
      PlexisAPI.verifyBillingSession(sessionId)
        .then((res) => {
          if (res?.verified) {
            setCurrentPlan('plus');
            setPlanData((prev) => (prev ? { ...prev, plan: 'plus' } : { plan: 'plus' }));
            try { localStorage.setItem('plexis_user_plan', 'plus'); } catch (_) {}
            showToast('Payment successful! You are now subscribed to Plexis Plus ($30/mo).');
            window.history.replaceState({}, document.title, window.location.pathname);
            PlexisAPI.getUserPlan().then((fresh) => {
              if (fresh?.plan) {
                setCurrentPlan(fresh.plan);
                setPlanData(fresh);
              }
            });
          } else {
            showToast('Payment could not be verified. Please contact support.');
          }
        })
        .catch((e) => {
          console.error('[Billing] Session verification error:', e);
          showToast(`Payment verification error: ${e.message}`);
        });
    } else if (isCanceled === 'true') {
      showToast('Checkout canceled. You can subscribe whenever you are ready.');
      window.history.replaceState({}, document.title, window.location.pathname);
    }
  }, []);

  const handleUpgradeToPlus = async () => {
    if (updating) return;
    setUpdating(true);
    showToast('Redirecting to Stripe Checkout...');
    try {
      const res = await PlexisAPI.createCheckoutSession();
      if (res?.checkout_url) {
        window.location.href = res.checkout_url;
      } else {
        throw new Error(res?.error || 'No checkout URL received from Stripe');
      }
    } catch (err) {
      console.error('[Billing] Stripe checkout initiation failed:', err);
      showToast(err.message || 'Failed to initiate Stripe Checkout. Please try again.');
      setUpdating(false);
    }
  };

  const handleManagePortal = async () => {
    setUpdating(true);
    try {
      const res = await PlexisAPI.createCustomerPortalSession();
      if (res?.portal_url) {
        window.location.href = res.portal_url;
      } else {
        throw new Error('No portal URL received');
      }
    } catch (err) {
      console.warn('[Billing] Portal error:', err);
      showToast('Stripe portal is unavailable in test mode. You can toggle your plan below.');
    } finally {
      setUpdating(false);
    }
  };

  const handleSwitchPlan = async (targetPlan) => {
    if (targetPlan === currentPlan || updating) return;
    if (targetPlan === 'plus') {
      return handleUpgradeToPlus();
    }
    setUpdating(true);
    try {
      const res = await PlexisAPI.updateUserPlan('free');
      setCurrentPlan('free');
      if (res?.plan) {
        setPlanData((prev) => (prev ? { ...prev, plan: res.plan } : prev));
      }
      showToast('Switched to Plexis Free plan.');
    } catch (err) {
      console.error('[Billing] Plan switch error:', err);
      showToast('Failed to update plan. Please try again.');
    } finally {
      setUpdating(false);
    }
  };

  const isPlus = currentPlan === 'plus';

  return (
    <motion.div
      className="v2-billing-page"
      initial={{ opacity: 0, y: 15 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -15 }}
      transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
    >
      <div className="v2-billing-ambient" />

      {/* Top Nav */}
      <header className="v2-billing-nav">
        <button
          className="v2-billing-back-btn"
          onClick={() => navigate('/dashboard')}
          aria-label="Back to Dashboard"
        >
          <ArrowLeft size={16} />
          <span>Dashboard</span>
        </button>

        <div className="v2-billing-nav-brand" onClick={() => navigate('/dashboard')}>
          <PlexisLogo width={22} height={22} />
          <span className="v2-billing-brand-text">Plexis</span>
          <span className={`v2-plan-badge v2-plan-badge--${currentPlan}`}>
            {currentPlan === 'plus' ? (
              <>
                <Sparkles size={11} color="#60a5fa" /> Plus
              </>
            ) : (
              'Free'
            )}
          </span>
        </div>

        <div style={{ width: '80px' }} />
      </header>

      {/* Hero */}
      <section className="v2-billing-hero">
        <motion.div
          className="v2-billing-hero-eyebrow"
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.3, delay: 0.1 }}
        >
          <Sparkles size={13} />
          <span>Plans & Intelligence Quotas</span>
        </motion.div>

        <motion.h1
          className="v2-billing-hero-title"
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.35, delay: 0.15 }}
        >
          Scale Your Data Intelligence
        </motion.h1>

        <motion.p
          className="v2-billing-hero-desc"
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.35, delay: 0.2 }}
        >
          Choose between the essential exploratory Free workspace and the unrestricted, persistent Plus engine with VIP model priority.
        </motion.p>
      </section>

      {/* Pricing Cards Grid */}
      <div className="v2-billing-grid">
        {/* FREE PLAN CARD */}
        <motion.div
          className={`v2-billing-card ${!isPlus ? 'v2-billing-card--active' : ''}`}
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.35, delay: 0.25 }}
        >
          <div className="v2-billing-card-header">
            <span className="v2-billing-card-name">
              Free Plan
              {!isPlus && (
                <span className="v2-plan-badge v2-plan-badge--free">Current</span>
              )}
            </span>
          </div>

          <div className="v2-billing-card-price-box">
            <span className="v2-billing-card-price">$0</span>
            <span className="v2-billing-card-period">/ month</span>
          </div>

          <p className="v2-billing-card-desc">
            Essential exploratory analysis for individuals and students getting started.
          </p>

          <ul className="v2-billing-features">
            <li className="v2-billing-feature-item">
              <span className="v2-billing-feature-icon v2-billing-feature-icon--warn">
                <Clock size={15} />
              </span>
              <span>
                <strong>Ephemeral Transformations</strong> — Resets to original dataset on sleep or server restart
              </span>
            </li>
            <li className="v2-billing-feature-item">
              <span className="v2-billing-feature-icon v2-billing-feature-icon--check">
                <Check size={15} />
              </span>
              <span>
                <strong>15,000 Rows</strong> rendered on virtual spreadsheet
              </span>
            </li>
            <li className="v2-billing-feature-item">
              <span className="v2-billing-feature-icon v2-billing-feature-icon--check">
                <Check size={15} />
              </span>
              <span>
                <strong>4 Transformations</strong> per day
              </span>
            </li>
            <li className="v2-billing-feature-item">
              <span className="v2-billing-feature-icon v2-billing-feature-icon--check">
                <Check size={15} />
              </span>
              <span>
                <strong>Max 10 Chats</strong> per account (delete to create new)
              </span>
            </li>
            <li className="v2-billing-feature-item">
              <span className="v2-billing-feature-icon v2-billing-feature-icon--check">
                <Check size={15} />
              </span>
              <span>
                <strong>50 Messages</strong> & 7,000 tokens per chat
              </span>
            </li>
            <li className="v2-billing-feature-item">
              <span className="v2-billing-feature-icon v2-billing-feature-icon--check">
                <Check size={15} />
              </span>
              <span>
                <strong>500 Total Messages</strong> across all chats
              </span>
            </li>
            <li className="v2-billing-feature-item">
              <span className="v2-billing-feature-icon v2-billing-feature-icon--check">
                <Check size={15} />
              </span>
              <span>
                <strong>35 MB Uploads</strong> (max 40,000 rows in dataset)
              </span>
            </li>
            <li className="v2-billing-feature-item">
              <span className="v2-billing-feature-icon v2-billing-feature-icon--warn">
                <Cpu size={15} />
              </span>
              <span>Standard LLM Inference queue</span>
            </li>
          </ul>

          <button
            className={`v2-billing-cta-btn ${!isPlus ? 'v2-billing-cta-btn--current' : 'v2-billing-cta-btn--downgrade'}`}
            disabled={!isPlus || updating}
            onClick={() => handleSwitchPlan('free')}
          >
            {!isPlus ? 'Current Plan' : 'Downgrade to Free'}
          </button>
        </motion.div>

        {/* PLUS PLAN CARD */}
        <motion.div
          className={`v2-billing-card v2-billing-card--plus ${isPlus ? 'v2-billing-card--active' : ''}`}
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.35, delay: 0.3 }}
        >
          {/* Strawberry-Pink Popular Badge on the left corner */}
          <div className="v2-billing-popular-tag">
            Popular
          </div>

          <div className="v2-billing-card-header">
            <span className="v2-billing-card-name">
              <Sparkles size={20} color="#3b82f6" />
              Plus Plan
              {isPlus && (
                <span className="v2-plan-badge v2-plan-badge--plus">Active</span>
              )}
            </span>
            <span className="v2-plan-badge v2-plan-badge--plus">PLUS</span>
          </div>

          <div className="v2-billing-card-price-box">
            <span className="v2-billing-card-price">$30</span>
            <span className="v2-billing-card-period">/ month</span>
          </div>

          <p className="v2-billing-card-desc">
            Complete data superpower with persistent memory, unlimited rows, and priority compute.
          </p>

          <ul className="v2-billing-features">
            <li className="v2-billing-feature-item v2-billing-feature-item--highlight">
              <span className="v2-billing-feature-icon v2-billing-feature-icon--plus">
                <Shield size={15} />
              </span>
              <span>
                <strong>100% Persistent Transformations</strong> — Changes stay intact across page refresh, backend sleep, and server restarts
              </span>
            </li>
            <li className="v2-billing-feature-item v2-billing-feature-item--highlight">
              <span className="v2-billing-feature-icon v2-billing-feature-icon--plus">
                <Zap size={15} />
              </span>
              <span>
                <strong>VIP Priority LLM Inference</strong> — Answered first ahead of standard requests
              </span>
            </li>
            <li className="v2-billing-feature-item v2-billing-feature-item--highlight">
              <span className="v2-billing-feature-icon v2-billing-feature-icon--plus">
                <Check size={15} />
              </span>
              <span>
                <strong>Whole Dataset Rendered</strong> — Unlimited rows on spreadsheet (No 15,000 cap)
              </span>
            </li>
            <li className="v2-billing-feature-item v2-billing-feature-item--highlight">
              <span className="v2-billing-feature-icon v2-billing-feature-icon--plus">
                <Check size={15} />
              </span>
              <span>
                <strong>Unlimited Transformations</strong> — Execute as many as you want
              </span>
            </li>
            <li className="v2-billing-feature-item v2-billing-feature-item--highlight">
              <span className="v2-billing-feature-icon v2-billing-feature-icon--plus">
                <Check size={15} />
              </span>
              <span>
                <strong>Unlimited Chats</strong> — Create and organize without limits
              </span>
            </li>
            <li className="v2-billing-feature-item v2-billing-feature-item--highlight">
              <span className="v2-billing-feature-icon v2-billing-feature-icon--plus">
                <Check size={15} />
              </span>
              <span>
                <strong>Unlimited Messages & Tokens</strong> per chat and account
              </span>
            </li>
            <li className="v2-billing-feature-item v2-billing-feature-item--highlight">
              <span className="v2-billing-feature-icon v2-billing-feature-icon--plus">
                <Check size={15} />
              </span>
              <span>
                <strong>50 MB File Uploads</strong> with unlimited dataset rows
              </span>
            </li>
          </ul>

          {isPlus ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', width: '100%' }}>
              <button
                type="button"
                className="v2-billing-cta-btn v2-billing-cta-btn--current"
                disabled
              >
                <CheckCircle2 size={16} /> Active Plan
              </button>
              <button
                type="button"
                className="v2-billing-cta-btn"
                style={{
                  background: 'rgba(37, 99, 235, 0.14)',
                  border: '1px solid rgba(59, 130, 246, 0.38)',
                  color: '#eff6ff',
                  fontSize: '13px',
                }}
                disabled={updating}
                onClick={handleUpgradeToPlus}
                title="Open Stripe payment interface to test card checkout"
              >
                <Sparkles size={14} color="#60a5fa" /> Open Stripe Checkout Page
              </button>
            </div>
          ) : (
            <button
              type="button"
              className="v2-billing-cta-btn v2-billing-cta-btn--upgrade"
              disabled={updating}
              onClick={handleUpgradeToPlus}
            >
              <Sparkles size={16} /> Upgrade to Plus ($30/mo)
            </button>
          )}
        </motion.div>
      </div>

      {/* Comparison Matrix */}
      <section className="v2-billing-matrix-section">
        <h2 className="v2-billing-matrix-title">Full Capability Comparison</h2>

        <table className="v2-billing-matrix-table">
          <thead>
            <tr>
              <th>Feature & Limit</th>
              <th>Free Plan</th>
              <th>Plus Plan</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td className="v2-matrix-col-feature">Transformation Persistence</td>
              <td className="v2-matrix-col-free">Resets on cold restart / wake up</td>
              <td className="v2-matrix-col-plus">Permanent & durable across restarts</td>
            </tr>
            <tr>
              <td className="v2-matrix-col-feature">Spreadsheet Rendering</td>
              <td className="v2-matrix-col-free">Max 15,000 rows</td>
              <td className="v2-matrix-col-plus">Whole dataset (Unlimited rows)</td>
            </tr>
            <tr>
              <td className="v2-matrix-col-feature">Daily AI Transformations</td>
              <td className="v2-matrix-col-free">4 transformations / day</td>
              <td className="v2-matrix-col-plus">Unlimited transformations</td>
            </tr>
            <tr>
              <td className="v2-matrix-col-feature">Chat Conversations</td>
              <td className="v2-matrix-col-free">10 chats per account</td>
              <td className="v2-matrix-col-plus">Unlimited chats</td>
            </tr>
            <tr>
              <td className="v2-matrix-col-feature">Messages per Chat</td>
              <td className="v2-matrix-col-free">50 messages</td>
              <td className="v2-matrix-col-plus">Unlimited messages</td>
            </tr>
            <tr>
              <td className="v2-matrix-col-feature">Tokens per Chat</td>
              <td className="v2-matrix-col-free">7,000 tokens</td>
              <td className="v2-matrix-col-plus">Unlimited tokens</td>
            </tr>
            <tr>
              <td className="v2-matrix-col-feature">Total Messages Across Account</td>
              <td className="v2-matrix-col-free">500 total messages</td>
              <td className="v2-matrix-col-plus">Unlimited total messages</td>
            </tr>
            <tr>
              <td className="v2-matrix-col-feature">Dataset Upload Capacity</td>
              <td className="v2-matrix-col-free">35 MB & max 40,000 rows</td>
              <td className="v2-matrix-col-plus">50 MB & unlimited rows</td>
            </tr>
            <tr>
              <td className="v2-matrix-col-feature">LLM Inference Scheduling</td>
              <td className="v2-matrix-col-free">Standard queue</td>
              <td className="v2-matrix-col-plus">VIP Priority Queue (Answered first)</td>
            </tr>
          </tbody>
        </table>
      </section>

      {/* Cursor-Anchored Toast Notification */}
      {(() => {
        let toastPosStyle = {};
        let placeAbove = true;
        let tailLeft = 20;

        if (toastMessage?.cursor) {
          const vw = typeof window !== 'undefined' ? window.innerWidth : 1200;
          const vh = typeof window !== 'undefined' ? window.innerHeight : 800;
          const bubbleWidth = 330;

          if (toastMessage.cursor.y >= 65) {
            placeAbove = true;
            toastPosStyle = {
              top: `${Math.max(10, toastMessage.cursor.y - 52)}px`,
            };
          } else {
            placeAbove = false;
            toastPosStyle = {
              top: `${Math.min(vh - 60, toastMessage.cursor.y + 22)}px`,
            };
          }

          const idealLeft = toastMessage.cursor.x - 140;
          const left = Math.max(16, Math.min(idealLeft, vw - bubbleWidth - 16));
          toastPosStyle.left = `${left}px`;
          toastPosStyle.bottom = 'auto';
          toastPosStyle.right = 'auto';

          tailLeft = Math.max(16, Math.min(toastMessage.cursor.x - left, bubbleWidth - 28));
        } else {
          toastPosStyle = {
            bottom: '28px',
            right: '28px',
            top: 'auto',
            left: 'auto',
          };
        }

        return (
          <AnimatePresence>
            {toastMessage && (
              <motion.div
                key={toastMessage.id || 'billing-toast'}
                className="v2-billing-toast"
                style={toastPosStyle}
                initial={{ opacity: 0, y: placeAbove ? 6 : -6, scale: 0.95 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, y: placeAbove ? 4 : -4, scale: 0.95 }}
                transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
                role="alert"
              >
                <div className="v2-plan-toast-logo" aria-hidden="true">
                  <Sparkles size={13} color="#3b82f6" />
                </div>
                <span className="v2-billing-toast-text">{toastMessage.text || toastMessage}</span>
                <button
                  type="button"
                  className="v2-billing-toast-close"
                  onClick={() => setToastMessage(null)}
                  aria-label="Dismiss notification"
                >
                  <X size={13} />
                </button>
                <span
                  className={`v2-billing-toast-tail ${
                    toastMessage.cursor
                      ? placeAbove
                        ? 'v2-billing-toast-tail--bottom'
                        : 'v2-billing-toast-tail--top'
                      : 'v2-billing-toast-tail--fallback'
                  }`}
                  style={toastMessage.cursor ? { left: `${tailLeft}px` } : undefined}
                />
              </motion.div>
            )}
          </AnimatePresence>
        );
      })()}
    </motion.div>
  );
}
