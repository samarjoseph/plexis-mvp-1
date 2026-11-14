import React, { useState, useEffect } from 'react';
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
  const [isWarmingUp, setIsWarmingUp] = useState(false);
  const [guestError, setGuestError] = useState(null);

  // Proactively ping health endpoint on landing page mount (0 LLM calls, wakes sleeping server)
  useEffect(() => {
    PlexisAPI.warmup();
  }, []);

  const handleGuestStart = async () => {
    if (isStartingGuest) return;
    setIsStartingGuest(true);
    setIsWarmingUp(false);
    setGuestError(null);

    // If backend is sleeping and takes > 2.5s, update status to reassure user
    const warmTimer = setTimeout(() => {
      setIsWarmingUp(true);
    }, 2500);

    try {
      const sessionData = await PlexisAPI.createGuestSession();
      clearTimeout(warmTimer);
      startGuestSession(sessionData);
      navigate('/dashboard');
    } catch (err) {
      clearTimeout(warmTimer);
      console.error('Failed to start guest session:', err);
      setGuestError(err.message || 'Could not start guest session. Please try again.');
      setIsStartingGuest(false);
      setIsWarmingUp(false);
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
          <span>Plexis · Your Intelligent Data Workspace</span>
        </motion.div>

        <motion.h1
          className="hero-headline"
          variants={fadeUp}
          initial="hidden"
          animate="visible"
          custom={1}
        >
          Your entire data workspace, <br className="mobile-break" /> controlled through conversation.
        </motion.h1>

        <motion.p
          className="hero-subheadline"
          variants={fadeUp}
          initial="hidden"
          animate="visible"
          custom={2}
        >
          Analyze datasets, uncover hidden patterns, and perform complex data transformations using natural language. Explore statistics, interact with live spreadsheets, and manage your entire analytical workflow in one intelligent workspace.
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
            onMouseEnter={() => PlexisAPI.warmup()}
            disabled={isStartingGuest}
          >
            {isStartingGuest ? (
              <>
                <Loader2 size={18} className="spin-icon" />
                <span>{isWarmingUp ? 'Waking up server (almost ready)...' : 'Starting guest session...'}</span>
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
          <p>Everything you need to investigate, understand, and transform data in one connected workspace.</p>
        </motion.div>

        <div className="features-grid">
          {[
            { icon: Database,      title: 'AI Data Analysis',                   desc: 'Investigate datasets, discover hidden patterns, explore statistical distributions, and uncover meaningful insights with intelligent AI-powered analysis.' },
            { icon: Shield,        title: 'Live Interactive Spreadsheet',       desc: 'Explore and modify your datasets through an integrated spreadsheet that synchronizes with your analytical workspace.' },
            { icon: Globe,         title: 'Intelligent Web Research',           desc: 'Combine web research with dataset intelligence to explore external information and enrich your analytical workflow.' },
            { icon: BarChart3,     title: 'Advanced Data Visualizations',       desc: 'Generate interactive charts and visualizations using natural-language instructions. Turn complex data into understandable insights.' },
            { icon: Brain,         title: 'Intelligent Transformations',        desc: 'Transform datasets using natural language. Review before-and-after changes, apply operations, and undo modifications whenever needed.' },
            { icon: MessageSquare, title: 'Connected Conversational Workspace', desc: 'Control workspace panels, access spreadsheet functionality, and execute multi-step operations directly through conversation.' },
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
          <h2>Turn Your Data Into Intelligence</h2>
          <p>Experience a smarter way to investigate, understand, and transform your datasets.</p>
          <div className="landing-cta-actions">
            <GoogleSignInButton variant="primary" label="Continue with Google" />
            <button
              type="button"
              className="hero-guest-btn glass-panel"
              onClick={handleGuestStart}
              onMouseEnter={() => PlexisAPI.warmup()}
              disabled={isStartingGuest}
            >
              {isStartingGuest ? (
                <>
                  <Loader2 size={16} className="spin-icon" />
                  <span>{isWarmingUp ? 'Waking up server...' : 'Starting...'}</span>
                </>
              ) : (
                <>
                  <Sparkles size={16} className="hero-guest-icon" />
                  <span>Try without signing up</span>
                </>
              )}
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
