import React, { useEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate, useNavigate } from 'react-router-dom';
import { GoogleOAuthProvider } from '@react-oauth/google';
import { AuthProvider, useAuth } from './context/AuthContext';
import LandingPage from './pages/LandingPage';
import Dashboard from './pages/Dashboard';
import ProtectedRoute from './components/oauth/ProtectedRoute';
import EarlyAccessPage from './components/early-access/EarlyAccessPage';
import EarlyAccessAdmin from './components/early-access/EarlyAccessAdmin';

const GOOGLE_CLIENT_ID = import.meta.env.VITE_GOOGLE_CLIENT_ID;

// Log exactly what client ID we have, as requested for debugging
console.log("Google Client ID:", GOOGLE_CLIENT_ID);

function AuthRedirect({ children }) {
  const { isAuthenticated, isLoading } = useAuth();
  if (isLoading) {
    return (
      <div className="auth-loading-screen">
        <div className="auth-loading-spinner" />
      </div>
    );
  }
  if (isAuthenticated) return <Navigate to="/dashboard" replace />;
  return children;
}

function OAuthCallback() {
  const navigate = useNavigate();
  const { loginWithGoogleCredential } = useAuth();

  useEffect(() => {
    // Parse hash fragment for access_token or id_token
    const hash = window.location.hash.substring(1);
    const params = new URLSearchParams(hash);
    const idToken = params.get('id_token');

    if (idToken) {
      // We got an implicit flow id_token!
      loginWithGoogleCredential({ credential: idToken });
      navigate('/dashboard', { replace: true });
    } else {
      // Something went wrong or user cancelled
      navigate('/', { replace: true });
    }
  }, [navigate, loginWithGoogleCredential]);

  return (
    <div className="auth-loading-screen">
      <div className="auth-loading-spinner" />
      <p>Authenticating...</p>
    </div>
  );
}

function AppRoutes() {
  return (
    <Routes>
      <Route
        path="/"
        element={
          <AuthRedirect>
            <LandingPage />
          </AuthRedirect>
        }
      />
      <Route path="/auth/callback" element={<OAuthCallback />} />
      <Route
        path="/dashboard"
        element={
          <ProtectedRoute>
            <Dashboard />
          </ProtectedRoute>
        }
      />
      <Route
        path="/dashboard/:chatRef"
        element={
          <ProtectedRoute>
            <Dashboard />
          </ProtectedRoute>
        }
      />
      <Route path="/early-access" element={<EarlyAccessPage />} />
      <Route path="/early-access/admin" element={<EarlyAccessAdmin />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export default function App() {
  // import.meta.env.BASE_URL is '/' in dev and '/PLEXIS-main/' in prod build.
  // Strip trailing slash for BrowserRouter basename.
  const basename = import.meta.env.BASE_URL.replace(/\/$/, '') || '/';
  return (
    <GoogleOAuthProvider clientId={GOOGLE_CLIENT_ID || 'missing-client-id'}>
      <AuthProvider>
        <BrowserRouter basename={basename}>
          <AppRoutes />
        </BrowserRouter>
      </AuthProvider>
    </GoogleOAuthProvider>
  );
}
