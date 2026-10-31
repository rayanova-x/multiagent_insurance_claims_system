import React, { useEffect, useRef, useState } from 'react';
import {
  Box, Paper, Typography, Chip, CircularProgress, Button, Alert, Stack, LinearProgress,
  IconButton, Collapse, Divider,
} from '@mui/material';
import {
  CheckCircle, HourglassEmpty, PlayCircle, PauseCircle, ErrorOutline, Gavel, ArrowForward,
  ExpandMore, ExpandLess, Code,
} from '@mui/icons-material';
import { useParams, useNavigate } from 'react-router-dom';
import { claimsAPI } from '../../services/api';
import useAuthStore from '../../store/authStore';

const ACTIVE_STATUSES = ['processing', 'submitted', 'pending_human_review'];

// The canonical pipeline steps for a non-fast-mode, non-denied claim (see
// src/agents/graph.py's route_after_intake). Rendered as connected nodes;
// a claim whose real trace diverges (fast mode, denial, auto-reject) still
// renders correctly since status is derived from pipeline_path membership,
// not a fixed index match.
const STEPS = [
  { key: 'document_agent', label: 'Document Agent', group: null },
  { key: 'intake_agent', label: 'Intake', group: null },
  { key: 'damage_assessor', label: 'Damage Assessor', group: 'fanout' },
  { key: 'policy_checker', label: 'Policy Checker', group: 'fanout' },
  // fraud_crew is deliberately not shown here - it still runs as a mocked,
  // inert no-op in the real pipeline (route_after_intake always fans out to
  // it), but fraud detection's CrewAI internals are out of scope for this
  // rebuild, so the visual trace omits it rather than show a step that's
  // never a real judgment.
  { key: 'consistency_coordinator', label: 'Consistency Check', group: null },
  { key: 'settlement_calculator', label: 'Settlement', group: null },
  { key: 'evaluator', label: 'Evaluator', group: null },
  { key: 'communication_agent', label: 'Communication', group: null },
];

const STATUS_STYLE = {
  pending: { color: 'rgba(22,22,22,0.35)', bg: '#F4F2EA', icon: HourglassEmpty },
  running: { color: '#0F766E', bg: '#DCF3EF', icon: PlayCircle },
  done: { color: '#1a7a4c', bg: '#dff3e6', icon: CheckCircle },
  paused: { color: '#8a5a00', bg: '#fff1cf', icon: PauseCircle },
  error: { color: '#a3242a', bg: '#fbdede', icon: ErrorOutline },
};

function stepStatus(stepKey, claim) {
  const path = claim.pipeline_path || [];
  const trace = claim.agent_outputs?._trace || [];
  const inPath = path.includes(stepKey);

  if (claim.status === 'failed') {
    // The step whose own trace entry is the last one recorded is the one
    // that was running when the pipeline crashed - shown as the error, not
    // merely "done" (it may not have finished) or "pending" (it did run).
    const lastTraceAgent = trace.length ? trace[trace.length - 1].agent : null;
    if (stepKey === lastTraceAgent && !inPath) return 'error';
    if (inPath) return 'done';
    return 'pending';
  }

  if (inPath) return 'done';

  const isActive = ACTIVE_STATUSES.includes(claim.status);
  if (!isActive) return 'pending'; // claim finished and this step never ran (e.g. fast mode, denial)

  if (claim.status === 'pending_human_review') {
    // pipeline_trace carries an entry for whatever step the claim is
    // CURRENTLY paused at even though pipeline_path hasn't been updated
    // with a post-resume entry yet - that's the step to mark 'paused', not
    // merely pending, so the reviewer sees exactly where it stopped.
    const lastTraceAgent = trace.length ? trace[trace.length - 1].agent : null;
    if (stepKey === lastTraceAgent) return 'paused';
    return 'pending';
  }

  // Currently processing: the step immediately after the last completed
  // one is "running"; anything further out is still pending.
  const stepIdx = STEPS.findIndex((s) => s.key === stepKey);
  const lastDoneIdx = Math.max(
    ...STEPS.map((s, i) => (path.includes(s.key) ? i : -1)),
    -1
  );
  if (stepIdx === lastDoneIdx + 1) return 'running';
  return 'pending';
}

function StepNode({ step, status, durationMs }) {
  const style = STATUS_STYLE[status];
  const Icon = style.icon;
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', minWidth: 110 }}>
      <Box sx={{
        width: 56, height: 56, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center',
        bgcolor: style.bg, border: `2px solid ${style.color}`, color: style.color,
        animation: status === 'running' ? 'pulse 1.6s infinite' : 'none',
        '@keyframes pulse': { '0%, 100%': { opacity: 1 }, '50%': { opacity: 0.45 } },
      }}>
        <Icon sx={{ fontSize: 28 }} />
      </Box>
      <Typography variant="caption" fontWeight={600} sx={{ mt: 1, textAlign: 'center', color: '#161616' }}>
        {step.label}
      </Typography>
      {durationMs != null && (
        <Typography variant="caption" color="text.secondary">{(durationMs / 1000).toFixed(1)}s</Typography>
      )}
    </Box>
  );
}

function CallDetail({ call }) {
  return (
    <Box sx={{ mb: 1.5, p: 1.5, bgcolor: '#F4F2EA', borderRadius: 2, border: '1px solid rgba(22,22,22,0.15)' }}>
      <Typography variant="caption" fontWeight={700} sx={{ display: 'block', mb: 0.5 }}>{call.label}</Typography>
      {call.prompt != null ? (
        <>
          <Typography variant="caption" color="text.secondary" fontWeight={600}>Prompt</Typography>
          <Box component="pre" sx={{
            fontFamily: 'monospace', fontSize: '0.7rem', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            bgcolor: '#FFFFFF', p: 1, borderRadius: 1, border: '1px solid rgba(22,22,22,0.1)', mt: 0.5, mb: 1,
            maxHeight: 240, overflow: 'auto',
          }}>{call.prompt}</Box>
        </>
      ) : (
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
          (prompt not captured for this call)
        </Typography>
      )}
      <Typography variant="caption" color="text.secondary" fontWeight={600}>Raw Output</Typography>
      <Box component="pre" sx={{
        fontFamily: 'monospace', fontSize: '0.7rem', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
        bgcolor: '#FFFFFF', p: 1, borderRadius: 1, border: '1px solid rgba(22,22,22,0.1)', mt: 0.5,
        maxHeight: 240, overflow: 'auto',
      }}>{JSON.stringify(call.raw_output, null, 2)}</Box>
    </Box>
  );
}

function AgentDetailRow({ t, isLast }) {
  const isAdmin = useAuthStore((s) => s.isAdmin());
  const [open, setOpen] = useState(false);
  const hasCalls = isAdmin && (t.calls?.length > 0 || t.calls_note);

  return (
    <Box sx={{ pb: 2, borderBottom: isLast ? 'none' : '1px solid rgba(22,22,22,0.15)' }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.5 }}>
        <Typography variant="subtitle2" fontWeight={700}>{t.agent}</Typography>
        {t.decision && <Chip size="small" label={t.decision} variant="outlined" />}
        {t.confidence != null && (
          <Typography variant="caption" color="text.secondary">confidence {(t.confidence * 100).toFixed(0)}%</Typography>
        )}
        <Typography variant="caption" color="text.secondary" sx={{ ml: 'auto' }}>{t.duration_ms}ms</Typography>
        {hasCalls && (
          <IconButton size="small" onClick={() => setOpen(!open)} title="Developer detail (admin only)">
            <Code fontSize="small" sx={{ color: open ? '#0F766E' : 'rgba(22,22,22,0.45)' }} />
            {open ? <ExpandLess fontSize="small" /> : <ExpandMore fontSize="small" />}
          </IconButton>
        )}
      </Box>
      {t.reasoning && <Typography variant="body2" color="text.secondary">{t.reasoning}</Typography>}
      {t.flags?.length > 0 && (
        <Stack direction="row" spacing={0.5} sx={{ mt: 1, flexWrap: 'wrap', gap: 0.5 }}>
          {t.flags.map((f, fi) => <Chip key={fi} size="small" label={f} color="warning" variant="outlined" />)}
        </Stack>
      )}
      {hasCalls && (
        <Collapse in={open}>
          <Divider sx={{ my: 1.5 }} />
          <Typography variant="caption" fontWeight={700} sx={{ display: 'block', mb: 1 }}>
            Developer detail - admin only
          </Typography>
          {t.calls_note && (
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1, fontStyle: 'italic' }}>
              {t.calls_note}
            </Typography>
          )}
          {(t.calls || []).map((call, ci) => <CallDetail key={ci} call={call} />)}
        </Collapse>
      )}
    </Box>
  );
}

export default function ClaimTrace() {
  const { claimId } = useParams();
  const navigate = useNavigate();
  const [claim, setClaim] = useState(null);
  const [error, setError] = useState(false);
  const intervalRef = useRef(null);

  const fetchClaim = async () => {
    try {
      const res = await claimsAPI.getClaimById(claimId);
      setClaim(res.data);
    } catch {
      setError(true);
    }
  };

  useEffect(() => {
    fetchClaim();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [claimId]);

  useEffect(() => {
    if (!claim) return;
    const active = ACTIVE_STATUSES.includes(claim.status);
    if (active && !intervalRef.current) {
      intervalRef.current = setInterval(fetchClaim, 3000);
    } else if (!active && intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    return () => {
      if (intervalRef.current) { clearInterval(intervalRef.current); intervalRef.current = null; }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [claim?.status]);

  if (error) {
    return (
      <Box sx={{ p: 3 }}>
        <Alert severity="warning">Couldn't load this claim's trace. Make sure you're signed in as a reviewer or admin.</Alert>
      </Box>
    );
  }

  if (!claim) {
    return <Box sx={{ display: 'flex', justifyContent: 'center', mt: 8 }}><CircularProgress /></Box>;
  }

  // fraud_crew is mocked/out-of-scope - never shown, see STEPS' own comment.
  const trace = (claim.agent_outputs?._trace || []).filter((t) => t.agent !== 'fraud_crew');
  const durationByAgent = Object.fromEntries(trace.map((t) => [t.agent, t.duration_ms]));
  const isActive = ACTIVE_STATUSES.includes(claim.status);
  const isPaused = claim.status === 'pending_human_review';

  return (
    <Box sx={{ p: 3 }}>
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 1 }}>
        <Typography variant="h5" fontWeight={700}>Claim Trace: {claimId}</Typography>
        <Chip
          label={isActive ? 'Processing' : claim.status?.replace(/_/g, ' ')}
          color={isActive ? 'info' : isPaused ? 'warning' : 'default'}
          icon={isActive ? <PlayCircle /> : undefined}
        />
      </Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        {claim.incident_type?.replace(/_/g, ' ')} claim, {claim.estimated_amount != null ? `$${claim.estimated_amount.toLocaleString()}` : ''}
        {isActive && ' - updating live every 3s'}
      </Typography>

      {isPaused && (
        <Alert severity="warning" icon={<Gavel />} sx={{ mb: 3 }}
          action={
            <Button color="inherit" size="small" endIcon={<ArrowForward />} onClick={() => navigate('/hitl-queue')}>
              Open HITL Review
            </Button>
          }>
          This claim is paused for human review{claim.hitl_ticket_id ? ` (ticket ${claim.hitl_ticket_id})` : ''}.
        </Alert>
      )}

      {claim.status === 'failed' && (
        <Alert severity="error" sx={{ mb: 3 }}>
          Pipeline failed{claim.error_log?.length ? `: ${claim.error_log[0]}` : '.'}
        </Alert>
      )}

      <Paper sx={{ p: 4, mb: 3, overflowX: 'auto' }}>
        <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1, minWidth: 'fit-content' }}>
          {STEPS.map((step, i) => {
            const status = stepStatus(step.key, claim);
            const isLast = i === STEPS.length - 1;
            return (
              <React.Fragment key={step.key}>
                <StepNode step={step} status={status} durationMs={durationByAgent[step.key]} />
                {!isLast && (
                  <Box sx={{ display: 'flex', alignItems: 'center', height: 56, mx: -0.5 }}>
                    <ArrowForward sx={{ color: status === 'done' ? '#1a7a4c' : 'rgba(22,22,22,0.25)' }} />
                  </Box>
                )}
              </React.Fragment>
            );
          })}
        </Box>
      </Paper>

      {trace.length > 0 && (
        <Paper sx={{ p: 3 }}>
          <Typography variant="h6" gutterBottom>Agent Details</Typography>
          <Stack spacing={2}>
            {trace.map((t, i) => (
              <AgentDetailRow key={i} t={t} isLast={i === trace.length - 1} />
            ))}
          </Stack>
        </Paper>
      )}
    </Box>
  );
}
