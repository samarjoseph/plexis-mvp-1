import React, { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ArrowLeft,
  BookOpen,
  HelpCircle,
  Send,
  UploadCloud,
  X,
  CheckCircle2,
  AlertCircle,
  Sparkles,
  Database,
  Cpu,
  Layers,
  Table,
  Terminal,
  ChevronDown,
  ChevronUp,
  ShieldCheck,
  FileText,
} from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import PlexisLogo from '../components/brand/PlexisLogo';
import { PlexisAPI } from '../api';
import '../styles/help.css';

export default function Help() {
  const navigate = useNavigate();
  const { user } = useAuth();

  // Form states
  const [name, setName] = useState(user?.name || '');
  const [email, setEmail] = useState(user?.email || '');
  const [category, setCategory] = useState('Bug / Issue');
  const [message, setMessage] = useState('');
  const [screenshot, setScreenshot] = useState(null); // { name, size, dataUrl }
  const [submitting, setSubmitting] = useState(false);
  const [submitSuccess, setSubmitSuccess] = useState(false);
  const [submitError, setSubmitError] = useState('');
  const [isDragOver, setIsDragOver] = useState(false);

  // FAQ accordion state
  const [openFaq, setOpenFaq] = useState(null);

  const fileInputRef = useRef(null);

  // Sync user info if loaded late
  useEffect(() => {
    if (user?.name && !name) setName(user.name);
    if (user?.email && !email) setEmail(user.email);
  }, [user]);

  // Support paste screenshot from clipboard
  useEffect(() => {
    const handlePaste = (e) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      for (let i = 0; i < items.length; i++) {
        if (items[i].type.indexOf('image') !== -1) {
          const file = items[i].getAsFile();
          if (file) {
            processImageFile(file);
            break;
          }
        }
      }
    };
    window.addEventListener('paste', handlePaste);
    return () => window.removeEventListener('paste', handlePaste);
  }, []);

  const processImageFile = (file) => {
    if (!file.type.startsWith('image/')) {
      setSubmitError('Please attach an image file (PNG, JPG, WebP).');
      return;
    }
    if (file.size > 5 * 1024 * 1024) {
      setSubmitError('Screenshot must be smaller than 5 MB.');
      return;
    }
    const reader = new FileReader();
    reader.onload = (e) => {
      setScreenshot({
        name: file.name || 'clipboard-screenshot.png',
        size: (file.size / 1024).toFixed(1) + ' KB',
        dataUrl: e.target.result,
      });
      setSubmitError('');
    };
    reader.readAsDataURL(file);
  };

  const handleFileChange = (e) => {
    const file = e.target.files?.[0];
    if (file) processImageFile(file);
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setIsDragOver(false);
    const file = e.dataTransfer.files?.[0];
    if (file) processImageFile(file);
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!message.trim()) {
      setSubmitError('Please describe the problem or your feedback.');
      return;
    }
    setSubmitting(true);
    setSubmitError('');
    setSubmitSuccess(false);

    try {
      await PlexisAPI.submitHelpFeedback({
        name: name.trim() || user?.name || 'Anonymous User',
        email: email.trim() || user?.email || 'unprovided@user.plexis',
        category,
        message: message.trim(),
        screenshot: screenshot?.dataUrl || null,
      });
      setSubmitSuccess(true);
      setMessage('');
      setScreenshot(null);
    } catch (err) {
      console.error('[Help] Submission failed:', err);
      setSubmitError(err.message || 'Failed to send message. Please try again.');
    } finally {
      setSubmitting(false);
    }
  };

  const faqs = [
    {
      q: 'How does Plexis eliminate AI hallucinations in data calculations?',
      a: 'Plexis executes all calculations deterministically using embedded DuckDB and Pandas analytical engines rather than generating guesses with an LLM. Every calculation returns verified evidence cards detailing exact row indices, column scopes, and mathematical operations.',
    },
    {
      q: 'How do transformations work and can I undo them?',
      a: 'When you ask Plexis to clean, rename, compute, or filter columns, it generates a transparent transformation plan. Once approved, the changes are applied to your active working copy with full version history and instant one-click undo.',
    },
    {
      q: 'Is my uploaded dataset kept private and secure?',
      a: 'Yes. Plexis enforces strict session and account isolation. Your dataset is sandboxed in encrypted storage, never shared across users, and never used to train external artificial intelligence models.',
    },
    {
      q: 'How can I export my cleaned or modified dataset?',
      a: 'At any point during your conversation, click the Export button or ask Plexis to export your dataset. It generates a clean CSV download preserving all approved transformations.',
    },
    {
      q: 'What is Guest Mode and what are its limits?',
      a: 'Guest Mode allows visitors to explore Plexis using our preloaded sample dataset with 2 free transformations per month without creating an account. Sign in with Google to upload your own files and access the live spreadsheet.',
    },
  ];

  return (
    <div className="v2-help-page">
      <div className="v2-help-ambient" />

      {/* Navigation Header */}
      <header className="v2-help-nav">
        <div className="v2-help-nav-left">
          <button
            type="button"
            className="v2-help-back-btn"
            onClick={() => navigate('/dashboard')}
          >
            <ArrowLeft size={16} /> Dashboard
          </button>
          <div className="v2-help-brand" onClick={() => navigate('/dashboard')}>
            <PlexisLogo size={22} />
            <span className="v2-help-brand-text">Plexis Help Center</span>
          </div>
        </div>

        <div className="v2-help-nav-right">
          <button
            type="button"
            className="v2-help-nav-link"
            onClick={() => navigate('/terms')}
            style={{ background: 'none', border: 'none', cursor: 'pointer' }}
          >
            Terms of Policy
          </button>
          <button
            type="button"
            className="v2-help-nav-link"
            onClick={() => navigate('/billing')}
            style={{ background: 'none', border: 'none', cursor: 'pointer' }}
          >
            Plan & Billing
          </button>
        </div>
      </header>

      <main className="v2-help-container">
        {/* Hero */}
        <section className="v2-help-hero">
          <div className="v2-help-hero-badge">
            <BookOpen size={14} /> Documentation & Support
          </div>
          <h1 className="v2-help-hero-title">How can we help you?</h1>
          <p className="v2-help-hero-desc">
            Explore guides on conversational data analysis, formulas, transformations, and spreadsheet controls. Or report an issue directly to our engineering team below.
          </p>

          <div className="v2-help-quick-links">
            <a href="#guide" className="v2-help-quick-btn">
              <Sparkles size={14} /> Quick Start Guide
            </a>
            <a href="#prompts" className="v2-help-quick-btn">
              <Terminal size={14} /> Prompt Cheat Sheet
            </a>
            <a href="#shortcuts" className="v2-help-quick-btn">
              <Table size={14} /> Keyboard Shortcuts
            </a>
            <a href="#faq" className="v2-help-quick-btn">
              <HelpCircle size={14} /> FAQ
            </a>
            <a href="#contact" className="v2-help-quick-btn" style={{ background: 'rgba(56, 189, 248, 0.15)', borderColor: '#38bdf8' }}>
              <Send size={14} /> Report an Issue
            </a>
          </div>
        </section>

        {/* SECTION 1: Guides Grid */}
        <section id="guide" style={{ marginBottom: 48 }}>
          <h2 className="v2-help-section-title">
            <Layers size={20} color="#38bdf8" /> Core Capabilities & Guide
          </h2>
          <div className="v2-help-grid">
            <div className="v2-help-card">
              <div className="v2-help-card-icon">
                <Database size={20} />
              </div>
              <h3>1. Upload & Connect Data</h3>
              <p>
                Upload CSV or TSV files up to 50 MB (or 35 MB on Free plan). Plexis instantly profiles your columns, identifies metric types, and builds an in-memory analytical index.
              </p>
              <div className="v2-help-card-examples">
                <span>Tip:</span>
                <code>Drag & drop your CSV or press Ctrl + U anywhere</code>
              </div>
            </div>

            <div className="v2-help-card">
              <div className="v2-help-card-icon">
                <Terminal size={20} />
              </div>
              <h3>2. Conversational Analysis</h3>
              <p>
                Ask questions in plain conversational English. The Plexis router parses intent, executes verifiable DuckDB SQL queries, and surfaces mathematical derivations without guess work.
              </p>
              <div className="v2-help-card-examples">
                <span>Example:</span>
                <code>"Which 5 routes have the highest departure delay rate?"</code>
              </div>
            </div>

            <div className="v2-help-card">
              <div className="v2-help-card-icon">
                <ShieldCheck size={20} />
              </div>
              <h3>3. Verified Evidence Cards</h3>
              <p>
                Every answer includes expandable evidence cards showing exactly which rows were evaluated, filter thresholds, and formulas used. Click "Locate" to jump directly to those rows in the spreadsheet.
              </p>
              <div className="v2-help-card-examples">
                <span>Zero Hallucination:</span>
                <code>Inspect row indices and statistical distributions</code>
              </div>
            </div>

            <div className="v2-help-card">
              <div className="v2-help-card-icon">
                <Cpu size={20} />
              </div>
              <h3>4. In-Place Transformations</h3>
              <p>
                Clean, format, or enrich your dataset. Ask Plexis to fill missing values, create calculated columns, or filter outliers. Review the proposed plan before committing changes.
              </p>
              <div className="v2-help-card-examples">
                <span>Example:</span>
                <code>"Add column High_Delay: 1 if Delay &gt; 30 else 0"</code>
              </div>
            </div>

            <div className="v2-help-card">
              <div className="v2-help-card-icon">
                <Table size={20} />
              </div>
              <h3>5. Virtual Spreadsheet & Analytics</h3>
              <p>
                Toggle the live spreadsheet workspace side-by-side with your chat. Sort, search, inspect cell values, and open the charts panel (Ctrl+Shift+A) for automated distributions.
              </p>
              <div className="v2-help-card-examples">
                <span>Toggle View:</span>
                <code>Press Ctrl + Shift + A to open charts & histograms</code>
              </div>
            </div>

            <div className="v2-help-card">
              <div className="v2-help-card-icon">
                <FileText size={20} />
              </div>
              <h3>6. Clean CSV Export</h3>
              <p>
                Download your transformed dataset at any time. All transformations, new columns, and cleaned values are cleanly exported to standard CSV ready for production.
              </p>
              <div className="v2-help-card-examples">
                <span>Action:</span>
                <code>Click "Export" in the workspace header</code>
              </div>
            </div>
          </div>
        </section>

        {/* SECTION 2: Power Prompts */}
        <section id="prompts" style={{ marginBottom: 48 }}>
          <h2 className="v2-help-section-title">
            <Terminal size={20} color="#38bdf8" /> Example Prompts to Try
          </h2>
          <div className="v2-help-grid">
            <div className="v2-help-card">
              <h3>Filtering & Conditionals</h3>
              <p>Extract specific subsets of records based on multiple thresholds:</p>
              <div className="v2-help-card-examples">
                <code>"Filter flights where Departure_Delay &gt; 45 and Rain &gt; 5"</code>
              </div>
            </div>

            <div className="v2-help-card">
              <h3>Grouping & Aggregations</h3>
              <p>Summarize metrics across categories, segments, or dates:</p>
              <div className="v2-help-card-examples">
                <code>"Average departure delay grouped by Airline with count of flights"</code>
              </div>
            </div>

            <div className="v2-help-card">
              <h3>Data Cleaning & Normalization</h3>
              <p>Modify and repair messy columns safely:</p>
              <div className="v2-help-card-examples">
                <code>"Replace missing values in Temperature with the column median"</code>
              </div>
            </div>
          </div>
        </section>

        {/* SECTION 3: Keyboard Shortcuts */}
        <section id="shortcuts" style={{ marginBottom: 48 }}>
          <h2 className="v2-help-section-title">
            <Table size={20} color="#38bdf8" /> Keyboard Shortcuts
          </h2>
          <div className="v2-help-shortcuts-grid">
            <div className="v2-help-shortcut-item">
              <span className="v2-help-shortcut-desc">Upload Dataset</span>
              <kbd className="v2-help-shortcut-kbd">Ctrl + U</kbd>
            </div>
            <div className="v2-help-shortcut-item">
              <span className="v2-help-shortcut-desc">Command Palette</span>
              <kbd className="v2-help-shortcut-kbd">Ctrl + K</kbd>
            </div>
            <div className="v2-help-shortcut-item">
              <span className="v2-help-shortcut-desc">Toggle Analytics Panel</span>
              <kbd className="v2-help-shortcut-kbd">Ctrl + Shift + A</kbd>
            </div>
            <div className="v2-help-shortcut-item">
              <span className="v2-help-shortcut-desc">New Conversation</span>
              <kbd className="v2-help-shortcut-kbd">Ctrl + Shift + O</kbd>
            </div>
            <div className="v2-help-shortcut-item">
              <span className="v2-help-shortcut-desc">Toggle Sidebar</span>
              <kbd className="v2-help-shortcut-kbd">Ctrl + B</kbd>
            </div>
          </div>
        </section>

        {/* SECTION 4: FAQs */}
        <section id="faq" style={{ marginBottom: 56 }}>
          <h2 className="v2-help-section-title">
            <HelpCircle size={20} color="#38bdf8" /> Frequently Asked Questions
          </h2>
          <div className="v2-help-faq-list">
            {faqs.map((faq, idx) => {
              const isOpen = openFaq === idx;
              return (
                <div key={idx} className="v2-help-faq-item">
                  <button
                    type="button"
                    className="v2-help-faq-question"
                    onClick={() => setOpenFaq(isOpen ? null : idx)}
                  >
                    <span>{faq.q}</span>
                    {isOpen ? <ChevronUp size={18} color="#38bdf8" /> : <ChevronDown size={18} color="#64748b" />}
                  </button>
                  {isOpen && <div className="v2-help-faq-answer">{faq.a}</div>}
                </div>
              );
            })}
          </div>
        </section>

        {/* SECTION 5: Direct Complaint & Support Box */}
        <section id="contact">
          <div className="v2-help-ticket-box">
            <div className="v2-help-ticket-header">
              <h2 className="v2-help-ticket-title">
                <AlertCircle size={24} color="#f43f5e" /> Report an Issue or Send Feedback
              </h2>
              <p className="v2-help-ticket-subtitle">
                Found a bug, unexpected calculation, or have a feature idea? Describe your problem or attach a screenshot below.
                Your submission is delivered directly to our founder and engineering team so we can fix it immediately.
              </p>
            </div>

            <form onSubmit={handleSubmit}>
              <div className="v2-help-form-grid">
                <div className="v2-help-field">
                  <label>Your Name</label>
                  <input
                    type="text"
                    placeholder="Enter your name"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                  />
                </div>

                <div className="v2-help-field">
                  <label>Email Address</label>
                  <input
                    type="email"
                    placeholder="your-email@example.com"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    required
                  />
                </div>
              </div>

              <div className="v2-help-field" style={{ marginBottom: 16 }}>
                <label>Topic / Category</label>
                <select
                  value={category}
                  onChange={(e) => setCategory(e.target.value)}
                >
                  <option value="Bug / Error">Bug / Something Broke</option>
                  <option value="Incorrect Calculation">Incorrect Calculation / Number</option>
                  <option value="Transformation Issue">Transformation or Export Problem</option>
                  <option value="UI / Layout Problem">UI / Visual Layout Glitch</option>
                  <option value="Feature Request">Feature Request / Suggestion</option>
                  <option value="Other">Other Question or Feedback</option>
                </select>
              </div>

              <div className="v2-help-field" style={{ marginBottom: 16 }}>
                <label>Problem Description / Details *</label>
                <textarea
                  placeholder="Explain what happened, what you were trying to do, or any error message you saw. You can also paste screenshots directly into this page!"
                  value={message}
                  onChange={(e) => setMessage(e.target.value)}
                  required
                />
              </div>

              {/* Screenshot Upload / Paste Area */}
              <div className="v2-help-field" style={{ marginBottom: 16 }}>
                <label>Attach Screenshot (Optional)</label>
                <input
                  type="file"
                  ref={fileInputRef}
                  accept="image/*"
                  style={{ display: 'none' }}
                  onChange={handleFileChange}
                />

                {!screenshot ? (
                  <div
                    className={`v2-help-upload-area ${isDragOver ? 'dragover' : ''}`}
                    onClick={() => fileInputRef.current?.click()}
                    onDragOver={(e) => { e.preventDefault(); setIsDragOver(true); }}
                    onDragLeave={() => setIsDragOver(false)}
                    onDrop={handleDrop}
                  >
                    <div className="v2-help-upload-content">
                      <UploadCloud size={28} color="#38bdf8" />
                      <div>
                        <strong>Click to upload</strong> or drag and drop screenshot here
                      </div>
                      <span style={{ fontSize: 11, color: '#64748b' }}>
                        PNG, JPG, or WebP up to 5 MB — or press Ctrl+V to paste from clipboard
                      </span>
                    </div>
                  </div>
                ) : (
                  <div className="v2-help-preview-container">
                    <div className="v2-help-preview-left">
                      <img
                        src={screenshot.dataUrl}
                        alt="Screenshot preview"
                        className="v2-help-preview-thumb"
                      />
                      <div>
                        <div className="v2-help-preview-name">{screenshot.name}</div>
                        <div className="v2-help-preview-size">{screenshot.size}</div>
                      </div>
                    </div>
                    <button
                      type="button"
                      className="v2-help-remove-btn"
                      onClick={() => setScreenshot(null)}
                    >
                      <X size={14} /> Remove
                    </button>
                  </div>
                )}
              </div>

              <button
                type="submit"
                className="v2-help-submit-btn"
                disabled={submitting}
              >
                {submitting ? (
                  <>Sending to Engineering Team...</>
                ) : (
                  <>
                    <Send size={16} /> Submit to Plexis Team
                  </>
                )}
              </button>

              {submitSuccess && (
                <div className="v2-help-status-banner v2-help-status-banner--success">
                  <CheckCircle2 size={18} />
                  <span>
                    Thank you! Your ticket has been dispatched directly to the engineering inbox. We will review your report and fix it promptly.
                  </span>
                </div>
              )}

              {submitError && (
                <div className="v2-help-status-banner v2-help-status-banner--error">
                  <AlertCircle size={18} />
                  <span>{submitError}</span>
                </div>
              )}
            </form>
          </div>
        </section>
      </main>
    </div>
  );
}
