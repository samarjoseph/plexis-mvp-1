import React, { useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { X, Check, Paintbrush, User, LogOut } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import GoogleSignInButton from '../oauth/GoogleSignInButton';
import { useAuth } from '../../context/AuthContext';

const COLORS = [
  { id: 'indigo', name: 'Indigo', hex: '#6366f1' },
  { id: 'purple', name: 'Purple', hex: '#a855f7' },
  { id: 'blue', name: 'Blue', hex: '#3b82f6' },
  { id: 'cyan', name: 'Cyan', hex: '#06b6d4' },
  { id: 'emerald', name: 'Emerald', hex: '#10b981' },
];

const ACCENT_KEY = 'plexis_accent';

export default function SettingsPanelV2({ isOpen, onClose }) {
  const { user, logout, isGuest, exitGuestSession } = useAuth();
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState('appearance');
  const [accent, setAccent] = useState(
    () => localStorage.getItem(ACCENT_KEY) || 'indigo'
  );

  useEffect(() => {
    document.documentElement.setAttribute('data-accent', accent);
    localStorage.setItem(ACCENT_KEY, accent);
  }, [accent]);

  if (!isOpen) return null;

  return (
    <div className="v2-settings-overlay" onClick={onClose}>
      <motion.div
        className="v2-settings-modal"
        onClick={(e) => e.stopPropagation()}
        initial={{ opacity: 0, scale: 0.95, y: 10 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95, y: 10 }}
        transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
      >
        {/* Left sidebar */}
        <div className="v2-settings-sidebar">
          <h2>Settings</h2>
          <button
            className={activeTab === 'appearance' ? 'active' : ''}
            onClick={() => setActiveTab('appearance')}
          >
            <Paintbrush size={16} />
            Appearance
          </button>
          <button
            className={activeTab === 'account' ? 'active' : ''}
            onClick={() => setActiveTab('account')}
          >
            <User size={16} />
            Account
          </button>
        </div>

        {/* Right content */}
        <div className="v2-settings-content">
          <header>
            <h3>{activeTab === 'appearance' ? 'Appearance' : 'Account'}</h3>
            <button onClick={onClose}>
              <X size={18} />
            </button>
          </header>

          <AnimatePresence mode="wait">
            {activeTab === 'appearance' && (
              <motion.div
                key="appearance"
                initial={{ opacity: 0, x: -10 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 10 }}
                transition={{ duration: 0.2 }}
              >
                <section>
                  <label>Accent Color</label>
                  <p style={{ color: 'var(--dash-muted)', fontSize: '0.82rem', marginBottom: '16px' }}>
                    Choose a color that defines your Plexis experience.
                  </p>
                  <div className="v2-color-picker">
                    {COLORS.map((c) => (
                      <div key={c.id} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '6px' }}>
                        <button
                          className={`v2-color-btn ${accent === c.id ? 'active' : ''}`}
                          style={{ backgroundColor: c.hex }}
                          title={c.name}
                          onClick={() => setAccent(c.id)}
                        >
                          {accent === c.id && <Check size={16} color="white" />}
                        </button>
                        <span style={{ fontSize: '0.7rem', color: accent === c.id ? 'var(--dash-text)' : 'var(--dash-dim)', fontWeight: accent === c.id ? '600' : '400' }}>
                          {c.name}
                        </span>
                      </div>
                    ))}
                  </div>
                </section>

                <section>
                  <label>Theme Preview</label>
                  <div className="v2-theme-previews">
                    <div className="v2-theme-preview active">
                      <div className="v2-theme-preview-label">Dark</div>
                      <div className="v2-theme-preview-colors">
                        <div className="v2-theme-preview-swatch" style={{ background: '#08090e' }} />
                        <div className="v2-theme-preview-swatch" style={{ background: '#0f1117' }} />
                        <div className="v2-theme-preview-swatch" style={{ background: COLORS.find(c => c.id === accent)?.hex || '#6366f1' }} />
                      </div>
                    </div>
                  </div>
                </section>
              </motion.div>
            )}

            {activeTab === 'account' && (
              <motion.div
                key="account"
                initial={{ opacity: 0, x: -10 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 10 }}
                transition={{ duration: 0.2 }}
              >
                <section>
                  <label>Profile</label>
                  <div className="v2-account-card">
                    {user?.avatar ? (
                      <img
                        className="v2-account-avatar"
                        src={user.avatar}
                        alt={user.name}
                        referrerPolicy="no-referrer"
                      />
                    ) : (
                      <div className="v2-account-avatar-fallback">
                        {user?.name?.charAt(0) || '?'}
                      </div>
                    )}
                    <div className="v2-account-info">
                      <span className="v2-account-name">{user?.name || 'User'}</span>
                      <span className="v2-account-email">{user?.email || 'No email'}</span>
                    </div>
                  </div>
                </section>

                <section>
                  <label>Authentication</label>
                  {isGuest ? (
                    <>
                      <p style={{ color: 'var(--dash-muted)', fontSize: '0.82rem', marginBottom: '14px' }}>
                        You are exploring Plexis in Guest Mode with a temporary session.
                      </p>
                      <div style={{ marginBottom: '14px' }}>
                        <GoogleSignInButton variant="nav" label="Sign in with Google" />
                      </div>
                      <button
                        className="v2-signout-btn"
                        onClick={() => {
                          onClose();
                          exitGuestSession?.();
                          logout();
                          navigate('/');
                        }}
                      >
                        <LogOut size={16} />
                        Exit Guest Mode
                      </button>
                    </>
                  ) : (
                    <>
                      <p style={{ color: 'var(--dash-muted)', fontSize: '0.82rem', marginBottom: '16px' }}>
                        Your account is managed via Google OAuth.
                      </p>
                      <button
                        className="v2-signout-btn"
                        onClick={() => {
                          onClose();
                          logout();
                        }}
                      >
                        <LogOut size={16} />
                        Sign out
                      </button>
                    </>
                  )}
                </section>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </motion.div>
    </div>
  );
}
