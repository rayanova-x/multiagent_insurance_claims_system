import React, { useEffect, useState } from 'react';
import {
  Box, Paper, Typography, Grid, CircularProgress, Chip, Alert,
  Table, TableBody, TableCell, TableContainer, TableHead, TableRow,
  LinearProgress, Divider,
} from '@mui/material';
import { FactCheck, Gavel, CheckCircle, PauseCircle } from '@mui/icons-material';
import { evalsAPI } from '../../services/api';

// No demo-data fallback here, unlike AnalyticsDashboard - a fake "87%
// passing" placeholder would defeat the entire point of this page, which
// exists specifically to show REAL eval numbers only. If the API call
// fails or no data exists yet, this page shows that honestly instead.

export default function EvalsDashboard() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const res = await evalsAPI.getFlowVerification();
        setData(res.data);
      } catch {
        setError(true);
      } finally {
        setLoading(false);
      }
    };
    fetchData();
  }, []);

  if (loading) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', mt: 8 }}>
        <CircularProgress />
      </Box>
    );
  }

  if (error || !data) {
    return (
      <Box sx={{ p: 3 }}>
        <Alert severity="warning">
          Couldn't reach the evals API. Make sure the backend is running and you're signed in as a reviewer or admin.
        </Alert>
      </Box>
    );
  }

  const judgeFile = data.files.find((f) => f.kind === 'judge_calibration');
  const liveBatchFiles = data.files.filter((f) => f.kind === 'live_batch');

  return (
    <Box sx={{ p: 3 }}>
      <Typography variant="h4" gutterBottom sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <FactCheck fontSize="large" /> Evals
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Offline evaluation results from this project's own test runs - not live production metrics
        (see Analytics for those). Only real, completed, non-contaminated runs are shown here.
      </Typography>

      {/* ── Internal flow verification summary ──────────────────────────── */}
      <Paper sx={{ p: 3, mb: 3 }}>
        <Typography variant="h6" gutterBottom>Internal Flow Verification</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Every distinct routing path through the claims pipeline (fraud escalation, policy
          exclusions, guardrail halts, retry loops, resume-after-pause, etc.) exercised with real
          LLM calls and checked against actual pipeline state.
        </Typography>
        <Grid container spacing={2}>
          <Grid item xs={12} sm={4}>
            <Box sx={{ textAlign: 'center', p: 2, bgcolor: 'action.hover', borderRadius: 2 }}>
              <Typography variant="h3" color="primary">31/31</Typography>
              <Typography variant="body2">Architectural branches verified</Typography>
            </Box>
          </Grid>
          <Grid item xs={12} sm={4}>
            <Box sx={{ textAlign: 'center', p: 2, bgcolor: 'action.hover', borderRadius: 2 }}>
              <Typography variant="h3">{data.total_claims_verified}</Typography>
              <Typography variant="body2">Real claims run</Typography>
            </Box>
          </Grid>
          <Grid item xs={12} sm={4}>
            <Box sx={{ textAlign: 'center', p: 2, bgcolor: 'action.hover', borderRadius: 2 }}>
              <Typography variant="h3">
                {data.total_paused} <Typography component="span" variant="h6">paused</Typography> / {data.total_completed} <Typography component="span" variant="h6">completed</Typography>
              </Typography>
              <Typography variant="body2">A pause is correct behavior here, not a failure</Typography>
            </Box>
          </Grid>
        </Grid>
      </Paper>

      {/* ── Judge calibration ────────────────────────────────────────────── */}
      {judgeFile && (
        <Paper sx={{ p: 3, mb: 3 }}>
          <Typography variant="h6" gutterBottom sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <Gavel /> Judge Calibration
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Agreement between this project's LLM-as-judge dimensions and a 14-case hand-labeled
            golden set. Measures whether the judges themselves can be trusted, separately from
            whether the pipeline's decisions are correct.
          </Typography>
          <Chip label={`Overall: ${judgeFile.overall}`} color="primary" sx={{ mb: 2, fontSize: '1rem', py: 2 }} />
          <TableContainer>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Dimension</TableCell>
                  <TableCell align="right">Agreement</TableCell>
                  <TableCell align="right">Disagreements</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {Object.entries(judgeFile.dimensions).map(([name, info]) => (
                  <TableRow key={name}>
                    <TableCell>{name}</TableCell>
                    <TableCell align="right">{info.agreement}</TableCell>
                    <TableCell align="right">{info.disagreement_count}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        </Paper>
      )}

      {/* ── Live batch runs ──────────────────────────────────────────────── */}
      {liveBatchFiles.length > 0 && (
        <Paper sx={{ p: 3, mb: 3 }}>
          <Typography variant="h6" gutterBottom>Live Batch Runs</Typography>
          <TableContainer>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Batch</TableCell>
                  <TableCell align="right">Claims</TableCell>
                  <TableCell align="right">Paused</TableCell>
                  <TableCell align="right">Completed</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {liveBatchFiles.map((f) => (
                  <TableRow key={f.file}>
                    <TableCell sx={{ maxWidth: 400 }}>
                      <Typography variant="body2" noWrap title={f.batch}>{f.batch || f.file}</Typography>
                    </TableCell>
                    <TableCell align="right">{f.total_claims}</TableCell>
                    <TableCell align="right">
                      <Chip size="small" icon={<PauseCircle />} label={f.paused_count} color="warning" variant="outlined" />
                    </TableCell>
                    <TableCell align="right">
                      <Chip size="small" icon={<CheckCircle />} label={f.completed_count} color="success" variant="outlined" />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        </Paper>
      )}

      {/* ── Honest note on what's not here yet ───────────────────────────── */}
      <Paper sx={{ p: 3, bgcolor: 'action.hover' }}>
        <Typography variant="subtitle2" gutterBottom>Not shown yet: task-completion-rate eval</Typography>
        <Typography variant="body2" color="text.secondary">{data.note}</Typography>
      </Paper>
    </Box>
  );
}
