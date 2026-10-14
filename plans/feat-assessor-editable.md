# feat/assessor-editable
- **Status:** releasing (fast path per Eli)
Assessor was purely derived at render (job owners). Eli: "snapshot needs to be editable." Add an operator
override, like company_name: ReportBoard.assessor column (+ migration e5f1a2b3c4d7, head c8e1a4f7b230);
render = engagement.assessor or _derive_assessor(engagement); editable in the engagement new/edit forms.
Empty -> derived (live default); set -> wins + frozen against later job changes. Test: override wins.
