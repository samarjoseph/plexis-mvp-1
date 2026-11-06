import React, { useState, useEffect } from 'react';
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

  const handleSwitchPlan = async (targetPlan) => {
    if (targetPlan === currentPlan || updating) return;
    setUpdating(true);
    try {
      const res = await PlexisAPI.updateUserPlan(targetPlan);
      setCurrentPlan(targetPlan);
      if (res?.plan) {
        setPlanData((prev) => (prev ? { ...prev, plan: res.plan } : prev));
      }
      setToastMessage(
        targetPlan === 'plus'
          ? '🌟 Upgraded to Plexis Plus! Unlimited persistence and VIP priority inference activated.'
          : 'Switched to Plexis Free plan.'
      );
      setTimeout(() => setToastMessage(null), 5000);
    } catch (err) {
      console.error('[Billing] Plan switch error:', err);
      setToastMessage('Failed to update plan. Please try again.');
      setTimeout(() => setToastMessage(null), 4000);
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
                <Sparkles size={11} /> Plus
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
          <div className="v2-billing-card-glow" />

          <div className="v2-billing-card-header">
            <span className="v2-billing-card-name">
              <Sparkles size={20} color="#a855f7" />
              Plus Plan
              {isPlus && (
                <span className="v2-plan-badge v2-plan-badge--plus">Active</span>
              )}
            </span>
            <span className="v2-plan-badge v2-plan-badge--plus">PLUS</span>
          </div>

          <div className="v2-billing-card-price-box">
            <span className="v2-billing-card-price">$20</span>
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

          <button
            className={`v2-billing-cta-btn ${isPlus ? 'v2-billing-cta-btn--current' : 'v2-billing-cta-btn--upgrade'}`}
            disabled={isPlus || updating}
            onClick={() => handleSwitchPlan('plus')}
          >
            {isPlus ? (
              <>
                <CheckCircle2 size={16} /> Active Plan
              </>
            ) : (
              <>
                <Sparkles size={16} /> Upgrade to Plus
              </>
            )}
          </button>
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

      {/* Toast Notification */}
      <AnimatePresence>
        {toastMessage && (
          <motion.div
            className="v2-billing-toast"
            initial={{ opacity: 0, y: 20, scale: 0.95 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 20, scale: 0.95 }}
            transition={{ duration: 0.2 }}
          >
            <Sparkles size={16} color="#a855f7" />
            <span>{toastMessage}</span>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
