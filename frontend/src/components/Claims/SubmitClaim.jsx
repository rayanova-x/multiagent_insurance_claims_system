import React, { useEffect, useState } from 'react';
import {
  Box, Paper, Typography, TextField, Button, Grid, MenuItem, Alert,
  CircularProgress, Divider, Chip, Collapse, IconButton, Tooltip,
} from '@mui/material';
import {
  Send, Add, ExpandMore, ExpandLess, CheckCircle, Policy,
  UploadFile, Warning, ErrorOutline, Close, Replay,
} from '@mui/icons-material';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { claimsAPI, evalsAPI, policyAPI } from '../../services/api';
import api from '../../services/api';
import useAuthStore from '../../store/authStore';
import useCaseRunStore from '../../store/caseRunStore';

// ── Quick-create panel defaults ────────────────────────────────────────────

function randomSuffix() {
  return String(Math.floor(100000 + Math.random() * 900000));
}

export default function SubmitClaim() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const user = useAuthStore((s) => s.user);
  const consumePendingCase = useCaseRunStore((s) => s.consumePendingCase);
  const [loading, setLoading] = useState(false);
  const [success, setSuccess] = useState(false);
  const [error, setError] = useState('');

  // Set when this submission was launched from the Evals tab's "Run this
  // case" button (?case=...) - changes the post-submit redirect target
  // (-> /observability/{claim_id} instead of /my-claims) and, for
  // golden-set cases, carries already-structured document content through
  // on submit instead of the real upload flow (see handleSubmit).
  const [activeCase, setActiveCase] = useState(null); // { caseId, source, prebuilt_documents } | null
  const [caseLoading, setCaseLoading] = useState(false);

  // Demo-scenario picker ("try a demo") - a curated subset of the same
  // golden-set/test_pdfs cases the admin-only Evals tab's "Run this case"
  // uses, exposed here under plain claimant auth (api/claims/demo-scenarios)
  // so a demo can start right from the claimant's own submit form.
  const setPendingCase = useCaseRunStore((s) => s.setPendingCase);
  const [demoScenarios, setDemoScenarios] = useState([]);
  const [demoLoading, setDemoLoading] = useState(null); // caseId currently being materialized, or null

  // Country-aware state
  const [claimTypes, setClaimTypes] = useState([]);
  const [requiredDocsByType, setRequiredDocsByType] = useState({});
  const [currencySymbol, setCurrencySymbol] = useState('$');
  const [countryName, setCountryName] = useState('');
  const [countryCode, setCountryCode] = useState('US');

  // Real document upload (replaces the old self-reported checklist). A
  // random token is generated once per form load - files upload against it
  // immediately on selection (see claimsAPI.uploadDocument), before a real
  // claim_id exists; /submit links the staged files to the claim via
  // upload_token (see api/routes_claims.py's /upload-staging endpoints).
  // docUploads: { [slot]: { status: 'idle'|'uploading'|'ok'|'problem'|'failed',
  //   message, keptAnyway, disputeReason } }
  const [uploadToken] = useState(() => crypto.randomUUID());
  const [docUploads, setDocUploads] = useState({});

  // Incident-type verification (src/security/incident_type_check.py via
  // POST /api/claims/verify-type) - runs once, synchronously, when the
  // claimant moves on from the description field, before final submit.
  const [typeCheck, setTypeCheck] = useState(null); // { suggested_type, reasoning, confidence } | null
  const [typeCheckLoading, setTypeCheckLoading] = useState(false);
  const [typeDisputeMode, setTypeDisputeMode] = useState(false);
  const [typeDisputeReason, setTypeDisputeReason] = useState('');
  const [typeConfirmed, setTypeConfirmed] = useState(false); // claimant accepted the suggestion, or dismissed after disputing

  // Existing policies list
  const [policies, setPolicies] = useState([]);
  const [policiesLoading, setPoliciesLoading] = useState(true);

  // Quick-create policy state
  const [templates, setTemplates] = useState([]);
  const [showQuickCreate, setShowQuickCreate] = useState(false);
  const [quickForm, setQuickForm] = useState(null);
  const [quickSaving, setQuickSaving] = useState(false);
  const [quickMsg, setQuickMsg] = useState(null);

  // Claim form
  const [form, setForm] = useState({
    policy_number: '',
    incident_type: '',
    incident_date: new Date().toISOString().split('T')[0],
    incident_description: '',
    incident_location: '',
    police_report_number: '',
    estimated_amount: '',
    vehicle_year: '',
    vehicle_make: '',
    vehicle_model: '',
  });

  // Load country settings + policies + templates on mount
  useEffect(() => {
    // Country settings
    (async () => {
      try {
        const res = await api.get('/api/settings/country');
        const { claim_types, required_documents, active } = res.data;
        setClaimTypes((claim_types || []).map((t) => ({
          value: t,
          label: t.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
        })));
        setRequiredDocsByType(required_documents || {});
        setCurrencySymbol(active?.currency_symbol || '$');
        setCountryName(active?.name || '');
        setCountryCode(active?.code || 'US');
        if (claim_types?.length) {
          setForm((f) => ({ ...f, incident_type: claim_types[0] }));
        }
      } catch {
        setClaimTypes([
          { value: 'auto_collision', label: 'Auto Collision' },
          { value: 'auto_theft', label: 'Auto Theft' },
          { value: 'property_fire', label: 'Property Fire' },
          { value: 'liability', label: 'Liability' },
        ]);
        setForm((f) => ({ ...f, incident_type: 'auto_collision' }));
      }
    })();

    // Existing policies
    (async () => {
      setPoliciesLoading(true);
      try {
        const res = await policyAPI.list();
        setPolicies(res.data || []);
      } catch {
        setPolicies([]);
      } finally {
        setPoliciesLoading(false);
      }
    })();

    // Policy templates (country-aware defaults)
    (async () => {
      try {
        const res = await policyAPI.defaults();
        setTemplates(res.data?.templates || []);
      } catch {
        setTemplates([]);
      }
    })();

    // Demo scenarios for the "try a demo" picker
    (async () => {
      try {
        const res = await claimsAPI.listDemoScenarios();
        setDemoScenarios(res.data?.scenarios || []);
      } catch {
        setDemoScenarios([]);
      }
    })();
  }, []);

  // Materialize a demo scenario and hand it off to the existing case-loader
  // effect below (same caseRunStore + ?case= contract the Evals tab's "Run
  // this case" button uses) - this just gives claimants a door onto it.
  const runDemoScenario = async (caseId) => {
    setDemoLoading(caseId);
    try {
      const res = await claimsAPI.materializeDemoScenario(caseId);
      setPendingCase({ caseId, source: res.data.source, ...res.data, viaClaimantEndpoint: true });
      navigate('/submit-claim?case=' + encodeURIComponent(caseId));
    } catch {
      setError('Could not load that demo scenario. Please try again.');
    } finally {
      setDemoLoading(null);
    }
  };

  // Pick up a case launched from the Evals tab's "Run this case" button.
  // The materialized data was handed off via caseRunStore (not just the
  // ?case= id) since it carries a full claim payload + either
  // prebuilt_documents or pdf files to stage - too much for a URL param.
  useEffect(() => {
    const caseId = searchParams.get('case');
    if (!caseId) return;
    const pending = consumePendingCase();
    if (!pending || pending.caseId !== caseId) return;

    setCaseLoading(true);
    const c = pending.claim || {};
    setForm((f) => ({
      ...f,
      policy_number: c.policy_number || f.policy_number,
      incident_type: c.incident_type || f.incident_type,
      incident_date: c.incident_date || f.incident_date,
      incident_description: c.incident_description || f.incident_description,
      incident_location: c.incident_location || f.incident_location,
      police_report_number: c.police_report_number || '',
      estimated_amount: c.estimated_amount != null ? String(c.estimated_amount) : f.estimated_amount,
      vehicle_year: c.vehicle_year != null ? String(c.vehicle_year) : '',
      vehicle_make: c.vehicle_make || '',
      vehicle_model: c.vehicle_model || '',
    }));

    if (pending.source === 'golden_set' && pending.prebuilt_documents) {
      setActiveCase({ caseId, source: 'golden_set', prebuilt_documents: pending.prebuilt_documents });
      setCaseLoading(false);
    } else if (pending.source === 'test_pdfs' && pending.pdf_files) {
      setActiveCase({ caseId, source: 'test_pdfs', prebuilt_documents: null });
      // Stage each case PDF through the REAL upload endpoint, same as a
      // live claimant choosing a file - fetched from the case's own PDF
      // endpoint and wrapped as a File, not a shortcut around the upload UI.
      (async () => {
        for (const [slot, filename] of Object.entries(pending.pdf_files)) {
          setDocUploads((prev) => ({ ...prev, [slot]: { status: 'uploading', message: '' } }));
          try {
            const pdfUrl = pending.viaClaimantEndpoint
              ? claimsAPI.getDemoScenarioPdfUrl(caseId, slot)
              : evalsAPI.getCasePdfUrl(caseId, slot);
            const res = await fetch(pdfUrl, {
              headers: { Authorization: `Bearer ${localStorage.getItem('token')}` },
            });
            const blob = await res.blob();
            const file = new File([blob], filename, { type: 'application/pdf' });
            const uploadRes = await claimsAPI.uploadDocument(uploadToken, slot, file);
            const { status, message } = uploadRes.data;
            setDocUploads((prev) => ({ ...prev, [slot]: { status, message, keptAnyway: false, disputeReason: '' } }));
          } catch {
            setDocUploads((prev) => ({ ...prev, [slot]: { status: 'failed', message: 'Could not stage this case file.' } }));
          }
        }
        setCaseLoading(false);
      })();
    } else {
      setCaseLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  // ── Quick-create helpers ─────────────────────────────────────────────────

  const startQuickCreate = (tmpl) => {
    setQuickForm({
      ...tmpl,
      policy_number: tmpl.policy_number + randomSuffix(),
      holder_name: user?.username || '',
    });
    setQuickMsg(null);
  };

  const handleQuickSave = async () => {
    if (!quickForm) return;
    setQuickSaving(true);
    setQuickMsg(null);
    try {
      // Remove the UI-only "label" field before sending
      const { label, ...payload } = quickForm;
      await policyAPI.upsert(payload);
      // Refresh policies list and auto-select the new one
      const res = await policyAPI.list();
      setPolicies(res.data || []);
      setForm((f) => ({ ...f, policy_number: quickForm.policy_number }));
      setQuickMsg({ type: 'success', text: `Policy ${quickForm.policy_number} created and selected!` });
      setQuickForm(null);
      // Auto-collapse after a beat
      setTimeout(() => setShowQuickCreate(false), 1500);
    } catch (err) {
      setQuickMsg({ type: 'error', text: err.response?.data?.detail || 'Failed to create policy' });
    } finally {
      setQuickSaving(false);
    }
  };

  const qf = (k, v) => setQuickForm((prev) => ({ ...prev, [k]: v }));

  // ── Claim form helpers ───────────────────────────────────────────────────

  const selectedPolicy = policies.find((p) => p.policy_number === form.policy_number);
  const isAuto = form.incident_type.includes('auto') || form.incident_type === 'own_damage' || form.incident_type === 'theft';

  // Country-aware placeholder hints
  const hints = countryCode === 'IN' ? {
    location: 'e.g. Bengaluru, Mumbai, Delhi',
    policeReport: 'e.g. FIR-2026-12345',
    amount: 'e.g. 50000',
    vehicleYear: 'e.g. 2022',
    vehicleMake: 'e.g. Maruti Suzuki, Tata, Hyundai',
    vehicleModel: 'e.g. Swift, Nexon, Creta',
    description: 'Describe what happened - e.g. "Vehicle hit by truck on NH-44 near Bengaluru toll plaza"',
  } : {
    location: 'e.g. Los Angeles, CA',
    policeReport: 'e.g. PR-2026-12345',
    amount: 'e.g. 5000',
    vehicleYear: 'e.g. 2022',
    vehicleMake: 'e.g. Toyota, Honda, Ford',
    vehicleModel: 'e.g. Camry, Civic, F-150',
    description: 'Describe what happened - e.g. "Rear-ended at intersection on I-405"',
  };
  const requiredDocs = requiredDocsByType[form.incident_type] || [];
  const docLabel = (d) => d.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());

  // A mismatch found by verify-type must be resolved (accepted or disputed)
  // before final submit - but only once a check has actually run. Changing
  // incident_type or the description invalidates any prior check, since
  // either could change the answer.
  const typeNeedsResolution = typeCheck && !typeCheck.match && !typeConfirmed && !typeDisputeMode;
  // Only a genuinely unreadable file (status 'failed', i.e. could_not_parse)
  // blocks submit - a 'problem' status (wrong type, arithmetic/amount
  // mismatch) never blocks, it follows the same dispute-and-continue
  // pattern as the incident-type check below.
  const hasBlockingUpload = Object.values(docUploads).some((d) => d.status === 'failed');
  const canSubmit = form.policy_number && form.incident_type && form.incident_description.length >= 10
    && parseFloat(form.estimated_amount) > 0 && !typeNeedsResolution && !typeCheckLoading && !hasBlockingUpload && !caseLoading;

  const handleChange = (field) => (e) => {
    const value = e.target.value;
    if (field === 'incident_type') {
      setDocUploads({});
      setTypeCheck(null);
      setTypeConfirmed(false);
      setTypeDisputeMode(false);
      setTypeDisputeReason('');
    }
    setForm((f) => ({ ...f, [field]: value }));
  };

  const handleDocFileChange = (slot) => async (e) => {
    const file = e.target.files?.[0];
    e.target.value = ''; // allow re-selecting the same file name
    if (!file) return;
    setDocUploads((prev) => ({ ...prev, [slot]: { status: 'uploading', message: '' } }));
    try {
      const res = await claimsAPI.uploadDocument(uploadToken, slot, file);
      const { status, message } = res.data;
      setDocUploads((prev) => ({ ...prev, [slot]: { status, message, keptAnyway: false, disputeReason: '' } }));
    } catch (err) {
      setDocUploads((prev) => ({
        ...prev,
        [slot]: { status: 'failed', message: err.response?.data?.detail || 'Upload failed - please try again.' },
      }));
    }
  };

  const handleDocRemove = async (slot) => {
    try {
      await claimsAPI.removeStagedDocument(uploadToken, slot);
    } catch {
      // best-effort - fall through and clear the local state regardless
    }
    setDocUploads((prev) => {
      const next = { ...prev };
      delete next[slot];
      return next;
    });
  };

  const handleDocKeepAnyway = (slot) => {
    setDocUploads((prev) => ({ ...prev, [slot]: { ...prev[slot], keptAnyway: true } }));
  };

  const handleDocDisputeReasonChange = (slot) => (e) => {
    const value = e.target.value;
    setDocUploads((prev) => ({ ...prev, [slot]: { ...prev[slot], disputeReason: value } }));
  };

  // Runs once the claimant finishes writing the description (on blur), so
  // it doesn't fire on every keystroke. A prior accepted/disputed answer is
  // invalidated, since the description just changed.
  const handleDescriptionBlur = async () => {
    if (form.incident_description.trim().length < 10) return;
    setTypeCheck(null);
    setTypeConfirmed(false);
    setTypeDisputeMode(false);
    setTypeDisputeReason('');
    setTypeCheckLoading(true);
    try {
      const res = await claimsAPI.verifyType({
        incident_type: form.incident_type,
        incident_description: form.incident_description,
      });
      setTypeCheck(res.data);
    } catch {
      // Classifier outage: don't block the claimant over a check that
      // couldn't run (src/security/incident_type_check.py fails open the
      // same way) - treat as a clean pass.
      setTypeCheck({ match: true });
    } finally {
      setTypeCheckLoading(false);
    }
  };

  const acceptSuggestedType = () => {
    setForm((f) => ({ ...f, incident_type: typeCheck.suggested_type }));
    setCheckedDocs({});
    setTypeConfirmed(true);
    setTypeDisputeMode(false);
  };

  const confirmDispute = () => {
    setTypeConfirmed(true);
    setTypeDisputeMode(false);
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const documentDisputes = Object.entries(docUploads)
        .filter(([, d]) => d.keptAnyway)
        .map(([slot, d]) => ({ slot, reason: d.disputeReason?.trim() || null }));
      const payload = {
        ...form,
        estimated_amount: parseFloat(form.estimated_amount),
        vehicle_year: form.vehicle_year ? parseInt(form.vehicle_year) : null,
        documents: Object.entries(docUploads).filter(([, d]) => d.status !== 'failed').map(([slot]) => slot),
        upload_token: uploadToken,
        document_disputes: documentDisputes,
        prebuilt_documents: activeCase?.prebuilt_documents || null,
        type_disputed_by_claimant: Boolean(typeCheck && !typeCheck.match && typeConfirmed && typeDisputeReason.trim()),
        type_dispute_reason: typeDisputeReason.trim() || null,
      };
      const res = await claimsAPI.submitClaim(payload);
      setSuccess(true);
      if (activeCase) {
        setTimeout(() => navigate(`/observability/${res.data.claim_id}`), 1200);
      } else {
        setTimeout(() => navigate('/my-claims'), 2000);
      }
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to submit claim');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Box>
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 3 }}>
        <Box>
          <Typography variant="h5" fontWeight={700} color="#161616">Submit New Claim</Typography>
          <Typography variant="body2" color="rgba(22,22,22,0.62)">
            Select a policy (or quick-create one), then fill in your claim details.
          </Typography>
        </Box>
        {countryName && (
          <Typography variant="caption" color="rgba(22,22,22,0.45)" sx={{ border: '1px solid #161616', borderRadius: 2, px: 1.5, py: 0.5 }}>
            {countryName} ({currencySymbol})
          </Typography>
        )}
      </Box>

      {!activeCase && demoScenarios.length > 0 && (
        <Paper sx={{ p: 2.5, mb: 3, bgcolor: '#F7F5EE', borderRadius: 3, border: '1px solid #161616' }}>
          <Typography variant="subtitle2" fontWeight={700} color="#161616" sx={{ mb: 1.5 }}>
            Try a demo scenario
          </Typography>
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
            {demoScenarios.map((s) => (
              <Button
                key={s.id}
                size="small"
                variant="outlined"
                disabled={demoLoading !== null}
                onClick={() => runDemoScenario(s.id)}
                startIcon={demoLoading === s.id ? <CircularProgress size={14} /> : undefined}
                sx={{ borderColor: '#161616', color: '#161616' }}
              >
                {s.label}
              </Button>
            ))}
          </Box>
        </Paper>
      )}

      {activeCase && (
        <Alert severity="info" sx={{ mb: 3 }}>
          Running test case <strong>{activeCase.caseId}</strong> - form fields
          {activeCase.source === 'test_pdfs' ? ' and documents have' : ' have'} been pre-filled.
          {caseLoading && ' Staging documents...'}
        </Alert>
      )}

      {success && (
        <Alert severity="success" sx={{ mb: 3 }}>
          Claim submitted successfully! {activeCase ? 'Redirecting to the live trace view...' : 'Redirecting to your claims...'}
        </Alert>
      )}
      {error && <Alert severity="error" sx={{ mb: 3 }}>{error}</Alert>}

      {/* ── Step 1: Policy Selection ──────────────────────────────────────── */}
      <Paper sx={{ p: 3, mb: 3, bgcolor: '#FFFFFF', borderRadius: 3, border: '1px solid #161616' }}>
        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 2 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <Policy sx={{ color: '#0F766E' }} />
            <Typography variant="subtitle1" fontWeight={700} color="#161616">
              Step 1: Select Your Insurance Policy
            </Typography>
          </Box>
          {selectedPolicy && (
            <Chip icon={<CheckCircle />} label={`${selectedPolicy.policy_number} - ${selectedPolicy.holder_name}`}
              color="success" size="small" sx={{ fontWeight: 600 }} />
          )}
        </Box>

        {policiesLoading ? (
          <Box sx={{ textAlign: 'center', py: 2 }}><CircularProgress size={24} /></Box>
        ) : (
          <>
            {/* Existing policies dropdown */}
            <TextField select fullWidth label="Choose an existing policy" value={form.policy_number}
              onChange={handleChange('policy_number')} sx={{ mb: 2 }}
              helperText={policies.length === 0 ? 'No policies found - create one below' : `${policies.length} policies available`}>
              <MenuItem value="">-- Select a policy --</MenuItem>
              {activeCase && form.policy_number && !policies.some((p) => p.policy_number === form.policy_number) && (
                <MenuItem value={form.policy_number}>
                  <Typography fontFamily="monospace" fontWeight={600}>{form.policy_number}</Typography>
                  <Chip label="test case" size="small" sx={{ ml: 1, height: 20, fontSize: '0.7rem' }} />
                </MenuItem>
              )}
              {policies.map((p) => (
                <MenuItem key={p.policy_number} value={p.policy_number}>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, width: '100%' }}>
                    <Typography fontFamily="monospace" fontWeight={600}>{p.policy_number}</Typography>
                    <Typography variant="body2" color="text.secondary">- {p.holder_name}</Typography>
                    <Chip label={p.type} size="small" sx={{ ml: 'auto', height: 20, fontSize: '0.7rem' }} />
                    <Chip label={p.status} size="small" color={p.status === 'active' ? 'success' : 'error'}
                      sx={{ height: 20, fontSize: '0.7rem' }} />
                  </Box>
                </MenuItem>
              ))}
            </TextField>

            {/* Quick-create toggle */}
            <Divider sx={{ my: 2, borderColor: '#161616' }}>
              <Chip label="OR" size="small" sx={{ color: 'rgba(22,22,22,0.45)', fontSize: '0.7rem' }} />
            </Divider>

            <Button
              onClick={() => setShowQuickCreate(!showQuickCreate)}
              startIcon={showQuickCreate ? <ExpandLess /> : <Add />}
              endIcon={!showQuickCreate ? <ExpandMore /> : null}
              sx={{ color: '#0F766E', fontWeight: 600, mb: 1 }}
            >
              Quick-Create a Policy with {countryName || 'Default'} Defaults
            </Button>

            <Collapse in={showQuickCreate}>
              {quickMsg && <Alert severity={quickMsg.type} sx={{ mb: 2 }} onClose={() => setQuickMsg(null)}>{quickMsg.text}</Alert>}

              {/* Template buttons */}
              {!quickForm && (
                <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', mb: 2, mt: 1 }}>
                  {templates.map((tmpl, i) => (
                    <Button key={i} variant="outlined" onClick={() => startQuickCreate(tmpl)}
                      sx={{ borderColor: '#161616', color: '#161616', textTransform: 'none', borderRadius: 2,
                        '&:hover': { borderColor: '#0F766E', bgcolor: '#0F766E20' } }}>
                      <Box sx={{ textAlign: 'left' }}>
                        <Typography variant="body2" fontWeight={600}>{tmpl.label}</Typography>
                        <Typography variant="caption" color="rgba(22,22,22,0.45)">
                          Deductible: {tmpl.deductible?.toLocaleString()} | Coverage keys: {Object.keys(tmpl.coverage).join(', ')}
                        </Typography>
                      </Box>
                    </Button>
                  ))}
                </Box>
              )}

              {/* Editable quick-create form */}
              {quickForm && (
                <Paper sx={{ p: 2, bgcolor: '#F7F5EE', borderRadius: 2, border: '1px solid #161616', mt: 1 }}>
                  <Typography variant="subtitle2" color="rgba(22,22,22,0.62)" fontWeight={600} sx={{ mb: 2 }}>
                    Customize & Create (defaults pre-filled for {countryName})
                  </Typography>
                  <Grid container spacing={2}>
                    <Grid item xs={6} md={4}>
                      <TextField fullWidth label="Policy Number" value={quickForm.policy_number}
                        onChange={(e) => qf('policy_number', e.target.value)} size="small" />
                    </Grid>
                    <Grid item xs={6} md={4}>
                      <TextField fullWidth label="Holder Name" value={quickForm.holder_name}
                        onChange={(e) => qf('holder_name', e.target.value)} size="small"
                        placeholder="Your name" />
                    </Grid>
                    <Grid item xs={6} md={4}>
                      <TextField fullWidth select label="Type" value={quickForm.type} size="small"
                        onChange={(e) => qf('type', e.target.value)}>
                        <MenuItem value="auto">Auto</MenuItem>
                        <MenuItem value="homeowners">Homeowners</MenuItem>
                      </TextField>
                    </Grid>
                    <Grid item xs={6} md={3}>
                      <TextField fullWidth label="Start Date" type="date" value={quickForm.start_date} size="small"
                        InputLabelProps={{ shrink: true }} onChange={(e) => qf('start_date', e.target.value)} />
                    </Grid>
                    <Grid item xs={6} md={3}>
                      <TextField fullWidth label="End Date" type="date" value={quickForm.end_date} size="small"
                        InputLabelProps={{ shrink: true }} onChange={(e) => qf('end_date', e.target.value)} />
                    </Grid>
                    <Grid item xs={6} md={3}>
                      <TextField fullWidth label="Deductible" type="number" value={quickForm.deductible} size="small"
                        onChange={(e) => qf('deductible', Number(e.target.value))} />
                    </Grid>
                    <Grid item xs={6} md={3}>
                      <TextField fullWidth label="Premium/mo" type="number" value={quickForm.premium_monthly} size="small"
                        onChange={(e) => qf('premium_monthly', Number(e.target.value))} />
                    </Grid>
                  </Grid>

                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 2 }}>
                    <Typography variant="caption" color="rgba(22,22,22,0.45)" sx={{ flex: 1 }}>
                      Coverage: {Object.entries(quickForm.coverage || {}).map(([k, v]) => `${k}: ${v.toLocaleString()}`).join(' | ')}
                    </Typography>
                  </Box>

                  <Box sx={{ display: 'flex', gap: 1, mt: 2 }}>
                    <Button variant="contained" size="small" onClick={handleQuickSave} disabled={quickSaving || !quickForm.policy_number || !quickForm.holder_name}
                      startIcon={quickSaving ? <CircularProgress size={16} /> : <CheckCircle />}
                      sx={{ borderRadius: 2, background: 'linear-gradient(135deg, #1a7a4c, #1a7a4c)' }}>
                      Create & Select
                    </Button>
                    <Button size="small" onClick={() => setQuickForm(null)} sx={{ color: 'rgba(22,22,22,0.62)' }}>Cancel</Button>
                  </Box>
                </Paper>
              )}
            </Collapse>
          </>
        )}
      </Paper>

      {/* ── Step 2: Claim Details ─────────────────────────────────────────── */}
      <Paper component="form" onSubmit={handleSubmit} sx={{ p: 3, bgcolor: '#FFFFFF', borderRadius: 3, border: '1px solid #161616' }}>
        <Typography variant="subtitle1" fontWeight={700} color="#161616" sx={{ mb: 2, display: 'flex', alignItems: 'center', gap: 1 }}>
          <Send sx={{ color: '#0F766E', fontSize: 20 }} /> Step 2: Claim Details
        </Typography>

        <Typography variant="subtitle2" color="rgba(22,22,22,0.62)" fontWeight={600} sx={{ mb: 2 }}>Incident</Typography>
        <Grid container spacing={2} sx={{ mb: 3 }}>
          <Grid item xs={12} md={6}>
            <TextField select fullWidth label="Incident Type" value={form.incident_type} onChange={handleChange('incident_type')}>
              {claimTypes.map((t) => <MenuItem key={t.value} value={t.value}>{t.label}</MenuItem>)}
            </TextField>
          </Grid>
          <Grid item xs={12} md={6}>
            <TextField fullWidth label="Incident Date" type="date" value={form.incident_date} onChange={handleChange('incident_date')} InputLabelProps={{ shrink: true }} />
          </Grid>
          <Grid item xs={12}>
            <TextField fullWidth multiline rows={3} label="Incident Description *"
              value={form.incident_description} onChange={handleChange('incident_description')}
              onBlur={handleDescriptionBlur}
              placeholder={hints.description}
              helperText={typeCheckLoading ? 'Checking your description against the selected incident type...'
                : `${form.incident_description.length}/500 characters (min 10)`}
              inputProps={{ maxLength: 500 }}
            />
          </Grid>

          {typeCheck && !typeCheck.match && !typeConfirmed && (
            <Grid item xs={12}>
              <Alert severity="warning" sx={{ mb: 1 }}>
                <Typography variant="body2" sx={{ mb: 1 }}>
                  Based on your description, this looks like it might be a{' '}
                  <strong>{docLabel(typeCheck.suggested_type)}</strong> claim rather than{' '}
                  <strong>{docLabel(form.incident_type)}</strong>. {typeCheck.reasoning}
                </Typography>
                {!typeDisputeMode ? (
                  <Box sx={{ display: 'flex', gap: 1 }}>
                    <Button size="small" variant="contained" onClick={acceptSuggestedType}>
                      Use {docLabel(typeCheck.suggested_type)} instead
                    </Button>
                    <Button size="small" variant="outlined" onClick={() => setTypeDisputeMode(true)}>
                      No, keep {docLabel(form.incident_type)}
                    </Button>
                  </Box>
                ) : (
                  <Box>
                    <TextField fullWidth multiline rows={2} size="small" sx={{ mb: 1, bgcolor: '#F7F5EE' }}
                      label="Tell us why your original selection is correct"
                      value={typeDisputeReason} onChange={(e) => setTypeDisputeReason(e.target.value)} />
                    <Box sx={{ display: 'flex', gap: 1 }}>
                      <Button size="small" variant="contained" color="warning" disabled={!typeDisputeReason.trim()}
                        onClick={confirmDispute}>
                        Continue with {docLabel(form.incident_type)}
                      </Button>
                      <Button size="small" onClick={() => setTypeDisputeMode(false)}>Back</Button>
                    </Box>
                  </Box>
                )}
              </Alert>
            </Grid>
          )}
          {typeCheck && !typeCheck.match && typeConfirmed && typeDisputeReason.trim() && (
            <Grid item xs={12}>
              <Alert severity="info" sx={{ mb: 1 }}>
                Kept as {docLabel(form.incident_type)}. This disagreement will be sent for human review
                along with your reason.
              </Alert>
            </Grid>
          )}

          <Grid item xs={12} md={6}>
            <TextField fullWidth label="Location" value={form.incident_location} onChange={handleChange('incident_location')}
              placeholder={hints.location} />
          </Grid>
          <Grid item xs={12} md={6}>
            <TextField fullWidth label="Police/FIR Report #" value={form.police_report_number} onChange={handleChange('police_report_number')}
              placeholder={hints.policeReport} />
          </Grid>

          {requiredDocs.length > 0 && (
            <Grid item xs={12}>
              <Typography variant="body2" color="rgba(22,22,22,0.62)" fontWeight={600} sx={{ mb: 1 }}>
                Required documents for {docLabel(form.incident_type)}
              </Typography>
              <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
                {requiredDocs.map((slot) => {
                  const d = docUploads[slot];
                  return (
                    <Box key={slot} sx={{ border: '1px solid #161616', borderRadius: 2, p: 1.5, bgcolor: '#F7F5EE' }}>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
                        <Typography variant="body2" fontWeight={600} color="#161616" sx={{ minWidth: 160 }}>
                          {docLabel(slot)}
                        </Typography>

                        {!d && (
                          <Button component="label" size="small" variant="outlined" startIcon={<UploadFile />}
                            sx={{ borderColor: '#161616', color: 'rgba(22,22,22,0.62)', textTransform: 'none' }}>
                            Choose PDF
                            <input type="file" accept="application/pdf" hidden onChange={handleDocFileChange(slot)} />
                          </Button>
                        )}

                        {d?.status === 'uploading' && (
                          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                            <CircularProgress size={16} />
                            <Typography variant="caption" color="rgba(22,22,22,0.62)">Uploading...</Typography>
                          </Box>
                        )}

                        {d?.status === 'ok' && (
                          <>
                            <Chip size="small" color="success" icon={<CheckCircle />} label="Uploaded" variant="filled" />
                            <IconButton size="small" onClick={() => handleDocRemove(slot)} sx={{ color: 'rgba(22,22,22,0.45)' }}>
                              <Close fontSize="small" />
                            </IconButton>
                          </>
                        )}

                        {(d?.status === 'problem' || d?.status === 'failed') && (
                          <Chip size="small" color={d.status === 'failed' ? 'error' : 'warning'}
                            icon={d.status === 'failed' ? <ErrorOutline /> : <Warning />}
                            label={d.status === 'failed' ? 'Failed' : 'Needs attention'} variant="filled" />
                        )}
                      </Box>

                      {(d?.status === 'problem' || d?.status === 'failed') && (
                        <Box sx={{ mt: 1 }}>
                          <Typography variant="caption" color="rgba(22,22,22,0.62)" sx={{ display: 'block', mb: 1 }}>
                            {d.message}
                          </Typography>
                          {!d.keptAnyway && (
                            <Box sx={{ display: 'flex', gap: 1 }}>
                              <Button component="label" size="small" variant="outlined" startIcon={<Replay />}
                                sx={{ borderColor: '#161616', color: 'rgba(22,22,22,0.62)', textTransform: 'none' }}>
                                Re-upload
                                <input type="file" accept="application/pdf" hidden onChange={handleDocFileChange(slot)} />
                              </Button>
                              {d.status === 'problem' && (
                                <Button size="small" variant="text" onClick={() => handleDocKeepAnyway(slot)}
                                  sx={{ color: 'rgba(22,22,22,0.62)', textTransform: 'none' }}>
                                  Keep this file anyway
                                </Button>
                              )}
                            </Box>
                          )}
                          {d.keptAnyway && (
                            <Box>
                              <TextField fullWidth multiline rows={1} size="small" sx={{ mb: 1, bgcolor: '#F7F5EE' }}
                                label="Optional: why you're keeping this file"
                                value={d.disputeReason || ''} onChange={handleDocDisputeReasonChange(slot)} />
                              <Typography variant="caption" color="rgba(22,22,22,0.45)">
                                Kept as uploaded. A reviewer will take a closer look.
                              </Typography>
                            </Box>
                          )}
                        </Box>
                      )}
                    </Box>
                  );
                })}
              </Box>
              <Typography variant="caption" color="rgba(22,22,22,0.45)" sx={{ display: 'block', mt: 1 }}>
                Upload each document in PDF format. Anything left empty may be requested later.
              </Typography>
            </Grid>
          )}
        </Grid>

        <Typography variant="subtitle2" color="rgba(22,22,22,0.62)" fontWeight={600} sx={{ mb: 2 }}>Damage Estimate</Typography>
        <Grid container spacing={2} sx={{ mb: 3 }}>
          <Grid item xs={12} md={3}>
            <TextField fullWidth label={`Estimated Amount (${currencySymbol}) *`} type="number" value={form.estimated_amount} onChange={handleChange('estimated_amount')}
              placeholder={hints.amount} inputProps={{ min: 0, step: 100 }} />
          </Grid>
          {isAuto && (
            <>
              <Grid item xs={12} md={3}>
                <TextField fullWidth label="Vehicle Year" type="number" value={form.vehicle_year} onChange={handleChange('vehicle_year')}
                  placeholder={hints.vehicleYear} inputProps={{ min: 1990, max: 2026 }} />
              </Grid>
              <Grid item xs={12} md={3}>
                <TextField fullWidth label="Vehicle Make" value={form.vehicle_make} onChange={handleChange('vehicle_make')}
                  placeholder={hints.vehicleMake} />
              </Grid>
              <Grid item xs={12} md={3}>
                <TextField fullWidth label="Vehicle Model" value={form.vehicle_model} onChange={handleChange('vehicle_model')}
                  placeholder={hints.vehicleModel} />
              </Grid>
            </>
          )}
        </Grid>

        <Alert severity="info" sx={{ mb: 3 }}>
          Your claim will be processed through our AI pipeline: intake validation, fraud check (CrewAI), damage assessment, policy compliance, settlement calculation, and quality evaluation. High-value or uncertain claims are escalated for human review.
        </Alert>

        <Button type="submit" variant="contained" size="large" disabled={!canSubmit || loading}
          startIcon={loading ? <CircularProgress size={20} /> : <Send />}
          sx={{ px: 4, py: 1.5, fontWeight: 600, borderRadius: 2, background: 'linear-gradient(135deg, #0F766E 0%, #0B5B54 100%)' }}
        >
          Submit Claim
        </Button>
      </Paper>
    </Box>
  );
}
