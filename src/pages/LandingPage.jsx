import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { motion } from 'framer-motion';
import { Database, BarChart3, MessageSquare, Globe, Brain, Shield, Sparkles, ArrowRight, Loader2 } from 'lucide-react';
import GoogleSignInButton from '../components/oauth/GoogleSignInButton';
import PlexisLogo from '../components/brand/PlexisLogo';
import { useAuth } from '../context/AuthContext';
import { PlexisAPI } from '../api';
import './LandingPage.css';

const fadeUp = {
  hidden: { opacity: 0, y: 30 },
  visible: (i = 0) => ({
    opacity: 1,
    y: 0,
    transition: { duration: 0.8, delay: i * 0.15, ease: [0.16, 1, 0.3, 1] },
  }),
};

export default function LandingPage() {
  const navigate = useNavigate();
  const { startGuestSession } = useAuth();
  const [isStartingGuest, setIsStartingGuest] = useState(false);
  const [guestError, setGuestError] = useState(null);

  const handleGuestStart = async () => {
    if (isStartingGuest) return;
    setIsStartingGuest(true);
    setGuestError(null);
    try {
      const sessionData = await PlexisAPI.createGuestSession();
      startGuestSession(sessionData);
      navigate('/dashboard');
    } catch (err) {
      console.error('Failed to start guest session:', err);
      setGuestError(err.message || 'Could not start guest session. Please try again.');
      setIsStartingGuest(false);
    }
  };

  return (
    <div className="landing">
      {/* Background */}
      <div className="landing-bg">
        <div className="bg-grid"></div>
      </div>

      {/* Navbar */}
      <nav className="landing-nav-top">
        <div className="nav-brand">
          <div className="nav-brand-logo-wrap" aria-hidden="true">
            <PlexisLogo width={28} height={28} />
          </div>
          <span className="nav-brand-text">Plexis</span>
        </div>
        <div className="nav-actions">
          <GoogleSignInButton variant="nav" label="Continue with Google" />
        </div>
      </nav>

      {/* Hero */}
      <section className="landing-hero">
        <motion.div
          className="hero-badge glass-panel"
          variants={fadeUp}
          initial="hidden"
          animate="visible"
          custom={0}
        >
          <span className="badge-dot"></span>
          <span>Plexis · Intelligent Data Analyst</span>
        </motion.div>

        <motion.h1
          className="hero-headline"
          variants={fadeUp}
          initial="hidden"
          animate="visible"
          custom={1}
        >
          Plexis – AI-Powered <br className="mobile-break" /> Data Analysis
        </motion.h1>

        <motion.p
          className="hero-subheadline"
          variants={fadeUp}
          initial="hidden"
          animate="visible"
          custom={2}
        >
          Meet Plexis, your intelligent AI data analyst. Upload CSV and Excel
          datasets, discover instant insights, perform AI spreadsheet analysis, and transform
          data seamlessly from one unified workspace.
        </motion.p>

        {/* Hero CTA Button Group */}
        <motion.div
          className="hero-cta-group"
          variants={fadeUp}
          initial="hidden"
          animate="visible"
          custom={3}
        >
          <GoogleSignInButton variant="hero" label="Continue with Google" />
          <button
            type="button"
            className="hero-guest-btn glass-panel"
            onClick={handleGuestStart}
            disabled={isStartingGuest}
          >
            {isStartingGuest ? (
              <>
                <Loader2 size={18} className="spin-icon" />
                <span>Starting guest session...</span>
              </>
            ) : (
              <>
                <Sparkles size={18} className="hero-guest-icon" />
                <span>Try without signing up</span>
                <ArrowRight size={16} className="hero-guest-arrow" />
              </>
            )}
          </button>
        </motion.div>

        {guestError && <p className="landing-guest-error">{guestError}</p>}
      </section>

      {/* Features */}
      <section id="features" className="landing-section">
        <motion.div
          className="section-header"
          initial={{ opacity: 0, y: 30 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.7 }}
        >
          <span className="section-tag">Features</span>
          <h2>Built for modern data teams</h2>
          <p>Everything you need to go from raw CSV to actionable intelligence.</p>
        </motion.div>

        <div className="features-grid">
          {[
            { icon: Database,      title: 'AI Data Analysis',          desc: 'Instant profiling, correlations, and structural insights powered by AI data analysis.' },
            { icon: Shield,        title: 'AI Spreadsheet Analysis',   desc: 'Analyze CSV and Excel tables with automated insight and formula precision.' },
            { icon: Globe,         title: 'Web Research',              desc: 'Blend live web data with local dataset intelligence effortlessly.' },
            { icon: BarChart3,     title: 'Advanced Charts',           desc: 'Generate complex charts and data visualizations from natural language.' },
            { icon: Brain,         title: 'Conversation Memory',       desc: 'Persistent session context and user preferences for seamless workflows.' },
            { icon: MessageSquare, title: 'Multi-Chat Workspace',      desc: 'Context-aware follow-ups with isolated chat memories and sessions.' },
          ].map((feature, i) => (
            <motion.div
              key={feature.title}
              className="feature-card glass-panel"
              initial={{ opacity: 0, y: 30 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ delay: i * 0.1, duration: 0.6 }}
              whileHover={{ y: -8, scale: 1.02, transition: { duration: 0.3 } }}
            >
              <div className="feature-icon-wrapper">
                <feature.icon size={24} className="feature-icon" />
              </div>
              <h3>{feature.title}</h3>
              <p>{feature.desc}</p>
            </motion.div>
          ))}
        </div>
      </section>

      {/* CTA */}
      <section className="landing-cta-section">
        <motion.div
          className="cta-card glass-panel"
          initial={{ opacity: 0, scale: 0.95 }}
          whileInView={{ opacity: 1, scale: 1 }}
          viewport={{ once: true }}
          transition={{ duration: 0.7 }}
        >
          <h2>Start analyzing smarter today</h2>
          <p>Experience intelligent data discovery and automated transformations.</p>
          <div className="landing-cta-actions">
            <GoogleSignInButton variant="primary" label="Continue with Google" />
            <button
              type="button"
              className="hero-guest-btn glass-panel"
              onClick={handleGuestStart}
              disabled={isStartingGuest}
            >
              <Sparkles size={16} className="hero-guest-icon" />
              <span>Try without signing up</span>
            </button>
          </div>
        </motion.div>
      </section>

      {/* Footer */}
      <footer className="landing-footer glass-panel">
        <div className="footer-content">
          <div className="footer-brand">
            <PlexisLogo width={20} height={20} />
            <span>Plexis</span>
          </div>
          <p className="footer-text">
            &copy; {new Date().getFullYear()} Plexis. All rights reserved.
          </p>
        </div>
      </footer>
    </div>
  );
}
