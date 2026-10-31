import React, { useState } from 'react';
import { Box, Chip, CircularProgress, Tooltip } from '@mui/material';
import { useNavigate } from 'react-router-dom';
import { authAPI } from '../../services/api';
import useAuthStore from '../../store/authStore';

// Demo-only fast account-switcher - a real logout+login against the 3
// seeded demo accounts (no new auth code path). Lets a demo walk through
// "submit as claimant, approve as reviewer" without manually logging out
// and back in. Always visible, per the explicit decision that this is a
// demo convenience, not a real security feature.
const PERSONAS = [
  { role: 'user', label: 'Claimant', username: 'claimant', password: 'claim123' },
  { role: 'reviewer', label: 'Reviewer', username: 'reviewer1', password: 'review123' },
  { role: 'admin', label: 'Admin', username: 'admin', password: 'admin123' },
];

const landingFor = (role) => (role === 'user' ? '/my-claims' : '/dashboard');

export default function PersonaSwitcher() {
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const login = useAuthStore((s) => s.login);
  const [switching, setSwitching] = useState(null);

  const switchTo = async (persona) => {
    if (user?.role === persona.role) return;
    setSwitching(persona.role);
    try {
      const res = await authAPI.login(persona.username, persona.password);
      login(res.data.user, res.data.token);
      navigate(landingFor(res.data.user.role));
    } catch {
      // Swallow - the demo accounts should always exist in a dev DB;
      // a failure here just leaves the current session unchanged.
    } finally {
      setSwitching(null);
    }
  };

  return (
    <Box sx={{ display: 'flex', gap: 0.75, alignItems: 'center', px: 1 }}>
      <Box component="span" sx={{ fontSize: '0.65rem', color: 'rgba(244,242,234,0.5)', mr: 0.5 }}>DEMO</Box>
      {PERSONAS.map((p) => (
        <Tooltip key={p.role} title={`View as ${p.label} (${p.username})`}>
          <Chip
            size="small"
            label={switching === p.role ? <CircularProgress size={12} sx={{ color: 'inherit' }} /> : p.label}
            onClick={() => switchTo(p)}
            variant={user?.role === p.role ? 'filled' : 'outlined'}
            sx={{
              height: 24, fontSize: '0.7rem', fontWeight: 600, cursor: 'pointer',
              color: user?.role === p.role ? '#161616' : 'rgba(244,242,234,0.8)',
              bgcolor: user?.role === p.role ? '#2DA89D' : 'transparent',
              borderColor: 'rgba(244,242,234,0.35)',
              '&:hover': { bgcolor: user?.role === p.role ? '#2DA89D' : 'rgba(244,242,234,0.1)' },
            }}
          />
        </Tooltip>
      ))}
    </Box>
  );
}
