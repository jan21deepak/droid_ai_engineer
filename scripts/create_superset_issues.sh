#!/usr/bin/env bash
# Create sample issues on jan21deepak/superset labeled Cursor-complete.
# Requires: gh auth as an account with write access to that repo.
set -euo pipefail
REPO="${REPO:-jan21deepak/superset}"
LABEL="${LABEL:-Cursor-complete}"

gh label create "$LABEL" --repo "$REPO" --color "0E8A16" --description "Trigger Cursor AI Engineer automation" 2>/dev/null || true

create_issue() {
  local title="$1"
  local body="$2"
  gh issue create --repo "$REPO" --title "$title" --label "$LABEL" --body "$body"
}

create_issue "Fix chart legend overlap on dashboard" "$(cat <<'BODY'
## Problem
On the Explore chart view, the legend overlaps the plot area when there are more than 8 series.

## Acceptance criteria
- Legend should not obscure the chart
- Layout should remain usable on mobile widths
- Add/adjust a unit or integration test if one exists for chart rendering

## Notes
Make only the requested changes. Run project tests, fix failures you introduce, open a PR, and summarize the work.
BODY
)"

create_issue "Add missing null-check in dataset metadata API" "$(cat <<'BODY'
## Problem
The dataset metadata endpoint can return a 500 when `columns` is null for a virtual dataset.

## Acceptance criteria
- Return a clear 4xx/empty columns response instead of 500
- Cover with a unit test
- Keep the change scoped to this bug

## Notes
Make only the requested changes. Run project tests, fix failures you introduce, open a PR, and summarize the work.
BODY
)"

echo "Created issues with label: $LABEL"

