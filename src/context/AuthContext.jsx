import React, { createContext, useContext, useState, useEffect, useCallback } from 'react';
import { jwtDecode } from 'jwt-decode';

const AUTH_STORAGE_KEY = 'plexis_auth_session';

const AuthContext = createContext(null);

function parseStoredSession() {
  try {
    const raw = localStorage.getItem(AUTH_STORAGE_KEY);
    if (!raw) return null;
    const session = JSON.parse(raw);
    if (session?.expiresAt && Date.now() > session.expiresAt) {
      localStorage.removeItem(AUTH_STORAGE_KEY);
      return null;
    }
    return session;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const session = parseStoredSession();
    if (session?.user) setUser(session.user);
    setIsLoading(false);
  }, []);

  const persistSession = useCallback((profile, credential = null) => {
    const session = {
      user: profile,
      credential,
      expiresAt: Date.now() + 7 * 24 * 60 * 60 * 1000,
      loginAt: Date.now(),
    };
    localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(session));
    if (profile?.email) {
      localStorage.setItem('plexis_user_email', profile.email.trim().toLowerCase());
    }
    setUser(profile);
    return profile;
  }, []);

  const loginWithGoogleCredential = useCallback(
    (credentialResponse) => {
      const decoded = jwtDecode(credentialResponse.credential);
      return persistSession({
        id: decoded.sub,
        name: decoded.name,
        email: decoded.email,
        avatar: decoded.picture,
      }, credentialResponse.credential);
    },
    [persistSession]
  );

  const logout = useCallback(() => {
    localStorage.removeItem(AUTH_STORAGE_KEY);
    localStorage.removeItem('plexis_user_email');
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider
      value={{
        user,
        isAuthenticated: !!user,
        isLoading,
        loginWithGoogleCredential,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}
