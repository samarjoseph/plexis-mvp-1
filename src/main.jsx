import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './index.css';
import App from './App.jsx';
import { PlexisAPI } from './api';

// Proactively warm up backend container on initial page load (0 LLM cost)
PlexisAPI.warmup();

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>
);
