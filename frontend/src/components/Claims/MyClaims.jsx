import React, { useEffect, useState } from 'react';
import {
  Box, Paper, Typography, Table, TableBody, TableCell, TableContainer, TableHead, TableRow,
  Chip, Button, CircularProgress, Alert, Stack, IconButton, Tooltip, Dialog, DialogTitle,
  DialogContent, DialogActions, Divider, LinearProgress, Grid,
} from '@mui/material';
import { Visibility, Refresh, HowToVote, Receipt } from '@mui/icons-material';
import { claimsAPI, appealsAPI } from '../../services/api';
import { fmt } from '../../services/currency';
import useAuthStore from '../../store/authStore';

const PROCESSING_STATUSES = ['processing', 'submitted'];

const STATUS_COLORS = {
  approved: 'success', approved_partial: 'warning', denied: 'error', auto_rejected: 'error',
  escalated_human_review: 'warning', fraud_investigation: 'error', pending: 'info',
  pending_documents: 'info', processing: 'info', submitted: 'info', completed: 'default',
  pending_human_review: 'warning', failed: 'error',
};

const STATUS_LABELS = {
  approved: 'Approved', approved_partial: 'Partial Approved', denied: 'Denied',
  auto_rejected: 'Auto Rejected', escalated_human_review: 'Under Review',
  fraud_investigation: 'Investigation', pending: 'Pending', pending_documents: 'Docs Needed',
  processing: 'Processing', submitted: 'Submitted', completed: 'Completed',
  pending_human_review: 'Under Review', failed: 'Failed',
};

export default function MyClaims() {
  const user = useAuthStore((s) => s.user);
  const [claims, setClaims] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState(null);
  const [appealDialog, setAppealDialog] = useState(false);
  const [appealReason, setAppealReason] = useState('');
  const [appealLoading, setAppealLoading] = useState(false);

  const fetchClaims = async () => {
    setLoading(true);
    try {
      const res = await claimsAPI.getUserClaims(user?.id || user?.username);
      const fresh = res.data.claims || res.data || [];
      setClaims(fresh);
      // Keep detail dialog in sync when auto-polling
      setSelected((prev) => prev ? fresh.find((c) => c.claim_id === prev.claim_id) || prev : null);
    } catch (err) {
      // Demo: show sample data if API not connected
      setClaims([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchClaims(); }, []);

  // Auto-poll every 5s while any claim is actively processing,
  // or every 30s if any claim is awaiting human review (so approval reflects quickly)
  useEffect(() => {
    const hasProcessing = claims.some((c) => PROCESSING_STATUSES.includes(c.status));
    const hasHITL = claims.some((c) => c.status === 'pending_human_review');
    if (!hasProcessing && !hasHITL) return;
    const interval = setInterval(fetchClaims, hasProcessing ? 5000 : 10000);
    return () => clearInterval(interval);
  }, [claims]);

  const handleAppeal = async () => {
    setAppealLoading(true);
    try {
      await appealsAPI.submitAppeal({ claim_id: selected.claim_id, reason: appealReason });
      setAppealDialog(false);
      setAppealReason('');
      fetchClaims();
    } catch (err) {
      setError(err.response?.data?.detail || 'Appeal failed');
    } finally {
      setAppealLoading(false);
    }
  };

  const canAppeal = (claim) => ['denied', 'auto_rejected', 'approved_partial'].includes(claim.status);

  return (
    <Box>
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 3 }}>
        <Box>
          <Typography variant="h5" fontWeight={700} color="#161616">My Claims</Typography>
          <Typography variant="body2" color="rgba(22,22,22,0.62)">Track your insurance claims and their status</Typography>
        </Box>
        <Button startIcon={<Refresh />} onClick={fetchClaims} sx={{ color: 'rgba(22,22,22,0.62)' }}>Refresh</Button>
      </Box>

      {/* Stats */}
      <Grid container spacing={2} sx={{ mb: 3 }}>
        {[
          { label: 'Total Claims', value: claims.length, color: '#0F766E' },
          { label: 'Approved', value: claims.filter((c) => c.status?.includes('approved')).length, color: '#1a7a4c' },
          { label: 'Under Review', value: claims.filter((c) => ['pending', 'processing', 'escalated_human_review'].includes(c.status)).length, color: '#8a5a00' },
          { label: 'Denied', value: claims.filter((c) => ['denied', 'auto_rejected'].includes(c.status)).length, color: '#a3242a' },
        ].map((stat) => (
          <Grid item xs={6} md={3} key={stat.label}>
            <Paper sx={{ p: 2, bgcolor: '#FFFFFF', borderRadius: 2, border: '1px solid #161616', textAlign: 'center' }}>
              <Typography variant="h4" fontWeight={700} sx={{ color: stat.color }}>{stat.value}</Typography>
              <Typography variant="caption" color="rgba(22,22,22,0.62)">{stat.label}</Typography>
            </Paper>
          </Grid>
        ))}
      </Grid>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      {loading ? (
        <Box sx={{ textAlign: 'center', py: 6 }}><CircularProgress /></Box>
      ) : claims.length === 0 ? (
        <Paper sx={{ p: 6, bgcolor: '#FFFFFF', borderRadius: 3, textAlign: 'center', border: '1px solid #161616' }}>
          <Receipt sx={{ fontSize: 64, color: '#161616', mb: 2 }} />
          <Typography variant="h6" color="rgba(22,22,22,0.62)">No claims yet</Typography>
          <Typography variant="body2" color="rgba(22,22,22,0.45)" sx={{ mb: 2 }}>Submit your first insurance claim to get started</Typography>
          <Button variant="contained" href="/submit-claim" sx={{ borderRadius: 2, background: 'linear-gradient(135deg, #0F766E, #0B5B54)' }}>
            Submit a Claim
          </Button>
        </Paper>
      ) : (
        <TableContainer component={Paper} sx={{ bgcolor: '#FFFFFF', borderRadius: 3, border: '1px solid #161616' }}>
          <Table>
            <TableHead>
              <TableRow>
                <TableCell sx={{ color: 'rgba(22,22,22,0.45)', fontWeight: 600 }}>Claim ID</TableCell>
                <TableCell sx={{ color: 'rgba(22,22,22,0.45)', fontWeight: 600 }}>Type</TableCell>
                <TableCell sx={{ color: 'rgba(22,22,22,0.45)', fontWeight: 600 }}>Amount</TableCell>
                <TableCell sx={{ color: 'rgba(22,22,22,0.45)', fontWeight: 600 }}>Settlement</TableCell>
                <TableCell sx={{ color: 'rgba(22,22,22,0.45)', fontWeight: 600 }}>Status</TableCell>
                <TableCell sx={{ color: 'rgba(22,22,22,0.45)', fontWeight: 600 }}>Date</TableCell>
                <TableCell sx={{ color: 'rgba(22,22,22,0.45)', fontWeight: 600 }}>Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {claims.map((claim) => (
                <TableRow key={claim.claim_id} hover sx={{ '&:hover': { bgcolor: '#EAF4F260' } }}>
                  <TableCell sx={{ color: '#161616', fontFamily: 'monospace', fontSize: '0.85rem' }}>{claim.claim_id}</TableCell>
                  <TableCell sx={{ color: 'rgba(22,22,22,0.62)' }}>{claim.incident_type?.replace('_', ' ')}</TableCell>
                  <TableCell sx={{ color: '#161616', fontWeight: 600 }}>{fmt(claim.estimated_amount)}</TableCell>
                  <TableCell sx={{ color: '#1a7a4c', fontWeight: 600 }}>
                    {fmt(claim.settlement_amount)}
                  </TableCell>
                  <TableCell>
                    {PROCESSING_STATUSES.includes(claim.status) ? (
                      <Chip
                        icon={<CircularProgress size={14} sx={{ color: '#0F766E' }} />}
                        label={STATUS_LABELS[claim.status] || claim.status}
                        color="info"
                        size="small"
                        sx={{
                          fontWeight: 600,
                          animation: 'pulse 2s ease-in-out infinite',
                          '@keyframes pulse': {
                            '0%, 100%': { opacity: 1 },
                            '50%': { opacity: 0.6 },
                          },
                        }}
                      />
                    ) : (
                      <Chip label={STATUS_LABELS[claim.status] || claim.status} color={STATUS_COLORS[claim.status] || 'default'} size="small" sx={{ fontWeight: 600 }} />
                    )}
                  </TableCell>
                  <TableCell sx={{ color: 'rgba(22,22,22,0.45)' }}>{claim.created_at ? new Date(claim.created_at).toLocaleDateString() : '-'}</TableCell>
                  <TableCell>
                    <Stack direction="row" spacing={0.5}>
                      <Tooltip title="View Details">
                        <IconButton size="small" sx={{ color: '#0F766E' }} onClick={() => setSelected(claim)}>
                          <Visibility fontSize="small" />
                        </IconButton>
                      </Tooltip>
                      {canAppeal(claim) && (
                        <Tooltip title="Appeal Decision">
                          <IconButton size="small" sx={{ color: '#8a5a00' }} onClick={() => { setSelected(claim); setAppealDialog(true); }}>
                            <HowToVote fontSize="small" />
                          </IconButton>
                        </Tooltip>
                      )}
                    </Stack>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      {/* Claim Detail Dialog */}
      <Dialog open={!!selected && !appealDialog} onClose={() => setSelected(null)} maxWidth="md" fullWidth
        PaperProps={{ sx: { bgcolor: '#FFFFFF', border: '1px solid #161616' } }}>
        {selected && (
          <>
            <DialogTitle sx={{ color: '#161616' }}>
              Claim: {selected.claim_id}
              <Chip label={STATUS_LABELS[selected.status] || selected.status} color={STATUS_COLORS[selected.status] || 'default'} size="small" sx={{ ml: 2 }} />
            </DialogTitle>
            <DialogContent>
              {PROCESSING_STATUSES.includes(selected.status) && (
                <Alert severity="info" icon={<CircularProgress size={18} />} sx={{ mb: 2 }}>
                  Pipeline is running - this page updates automatically every few seconds.
                </Alert>
              )}
              <Grid container spacing={2}>
                <Grid item xs={6}><Typography variant="caption" color="rgba(22,22,22,0.45)">Policy</Typography><Typography color="#161616">{selected.policy_number}</Typography></Grid>
                <Grid item xs={6}><Typography variant="caption" color="rgba(22,22,22,0.45)">Type</Typography><Typography color="#161616">{selected.incident_type?.replace('_', ' ')}</Typography></Grid>
                <Grid item xs={4}><Typography variant="caption" color="rgba(22,22,22,0.45)">Claimed Amount</Typography><Typography color="#161616" fontWeight={600}>{fmt(selected.estimated_amount)}</Typography></Grid>
                <Grid item xs={4}>
                  <Typography variant="caption" color="rgba(22,22,22,0.45)">Assessed Damage</Typography>
                  <Typography color="#8a5a00" fontWeight={600}>
                    {selected.agent_outputs?.damage_output?.assessed_damage_usd != null
                      ? fmt(selected.agent_outputs.damage_output.assessed_damage_usd)
                      : '-'}
                  </Typography>
                  {selected.agent_outputs?.damage_output?.assessed_damage_usd != null &&
                    selected.agent_outputs.damage_output.assessed_damage_usd < selected.estimated_amount && (
                    <Typography variant="caption" color="rgba(22,22,22,0.62)" sx={{ fontSize: '0.7rem' }}>
                      Assessor valued lower than your estimate
                    </Typography>
                  )}
                </Grid>
                <Grid item xs={4}><Typography variant="caption" color="rgba(22,22,22,0.45)">Settlement</Typography><Typography color="#1a7a4c" fontWeight={600}>{selected.settlement_amount != null ? fmt(selected.settlement_amount) : 'Pending'}</Typography></Grid>
                <Grid item xs={12}><Typography variant="caption" color="rgba(22,22,22,0.45)">Description</Typography><Typography color="rgba(22,22,22,0.62)">{selected.incident_description}</Typography></Grid>
                {selected.fraud_score != null && (
                  <Grid item xs={6}><Typography variant="caption" color="rgba(22,22,22,0.45)">Fraud Score</Typography><Typography color={selected.fraud_score > 0.5 ? '#a3242a' : '#1a7a4c'}>{(selected.fraud_score * 100).toFixed(0)}%</Typography></Grid>
                )}
                {selected.evaluation_score != null && (
                  <Grid item xs={6}><Typography variant="caption" color="rgba(22,22,22,0.45)">Quality Score</Typography><Typography color="#0F766E">{(selected.evaluation_score * 100).toFixed(0)}%</Typography></Grid>
                )}
                {selected.denial_reasons?.length > 0 && (
                  <Grid item xs={12}>
                    <Typography variant="caption" color="rgba(22,22,22,0.45)">Denial Reasons</Typography>
                    {selected.denial_reasons.map((r, i) => (
                      <Typography key={i} color="#a3242a" sx={{ fontSize: '0.9rem' }}>• {r}</Typography>
                    ))}
                  </Grid>
                )}
                {selected.communication_message && (
                  <Grid item xs={12}>
                    <Typography variant="caption" color="rgba(22,22,22,0.45)">Decision Notice</Typography>
                    <Typography color="rgba(22,22,22,0.62)" sx={{ whiteSpace: 'pre-line', fontSize: '0.9rem' }}>{selected.communication_message}</Typography>
                  </Grid>
                )}
                {selected.appeal_instructions && canAppeal(selected) && (
                  <Grid item xs={12}>
                    <Typography variant="caption" color="rgba(22,22,22,0.45)">Appeal Instructions</Typography>
                    <Typography color="#8a5a00" sx={{ whiteSpace: 'pre-line', fontSize: '0.9rem' }}>{selected.appeal_instructions}</Typography>
                  </Grid>
                )}
              </Grid>
            </DialogContent>
            <DialogActions>
              {canAppeal(selected) && (
                <Button onClick={() => setAppealDialog(true)} sx={{ color: '#8a5a00' }}>Appeal Decision</Button>
              )}
              <Button onClick={() => setSelected(null)} sx={{ color: 'rgba(22,22,22,0.62)' }}>Close</Button>
            </DialogActions>
          </>
        )}
      </Dialog>

      {/* Appeal Dialog */}
      <Dialog open={appealDialog} onClose={() => { setAppealDialog(false); setAppealReason(''); }} maxWidth="sm" fullWidth
        PaperProps={{ sx: { bgcolor: '#FFFFFF', border: '1px solid #161616' } }}>
        <DialogTitle sx={{ color: '#161616' }}>Appeal Claim Decision</DialogTitle>
        <DialogContent>
          {selected && (
            <Alert severity="info" sx={{ mb: 2 }}>
              Appealing: {selected.claim_id} - Current decision: {STATUS_LABELS[selected.status] || selected.status}
            </Alert>
          )}
          <Typography variant="body2" color="rgba(22,22,22,0.62)" sx={{ mb: 2 }}>
            Explain why you believe this decision should be reconsidered. Appeals are reviewed by a reviewer.
          </Typography>
          <textarea
            value={appealReason}
            onChange={(e) => setAppealReason(e.target.value)}
            placeholder="Describe why you are appealing this decision..."
            style={{
              width: '100%', minHeight: 120, padding: 12, borderRadius: 8, fontSize: '0.9rem',
              background: '#F7F5EE', color: '#161616', border: '1px solid #161616', resize: 'vertical',
            }}
          />
          <Typography variant="caption" color="rgba(22,22,22,0.45)" sx={{ mt: 1, display: 'block' }}>
            {appealReason.length}/2000 characters
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => { setAppealDialog(false); setAppealReason(''); }} sx={{ color: 'rgba(22,22,22,0.62)' }}>Cancel</Button>
          <Button onClick={handleAppeal} variant="contained" disabled={appealReason.length < 10 || appealLoading}
            sx={{ borderRadius: 2, background: 'linear-gradient(135deg, #8a5a00, #8a5a00)' }}>
            {appealLoading ? <CircularProgress size={20} /> : 'Submit Appeal'}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
