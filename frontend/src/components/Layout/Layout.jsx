import React from 'react';
import { Outlet, useNavigate, useLocation } from 'react-router-dom';
import {
  Box, Drawer, List, ListItemButton, ListItemIcon, ListItemText, Typography,
  AppBar, Toolbar, IconButton, Avatar, Chip, Badge, Divider, Menu, MenuItem, Tooltip,
} from '@mui/material';
import {
  Dashboard, Description, Gavel, Security, Assessment, People, Settings, Logout,
  AccountBalance, ReceiptLong, HowToVote, Analytics, FactCheck, Menu as MenuIcon, Timeline,
} from '@mui/icons-material';
import useAuthStore from '../../store/authStore';
import useClaimsStore from '../../store/claimsStore';
import PersonaSwitcher from './PersonaSwitcher';

const DRAWER_WIDTH = 260;

// Backend roles: user | reviewer | admin
const ROLE_COLORS = {
  user: '#2DA89D',      // teal (lighter, for contrast on the dark sidebar) - claimant
  reviewer: '#F5B23A',  // amber - HITL approver
  admin: '#E8737A',     // red - admin
};

const ROLE_LABELS = {
  user: 'Claimant',
  reviewer: 'Reviewer',
  admin: 'Admin',
};

export default function Layout() {
  const navigate = useNavigate();
  const location = useLocation();
  const {
    user, logout, isClaimant, canReviewClaims, canReviewHITL,
    canReviewAppeals, canViewAnalytics, canManageUsers,
  } = useAuthStore();
  const hitlStats = useClaimsStore((s) => s.hitlStats);
  const [mobileOpen, setMobileOpen] = React.useState(false);

  const handleLogout = () => {
    logout();
    navigate('/login');
  };

  const claimantOnly = isClaimant();       // role === 'user'
  const reviewerOrAdmin = canReviewClaims(); // role >= reviewer
  const adminOnly = canManageUsers();       // role === 'admin'

  const navItems = [
    // Claimant items (hidden from reviewers/admins - they use the staff views)
    { path: '/my-claims',    label: 'My Claims',        icon: <Description />, show: claimantOnly },
    { path: '/submit-claim', label: 'Submit Claim',     icon: <ReceiptLong />, show: claimantOnly },
    { path: '/my-appeals',   label: 'My Appeals',       icon: <HowToVote />,   show: claimantOnly },
    { divider: true, show: reviewerOrAdmin },

    // Reviewer + admin items
    { path: '/dashboard',  label: 'Claims Dashboard',  icon: <Dashboard />, show: reviewerOrAdmin },
    { path: '/hitl-queue', label: 'HITL Queue', icon: <Security />,  show: canReviewHITL(),   badge: hitlStats?.pending_total, tooltip: 'HITL (Human-In-The-Loop) Review Queue' },
    { path: '/appeals',    label: 'Appeals Review',    icon: <Gavel />,     show: canReviewAppeals() },
    { path: '/analytics',  label: 'Analytics',         icon: <Analytics />, show: canViewAnalytics() },
    { path: '/evals',      label: 'Evals',             icon: <FactCheck />, show: canViewAnalytics(), tooltip: 'Offline evaluation results (internal flow verification, judge calibration)' },

    // Admin-only
    { divider: true, show: adminOnly },
    { path: '/user-management', label: 'User Management', icon: <People />, show: adminOnly },
    { path: '/policy-management', label: 'Policy Management', icon: <Description />, show: adminOnly },

    // Everyone
    { divider: true, show: true },
    { path: '/settings', label: 'Settings', icon: <Settings />, show: true },
  ];

  const roleColor = ROLE_COLORS[user?.role] || 'rgba(244,242,234,0.6)';

  // Sidebar is the one permanent dark element in the new light theme -
  // #161616 bg, #f4f2ea text (see theme.js's sidebarBg/sidebarFg tokens).
  const drawer = (
    <Box sx={{ height: '100%', display: 'flex', flexDirection: 'column', bgcolor: '#161616' }}>
      {/* Brand */}
      <Box sx={{ p: 2.5, display: 'flex', alignItems: 'center', gap: 1.5 }}>
        <AccountBalance sx={{ fontSize: 32, color: '#2DA89D' }} />
        <Box>
          <Typography variant="subtitle1" fontWeight={700} color="#f4f2ea" lineHeight={1.2}>Smart Claims</Typography>
          <Typography variant="caption" color="rgba(244,242,234,0.6)">Insurance AI</Typography>
        </Box>
      </Box>
      <Divider sx={{ borderColor: '#333333' }} />

      {/* User info */}
      <Box sx={{ p: 2, display: 'flex', alignItems: 'center', gap: 1.5 }}>
        <Avatar sx={{ width: 36, height: 36, bgcolor: roleColor, color: '#161616', fontSize: '0.9rem', fontWeight: 600 }}>
          {user?.username?.[0]?.toUpperCase() || '?'}
        </Avatar>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography variant="body2" fontWeight={600} color="#f4f2ea" noWrap>{user?.username}</Typography>
          <Chip label={ROLE_LABELS[user?.role] || user?.role} size="small"
            sx={{ height: 20, fontSize: '0.7rem', fontWeight: 600, bgcolor: `${roleColor}30`, color: roleColor, border: `1px solid ${roleColor}60` }}
          />
        </Box>
      </Box>
      <Divider sx={{ borderColor: '#333333' }} />

      {/* Nav items */}
      <List sx={{ flex: 1, px: 1, py: 1 }}>
        {navItems.filter((n) => n.show).map((item, i) =>
          item.divider ? (
            <Divider key={i} sx={{ my: 1, borderColor: '#333333' }} />
          ) : (
            <Tooltip title={item.tooltip || ''} placement="right" arrow>
              <ListItemButton
                key={item.path}
                selected={location.pathname === item.path}
                onClick={() => { navigate(item.path); setMobileOpen(false); }}
                sx={{
                  borderRadius: 2, mb: 0.5, py: 1,
                  '&.Mui-selected': { bgcolor: '#262626', '& .MuiListItemIcon-root': { color: '#2DA89D' }, '& .MuiListItemText-primary': { color: '#f4f2ea', fontWeight: 600 } },
                  '&:hover': { bgcolor: '#262626' },
                }}
              >
                <ListItemIcon sx={{ color: 'rgba(244,242,234,0.55)', minWidth: 40 }}>
                  {item.badge ? <Badge badgeContent={item.badge} color="error">{item.icon}</Badge> : item.icon}
                </ListItemIcon>
                <ListItemText primary={item.label} sx={{ '& .MuiListItemText-primary': { fontSize: '0.875rem', color: 'rgba(244,242,234,0.75)' } }} />
              </ListItemButton>
            </Tooltip>
          )
        )}
      </List>

      {/* Demo persona switcher */}
      <Divider sx={{ borderColor: '#333333' }} />
      <Box sx={{ py: 1.5 }}>
        <PersonaSwitcher />
      </Box>

      {/* Logout */}
      <Divider sx={{ borderColor: '#333333' }} />
      <ListItemButton onClick={handleLogout} sx={{ m: 1, borderRadius: 2, py: 1 }}>
        <ListItemIcon sx={{ color: '#E8737A', minWidth: 40 }}><Logout /></ListItemIcon>
        <ListItemText primary="Sign Out" sx={{ '& .MuiListItemText-primary': { color: '#E8737A', fontSize: '0.875rem', fontWeight: 500 } }} />
      </ListItemButton>
    </Box>
  );

  return (
    <Box sx={{ display: 'flex', minHeight: '100vh', bgcolor: '#F7F5EE' }}>
      {/* Mobile app bar */}
      <AppBar position="fixed" sx={{ display: { md: 'none' }, bgcolor: '#161616', borderBottom: '2px solid #161616' }}>
        <Toolbar>
          <IconButton onClick={() => setMobileOpen(!mobileOpen)} sx={{ color: '#f4f2ea' }}><MenuIcon /></IconButton>
          <Typography variant="subtitle1" fontWeight={600} color="#f4f2ea" sx={{ ml: 1 }}>Smart Claims</Typography>
        </Toolbar>
      </AppBar>

      {/* Drawer */}
      <Drawer variant="temporary" open={mobileOpen} onClose={() => setMobileOpen(false)}
        sx={{ display: { xs: 'block', md: 'none' }, '& .MuiDrawer-paper': { width: DRAWER_WIDTH, bgcolor: '#161616', borderRight: '2px solid #161616' } }}
      >
        {drawer}
      </Drawer>
      <Drawer variant="permanent"
        sx={{ display: { xs: 'none', md: 'block' }, '& .MuiDrawer-paper': { width: DRAWER_WIDTH, bgcolor: '#161616', borderRight: '2px solid #161616' } }}
      >
        {drawer}
      </Drawer>

      {/* Main content */}
      <Box component="main" sx={{ flex: 1, ml: { md: `${DRAWER_WIDTH}px` }, mt: { xs: '56px', md: 0 }, p: 3, minHeight: '100vh' }}>
        <Outlet />
      </Box>
    </Box>
  );
}
