import { createTheme } from '@mui/material';

// Design tokens - warm light canvas, near-black ink/borders, flat hard-edged
// "sticker" shadows, teal accent. Styled after D:\it_helpdesk_full_project's
// visual language (adapted: that project is plain HTML/CSS, this is MUI).
export const tokens = {
  canvas: '#F7F5EE',
  surface: '#FFFFFF',
  surfaceAlt: '#EAF4F2',       // soft teal tint - selected/highlighted rows
  surfaceSunken: '#F4F2EA',    // inset wells: track backgrounds, code blocks
  ink: '#161616',
  inkMuted: 'rgba(22,22,22,0.62)',
  inkSubtle: 'rgba(22,22,22,0.45)',
  border: '#161616',
  accent: '#0F766E',           // teal
  accentSoft: '#DCF3EF',
  sidebarBg: '#161616',
  sidebarFg: '#f4f2ea',
  sidebarHover: '#262626',
  good: '#1a7a4c', goodSoft: '#dff3e6',
  warn: '#8a5a00', warnSoft: '#fff1cf',
  bad: '#a3242a', badSoft: '#fbdede',
  shadow: '4px 4px 0 0 #161616',
  shadowLg: '6px 6px 0 0 #161616',
};

const t = tokens;

const theme = createTheme({
  palette: {
    mode: 'light',
    primary: { main: t.accent },
    secondary: { main: t.ink },
    success: { main: t.good },
    warning: { main: t.warn },
    error: { main: t.bad },
    info: { main: t.accent },
    background: { default: t.canvas, paper: t.surface },
    text: { primary: t.ink, secondary: t.inkMuted },
    divider: t.border,
  },
  typography: {
    fontFamily: '"Space Grotesk", "Inter", "Roboto", "Helvetica", "Arial", sans-serif',
    h1: { fontWeight: 700, letterSpacing: '-0.01em' },
    h2: { fontWeight: 700, letterSpacing: '-0.01em' },
    h3: { fontWeight: 700, letterSpacing: '-0.01em' },
    h4: { fontWeight: 700, letterSpacing: '-0.01em' },
    h5: { fontWeight: 700, letterSpacing: '-0.01em' },
    h6: { fontWeight: 700, letterSpacing: '-0.01em' },
  },
  shape: { borderRadius: 12 },
  components: {
    MuiCssBaseline: {
      styleOverrides: {
        body: { backgroundColor: t.canvas },
        '.tabular-nums': { fontVariantNumeric: 'tabular-nums' },
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: {
          backgroundImage: 'none',
          border: `2px solid ${t.border}`,
          boxShadow: t.shadow,
        },
        outlined: { border: `2px solid ${t.border}` },
      },
    },
    MuiCard: {
      styleOverrides: {
        root: {
          border: `2px solid ${t.border}`,
          boxShadow: t.shadow,
          borderRadius: 12,
        },
      },
    },
    MuiButton: {
      styleOverrides: {
        root: {
          textTransform: 'none',
          fontWeight: 600,
          borderRadius: 8,
        },
        contained: {
          border: `2px solid ${t.border}`,
          boxShadow: t.shadow,
          '&:hover': { boxShadow: t.shadowLg, transform: 'translate(-2px, -2px)' },
          transition: 'transform 0.15s ease, box-shadow 0.15s ease',
        },
        outlined: {
          border: `2px solid ${t.border}`,
          '&:hover': { border: `2px solid ${t.border}`, boxShadow: t.shadow, transform: 'translate(-2px, -2px)' },
          transition: 'transform 0.15s ease, box-shadow 0.15s ease',
        },
      },
    },
    MuiChip: {
      styleOverrides: {
        root: { fontWeight: 600, borderRadius: 8 },
      },
    },
    MuiTextField: {
      styleOverrides: {
        root: {
          '& .MuiOutlinedInput-root': {
            borderRadius: 8,
            backgroundColor: t.surface,
            '& fieldset': { borderColor: t.border, borderWidth: 2 },
            '&:hover fieldset': { borderColor: t.border },
            '&.Mui-focused fieldset': { borderColor: t.accent, borderWidth: 2 },
          },
          '& .MuiInputLabel-root': { color: t.inkMuted },
          '& .MuiInputBase-input': { color: t.ink },
        },
      },
    },
    MuiTableCell: {
      styleOverrides: {
        root: { borderColor: 'rgba(22,22,22,0.15)', padding: '12px 16px' },
        head: { backgroundColor: t.surfaceSunken, fontWeight: 700, color: t.ink },
      },
    },
    MuiTableRow: {
      styleOverrides: {
        root: { '&:hover': { backgroundColor: t.surfaceAlt } },
      },
    },
    MuiDialog: {
      styleOverrides: {
        paper: { borderRadius: 12, border: `2px solid ${t.border}`, boxShadow: t.shadowLg },
      },
    },
    MuiAlert: {
      styleOverrides: {
        root: { border: `2px solid ${t.border}`, borderRadius: 8 },
      },
    },
  },
});

export default theme;
