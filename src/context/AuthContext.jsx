import React, { createContext, useContext, useState, useEffect, useCallback } from 'react';
import { jwtDecode } from 'jwt-decode';

const AUTH_STORAGE_KEY = 'plexis_auth_session';
const GUEST_STORAGE_KEY = 'plexis_guest_session';

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

function parseStoredGuestSession() {
  try {
    const raw = localStorage.getItem(GUEST_STORAGE_KEY);
    if (!raw) return null;
    const guest = JSON.parse(raw);
    if (guest?.expires_at && Date.now() > guest.expires_at * 1000) {
      localStorage.removeItem(GUEST_STORAGE_KEY);
      return null;
    }
    return guest;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [guestSession, setGuestSession] = useState(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const session = parseStoredSession();
    if (session?.user) {
      setUser(session.user);
      setGuestSession(null);
    } else {
      const guest = parseStoredGuestSession();
      if (guest) {
        setGuestSession(guest);
        setUser({
          id: 'guest',
          name: 'Guest User',
          email: 'guest@plexis.local',
          plan: 'guest',
          isGuest: true,
        });
      }
    }
    setIsLoading(false);
  }, []);

  const persistSession = useCallback((profile, credential = null) => {
    // Clear guest session upon genuine sign in
    localStorage.removeItem(GUEST_STORAGE_KEY);
    setGuestSession(null);

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

  const startGuestSession = useCallback((sessionData) => {
    localStorage.setItem(GUEST_STORAGE_KEY, JSON.stringify(sessionData));
    setGuestSession(sessionData);
    const guestUser = {
      id: 'guest',
      name: 'Guest User',
      email: 'guest@plexis.local',
      plan: 'guest',
      isGuest: true,
    };
    setUser(guestUser);
    return guestUser;
  }, []);

  const updateGuestSession = useCallback((updater) => {
    setGuestSession((prev) => {
      const next = typeof updater === 'function' ? updater(prev) : { ...prev, ...updater };
      if (next) {
        localStorage.setItem(GUEST_STORAGE_KEY, JSON.stringify(next));
      }
      return next;
    });
  }, []);

  const exitGuestSession = useCallback(() => {
    localStorage.removeItem(GUEST_STORAGE_KEY);
    setGuestSession(null);
    setUser(null);
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
    localStorage.removeItem(GUEST_STORAGE_KEY);
    localStorage.removeItem('plexis_user_email');
    setGuestSession(null);
    setUser(null);
  }, []);

  const isGuest = !!guestSession;
  const isAuthenticated = !!user && !isGuest;

  return (
    <AuthContext.Provider
      value={{
        user,
        isAuthenticated,
        isGuest,
        guestSession,
        isLoading,
        startGuestSession,
        updateGuestSession,
        exitGuestSession,
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
