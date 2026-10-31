import { create } from 'zustand';

// One-shot handoff from the Evals tab's "Run this case" button to
// SubmitClaim.jsx - holds the materialized case data (form fields +
// either prebuilt_documents or pdf_files to stage) across the navigation.
// Deliberately NOT persisted to localStorage: this is in-flight navigation
// state, not something that should survive a refresh or leak across tabs.
const useCaseRunStore = create((set, get) => ({
  pendingCase: null, // { caseId, source, claim, prebuilt_documents, pdf_files } | null

  setPendingCase: (data) => set({ pendingCase: data }),
  consumePendingCase: () => {
    const c = get().pendingCase;
    set({ pendingCase: null });
    return c;
  },
}));

export default useCaseRunStore;
