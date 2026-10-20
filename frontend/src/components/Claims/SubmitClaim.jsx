import React, { useEffect, useState } from 'react';
import {
  Box, Paper, Typography, TextField, Button, Grid, MenuItem, Alert,
  CircularProgress, Divider, Chip, Collapse, IconButton, Tooltip,
} from '@mui/material';
import { Send, Add, ExpandMore, ExpandLess, CheckCircle, Policy } from '@mui/icons-material';
import { useNavigate } from 'react-router-dom';
import { claimsAPI, policyAPI } from '../../services/api';
import api from '../../services/api';
import useAuthStore from '../../store/authStore';

// ── Quick-create panel defaults ────────────────────────────────────────────

function randomSuffix() {
  return String(Math.floor(100000 + Math.random() * 900000));
}

export default function SubmitClaim() {
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const [loading, setLoading] = useState(false);
  const [success, setSuccess] = useState(false);
  const [error, setError] = useState('');

  // Country-aware state
  const [claimTypes, setClaimTypes] = useState([]);
  const [requiredDocsByType, setRequiredDocsByType] = useState({});
  const [currencySymbol, setCurrencySymbol] = useState('$');
  const [countryName, setCountryName] = useState('');
  const [countryCode, setCountryCode] = useState('US');

  // Document checklist (which of the required docs for the selected type
  // the claimant says they have - real file upload doesn't exist yet, this
  // is a self-reported checklist, same as the backend's `documents: list[str]`)
  const [checkedDocs, setCheckedDocs] = useState({});

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
  }, []);

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
  const canSubmit = form.policy_number && form.incident_type && form.incident_description.length >= 10
    && parseFloat(form.estimated_amount) > 0 && !typeNeedsResolution && !typeCheckLoading;

  const handleChange = (field) => (e) => {
    const value = e.target.value;
    if (field === 'incident_type') {
      setCheckedDocs({});
      setTypeCheck(null);
      setTypeConfirmed(false);
      setTypeDisputeMode(false);
      setTypeDisputeReason('');
    }
    setForm((f) => ({ ...f, [field]: value }));
  };

  const toggleDoc = (docKey) => setCheckedDocs((prev) => ({ ...prev, [docKey]: !prev[docKey] }));

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
      const payload = {
        ...form,
        estimated_amount: parseFloat(form.estimated_amount),
        vehicle_year: form.vehicle_year ? parseInt(form.vehicle_year) : null,
        documents: requiredDocs.filter((d) => checkedDocs[d]),
        type_disputed_by_claimant: Boolean(typeCheck && !typeCheck.match && typeConfirmed && typeDisputeReason.trim()),
        type_dispute_reason: typeDisputeReason.trim() || null,
      };
      await claimsAPI.submitClaim(payload);
      setSuccess(true);
      setTimeout(() => navigate('/my-claims'), 2000);
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
          <Typography variant="h5" fontWeight={700} color="#F1F5F9">Submit New Claim</Typography>
          <Typography variant="body2" color="#94A3B8">
            Select a policy (or quick-create one), then fill in your claim details.
          </Typography>
        </Box>
        {countryName && (
          <Typography variant="caption" color="#64748B" sx={{ border: '1px solid #334155', borderRadius: 2, px: 1.5, py: 0.5 }}>
            {countryName} ({currencySymbol})
          </Typography>
        )}
      </Box>

      {success && (
        <Alert severity="success" sx={{ mb: 3 }}>
          Claim submitted successfully! Redirecting to your claims...
        </Alert>
      )}
      {error && <Alert severity="error" sx={{ mb: 3 }}>{error}</Alert>}

      {/* ── Step 1: Policy Selection ──────────────────────────────────────── */}
      <Paper sx={{ p: 3, mb: 3, bgcolor: '#1E293B', borderRadius: 3, border: '1px solid #334155' }}>
        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 2 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <Policy sx={{ color: '#3B82F6' }} />
            <Typography variant="subtitle1" fontWeight={700} color="#F1F5F9">
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
            <Divider sx={{ my: 2, borderColor: '#334155' }}>
              <Chip label="OR" size="small" sx={{ color: '#64748B', fontSize: '0.7rem' }} />
            </Divider>

            <Button
              onClick={() => setShowQuickCreate(!showQuickCreate)}
              startIcon={showQuickCreate ? <ExpandLess /> : <Add />}
              endIcon={!showQuickCreate ? <ExpandMore /> : null}
              sx={{ color: '#3B82F6', fontWeight: 600, mb: 1 }}
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
                      sx={{ borderColor: '#334155', color: '#F1F5F9', textTransform: 'none', borderRadius: 2,
                        '&:hover': { borderColor: '#3B82F6', bgcolor: '#3B82F620' } }}>
                      <Box sx={{ textAlign: 'left' }}>
                        <Typography variant="body2" fontWeight={600}>{tmpl.label}</Typography>
                        <Typography variant="caption" color="#64748B">
                          Deductible: {tmpl.deductible?.toLocaleString()} | Coverage keys: {Object.keys(tmpl.coverage).join(', ')}
                        </Typography>
                      </Box>
                    </Button>
                  ))}
                </Box>
              )}

              {/* Editable quick-create form */}
              {quickForm && (
                <Paper sx={{ p: 2, bgcolor: '#0F172A', borderRadius: 2, border: '1px solid #334155', mt: 1 }}>
                  <Typography variant="subtitle2" color="#94A3B8" fontWeight={600} sx={{ mb: 2 }}>
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
                    <Typography variant="caption" color="#64748B" sx={{ flex: 1 }}>
                      Coverage: {Object.entries(quickForm.coverage || {}).map(([k, v]) => `${k}: ${v.toLocaleString()}`).join(' | ')}
                    </Typography>
                  </Box>

                  <Box sx={{ display: 'flex', gap: 1, mt: 2 }}>
                    <Button variant="contained" size="small" onClick={handleQuickSave} disabled={quickSaving || !quickForm.policy_number || !quickForm.holder_name}
                      startIcon={quickSaving ? <CircularProgress size={16} /> : <CheckCircle />}
                      sx={{ borderRadius: 2, background: 'linear-gradient(135deg, #10B981, #059669)' }}>
                      Create & Select
                    </Button>
                    <Button size="small" onClick={() => setQuickForm(null)} sx={{ color: '#94A3B8' }}>Cancel</Button>
                  </Box>
                </Paper>
              )}
            </Collapse>
          </>
        )}
      </Paper>

      {/* ── Step 2: Claim Details ─────────────────────────────────────────── */}
      <Paper component="form" onSubmit={handleSubmit} sx={{ p: 3, bgcolor: '#1E293B', borderRadius: 3, border: '1px solid #334155' }}>
        <Typography variant="subtitle1" fontWeight={700} color="#F1F5F9" sx={{ mb: 2, display: 'flex', alignItems: 'center', gap: 1 }}>
          <Send sx={{ color: '#3B82F6', fontSize: 20 }} /> Step 2: Claim Details
        </Typography>

        <Typography variant="subtitle2" color="#94A3B8" fontWeight={600} sx={{ mb: 2 }}>Incident</Typography>
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
                    <TextField fullWidth multiline rows={2} size="small" sx={{ mb: 1, bgcolor: '#0F172A' }}
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
              <Typography variant="body2" color="#94A3B8" fontWeight={600} sx={{ mb: 1 }}>
                Required documents for {docLabel(form.incident_type)}
              </Typography>
              <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
                {requiredDocs.map((d) => (
                  <Chip key={d} label={docLabel(d)} clickable onClick={() => toggleDoc(d)}
                    color={checkedDocs[d] ? 'success' : 'default'}
                    icon={checkedDocs[d] ? <CheckCircle /> : undefined}
                    variant={checkedDocs[d] ? 'filled' : 'outlined'} />
                ))}
              </Box>
              <Typography variant="caption" color="#64748B" sx={{ display: 'block', mt: 0.5 }}>
                Click each document you have ready. Anything left unchecked may be requested later.
              </Typography>
            </Grid>
          )}
        </Grid>

        <Typography variant="subtitle2" color="#94A3B8" fontWeight={600} sx={{ mb: 2 }}>Damage Estimate</Typography>
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
          sx={{ px: 4, py: 1.5, fontWeight: 600, borderRadius: 2, background: 'linear-gradient(135deg, #2563EB 0%, #1D4ED8 100%)' }}
        >
          Submit Claim
        </Button>
      </Paper>
    </Box>
  );
}
