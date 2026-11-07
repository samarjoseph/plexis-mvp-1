import React from 'react';
import { Navigate } from 'react-router-dom';
import { useAuth } from '../../context/AuthContext';

export default function ProtectedRoute({ children }) {
  const { isAuthenticated, isGuest, isLoading } = useAuth();

  if (isLoading) {
    return (
      <div className="auth-loading-screen">
        <div className="auth-loading-spinner" />
        <p>Loading Plexis...</p>
      </div>
    );
  }

  // Allow access if user is authenticated with Google OR is in guest mode
  if (!isAuthenticated && !isGuest) {
    return <Navigate to="/" replace />;
  }

  return children;
}
