import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, Shield, FileText, Lock, RefreshCw, Mail } from 'lucide-react';
import PlexisLogo from '../components/brand/PlexisLogo';
import '../styles/terms.css';

export default function Terms() {
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState('terms');

  return (
    <div className="v2-terms-page">
      {/* Header */}
      <header className="v2-terms-nav">
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <button
            type="button"
            className="v2-help-back-btn"
            onClick={() => navigate('/dashboard')}
          >
            <ArrowLeft size={16} /> Dashboard
          </button>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer' }} onClick={() => navigate('/dashboard')}>
            <PlexisLogo size={22} />
            <span style={{ fontWeight: 700, fontSize: 18, color: '#ffffff' }}>Plexis Legal</span>
          </div>
        </div>

        <div>
          <button
            type="button"
            className="v2-help-nav-link"
            onClick={() => navigate('/help')}
            style={{ background: 'none', border: 'none', cursor: 'pointer' }}
          >
            Help Center
          </button>
        </div>
      </header>

      <main className="v2-terms-container">
        <div className="v2-terms-header">
          <div className="v2-terms-badge">Legal & Privacy Center</div>
          <h1 className="v2-terms-title">Terms of Service & Privacy Policy</h1>
          <div className="v2-terms-date">Effective Date: November 9, 2026 • Version 2.0</div>
        </div>

        {/* Navigation Tabs */}
        <div className="v2-terms-tabs">
          <button
            type="button"
            className={`v2-terms-tab-btn ${activeTab === 'terms' ? 'active' : ''}`}
            onClick={() => setActiveTab('terms')}
          >
            <FileText size={15} style={{ verticalAlign: 'middle', marginRight: 6 }} />
            Terms of Service
          </button>

          <button
            type="button"
            className={`v2-terms-tab-btn ${activeTab === 'privacy' ? 'active' : ''}`}
            onClick={() => setActiveTab('privacy')}
          >
            <Shield size={15} style={{ verticalAlign: 'middle', marginRight: 6 }} />
            Privacy Policy
          </button>

          <button
            type="button"
            className={`v2-terms-tab-btn ${activeTab === 'security' ? 'active' : ''}`}
            onClick={() => setActiveTab('security')}
          >
            <Lock size={15} style={{ verticalAlign: 'middle', marginRight: 6 }} />
            Data Security & Isolation
          </button>

          <button
            type="button"
            className={`v2-terms-tab-btn ${activeTab === 'refund' ? 'active' : ''}`}
            onClick={() => setActiveTab('refund')}
          >
            <RefreshCw size={15} style={{ verticalAlign: 'middle', marginRight: 6 }} />
            Refund & Cancellation
          </button>

          <button
            type="button"
            className={`v2-terms-tab-btn ${activeTab === 'contact' ? 'active' : ''}`}
            onClick={() => setActiveTab('contact')}
          >
            <Mail size={15} style={{ verticalAlign: 'middle', marginRight: 6 }} />
            Contact & Support
          </button>
        </div>

        {/* Tab 1: Terms of Service */}
        {activeTab === 'terms' && (
          <div className="v2-terms-content">
            <h2>1. Agreement to Terms</h2>
            <p>
              By accessing or using <strong>Plexis</strong> ("Service", "we", "us", or "our") at plexisai.vercel.app or related subdomains, you agree to be bound by these Terms of Service. If you do not agree to these terms, you may not access or use the Service.
            </p>

            <h2>2. Description of the Service</h2>
            <p>
              Plexis is a cloud-based conversational artificial intelligence and data analysis platform that enables users to upload tabular datasets (such as CSV and TSV files), perform analytical queries, inspect calculations through evidence references, execute verified data transformations, and view virtual spreadsheet representations.
            </p>

            <h2>3. User Accounts & Fair Use</h2>
            <p>
              To access full features, you may register via Google OAuth. You are responsible for maintaining the confidentiality of your credentials. You agree not to:
            </p>
            <ul>
              <li>Upload malicious code, viruses, or illegal materials.</li>
              <li>Attempt to reverse-engineer, decompile, or disrupt the backend analytical engines or API infrastructure.</li>
              <li>Exceed stated plan rate limits through automated scrapers or abusive traffic generation.</li>
              <li>Use the Service to analyze unauthorized sensitive medical records or financial surveillance data in violation of applicable laws.</li>
            </ul>

            <h2>4. Intellectual Property</h2>
            <p>
              You retain 100% full ownership, rights, title, and interest in and to all datasets, files, and queries you upload to Plexis. We claim no intellectual property rights over your data.
            </p>

            <h2>5. Service Availability & Modifications</h2>
            <p>
              We continually improve Plexis and reserve the right to update, modify, or temporarily suspend features with reasonable notice. We strive for high availability but provide the Service on an "as is" and "as available" basis.
            </p>
          </div>
        )}

        {/* Tab 2: Privacy Policy */}
        {activeTab === 'privacy' && (
          <div className="v2-terms-content">
            <h2>1. Information We Collect</h2>
            <p>We collect only the minimum information necessary to operate the Service effectively:</p>
            <ul>
              <li><strong>Account Information:</strong> When signing in with Google, we receive your name, email address, and avatar image.</li>
              <li><strong>Dataset Files:</strong> Tabular data (CSV, TSV) uploaded to your isolated user sandbox for processing.</li>
              <li><strong>Telemetry & Analytics:</strong> Operational logs, request duration, and error traces to ensure high performance and prevent abuse.</li>
            </ul>

            <h2>2. How We Use Your Information</h2>
            <p>Your information is used strictly to:</p>
            <ul>
              <li>Provide, maintain, and execute your conversational data queries and transformations.</li>
              <li>Authenticate your access and enforce plan quotas.</li>
              <li>Notify you of important service changes or respond to support requests.</li>
            </ul>

            <h2>3. Zero Model Training Guarantee</h2>
            <p>
              <strong>We do not use your private datasets or conversational messages to train foundation AI models.</strong> Your datasets are processed strictly in ephemeral memory and isolated database sessions.
            </p>

            <h2>4. Third-Party Service Providers</h2>
            <p>
              We work with trusted enterprise cloud infrastructure providers including Render (cloud compute), Supabase / PostgreSQL (isolated encrypted metadata), and Google OAuth (authentication). All third parties comply with industry-standard privacy safeguards.
            </p>
          </div>
        )}

        {/* Tab 3: Data Security & Isolation */}
        {activeTab === 'security' && (
          <div className="v2-terms-content">
            <h2>1. Multi-Tenant Sandbox Isolation</h2>
            <p>
              Plexis employs strict cryptographic tenant isolation. Every dataset is scoped to an account tag and isolated chat reference. No user or session can inspect, query, or leak data across boundaries.
            </p>

            <h2>2. Encryption in Transit and at Rest</h2>
            <p>
              All communication between your browser and our application is encrypted using Transport Layer Security (TLS 1.3 / HTTPS). Database storage utilizes AES-256 encryption at rest.
            </p>

            <h2>3. Ephemeral Guest Sessions</h2>
            <p>
              Guest Mode sessions are completely temporary. When a guest session terminates or expires, temporary transformation working copies are automatically purged from runtime caches.
            </p>
          </div>
        )}

        {/* Tab 4: Refund & Cancellation */}
        {activeTab === 'refund' && (
          <div className="v2-terms-content">
            <h2>1. Free Promotional Launch Plans</h2>
            <p>
              During our launch phase, Plus Plan subscriptions may be offered under <strong>Free Launch Access</strong> promotions. Free promotional tiers require no credit card and incur $0 charges.
            </p>

            <h2>2. Paid Subscriptions & Billing</h2>
            <p>
              When paid subscriptions are activated, subscriptions renew on a monthly or annual basis unless cancelled prior to the renewal date.
            </p>

            <h2>3. Cancellation Policy</h2>
            <p>
              You can cancel your subscription at any time directly through the Billing settings page. Upon cancellation, your account retains full Plus access until the end of the current billing cycle.
            </p>

            <h2>4. Refund Requests</h2>
            <p>
              If you experience technical issues or are dissatisfied with your paid subscription, contact our support team at <strong>samaryadavclass2b@gmail.com</strong> within 7 days of purchase for a prompt review and full refund.
            </p>
          </div>
        )}

        {/* Tab 5: Contact & Support */}
        {activeTab === 'contact' && (
          <div className="v2-terms-content">
            <h2>Contact Information</h2>
            <p>
              If you have any questions, concerns, or requests regarding these Terms of Service or our privacy practices, please contact our team directly:
            </p>
            <ul>
              <li><strong>Product Name:</strong> Plexis (AI-Powered Data Analysis)</li>
              <li><strong>Engineering & Founder Email:</strong> <a href="mailto:samaryadavclass2b@gmail.com">samaryadavclass2b@gmail.com</a></li>
              <li><strong>Help & Complaint Submission:</strong> Visit our <a href="/help">Help Center</a> to submit tickets with screenshots.</li>
              <li><strong>Location:</strong> India</li>
            </ul>
          </div>
        )}
      </main>
    </div>
  );
}
